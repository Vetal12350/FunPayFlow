"""Offline variant persistence and catalogue verification, without publication."""

from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest
from FunPayAPI.common.enums import SubCategoryTypes
from FunPayAPI.types import Category, SubCategory

from funpayflow.game_routes import RouteService
from funpayflow.product_store import ProductStore, initialize_schema
from funpayflow.product_templates import TemplateService
from funpayflow.product_variants import ProductVariant, VariantService
from funpayflow.state import ReviewReceiptStore


@pytest.fixture
def variants(tmp_path):
    receipts = ReviewReceiptStore(tmp_path / "state.sqlite3")
    receipts.initialize()
    store = ProductStore(receipts, "owner", 10)
    template = TemplateService(store).ensure_builtin()
    game = Category(1, "Game")
    nodes = [SubCategory(50, "Keys", SubCategoryTypes.COMMON, game),
             SubCategory(51, "Other keys", SubCategoryTypes.COMMON, game),
             SubCategory(52, "Currency", SubCategoryTypes.CURRENCY, game),
             SubCategory(60, "Keys", SubCategoryTypes.COMMON, Category(2, "Other"))]

    class Account:
        id = 10
        subcategories = nodes

        def get_subcategory(self, kind, node_id):
            return next((node for node in self.subcategories if node.id == node_id and node.type is kind), None)

    routes = RouteService(SimpleNamespace(account=Account(), _account_lock=threading.RLock()), store)
    parent = store.save_route(routes.from_url(template["id"], "https://funpay.com/lots/50/"))
    return VariantService(store, routes), parent


def definition(parent, name="Deluxe", **changes):
    return {"template_id": parent["template_id"], "route_id": parent["id"], "display_name": name,
            "aliases": ["DX"], "overrides": {"variables": {"edition": "Deluxe", "region": "EU"}}, **changes}


def test_persistence_restart_revision_disable_and_snapshot(variants):
    service, parent = variants
    saved = service.save(definition(parent), variant_id="deluxe")
    assert saved["revision"] == 1 and saved["created_at"] == saved["updated_at"]
    assert service.find(parent["id"], "  dx ")["id"] == saved["id"]
    restarted = VariantService(ProductStore(ReviewReceiptStore(service.store.store.path), "owner", 10), service.routes)
    assert restarted.get("deluxe") == saved
    edited = service.save(dict(saved, display_name="Deluxe updated"), "deluxe", saved["revision"])
    assert edited["revision"] == 2 and edited["created_at"] == saved["created_at"]
    with pytest.raises(ValueError):
        service.save(saved, "deluxe", saved["revision"])
    disabled = service.disable("deluxe", edited["revision"])
    assert not disabled["enabled"] and disabled["revision"] == 3
    assert not service.list(parent["id"]) and service.find(parent["id"], "DX") is None
    with pytest.raises(ValueError):
        service.get("deluxe")
    assert service.get("deluxe", enabled_only=False) == disabled
    assert service.list(parent["id"], enabled_only=False) == [disabled]
    assert service.validate_snapshot(saved, parent) == saved
    assert service.resolve_route(saved, parent) == parent


def test_names_alias_ambiguity_and_bounded_pages(variants):
    service, parent = variants
    one = service.save(definition(parent))
    with pytest.raises(ValueError):
        service.save(definition(parent, "  DELUXE  "))
    service.save(definition(parent, "Standard"))
    with pytest.raises(ValueError, match="ambiguous"):
        service.find(parent["id"], "DX")
    assert service.find(parent["id"], "Deluxe")["id"] == one["id"]
    assert service.find(parent["id"], "Del") is None
    assert len(service.list(parent["id"], limit=1, offset=1)) == 1
    for kwargs in ({"limit": True}, {"limit": 101}, {"offset": -1}, {"enabled_only": 1}):
        with pytest.raises(ValueError):
            service.list(parent["id"], **kwargs)


def test_owner_account_parent_and_template_scopes(variants):
    service, parent = variants
    saved = service.save(definition(parent))
    other_route = service.store.save_route(service.routes.from_url(parent["template_id"], "https://funpay.com/lots/60/"))
    assert not service.list(other_route["id"])
    service.save(definition(other_route))  # Names are unique per parent route.
    for owner, account in (("other", 10), ("owner", 11)):
        other = VariantService(ProductStore(service.store.store, owner, account))
        with pytest.raises(ValueError):
            other.get(saved["id"])
        assert not other.list(parent["id"])
        with pytest.raises(ValueError):
            other.save(definition(parent))
    with pytest.raises(ValueError):
        service.save(dict(saved, route_id=other_route["id"]), saved["id"], saved["revision"])
    with pytest.raises(ValueError):
        service.save(definition(parent, template_id="missing"))


@pytest.mark.parametrize("changes", [
    {"price": 5}, {"account_id": 10}, {"node_id": 51}, {"enabled": "false"},
    {"display_name": ""}, {"aliases": [" "]}, {"aliases": ["DX", "dx"]},
    {"aliases": ["Deluxe"]}, {"aliases": "DX"},
    {"overrides": {"variables": {"price": "1"}}},
    {"overrides": {"variables": {"region": "{{game}}"}}},
    {"overrides": {"title_ru": "{{secret}}"}},
    {"overrides": {"node_id": 51}}, {"route_override": {"node_id": 51}},
])
def test_invalid_variant_fields_rejected_by_service_and_store(variants, changes):
    service, parent = variants
    for save in (service.save, service.store.save_variant):
        with pytest.raises(ValueError):
            save(definition(parent, **changes))
    assert not service.list(parent["id"])


def test_verified_same_game_section_override_retains_parent_and_rechecks_cache(variants):
    service, parent = variants
    saved = service.save(definition(parent, route_override_url="https://funpay.com/lots/51/"))
    assert "route_override_url" not in saved
    assert saved["route_override"]["node_id"] == 51
    effective = service.resolve_route(saved, parent)
    assert effective["id"] == parent["id"] and effective["node_id"] == 51
    assert effective["overrides"] == parent["overrides"]
    assert service.store.get_route(parent["id"])["node_id"] == 50
    assert len(service.store.list_routes()) == 1
    no_routes = VariantService(service.store)
    with pytest.raises(ValueError):
        no_routes.save(saved, saved["id"], saved["revision"])
    service.routes.client.account.subcategories = [node for node in service.routes.client.account.subcategories if node.id != 51]
    with pytest.raises(ValueError):
        service.resolve_route(saved, parent)
    assert not service.disable(saved["id"], saved["revision"])["enabled"]


@pytest.mark.parametrize("url", ["https://funpay.com/lots/60/", "https://funpay.com/lots/52/",
                                "https://funpay.com/lots/999/", "https://evil.test/lots/51/"])
def test_cross_game_currency_unknown_and_untrusted_override_rejected(variants, url):
    service, parent = variants
    with pytest.raises(ValueError):
        service.save(definition(parent, route_override_url=url))
    assert not service.list(parent["id"])


def test_tampered_verified_snapshot_and_model_copy(variants):
    service, parent = variants
    saved = service.save(definition(parent, route_override_url="https://funpay.com/lots/51/"))
    forged = deepcopy(saved)
    forged["route_override"]["section_name"] = "Forged"
    with pytest.raises(ValueError):
        service.save(forged, saved["id"], saved["revision"])
    snapshot = ProductVariant(saved)
    saved["overrides"]["variables"]["edition"] = "Mutated"
    assert snapshot.to_dict()["overrides"]["variables"]["edition"] == "Deluxe"


def test_frozen_template_can_validate_existing_variant_after_template_edit(variants):
    service, parent = variants
    templates = TemplateService(service.store)
    original = templates.get(parent["template_id"])
    custom = deepcopy(original)
    custom["allowed_variables"].append("public_note")
    frozen = templates.save(custom, original["id"], original["revision"])
    saved = service.save(definition(parent, overrides={"variables": {"public_note": "Public"}}))
    templates.save(original, original["id"], frozen["revision"])
    with pytest.raises(ValueError):
        service.validate_snapshot(saved, parent)
    assert service.validate_snapshot(saved, parent, template=frozen) == saved
    assert service.resolve_route(saved, parent, template=frozen) == parent
    disabled = service.disable(saved["id"], saved["revision"])
    assert not disabled["enabled"]
    assert disabled["overrides"] == saved["overrides"]


def test_additive_schema_is_idempotent_and_preserves_existing_tables(variants):
    service, parent = variants
    saved = service.save(definition(parent))
    with service.store.store._connect() as db:
        initialize_schema(db)
        initialize_schema(db)
        assert db.execute("SELECT count(*) FROM product_variants").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM game_routes").fetchone()[0] == 1
    assert service.get(saved["id"]) == saved
