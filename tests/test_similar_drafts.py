"""Offline similar drafts use canonical definitions without persisting wizard state."""
from copy import deepcopy

import pytest

from funpayflow.product_drafts import DraftService
from funpayflow.product_store import ProductStore
from funpayflow.product_templates import TemplateService
from funpayflow.product_variants import VariantService
from test_product_drafts import ReceiptFake, RoutesFake


@pytest.fixture
def drafts(tmp_path):
    receipt = ReceiptFake(tmp_path / "products.sqlite")
    store = ProductStore(receipt, "owner", 1)
    template = TemplateService(store).ensure_builtin()
    route = store.save_route({"template_id": template["id"], "game_name": "Game",
        "game_aliases": [], "node_id": 12, "section_name": "Keys",
        "metadata": {"category_id": 2, "type": "COMMON"},
        "overrides": {"variables": {"region": "EU", "edition": "Route"}, "emoji": "🔑"}})
    service = DraftService(store, RoutesFake(store))
    variant = VariantService(store, service.routes).save({"template_id": template["id"],
        "route_id": route["id"], "display_name": "Deluxe",
        "overrides": {"variables": {"edition": "Deluxe", "platform": "GOG"}, "emoji": "⭐"}})
    return service, store, template, route, variant


def save(service, preview):
    return service.confirm(service.stage([preview])["token"])["results"][0]


def test_clone_is_transient_immutable_and_confirm_is_idempotent(drafts):
    service, store, template, route, variant = drafts
    source = save(service, service.create_preview(template["id"], route["id"],
        price="12.50", variables={"quantity": "3", "product_name": "Custom 🎁"},
        overrides={"emoji": "💎", "description_en": "Custom {{product_name}}"}, variant_id=variant["id"]))
    before = deepcopy(source)
    clone = service.clone_preview(source["id"], source["revision"])
    assert not set(clone) & {"id", "revision", "status", "created_at", "updated_at"}
    assert store.list_drafts() == [source]
    assert clone["payload"]["amount"] == 3
    assert clone["payload"]["price"] == "12.50"
    assert clone["payload"]["description_en"] == "Custom Custom 🎁"
    assert clone["emoji"] == "💎"
    assert service.rebuild(clone) == clone
    batch = service.stage([clone])
    first = service.confirm(batch["token"])
    second = service.confirm(batch["token"])
    assert first["results"] == second["results"]
    assert first["results"][0]["id"] != source["id"]
    assert len(store.list_drafts()) == 2
    assert store.get_draft(source["id"]) == before


def test_variant_switches_preserve_explicit_inputs_without_freezing_defaults(drafts):
    service, _, template, route, variant = drafts
    plain = service.create_preview(template["id"], route["id"], price="9")
    deluxe = service.switch_variant(plain, variant["id"])
    assert deluxe["variables"]["edition"] == "Deluxe"
    assert deluxe["variables"]["platform"] == "GOG"
    assert deluxe["variables"]["region"] == "EU"
    assert deluxe["emoji"] == "⭐"
    assert service.switch_variant(deluxe, None) == plain
    explicit = service.rebuild(plain, variables={"edition": "Route", "region": "Global"},
                               overrides={"variables": {"platform": "Epic"}})
    changed = service.switch_variant(explicit, variant["id"])
    assert {key: changed["variables"][key] for key in ("edition", "region", "platform")} == {
        "edition": "Route", "region": "Global", "platform": "Epic"}
    assert service.switch_variant(changed, None)["variables"] == explicit["variables"]
    assert service.rebuild(changed) == changed


def test_clone_uses_current_definitions_but_preserves_explicit_equal_default(drafts):
    service, store, template, route, variant = drafts
    source = save(service, service.create_preview(template["id"], route["id"], price="9",
        variables={"region": "EU"}, variant_id=variant["id"]))
    updated = TemplateService(store).save(dict(template, default_emoji="🆕",
        defaults={**template["defaults"], "quantity": "2"}), template["id"], template["revision"])
    new_route = store.save_route(dict(route, overrides={"variables": {"region": "US"}}), route["revision"])
    new_variant = VariantService(store, service.routes).save(dict(variant,
        overrides={"variables": {"edition": "Ultimate"}}), variant["id"], variant["revision"])
    clone = service.clone_preview(source["id"], source["revision"])
    assert clone["template_revision"] == updated["revision"]
    assert clone["route_revision"] == new_route["revision"]
    assert clone["variant_snapshot"]["revision"] == new_variant["revision"]
    assert clone["variables"]["region"] == "EU"
    assert clone["variables"]["edition"] == "Ultimate"
    assert clone["payload"]["amount"] == 2
    assert clone["emoji"] == "🆕"
    assert store.get_draft(source["id"]) == source


def test_stale_cancelled_missing_and_foreign_sources_fail(drafts):
    service, store, template, route, _ = drafts
    source = save(service, service.create_preview(template["id"], route["id"], price="1"))
    for revision in (True, None, 0, source["revision"] + 1):
        with pytest.raises(ValueError):
            service.clone_preview(source["id"], revision)
    for owner, account in (("other", 1), ("owner", 2)):
        foreign = ProductStore(store.store, owner, account)
        with pytest.raises(ValueError):
            DraftService(foreign, RoutesFake(foreign)).clone_preview(source["id"], source["revision"])
    cancelled = store.cancel_draft(source["id"], source["revision"])
    for revision in (source["revision"], cancelled["revision"]):
        with pytest.raises(ValueError):
            service.clone_preview(source["id"], revision)
    with pytest.raises(ValueError):
        service.clone_preview("missing", 1)


def test_unavailable_route_disabled_and_wrong_route_variants_fail(drafts):
    service, store, template, route, variant = drafts
    source = save(service, service.create_preview(template["id"], route["id"], price="1", variant_id=variant["id"]))
    VariantService(store, service.routes).disable(variant["id"], variant["revision"])
    with pytest.raises(ValueError):
        service.clone_preview(source["id"], source["revision"])
    other_route = store.save_route({key: value for key, value in dict(route, game_name="Other", node_id=13).items()
                                   if key not in {"id", "revision", "created_at", "updated_at", "game_key"}})
    other_variant = VariantService(store, service.routes).save({"template_id": template["id"],
        "route_id": other_route["id"], "display_name": "Other"})
    with pytest.raises(ValueError):
        service.switch_variant(source, other_variant["id"])
    service.routes.available = False
    with pytest.raises(ValueError):
        service.clone_preview(source["id"], source["revision"])


def test_legacy_inputs_conservative_fallback_and_stage_compatibility(drafts):
    service, _, template, route, variant = drafts
    legacy = service.create_preview(template["id"], route["id"], price="1",
        variables={"product_name": "Custom", "region": "EU"}, overrides={"variables": {"quantity": "4"}})
    for key in ("input_variables", "route_snapshot", "route_revision"):
        legacy.pop(key)
    assert service.rebuild(legacy) == legacy
    source = save(service, legacy)
    assert service.input_warnings(source)
    assert service.variable_input(source) == {"product_name": "Custom", "quantity": "4"}
    clone = service.clone_preview(source["id"], source["revision"])
    assert service.switch_variant(clone, variant["id"])["variables"]["edition"] == "Deluxe"
    assert not service.input_warnings(clone)
    inputs = service.variable_input(clone)
    inputs["quantity"] = "99"
    assert service.variable_input(clone)["quantity"] == "4"
