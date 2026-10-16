from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest

from funpayflow.product_drafts import DraftService, validate_price
from funpayflow.product_store import ProductStore, initialize_schema
from funpayflow.product_templates import ProductValidationError, TemplateService


class ReceiptFake:
    def __init__(self, path):
        self.path = path
        with closing(self._connect()) as db, db:
            initialize_schema(db)

    def _connect(self):
        return sqlite3.connect(self.path)


class RoutesFake:
    def __init__(self, store):
        self.store, self.available = store, True

    def resolve_route(self, route_id):
        if not self.available:
            raise ValueError("route unavailable")
        route = self.store.get_route(route_id)
        if route is None:
            raise ValueError("route missing")
        return route


class ProductDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.receipts = ReceiptFake(Path(self.temp.name) / "state.sqlite")
        self.store = ProductStore(self.receipts, "owner", 1)
        self.templates = TemplateService(self.store)
        self.template = self.templates.ensure_builtin()
        self.route = self.store.save_route({"template_id": self.template["id"], "game_name": "Game",
            "game_aliases": ["Alias"], "node_id": 12, "section_name": "Keys",
            "metadata": {"category_id": 2, "type": "COMMON"}, "overrides": {"emoji": "🔑"}})
        self.routes = RoutesFake(self.store)
        self.service = DraftService(self.store, self.routes)

    def preview(self, **kwargs):
        return self.service.create_preview(self.template["id"], self.route["id"], price="10.50", **kwargs)

    def test_preview_has_no_writes_and_emoji_priority(self):
        preview = self.preview()
        self.assertEqual(self.store.list_drafts(), [])
        self.assertEqual(preview["emoji"], "🔑")
        self.assertTrue(preview["payload"]["title_ru"].startswith("🔑 "))
        self.assertEqual(preview["payload"]["price"], "10.50")
        self.assertEqual(preview["payload"]["amount"], 1)
        self.assertEqual(self.preview(overrides={"emoji": "⭐"})["emoji"], "⭐")
        self.assertTrue(self.preview(overrides={"emoji": "⭐"})["payload"]["title_en"].startswith("⭐ "))
        self.routes.available = False
        with self.assertRaises(ValueError):
            self.preview()
        with self.assertRaises(ValueError):
            DraftService(self.store).create_preview(self.template["id"], self.route["id"], price="1")

    def test_bulk_partial_failure_and_aliases(self):
        result = self.service.preview_bulk(self.template["id"], "Alias | 12.00 | region=EU\nMissing | 3\nGame | NaN\nGame | 4 | hidden=x")
        self.assertEqual(len(result["previews"]), 1)
        self.assertEqual(len(result["errors"]), 3)
        self.assertEqual(result["previews"][0]["variables"]["region"], "EU")
        self.assertEqual(self.store.list_drafts(), [])
        with self.assertRaises(ValueError):
            self.service.preview_bulk(self.template["id"], "Game | 1\n" * 101)
        duplicate = self.service.preview_bulk(self.template["id"], "Game | 2\nAlias | 2")
        self.assertEqual(len(duplicate["previews"]), 1)
        self.assertEqual(duplicate["errors"][0]["status"], "duplicate")

    def test_snapshot_edit_stage_confirm_restart_idempotent_and_cancel(self):
        preview = self.preview()
        edited_template = dict(self.template, title_en="Changed {{game}}")
        self.templates.save(edited_template, self.template["id"], self.template["revision"])
        edited = self.service.rebuild(preview, price="15", variables={"quantity": "2"}, overrides={"title_ru": "Свои {{game}}"})
        self.assertEqual(edited["payload"]["title_en"], preview["payload"]["title_en"])
        self.assertEqual(edited["payload"]["amount"], 2)
        batch = self.service.stage([edited])
        self.assertEqual(self.store.list_drafts(), [])
        fresh = DraftService(ProductStore(self.receipts, "owner", 1), self.routes)
        first = fresh.confirm(batch["token"])
        second = fresh.confirm(batch["token"])
        self.assertEqual(first["results"], second["results"])
        self.assertEqual(len(self.store.list_drafts()), 1)
        cancelled = self.service.stage([preview])
        self.service.cancel(cancelled["token"])
        self.service.confirm(cancelled["token"])
        self.assertEqual(len(self.store.list_drafts()), 1)

    def test_price_and_hidden_override_validation(self):
        for value in (True, "NaN", "Infinity", "0", "-1", "1 USD", 1.2):
            with self.subTest(value=value), self.assertRaises(ProductValidationError):
                validate_price(value)
        with self.assertRaises(ProductValidationError):
            self.preview(overrides={"csrf_token": "hidden"})

    def test_rebuild_preserves_overrides_and_explicit_variable_edits(self):
        preview = self.preview(overrides={"emoji": "⭐", "variables": {"quantity": "3"}, "description_en": "Custom {{game}}"})
        edited = self.service.rebuild(preview, variables={"quantity": "2"}, price="8")
        self.assertEqual(edited["emoji"], "⭐")
        self.assertEqual(edited["payload"]["description_en"], "Custom Game")
        self.assertEqual(edited["payload"]["amount"], 2)
        self.assertEqual(self.service.rebuild(edited), edited)

    def test_staging_rejects_tampered_preview_and_unavailable_route(self):
        preview = self.preview()
        preview["payload"]["price"] = "0"
        with self.assertRaises(ValueError):
            self.service.stage([preview])
        preview = self.preview()
        preview["payload"]["title_en"] = "Unreviewed mutation"
        with self.assertRaises(ValueError):
            self.service.stage([preview])
        preview = self.preview()
        self.routes.available = False
        with self.assertRaises(ValueError):
            self.service.stage([preview])

    def test_builtin_seed_keeps_user_edit_and_safe_failed_crud(self):
        changed = self.templates.save(dict(self.template, default_emoji="⭐"),
                                      self.template["id"], self.template["revision"])
        self.assertEqual(self.templates.ensure_builtin(), changed)
        self.assertEqual(self.templates.list()[0]["default_emoji"], "⭐")
        with self.assertRaises(ValueError):
            self.templates.delete(self.template["id"], changed["revision"])
        invalid = dict(changed, title_en="")
        with self.assertRaises(ValueError):
            self.templates.save(invalid, changed["id"], changed["revision"])
        self.assertEqual(self.templates.get(changed["id"]), changed)

    def test_concurrent_builtin_first_visits_share_one_definition(self):
        def seed(_):
            return TemplateService(ProductStore(self.receipts, "second-owner", 1)).ensure_builtin()
        with ThreadPoolExecutor(max_workers=2) as pool:
            seeded = list(pool.map(seed, range(2)))
        self.assertEqual(seeded[0], seeded[1])
        self.assertEqual(len(ProductStore(self.receipts, "second-owner", 1).list_templates()), 1)

    def test_draft_game_cannot_diverge_from_verified_route(self):
        for value in ("Different game", "game", "Alias"):
            with self.subTest(value=value), self.assertRaises(ProductValidationError):
                self.preview(variables={"game": value})
            with self.subTest(override=value), self.assertRaises(ProductValidationError):
                self.preview(overrides={"variables": {"game": value}})
        preview = self.preview(variables={"game": "Game", "product_name": "Custom edition"})
        self.assertEqual(preview["game"], "Game")
        self.assertIn("Custom edition", preview["payload"]["description_en"])
        self.route = self.store.save_route(dict(self.route, overrides={"variables": {"game": "Other"}}),
                                           expected_revision=self.route["revision"])
        with self.assertRaises(ProductValidationError):
            self.preview()

    def test_template_lookup_revalidates_before_a_wizard_can_ask_for_secrets(self):
        unsafe = dict(self.template, allowed_variables=[*self.template["allowed_variables"], "password"])
        self.store.save_template(unsafe, self.template["id"], self.template["revision"])
        with self.assertRaises(ProductValidationError):
            self.templates.get(self.template["id"])


if __name__ == "__main__":
    unittest.main()
