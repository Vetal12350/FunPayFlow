"""Independent own-inventory read-back through real parser/GET adapter, offline."""
from copy import deepcopy
from dataclasses import asdict, replace
from html import escape
import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from FunPayAPI import Account

from test_publication_preview import saved
from test_telegram_products import context, database_snapshot
from funpayflow import lot_publisher as module, runtime_control
from funpayflow.lot_publisher import LotPublisher, InventoryStatus, InventoryReason, FormContext


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    denied = Mock(side_effect=AssertionError('No external IO or Account construction'))
    for target, name in ((requests.sessions.Session, 'request'), (requests, 'post'),
                         (socket.socket, 'connect'), (Account, '__init__'),
                         (Account, 'method'), (Account, 'save_lot')):
        monkeypatch.setattr(target, name, denied)
    runtime_control.begin_runtime_cycle()
    yield
    denied.assert_not_called()
    runtime_control.begin_runtime_cycle()


def editor(c, offer_id):
    values = dict(c.fields)
    values.update(offer_id=str(offer_id), node_id='986')
    return '<form>' + ''.join(
        f'<input name="{escape(k, quote=True)}" value="{escape(v, quote=True)}">'
        for k, v in values.items()) + '</form>'


def changed_field(c, html, name, value):
    return html.replace(f'name="{name}" value="{escape(c.fields[name], quote=True)}"',
                        f'name="{name}" value="{value}"')


def trade(c):
    rows = []
    for offer_id, changes in c.remote.items():
        data = dict(price='15', currency='$', amount='1', auto=False,
                    title=escape(c.fields['fields[summary][ru]']))
        data.update(changes)
        rows.append(f'<a class="tc-item" data-offer="{offer_id}">'
            f'<div class="tc-desc-text">{data["title"]}</div>'
            f'<div class="tc-price" data-s="{data["price"]}">'
            f'<span class="unit">{data["currency"]}</span>'
            + ('<i class="auto-dlv-icon"></i>' if data['auto'] else '')
            + f'</div><div class="tc-amount">{data["amount"]}</div></a>')
    return (f'<html><body data-app-data=\'{json.dumps({"userId": c.owner})}\'>'
            '<div class="user-link-name">authenticated</div>'
            '<a href="/lots/offerEdit?node=986">add</a>'
            + ''.join(rows) + '</body></html>')


@pytest.fixture
def inventory(saved, monkeypatch):
    c = saved
    original = c.client.account
    # No constructor/login/second runtime Account: inert pinned class shell,
    # with synthetic credentials used solely to inspect offline GET arguments.
    account = object.__new__(Account)
    account.id = 42
    account._Account__initiated = True
    account.golden_key, account.phpsessid = 'PRIVATE-COOKIE', 'PRIVATE-SESSION'
    account.user_agent, account.proxy = 'offline', None
    account.get_subcategory = original.get_subcategory
    c.client.account = account
    c.now = c.draft['node_schema']['fetched_at'] + 1
    c.publisher = LotPublisher(c.client, c.store, late_check=lambda: True, clock=lambda: c.now)
    c.review = c.publisher.review(c.draft['id'], c.draft['revision'], 'USD')
    c.fields = {module.TEXT_FIELDS[name]: text for name, text in c.review.content}
    c.fields.update((name, value) for name, value, *_ in c.review.selections)
    c.fields.update(price='15', amount='1', csrf_token='PRIVATE-CSRF', secrets='PRIVATE-CODE')
    c.owner, c.remote, c.editors, c.responses = 42, {71: {}}, {}, []
    c.trade_filter = lambda html: html
    c.after_get = lambda: None

    def get(url, **kwargs):
        assert c.client._account_lock._is_owned()
        assert kwargs['allow_redirects'] is False and kwargs['verify'] is True
        assert kwargs['timeout'] == (5, 10) and kwargs['stream'] is True
        assert kwargs['headers']['cookie'] == 'golden_key=PRIVATE-COOKIE; PHPSESSID=PRIVATE-SESSION'
        if url.endswith('/lots/986/trade'):
            raw = c.trade_filter(trade(c)).encode()
        else:
            assert url.startswith('https://funpay.com/lots/offerEdit?offer=')
            offer_id = int(url.split('=')[-1])
            assert offer_id in c.remote
            raw = json.dumps({'html': c.editors.get(offer_id, editor(c, offer_id))}).encode()
        response = SimpleNamespace(status_code=200, history=[], url=url, headers={},
            iter_content=lambda size: [raw], close=Mock())
        c.responses.append(response)
        c.after_get()
        return response

    c.get = Mock(side_effect=get)
    monkeypatch.setattr(requests, 'get', c.get)
    yield c
    for response in c.responses:
        response.close.assert_called_once()
    c.client.account = original


def before(c):
    result = c.publisher.capture_own_inventory(c.review)
    assert result.status is InventoryStatus.READY, result
    assert result.remote_id is None
    return result


def verify(c, baseline):
    return c.publisher.verify_new_own_offer(c.review, baseline)


def test_exactly_one_independent_id_and_unchanged_database(inventory):
    c = inventory
    db = database_snapshot(c)
    baseline = before(c)
    c.remote[72] = {}
    result = verify(c, baseline)
    assert (result.status, result.reason, result.remote_id) == (
        InventoryStatus.CONFIRMED, InventoryReason.VERIFIED, 72)
    assert c.get.call_count == 7  # 3 before, 4 after; no extra account/profile GET
    assert database_snapshot(c) == db
    assert c.publisher.outcome(c.draft['id']) is None
    assert verify(c, baseline).status is InventoryStatus.UNKNOWN


@pytest.mark.parametrize('ids', [[], [72, 73]])
def test_zero_or_two_new_ids_unknown(inventory, ids):
    c = inventory
    baseline = before(c)
    c.remote.update({offer_id: {} for offer_id in ids})
    result = verify(c, baseline)
    assert result.reason is InventoryReason.NEW_IDS and result.remote_id is None


@pytest.mark.parametrize('change', ['removed', 'price', 'title', 'description', 'selection', 'active'])
def test_existing_offer_changed(inventory, change):
    c = inventory
    baseline = before(c)
    c.remote[72] = {}
    if change == 'removed':
        del c.remote[71]
    else:
        html = editor(c, 71)
        if change == 'price':
            c.remote[71]['price'] = '16'
            html = html.replace('name="price" value="15"', 'name="price" value="16"')
        elif change == 'title':
            html = changed_field(c, html, 'fields[summary][ru]', 'changed')
        elif change == 'description':
            html = changed_field(c, html, 'fields[desc][ru]', 'changed')
        elif change == 'selection':
            html = html.replace('value="pc"', 'value="other"')
        else:
            html = html.replace('</form>', '<input type="checkbox" name="active" checked></form>')
        c.editors[71] = html
    result = verify(c, baseline)
    assert result.reason is InventoryReason.EXISTING_CHANGED and result.remote_id is None


@pytest.mark.parametrize('change', ['node', 'id_echo', 'price', 'currency', 'amount', 'manual',
                                  'unknown_currency', 'missing_amount', 'text', 'selection', 'missing_text'])
def test_wrong_or_unavailable_candidate_fields(inventory, change):
    c = inventory
    baseline = before(c)
    c.remote[72] = {}
    html = editor(c, 72)
    if change == 'node': html = html.replace('name="node_id" value="986"', 'name="node_id" value="976"')
    elif change == 'id_echo': html = html.replace('name="offer_id" value="72"', 'name="offer_id" value="71"')
    elif change == 'price':
        c.remote[72]['price'] = '16'
        html = html.replace('name="price" value="15"', 'name="price" value="16"')
    elif change == 'currency': c.remote[72]['currency'] = '€'
    elif change == 'amount':
        c.remote[72]['amount'] = '2'
        html = html.replace('name="amount" value="1"', 'name="amount" value="2"')
    elif change == 'manual': c.remote[72]['auto'] = True
    elif change == 'unknown_currency': c.remote[72]['currency'] = '?'
    elif change == 'missing_amount': c.remote[72]['amount'] = ''
    elif change == 'text': html = changed_field(c, html, 'fields[summary][ru]', 'different')
    elif change == 'selection': html = html.replace('value="pc"', 'value="other"')
    else: html = html.replace('name="fields[desc][en]"', 'name="ignored"')
    c.editors[72] = html
    result = verify(c, baseline)
    assert result.status is InventoryStatus.UNKNOWN and result.remote_id is None


@pytest.mark.parametrize('change', ['owner', 'account', 'instance', 'owner_scope', 'late', 'restart',
                                  'stale', 'replaced_baseline', 'forged_baseline', 'revision'])
def test_scope_gates_staleness_and_baseline_provenance(inventory, change):
    c = inventory
    baseline = before(c)
    c.remote[72] = {}
    if change == 'owner': c.owner = 43
    elif change == 'account': c.client.account.id = 43
    elif change == 'instance': c.client.account = deepcopy(c.client.account)
    elif change == 'owner_scope': c.store.owner_id = '2'
    elif change == 'late': c.publisher.late_check = lambda: False
    elif change == 'restart': runtime_control.claim_restart()
    elif change == 'stale': c.now += 61
    elif change == 'replaced_baseline': before(c)
    elif change == 'forged_baseline': baseline = replace(baseline, snapshot=replace(baseline.snapshot))
    else: c.store.edit_draft(c.draft['id'], c.draft, c.draft['revision'])
    result = verify(c, baseline)
    assert result.status is InventoryStatus.UNKNOWN and result.remote_id is None


@pytest.mark.parametrize('change', ['partial', 'missing_context', 'missing_owner', 'duplicate', 'concurrent', 'switch'])
def test_incomplete_or_unstable_snapshot(inventory, change):
    c = inventory
    if change == 'partial': c.trade_filter = lambda h: h + '<ul class="pagination"></ul>'
    elif change == 'missing_context': c.trade_filter = lambda h: h.replace('?node=986', '?node=976')
    elif change == 'missing_owner': c.trade_filter = lambda h: h.replace('userId', 'unknown')
    elif change == 'duplicate': c.trade_filter = lambda h: h + '<a class="tc-item" data-offer="71"></a>'
    elif change == 'switch': c.after_get = lambda: setattr(c.client.account, 'id', 43)
    else:
        def alter():
            if c.get.call_count == 2: c.remote[72] = {}
        c.after_get = alter
    result = c.publisher.capture_own_inventory(c.review)
    assert result.status is InventoryStatus.UNKNOWN and result.snapshot is None


def test_safe_failures_and_snapshot_have_no_secrets(inventory, caplog, capsys):
    c = inventory
    baseline = before(c)
    safe = str(asdict(baseline)) + repr(baseline.snapshot)
    c.get.side_effect = RuntimeError('PRIVATE-COOKIE PRIVATE-SESSION PRIVATE-CSRF PRIVATE-CODE <html>')
    result = verify(c, baseline)
    safe += str(asdict(result)) + caplog.text + str(capsys.readouterr())
    assert result.status is InventoryStatus.UNKNOWN
    for canary in ['PRIVATE-COOKIE', 'PRIVATE-SESSION', 'PRIVATE-CSRF', 'PRIVATE-CODE', '<html>',
                   c.draft['payload']['description_ru']]:
        assert canary not in safe


@pytest.mark.parametrize('failure', ['redirect', 'cached', 'status', 'oversize'])
def test_transport_rejects_unsafe_response_without_retry(inventory, failure):
    c = inventory
    get = c.get.side_effect
    def unsafe(*args, **kwargs):
        response = get(*args, **kwargs)
        if failure == 'redirect': response.history = [object()]
        elif failure == 'cached': response.headers = {'Age': '12'}
        elif failure == 'status': response.status_code = 403
        else: response.iter_content = lambda size: [b'x' * 2_000_001]
        return response
    c.get.side_effect = unsafe
    result = c.publisher.capture_own_inventory(c.review)
    assert result.status is InventoryStatus.UNKNOWN
    assert c.get.call_count == 1


def test_empty_baseline_and_durable_unknown_remains_unknown(inventory):
    c = inventory
    c.remote.clear()
    baseline = before(c)
    form = FormContext('1', 42, id(c.client.account), deepcopy(c.draft['node_schema']),
                       'USD', 'synthetic-v1')
    prepared = c.publisher.prepare(c.draft['id'], c.draft['revision'], form, price='15', currency='USD')
    c.publisher._claim(prepared, form)
    c.remote[72] = {}
    assert verify(c, baseline).remote_id == 72
    assert c.publisher.outcome(c.draft['id']) == {
        'state': 'UNKNOWN', 'reason': 'CLAIMED', 'remote_id': None}


def test_bounded_inventory_and_pinned_version(inventory, monkeypatch):
    c = inventory
    c.remote = {i: {} for i in range(1, 22)}
    assert c.publisher.capture_own_inventory(c.review).status is InventoryStatus.UNKNOWN
    assert c.get.call_count == 1
    c.get.reset_mock()
    monkeypatch.setattr(module, 'version', lambda name: 'unverified')
    assert c.publisher.capture_own_inventory(c.review).status is InventoryStatus.UNKNOWN
    c.get.assert_not_called()


def test_inventory_title_change_and_contradictory_manual_flag(inventory):
    c = inventory
    baseline = before(c)
    c.remote[71]['title'] = 'changed remotely'
    c.remote[72] = {}
    assert verify(c, baseline).reason is InventoryReason.EXISTING_CHANGED
    baseline = before(c)
    c.remote[73] = {}
    c.editors[73] = editor(c, 73).replace('</form>',
        '<input type="checkbox" name="auto_delivery" checked></form>')
    assert verify(c, baseline).status is InventoryStatus.UNKNOWN


@pytest.mark.parametrize('name,value', [('currency', 'EUR'), ('user_id', '43'),
                                      ('auto_delivery', 'unknown')])
def test_explicit_editor_conflicts_fail_closed(inventory, name, value):
    c = inventory
    baseline = before(c)
    c.remote[72] = {}
    c.editors[72] = editor(c, 72).replace('</form>',
        f'<input name="{name}" value="{value}"></form>')
    result = verify(c, baseline)
    assert result.status is InventoryStatus.UNKNOWN and result.remote_id is None
