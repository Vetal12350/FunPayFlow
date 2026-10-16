"""Offline drafts and explicit creation-schema blocker; no live credentials."""
import log_isolation

import inspect
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from FunPayAPI.account import Account
from FunPayAPI.common.enums import SubCategoryTypes
from FunPayAPI.types import LotFields

from funpayflow import runtime_control
from funpayflow.lot_templates import LotTemplateError, LotTemplateService, PUBLICATION_BLOCKER
from funpayflow.state import ReviewReceiptStore, StateError


def form(**changes):
    fields = {"fields[summary][ru]": "Manual service", "fields[summary][en]": "",
              "fields[desc][ru]": "Delivery by seller", "fields[desc][en]": "",
              "price": "12.50", "amount": "3", "active": "on",
              "deactivate_after_sale": "", "offer_id": "11", "node_id": "7",
              "csrf_token": "synthetic-transient", "form_created_at": "1"}
    fields.update(changes)
    return LotFields(11, fields)


@pytest.fixture
def setup_service(tmp_path):
    store = ReviewReceiptStore(tmp_path / "state.sqlite3")
    store.initialize()
    common = SimpleNamespace(id=7, name="Services", type=SubCategoryTypes.COMMON)
    currency = SimpleNamespace(id=8, name="Currency", type=SubCategoryTypes.CURRENCY)
    lots = [SimpleNamespace(id=11, title="Manual service", subcategory=common),
            SimpleNamespace(id=12, title="Currency", subcategory=currency)]
    account = SimpleNamespace(id=42, get_user=Mock(return_value=SimpleNamespace(
        id=42, get_lots=lambda: lots)), get_lot_fields=Mock(side_effect=lambda _: form()),
        save_lot=Mock())
    client = SimpleNamespace(account=account, review_state=store, _account_lock=threading.RLock())
    runtime_control.begin_runtime_cycle()
    return LotTemplateService(client, late_check=lambda: True), client, store


def test_discovery_is_own_common_only_and_does_not_claim_support(setup_service):
    service, client, _ = setup_service
    assert service.discover() == [{"source_id": 11, "title": "Manual service",
        "subcategory_id": 7, "subcategory": "Services", "publish_supported": False}]
    client.account.get_user.assert_called_once_with(42)
    client.account.get_lot_fields.assert_not_called()
    client.account.save_lot.assert_not_called()


def test_legacy_sqlite_without_drafts_preserves_existing_data(setup_service):
    _, _, store = setup_service
    store.record_audit_event('system', 'SETTINGS', 'settings', 'SAVED')
    cycle = store.autobump_cycle([7])
    audit = store.list_audit_events()
    with sqlite3.connect(store.path) as db:
        db.execute('DROP TABLE lot_template_drafts')
        before = list(db.execute('SELECT * FROM autobump_progress'))
    restored = ReviewReceiptStore(store.path)
    restored.initialize()
    restored.initialize()  # Migration is repeatable.
    assert restored.list_audit_events() == audit
    assert restored.list_lot_template_drafts(101, 42) == []
    with sqlite3.connect(store.path) as db:
        assert list(db.execute('SELECT * FROM autobump_progress')) == before
    assert cycle[0]['game_id'] == 7


@pytest.mark.parametrize('offset', [-1, True, '10', 2147483648])
def test_invalid_draft_page_rejected(setup_service, offset):
    service, _, _ = setup_service
    with pytest.raises(LotTemplateError):
        service.list_drafts(101, offset)


def test_persistent_edit_preview_cancel_and_no_form_metadata(setup_service):
    service, client, store = setup_service
    draft = service.create_draft(101, 11)
    draft_id = draft["draft_id"]
    assert draft["payload"]["price"] == "12.50"
    row = store.get_lot_template_draft(101, draft_id)
    assert row["baseline_ids"] == [11, 12]
    assert "synthetic-transient" not in json.dumps(row)
    service = LotTemplateService(client, late_check=lambda: True)
    assert service.list_drafts(101)[0]["draft_id"] == draft_id
    assert service.list_drafts(102) == []
    assert service.edit_draft(101, draft_id, "title_ru", "Edited")["state"] == "draft"
    preview = service.preview(101, draft_id)
    assert preview["state"] == "previewed"
    edited = service.edit_draft(101, draft_id, "price", "20")
    assert edited["revision"] > preview["revision"]
    assert edited["state"] == "draft"
    assert service.cancel(101, draft_id)["state"] == "cancelled"
    assert service.list_drafts(101) == []
    with pytest.raises(LotTemplateError):
        service.edit_draft(101, draft_id, "price", "21")
    client.account.save_lot.assert_not_called()


@pytest.mark.parametrize("field,value", [("price", "NaN"), ("price", "Infinity"),
    ("price", "0"), ("price", "-1"), ("amount", "0"), ("amount", "1.2"),
    ("amount", "100000001"), ("amount", "١"), ("title_ru", ""),
    ("description_ru", ""), ("title_ru", "x" * 501), ("description_ru", "\x00")])
def test_invalid_edits_do_not_change_stored_draft(setup_service, field, value):
    service, _, _ = setup_service
    before = service.create_draft(101, 11)
    with pytest.raises(LotTemplateError):
        service.edit_draft(101, before["draft_id"], field, value)
    assert service.get_draft(101, before["draft_id"]) == before


@pytest.mark.parametrize("extra", ["auto_delivery", "secrets", "password", "token",
    "fields[server]", "unknown"])
def test_unreviewed_goods_credentials_category_fields_rejected(setup_service, extra):
    service, client, _ = setup_service
    client.account.get_lot_fields.side_effect = lambda _: form(**{extra: "synthetic"})
    with pytest.raises(LotTemplateError, match="неподдерживаемые"):
        service.create_draft(101, 11)
    assert service.list_drafts(101) == []


def test_source_and_owner_account_boundaries(setup_service):
    service, client, _ = setup_service
    for source_id in (12, 999, -1, True):
        with pytest.raises(LotTemplateError):
            service.create_draft(101, source_id)
    draft = service.create_draft(101, 11)
    for operation in (service.get_draft, service.preview, service.cancel, service.confirm):
        with pytest.raises(LotTemplateError):
            operation(102, draft["draft_id"])
    client.account.id = 43
    with pytest.raises(LotTemplateError):
        service.get_draft(101, draft["draft_id"])
    assert service.list_drafts(101) == []


def test_confirm_always_blocked_without_claim_or_modifying_request(setup_service):
    service, client, store = setup_service
    draft = service.create_draft(101, 11)
    with pytest.raises(LotTemplateError, match="предпросмотр"):
        service.confirm(101, draft["draft_id"])
    preview = service.preview(101, draft["draft_id"])
    for _ in range(2):
        with pytest.raises(LotTemplateError) as error:
            service.confirm(101, draft["draft_id"], preview["revision"])
        assert str(error.value) == PUBLICATION_BLOCKER
    row = store.get_lot_template_draft(101, draft["draft_id"])
    assert row["state"] == "previewed" and row["attempt_id"] is None
    assert service.verify(101, draft["draft_id"])["verification"] == "unsupported_schema"
    client.account.save_lot.assert_not_called()


def test_safety_gates_and_stale_preview(setup_service):
    service, client, _ = setup_service
    draft = service.preview(101, service.create_draft(101, 11)["draft_id"])
    with pytest.raises(LotTemplateError, match="предпросмотр"):
        service.confirm(101, draft["draft_id"], draft["revision"] - 1)
    service.late_check = lambda: False
    with pytest.raises(LotTemplateError, match="SAFE_MODE"):
        service.confirm(101, draft["draft_id"])
    service.late_check = lambda: True
    assert runtime_control.claim_restart()
    try:
        with pytest.raises(LotTemplateError, match="SAFE_MODE"):
            service.confirm(101, draft["draft_id"])
    finally:
        runtime_control.begin_runtime_cycle()
    client.account.save_lot.assert_not_called()


def test_durable_claim_cas_and_unknown_never_resend_after_restart(setup_service):
    service, client, store = setup_service
    draft = service.preview(101, service.create_draft(101, 11)["draft_id"])
    def claim(index):
        return store.claim_lot_template_draft(101, draft["draft_id"], draft["revision"], str(index))
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(claim, range(4))) == 1
    store = ReviewReceiptStore(store.path)
    store.initialize()
    store.recover_lot_template_drafts()
    assert store.get_lot_template_draft(101, draft["draft_id"])["state"] == "ambiguous"
    assert not store.claim_lot_template_draft(101, draft["draft_id"], draft["revision"], "new")
    client.review_state = store
    restarted = LotTemplateService(client, late_check=lambda: True)
    with pytest.raises(LotTemplateError, match="неизвестен"):
        restarted.confirm(101, draft["draft_id"])
    assert restarted.verify(101, draft["draft_id"])["state"] == "ambiguous"
    client.account.save_lot.assert_not_called()


def test_network_and_sqlite_errors_are_safe(setup_service):
    service, client, store = setup_service
    client.account.get_user.side_effect = TimeoutError("private raw transport content")
    with pytest.raises(LotTemplateError) as error:
        service.discover()
    assert "private" not in str(error.value)
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE lot_template_drafts")
    with pytest.raises(StateError) as error:
        service.list_drafts(101)
    assert "no such table" not in str(error.value)


def test_installed_library_has_edit_contract_not_reference_create_contract():
    # A dependency update needs reinspection rather than silently enabling creation.
    assert list(inspect.signature(Account.get_lot_fields).parameters) == ["self", "lot_id"]
    account = Account("synthetic-offline")
    account._Account__initiated = True
    response = Mock()
    response.json.return_value = {"html": '<input name="offer_id" value="11">'
        '<input name="price" value="12.5"><input name="amount" value="3">'}
    account.method = Mock(return_value=response)
    fields = account.get_lot_fields(11)
    assert account.method.call_args.args[1] == "lots/offerEdit?offer=11"
    fields.lot_id = 0
    fields.renew_fields()
    assert fields.fields["offer_id"] == "11"  # Changing lot_id would edit original offer.
    response.json.return_value = {}
    assert account.save_lot(fields) is None
    assert account.method.call_args.args[0:2] == ("post", "lots/offerSave")
    assert account.method.call_args.args[3]["offer_id"] == "11"
