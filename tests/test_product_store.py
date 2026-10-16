"""Offline product registry migration, scope and confirmation tests."""
import pytest
from concurrent.futures import ThreadPoolExecutor

from funpayflow.product_store import ProductStore, initialize_schema
from funpayflow.state import ReviewReceiptStore


@pytest.fixture
def store(tmp_path):
    receipt = ReviewReceiptStore(tmp_path / "state.sqlite3")
    receipt.initialize()
    with receipt._connect() as db:
        initialize_schema(db)
    return ProductStore(receipt, "1", 10)


def route(template_id, name="Игра", node_id=50):
    return dict(template_id=template_id, game_name=name, node_id=node_id,
                section_name="Аккаунты", game_aliases=["Alias"],
                metadata={"category_id": 5, "type": "COMMON"}, overrides={})


def test_scoped_ids_and_revision(store):
    item = store.save_template({"name": "T"}, "builtin")
    other = ProductStore(store.store, "2", 10)
    assert other.get_template(item["id"]) is None
    assert other.save_template({"name": "Other"}, "builtin")["revision"] == 1
    updated = store.save_template({"name": "New"}, "builtin", 1)
    assert updated["revision"] == 2
    with pytest.raises(ValueError):
        store.save_template({"name": "Stale"}, "builtin", 1)


def test_routes_duplicates_aliases_and_template_scope(store):
    first = store.save_template({"name": "T"})
    second = store.save_template({"name": "U"})
    saved = store.save_route(route(first["id"]))
    assert store.find_route(first["id"], "alias")["id"] == saved["id"]
    assert store.list_routes(query="ИГРА")[0]["id"] == saved["id"]
    with pytest.raises(ValueError):
        store.save_route(route(first["id"], "  ИГРА  "))
    remapped = dict(saved, node_id=51)
    with pytest.raises(ValueError):
        store.save_route(remapped)
    assert store.save_route(remapped, expected_revision=1)["node_id"] == 51
    with pytest.raises(ValueError):
        store.save_route(remapped, expected_revision=1)
    assert store.save_route(route(second["id"]))
    with pytest.raises(ValueError):
        store.delete_template(first["id"], first["revision"])


def test_alias_ambiguity_never_selects_first_route(store):
    template = store.save_template({"name": "T"})
    store.save_route(route(template["id"], "First"))
    store.save_route(route(template["id"], "Second", 51))
    with pytest.raises(ValueError, match="неоднозначно"):
        store.find_route(template["id"], "Alias")


def test_template_name_normalized_uniqueness(store):
    store.save_template({"name": "Game Key"})
    with pytest.raises(ValueError):
        store.save_template({"name": " game KEY "})


def test_builtin_concurrent_creation_and_scope(store):
    from funpayflow.product_templates import TemplateService
    def create(_):
        return TemplateService(ProductStore(store.store, "1", 10)).ensure_builtin()
    with ThreadPoolExecutor(max_workers=2) as workers:
        rows = list(workers.map(create, range(2)))
    assert rows[0]["id"] == rows[1]["id"]
    assert len(store.list_templates()) == 1
    other = ProductStore(store.store, "2", 10)
    assert TemplateService(other).ensure_builtin()["id"] == rows[0]["id"]
    assert len(other.list_templates()) == 1


def test_preview_confirm_cancel_restart_and_partial_success(store):
    template = store.save_template({"name": "T"})
    batch = store.stage_batch("routes", [route(template["id"]), route(template["id"]), {}])
    assert not store.list_routes()
    restarted = ProductStore(store.store, "1", 10)
    result = restarted.confirm_batch(batch["token"])
    assert len(result["results"]) == 1 and len(result["errors"]) == 2
    assert restarted.confirm_batch(batch["token"])["results"] == result["results"]
    assert len(restarted.list_routes()) == 1
    cancelled = store.stage_batch("routes", [route(template["id"], "Other")])
    store.cancel_batch(cancelled["token"])
    assert store.confirm_batch(cancelled["token"])["status"] == "cancelled"
    with pytest.raises(ValueError):
        ProductStore(store.store, "2", 10).confirm_batch(batch["token"])


def test_draft_snapshot_idempotence_and_legacy_untouched(store):
    with store.store._connect() as db:
        before = db.execute("SELECT sql FROM sqlite_master WHERE name='lot_template_drafts'").fetchone()
        initialize_schema(db)
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='lot_template_drafts'").fetchone() == before
    body = {"template": {"title_ru": "Snapshot"}, "payload": {"title_ru": "Text"}, "emoji": "🎮"}
    token = store.stage_draft_batch([body])
    assert not store.list_drafts()
    results = store.confirm_draft_batch(token)
    assert store.confirm_draft_batch(token) == results
    assert len(store.list_drafts()) == 1
    assert store.get_draft(results[0]["id"])["template"] == body["template"]
    assert store.cancel_draft(results[0]["id"], 1)["status"] == "cancelled"


def test_route_rejects_service_fields(store):
    template = store.save_template({"name": "T"})
    body = route(template["id"])
    for field, value in (("csrf_token", "secret"), ("overrides", {"delivery": "auto"}),
                         ("metadata", {"category_id": 5, "type": "COMMON", "offer": 7})):
        with pytest.raises(ValueError):
            store.save_route(dict(body, **{field: value}))


def test_recent_routes_only_touched_by_draft_confirm(store):
    template = store.save_template({"name": "T"})
    saved = store.save_route(route(template["id"]))
    token = store.stage_draft_batch([{"route_id": saved["id"], "payload": {"title_ru": "Snapshot"}}])
    def last_used():
        with store.store._connect() as db:
            return db.execute("SELECT last_used FROM game_routes WHERE id=?", (saved["id"],)).fetchone()[0]
    assert last_used() is None
    store.confirm_draft_batch(token)
    assert last_used() is not None
    assert store.get_route(saved["id"])["revision"] == 1
