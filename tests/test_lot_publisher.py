"""Synthetic saved-draft -> NEW -> exact read-back. No external IO allowed."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import socket
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from FunPayAPI import Account
from FunPayAPI.types import LotFields
from FunPayAPI.common.enums import SubCategoryTypes

from test_telegram_products import context, seed_route
from funpayflow.lot_publisher import (
    FormContext, ServiceControls, SubmitReceipt, SyntheticTransport,
    LotPublisher, PublisherError, build_new_offer,
)
from funpayflow.node_products import NodeDraftService
from funpayflow.product_store import ProductStore
from funpayflow.state import ReviewReceiptStore
from funpayflow import runtime_control


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    denied = Mock(side_effect=AssertionError('External IO forbidden'))
    monkeypatch.setattr(requests.sessions.Session, 'request', denied)
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(Account, 'method', denied)
    monkeypatch.setattr(Account, 'save_lot', denied)
    runtime_control.begin_runtime_cycle()
    yield denied
    assert denied.call_count == 0
    runtime_control.begin_runtime_cycle()


@pytest.fixture
def case(context, monkeypatch):
    c = context
    c.nodes[0].id = 986
    c.nodes[0].category.id = 274
    c.client.account.is_initiated = True
    html = (Path(__file__).parent / 'fixtures/node_forms/elden_ring.html').read_bytes()
    c.client.account.method = Mock(return_value=SimpleNamespace(
        status_code=200, url='https://funpay.com/lots/offerEdit?node=986', content=html))
    c.route = seed_route(c)
    nd = NodeDraftService(c.store, c.routes)
    # All values deliberately selected, including optional fields. No defaults
    # inferred by the publisher and no claimed live Elden Ring option contract.
    c.values = {'fields[platform]': 'pc', 'fields[edition]': 'base',
                'fields[edition_extra]': 'standard', 'fields[delivery]': 'key',
                'fields[region]': 'eu'}
    preview = nd.create_node_preview(c.template['id'], c.route['id'], c.values, price='15.00')
    c.draft = nd.confirm(nd.stage([preview])['token'])['results'][0]
    c.now = c.draft['node_schema']['fetched_at'] + 1
    c.form = FormContext('1', 42, id(c.client.account), deepcopy(c.draft['node_schema']), 'USD', 'synthetic-v1')
    c.controls = ServiceControls('SYNTHETIC_NEW', 'SYNTHETIC-PRIVATE-TOKEN', 'SYNTHETIC_MANUAL')
    c.publisher = LotPublisher(c.client, c.store, late_check=lambda: True, clock=lambda: c.now)
    c.prepared = c.publisher.prepare(c.draft['id'], c.draft['revision'], c.form,
                                     price='15.00', currency='USD')
    c.transport = SyntheticTransport()
    c.remote = {71: {'node': c.nodes[1], 'fields': {
        'offer_id': '71', 'node_id': str(c.nodes[1].id), 'user_id': '42',
        'fields[summary][ru]': 'pre-existing', 'fields[summary][en]': 'existing',
        'price': '7.00', 'amount': '3', 'currency': 'EUR', 'active': 'on',
        'fields[unrelated]': 'keep', 'csrf_token': 'DO-NOT-PERSIST'}}}

    def profile(account_id):
        assert account_id == 42
        assert c.client._account_lock._is_owned()
        return SimpleNamespace(id=42, get_lots=lambda: [
            SimpleNamespace(id=k, subcategory=v['node']) for k, v in c.remote.items()])

    def fields(lot_id):
        assert c.client._account_lock._is_owned()
        return LotFields(lot_id, deepcopy(c.remote[lot_id]['fields']))

    c.client.account.get_user = Mock(side_effect=profile)
    c.client.account.get_lot_fields = Mock(side_effect=fields)
    # Draft preparation used a synthetic form GET; publication never calls it.
    c.client.account.method.reset_mock()

    def submit(business, controls, **policy):
        assert c.client._account_lock._is_owned()
        assert policy == dict(node_id=986, method='POST', path='lots/offerSave',
                              allow_redirects=False, max_attempts=1)
        # A separate connection must see the committed claim before this call.
        assert c.publisher.outcome(c.draft['id']) == dict(
            state='UNKNOWN', reason='CLAIMED', remote_id=None)
        assert controls == c.controls
        c.remote[72] = {'node': c.nodes[0], 'fields': dict(business,
            offer_id='72', node_id='986', user_id='42', auto_delivery='SYNTHETIC_MANUAL')}
        return SubmitReceipt(72)

    c.submit = submit
    c.send = Mock(side_effect=submit)
    monkeypatch.setattr(SyntheticTransport, 'submit_once', c.send)
    yield c
    c.client.account.method.assert_not_called()
    c.client.account.save_lot.assert_not_called()


def confirm(c, **kwargs):
    return c.publisher.confirm_synthetic(c.prepared, c.form, c.controls,
                                          transport=c.transport, **kwargs)


def restarted(c):
    receipt = ReviewReceiptStore(c.receipt.path)
    receipt.initialize()
    client = SimpleNamespace(account=c.client.account, review_state=receipt,
                             _account_lock=c.client._account_lock)
    return LotPublisher(client, ProductStore(receipt, 1, 42),
                        late_check=lambda: True, clock=lambda: c.now)


def test_one_saved_draft_success_exact_id_and_unchanged_other_lots(case):
    c = case
    before = deepcopy(c.remote[71])
    result = confirm(c)
    assert result == {'state': 'CONFIRMED', 'reason': 'CONFIRMED', 'remote_id': 72}
    assert c.remote[71] == before
    assert set(c.remote) == {71, 72}
    assert c.remote[72]['fields'] == dict(c.prepared.business,
        offer_id='72', node_id='986', user_id='42', auto_delivery='SYNTHETIC_MANUAL')
    assert c.client.account.get_lot_fields.call_args_list[-1].args == (72,)
    assert c.send.call_count == 1
    assert restarted(c).outcome(c.draft['id']) == result
    # Publisher does not replace the saved product draft or its route.
    assert c.store.get_draft(c.draft['id']) == c.draft
    assert c.store.get_route(c.route['id']) == c.route


def test_builder_is_pure_allowlisted_and_has_no_hidden_identity(case):
    c = case
    before = deepcopy((c.draft, c.route, c.template, c.form.schema))
    result = build_new_offer(c.draft, c.route, c.template, c.form, owner_id='1', account_id=42,
        account_instance=id(c.client.account),
        revision=c.draft['revision'], price='15.00', currency='USD', now=c.now)
    assert result == c.prepared
    assert before == (c.draft, c.route, c.template, c.form.schema)
    assert set(dict(result.business)) == {
        'fields[summary][ru]', 'fields[summary][en]', 'fields[desc][ru]', 'fields[desc][en]',
        'price', 'amount', 'currency', *c.values}
    assert result.summary() == {'mode': 'synthetic', 'live_enabled': False,
                                'node': 986, 'quantity': 1, 'state': 'PREPARED'}
    assert c.publisher.outcome(c.draft['id']) is None


@pytest.mark.parametrize('change,reason', [
    ('node', 'SCOPE'), ('category', 'SCOPE'), ('type', 'SCOPE'), ('game', 'SCOPE'),
    ('template', 'SCOPE'), ('variant', 'SCOPE'), ('owner', 'SCOPE'), ('account', 'SCOPE'),
    ('revision', 'REVISION'), ('route_revision', 'REVISION'), ('template_revision', 'REVISION'),
    ('cancelled', 'REVISION'), ('stale', 'FORM_STALE'), ('future', 'FORM_STALE'),
    ('nan', 'FORM_STALE'), ('fingerprint', 'SCHEMA'), ('option', 'SCHEMA'),
    ('missing_option', 'SCHEMA'), ('duplicate_option', 'SCHEMA'), ('hidden', 'SCHEMA'),
    ('price', 'PRICE'), ('currency', 'PRICE'), ('zero', 'PRICE'), ('infinite', 'PRICE'),
    ('quantity', 'QUANTITY'), ('bool_quantity', 'QUANTITY'), ('title', 'CONTENT'),
    ('extra_payload', 'CONTENT'), ('unverified', 'CONTRACT_UNVERIFIED'),
])
def test_builder_rejects_wrong_scope_stale_or_inexact_submission(case, change, reason):
    c = case
    d, r, t, f = deepcopy((c.draft, c.route, c.template, c.form))
    args = dict(owner_id='1', account_id=42, revision=d['revision'], price='15.00', currency='USD', now=c.now)
    args['account_instance'] = id(c.client.account)
    if change == 'node': d['node'] = 976
    elif change == 'category': r['metadata']['category_id'] = 1
    elif change == 'type': r['metadata']['type'] = 'CURRENCY'
    elif change == 'game': d['game'] = 'Another game'
    elif change == 'template': t['name'] = 'GIFT'
    elif change == 'variant': d['variant_id'] = 'another'
    elif change == 'owner': args['owner_id'] = '2'
    elif change == 'account': args['account_id'] = 43
    elif change == 'revision': args['revision'] += 1
    elif change == 'route_revision': r['revision'] += 1
    elif change == 'template_revision': t['revision'] += 1
    elif change == 'cancelled': d['status'] = 'cancelled'
    elif change == 'stale': args['now'] += 301
    elif change == 'future': args['now'] -= 2
    elif change == 'nan': args['now'] = float('nan')
    elif change == 'fingerprint': f.schema['fingerprint'] = 'changed'
    elif change == 'option': d['node_fields'][0]['value'] = 'missing'
    elif change == 'missing_option': d['node_fields'].pop()
    elif change == 'duplicate_option': d['node_fields'].append(d['node_fields'][0])
    elif change == 'hidden': f.schema['fields'][0]['html_name'] = 'csrf_token'
    elif change == 'price': args['price'] = '16'
    elif change == 'currency': args['currency'] = 'RUB'
    elif change == 'zero': d['payload']['price'] = '0'
    elif change == 'infinite': d['payload']['price'] = 'Infinity'
    elif change == 'quantity': d['payload']['amount'] = 2
    elif change == 'bool_quantity': d['payload']['amount'] = True
    elif change == 'title': d['payload']['title_ru'] = ''
    elif change == 'extra_payload': d['payload']['offer_id'] = 71
    elif change == 'unverified': f = replace(f, provenance='response')
    with pytest.raises(PublisherError, match=f'^{reason}$'):
        build_new_offer(d, r, t, f, **args)
    c.send.assert_not_called()


@pytest.mark.parametrize('controls,reason', [
    (ServiceControls('0', 'present', 'SYNTHETIC_MANUAL'), 'NEW_IDENTITY'),
    (ServiceControls('71', 'present', 'SYNTHETIC_MANUAL'), 'NEW_IDENTITY'),
    (ServiceControls('', 'present', 'SYNTHETIC_MANUAL'), 'NEW_IDENTITY'),
    (ServiceControls('SYNTHETIC_NEW', '', 'SYNTHETIC_MANUAL'), 'CSRF'),
    (ServiceControls('SYNTHETIC_NEW', None, 'SYNTHETIC_MANUAL'), 'CSRF'),
    (ServiceControls('SYNTHETIC_NEW', 'present', ''), 'MANUAL_MODE'),
    (ServiceControls('SYNTHETIC_NEW', 'present', 'auto'), 'MANUAL_MODE'),
])
def test_service_controls_fail_before_claim(case, controls, reason):
    c = case
    c.controls = controls
    with pytest.raises(PublisherError, match=f'^{reason}$'):
        confirm(c)
    assert c.publisher.outcome(c.draft['id']) is None
    c.send.assert_not_called()


@pytest.mark.parametrize('receipt,reason', [
    (SubmitReceipt(None), 'RESPONSE_ID'), (SubmitReceipt(True), 'RESPONSE_ID'),
    (SubmitReceipt(0), 'RESPONSE_ID'), (SubmitReceipt(71), 'RESPONSE_ID'),
    (SubmitReceipt(72, 302), 'TRANSPORT_POLICY'),
    (SubmitReceipt(72, redirects=1), 'TRANSPORT_POLICY'),
    (SubmitReceipt(72, attempts=2), 'TRANSPORT_POLICY'),
    ({'success': True}, 'TRANSPORT_POLICY'), (None, 'TRANSPORT_POLICY'),
])
def test_misleading_success_or_redirect_is_unknown_without_resend(case, receipt, reason):
    c = case
    c.send.side_effect = lambda *a, **kw: (c.submit(*a, **kw), receipt)[1]
    assert confirm(c) == dict(state='UNKNOWN', reason=reason, remote_id=None)
    c.publisher = restarted(c)
    with pytest.raises(PublisherError, match='ALREADY_CLAIMED'):
        confirm(c)
    assert c.send.call_count == 1


@pytest.mark.parametrize('change,reason', [
    ('owner', 'READBACK'), ('node', 'READBACK'), ('category', 'READBACK'), ('kind', 'READBACK'),
    ('lot_id', 'READBACK'), ('hidden_id', 'READBACK'), ('hidden_owner', 'READBACK'),
    ('hidden_node', 'READBACK'), ('missing_identity', 'READBACK'),
    ('price', 'READBACK'), ('quantity', 'READBACK'),
    ('manual', 'READBACK'), ('currency', 'READBACK'), ('option', 'READBACK'),
    ('title', 'READBACK'), ('missing_new', 'OTHER_LOTS_CHANGED'),
    ('extra_new', 'OTHER_LOTS_CHANGED'), ('edit_other', 'OTHER_LOTS_CHANGED'),
    ('delete_other', 'OTHER_LOTS_CHANGED'),
])
def test_exact_readback_and_other_lot_protection(case, change, reason):
    c = case
    def submit(*args, **kwargs):
        receipt = c.submit(*args, **kwargs)
        if change == 'owner': c.client.account.get_user.side_effect = lambda _: SimpleNamespace(id=43)
        elif change == 'node': c.remote[72]['node'] = c.nodes[1]
        elif change == 'category': c.remote[72]['node'] = SimpleNamespace(
            id=986, category=SimpleNamespace(id=275), type=SubCategoryTypes.COMMON)
        elif change == 'kind': c.remote[72]['node'] = SimpleNamespace(
            id=986, category=SimpleNamespace(id=274), type=SubCategoryTypes.CURRENCY)
        elif change == 'lot_id': c.client.account.get_lot_fields.side_effect = lambda _: LotFields(999, {})
        elif change == 'hidden_id': c.remote[72]['fields']['offer_id'] = '71'
        elif change == 'hidden_owner': c.remote[72]['fields']['user_id'] = '43'
        elif change == 'hidden_node': c.remote[72]['fields']['node_id'] = '976'
        elif change == 'missing_identity': c.remote[72]['fields'].pop('offer_id')
        elif change == 'price': c.remote[72]['fields']['price'] = '16'
        elif change == 'quantity': c.remote[72]['fields']['amount'] = '2'
        elif change == 'manual': c.remote[72]['fields'].pop('auto_delivery')
        elif change == 'currency': c.remote[72]['fields']['currency'] = 'RUB'
        elif change == 'option': c.remote[72]['fields']['fields[platform]'] = 'ps'
        elif change == 'title': c.remote[72]['fields']['fields[summary][ru]'] = 'wrong'
        elif change == 'missing_new': del c.remote[72]
        elif change == 'extra_new':
            c.remote[73] = deepcopy(c.remote[72])
            c.remote[73]['fields']['offer_id'] = '73'
        elif change == 'edit_other': c.remote[71]['fields']['price'] = '999'
        elif change == 'delete_other': del c.remote[71]
        return receipt
    c.send.side_effect = submit
    assert confirm(c) == dict(state='UNKNOWN', reason=reason, remote_id=None)
    assert restarted(c).outcome(c.draft['id'])['state'] == 'UNKNOWN'
    assert c.send.call_count == 1


@pytest.mark.parametrize('phase', ['before', 'after'])
def test_timeout_is_unknown_even_if_server_created_lot(case, phase):
    c = case
    def timeout(*args, **kwargs):
        if phase == 'after': c.submit(*args, **kwargs)
        raise TimeoutError('SYNTHETIC-PRIVATE-TOKEN raw HTML cookies user text')
    c.send.side_effect = timeout
    assert confirm(c) == dict(state='UNKNOWN', reason='TRANSPORT_UNKNOWN', remote_id=None)
    c.publisher = restarted(c)
    assert c.publisher.outcome(c.draft['id'])['state'] == 'UNKNOWN'
    with pytest.raises(PublisherError, match='ALREADY_CLAIMED'): confirm(c)
    assert c.send.call_count == 1


@pytest.mark.parametrize('phase', ['claim', 'dispatch'])
def test_crash_after_claim_survives_restart_and_revision_edit(case, monkeypatch, phase):
    c = case
    class Crash(BaseException): pass
    if phase == 'claim':
        original = c.publisher._claim
        def crash(*args):
            original(*args)
            raise Crash()
        monkeypatch.setattr(c.publisher, '_claim', crash)
    else:
        c.send.side_effect = Crash()
    with pytest.raises(Crash): confirm(c)
    c.publisher = restarted(c)
    assert c.publisher.outcome(c.draft['id']) == dict(state='UNKNOWN', reason='CLAIMED', remote_id=None)
    changed = c.store.edit_draft(c.draft['id'], c.draft, c.draft['revision'])
    c.prepared = c.publisher.prepare(changed['id'], changed['revision'], c.form, price='15.00', currency='USD')
    with pytest.raises(PublisherError, match='ALREADY_CLAIMED'): confirm(c)
    assert c.send.call_count == (0 if phase == 'claim' else 1)


def test_concurrent_confirm_from_independent_locks_and_connections(case):
    c = case
    # The first fixture fake asserts its own lock; wrap both clients in that
    # same lock only for their read methods, not around claim/dispatch.
    original_snapshot = c.publisher._snapshot
    def snapshot(account):
        with c.client._account_lock:
            return original_snapshot(account)
    publishers = []
    for _ in range(2):
        p = restarted(c)
        p.client._account_lock = threading.RLock()
        p._snapshot = snapshot
        publishers.append(p)
    original_send = c.submit
    def submit(*args, **kwargs):
        with c.client._account_lock:
            return original_send(*args, **kwargs)
    c.send.side_effect = submit
    def run(p):
        try:
            return p.confirm_synthetic(c.prepared, c.form, c.controls, transport=c.transport)['state']
        except PublisherError as exc:
            return exc.reason
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, publishers))
    assert sorted(results) == ['ALREADY_CLAIMED', 'CONFIRMED']
    assert c.send.call_count == 1


def test_db_failure_before_claim_means_zero_dispatch(case):
    c = case
    with closing(c.receipt._connect()) as db, db:
        db.execute("CREATE TRIGGER deny_claim BEFORE INSERT ON lot_publish_attempts "
                   "BEGIN SELECT RAISE(ABORT, 'private db details'); END")
    with pytest.raises(PublisherError, match='DATABASE'): confirm(c)
    c.send.assert_not_called()
    assert c.publisher.outcome(c.draft['id']) is None


def test_atomic_claim_competes_across_sqlite_connections(case):
    c = case
    barrier = threading.Barrier(2)
    publishers = [restarted(c), restarted(c)]
    def claim(p):
        barrier.wait(timeout=5)
        try:
            p._claim(c.prepared, c.form)
            return 'CLAIMED'
        except PublisherError as exc:
            return exc.reason
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, publishers)) == ['ALREADY_CLAIMED', 'CLAIMED']
    assert restarted(c).outcome(c.draft['id'])['state'] == 'UNKNOWN'
    c.send.assert_not_called()


def test_claim_commit_failure_prevents_dispatch(case, monkeypatch):
    c = case
    class FailingCommit(sqlite3.Connection):
        claim = False
        def execute(self, sql, *args, **kwargs):
            if sql.startswith('INSERT INTO lot_publish_attempts'):
                self.claim = True
            return super().execute(sql, *args, **kwargs)
        def __exit__(self, *args):
            if self.claim:
                self.rollback()
                raise sqlite3.OperationalError('PRIVATE commit failure')
            return super().__exit__(*args)
    monkeypatch.setattr(c.receipt, '_connect', lambda: sqlite3.connect(c.receipt.path, factory=FailingCommit))
    with pytest.raises(PublisherError, match='DATABASE'): confirm(c)
    c.send.assert_not_called()
    assert restarted(c).outcome(c.draft['id']) is None


@pytest.mark.parametrize('change', ['revision', 'expiry', 'account'])
def test_late_change_after_durable_claim_never_dispatches(case, monkeypatch, change):
    c = case
    original = c.publisher._claim
    def claim(*args):
        original(*args)
        if change == 'revision': c.store.edit_draft(c.draft['id'], c.draft, c.draft['revision'])
        elif change == 'expiry': c.now += 301
        else: c.client.account = SimpleNamespace(**vars(c.client.account))
    monkeypatch.setattr(c.publisher, '_claim', claim)
    assert confirm(c)['state'] == 'UNKNOWN'
    c.send.assert_not_called()
    assert restarted(c).outcome(c.draft['id'])['state'] == 'UNKNOWN'


def test_failed_result_commit_preserves_unknown(case):
    c = case
    with closing(c.receipt._connect()) as db, db:
        db.execute("CREATE TRIGGER deny_result BEFORE UPDATE ON lot_publish_attempts "
                   "BEGIN SELECT RAISE(ABORT, 'private db details'); END")
    with pytest.raises(PublisherError, match='DATABASE'): confirm(c)
    assert restarted(c).outcome(c.draft['id']) == dict(state='UNKNOWN', reason='CLAIMED', remote_id=None)
    with pytest.raises(PublisherError, match='ALREADY_CLAIMED'): confirm(c)
    assert c.send.call_count == 1


@pytest.mark.parametrize('change', ['draft', 'route', 'template', 'account', 'session', 'owner', 'stale'])
def test_changes_after_prepare_fail_before_dispatch(case, change):
    c = case
    if change == 'draft': c.store.edit_draft(c.draft['id'], c.draft, c.draft['revision'])
    elif change == 'route': c.store.save_route(c.route, c.route['revision'])
    elif change == 'template': c.templates.save(c.template, c.template['id'], c.template['revision'])
    elif change == 'account': c.client.account.id = 43
    elif change == 'session': c.client.account = SimpleNamespace(**vars(c.client.account))
    elif change == 'owner': c.publisher.store = ProductStore(c.receipt, 2, 42)
    elif change == 'stale': c.now += 301
    with pytest.raises(PublisherError): confirm(c)
    c.send.assert_not_called()


@pytest.mark.parametrize('phase', ['default', 'before', 'after_claim', 'restart'])
def test_runtime_and_late_safe_mode_gates(case, phase):
    c = case
    if phase == 'default': c.publisher = LotPublisher(c.client, c.store, clock=lambda: c.now)
    elif phase == 'before': c.publisher.late_check = lambda: False
    elif phase == 'after_claim': c.publisher.late_check = Mock(side_effect=[True, True, False])
    else: runtime_control.claim_restart()
    if phase == 'after_claim':
        assert confirm(c) == dict(state='UNKNOWN', reason='GATE', remote_id=None)
    else:
        with pytest.raises(PublisherError, match='GATE'): confirm(c)
        assert c.publisher.outcome(c.draft['id']) is None
    c.send.assert_not_called()


def test_production_entry_is_closed_even_with_settings_and_real_account(case, monkeypatch):
    c = case
    monkeypatch.setenv('FUNPAY_PUBLISH_ENABLED', '1')
    with pytest.raises(PublisherError, match='LIVE_DISABLED'):
        c.publisher.confirm_synthetic(c.prepared, c.form, c.controls)
    # Construct no authorized Account or credentials; class identity alone blocks.
    original = c.client.account
    c.client.account = object.__new__(Account)
    with pytest.raises(PublisherError, match='LIVE_DISABLED'): confirm(c)
    # Restore fake for fixture assertions.
    c.client.account = original
    c.send.assert_not_called()


def test_safe_mode_changed_during_last_database_read_blocks_boundary(case, monkeypatch):
    c = case
    original = c.publisher.prepare
    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        c.publisher.late_check = lambda: False
        return result
    monkeypatch.setattr(c.publisher, 'prepare', prepare)
    assert confirm(c) == dict(state='UNKNOWN', reason='GATE', remote_id=None)
    c.send.assert_not_called()


def test_unpatched_transport_has_no_live_implementation():
    with pytest.raises(PublisherError, match='LIVE_DISABLED'):
        SyntheticTransport().submit_once({}, ServiceControls('', '', ''), node_id=986,
            method='POST', path='lots/offerSave', allow_redirects=False, max_attempts=1)


def test_no_secret_content_or_exception_in_outcome_logs_or_attempt_table(case, capsys, caplog):
    c = case
    c.send.side_effect = RuntimeError('SYNTHETIC-PRIVATE-TOKEN raw HTML POST cookies private-text')
    result = confirm(c)
    with closing(c.receipt._connect()) as db:
        rows = db.execute('SELECT * FROM lot_publish_attempts').fetchall()
        dump = '\n'.join(db.iterdump())
    output = str((result, rows, c.prepared.summary(), repr(c.prepared), repr(c.form), repr(c.controls)))
    output += capsys.readouterr().out + capsys.readouterr().err + caplog.text
    for canary in ('SYNTHETIC-PRIVATE-TOKEN', 'DO-NOT-PERSIST', 'raw HTML', 'POST cookies',
                   'private-text', c.draft['payload']['description_ru']):
        assert canary not in output
    assert 'SYNTHETIC-PRIVATE-TOKEN' not in dump
    assert 'DO-NOT-PERSIST' not in dump
