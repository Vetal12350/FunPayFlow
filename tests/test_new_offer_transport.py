"""Dormant offerSave boundary: all HTTP is intercepted, native Account is synthetic."""
from dataclasses import replace
import ast
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from FunPayAPI import Account

from test_telegram_products import context
from test_lot_publisher import case, no_external_calls, restarted
from test_create_wire_contract import form
from funpayflow import create_wire_contract as wire, runtime_control
from funpayflow.lot_publisher import PublisherError, publication_blockers
from funpayflow.node_form import prove_new_form_contract
from test_publication_preview import new_html


@pytest.fixture
def exchange(case, form, monkeypatch):
    c = case
    # Never initialize an authorized session or create a second runtime client.
    account = Account('SYNTHETIC-AUTH-CANARY', user_agent='SYNTHETIC-UA')
    account.id = 42
    account._Account__initiated = True
    account.phpsessid = 'SYNTHETIC-SESSION-CANARY'
    account.method = c.client.account.method
    account.save_lot = c.client.account.save_lot
    c.client.account = account
    c.form = replace(c.form, account_instance=id(account))
    c.prepared = c.publisher.prepare(c.draft['id'], c.draft['revision'], c.form,
                                      price='15.00', currency='USD')
    c.envelope = wire.assemble_new_request(c.prepared, form, now=c.now)
    c.response = SimpleNamespace(status_code=200, history=[],
        url='https://funpay.com/lots/offerSave',
        iter_content=Mock(return_value=iter([b'{"success":true,"id":72}'])), close=Mock())
    def post(*args, **kwargs):
        assert c.client._account_lock._is_owned()
        assert restarted(c).outcome(c.draft['id']) == {
            'state': 'UNKNOWN', 'reason': 'CLAIMED', 'remote_id': None}
        return c.response
    c.post = Mock(side_effect=post)
    monkeypatch.setattr(requests, 'post', c.post)
    c.boundary = wire.NewOfferTransport()
    # Explicit test-only proof seam. No real form or current envelope passes
    # the production gate; transport behavior is tested independently of it.
    monkeypatch.setattr(c.boundary, '_require_live_contract', Mock())
    return c


def send(c):
    return c.boundary._exchange(c.publisher, c.prepared, c.form, c.envelope)


def test_one_bounded_post_existing_account_lock_durable_claim_no_id_guess(exchange):
    c = exchange
    receipt = send(c)
    assert receipt.remote_id is None  # JSON above is deliberately NOT evidence.
    assert c.post.call_count == 1
    args, kwargs = c.post.call_args
    assert args == ('https://funpay.com/lots/offerSave',)
    assert kwargs['data'] == c.envelope.service + c.envelope.business
    assert kwargs['timeout'] == (5, 10)
    assert kwargs['allow_redirects'] is False and kwargs['verify'] is True
    assert kwargs['stream'] is True and kwargs['proxies'] == {}
    headers = kwargs['headers']
    assert headers['Origin'] == 'https://funpay.com'
    assert headers['Referer'] == 'https://funpay.com/lots/offerEdit?node=986'
    assert headers['X-Requested-With'] == 'XMLHttpRequest'
    assert headers['cookie'] == 'golden_key=SYNTHETIC-AUTH-CANARY; PHPSESSID=SYNTHETIC-SESSION-CANARY'
    c.response.close.assert_called_once()
    with pytest.raises(PublisherError, match='ALREADY_CLAIMED'):
        send(c)
    c.boundary = wire.NewOfferTransport()
    with pytest.raises(PublisherError, match='ALREADY_CLAIMED'):
        send(c)
    assert c.post.call_count == 1
    assert restarted(c).outcome(c.draft['id'])['state'] == 'UNKNOWN'
    with closing(c.receipt._connect()) as db:
        persisted = '\n'.join(db.iterdump())
    for secret in ('SYNTHETIC-AUTH-CANARY', 'SYNTHETIC-SESSION-CANARY', c.controls.csrf):
        assert secret not in persisted


@pytest.mark.parametrize('failure,reason', [
    ('redirect', 'TRANSPORT_POLICY'), ('history', 'TRANSPORT_POLICY'),
    ('origin', 'TRANSPORT_POLICY'), ('server', 'TRANSPORT_POLICY'),
    ('auth', 'TRANSPORT_POLICY'), ('timeout', 'TRANSPORT_UNKNOWN'),
    ('stream_timeout', 'TRANSPORT_UNKNOWN'), ('oversize', 'TRANSPORT_UNKNOWN'),
    ('deadline', 'TRANSPORT_UNKNOWN'), ('crash', None),
])
def test_ambiguous_exchange_never_retries_or_clears_unknown(exchange, monkeypatch, failure, reason,
                                                          caplog, capsys):
    c = exchange
    class Crash(BaseException):
        pass
    if failure == 'redirect': c.response.status_code = 302
    elif failure == 'history': c.response.history = [object()]
    elif failure == 'origin': c.response.url = 'https://other.invalid/lots/offerSave'
    elif failure == 'server': c.response.status_code = 503
    elif failure == 'auth': c.response.status_code = 403
    elif failure == 'timeout': c.post.side_effect = requests.Timeout('SYNTHETIC-PRIVATE')
    elif failure == 'stream_timeout': c.response.iter_content.side_effect = requests.Timeout('SYNTHETIC-PRIVATE')
    elif failure == 'oversize': c.response.iter_content.return_value = iter([b'x' * 262145])
    elif failure == 'deadline': monkeypatch.setattr(wire.time, 'monotonic', Mock(side_effect=[0, 16]))
    elif failure == 'crash': c.post.side_effect = Crash()
    with pytest.raises(Crash if reason is None else PublisherError) as raised:
        send(c)
    if reason is not None:
        assert str(raised.value) == reason
    with pytest.raises(PublisherError, match='ALREADY_CLAIMED'):
        send(c)
    assert c.post.call_count == 1
    if failure not in {'timeout', 'crash'}:
        c.response.close.assert_called_once()
    assert restarted(c).outcome(c.draft['id']) == {
        'state': 'UNKNOWN', 'reason': 'CLAIMED', 'remote_id': None}
    c.publisher = restarted(c)
    changed = c.store.edit_draft(c.draft['id'], c.draft, c.draft['revision'])
    c.prepared = c.publisher.prepare(changed['id'], changed['revision'], c.form,
                                      price='15.00', currency='USD')
    c.boundary = wire.NewOfferTransport()
    with pytest.raises(PublisherError, match='ALREADY_CLAIMED'):
        send(c)
    assert c.post.call_count == 1
    captured = capsys.readouterr()
    assert 'SYNTHETIC-' not in caplog.text + captured.out + captured.err


@pytest.mark.parametrize('change', ['gate', 'restart', 'owner', 'account', 'revision', 'route',
                                  'stale', 'no_claim', 'business', 'version'])
def test_pre_dispatch_guards_make_zero_requests(exchange, monkeypatch, change):
    c = exchange
    if change == 'gate': c.publisher.late_check = lambda: False
    elif change == 'restart': runtime_control.claim_restart()
    elif change == 'owner': c.publisher.store.owner_id = '2'
    elif change == 'account': c.client.account.id = 43
    elif change == 'revision': c.store.edit_draft(c.draft['id'], c.draft, c.draft['revision'])
    elif change == 'route': c.store.save_route(c.route, c.route['revision'])
    elif change == 'stale': c.now += 301
    elif change == 'no_claim': c.prepared = replace(c.prepared, draft_id='missing')
    elif change == 'business': c.envelope = replace(c.envelope, business=())
    elif change == 'version': monkeypatch.setattr(wire, 'version', lambda _: '2.0')
    with pytest.raises(PublisherError):
        send(c)
    c.post.assert_not_called()


def test_public_transport_rejects_even_synthetic_full_contract_and_live_switch(exchange, monkeypatch):
    c = exchange
    monkeypatch.setenv('FUNPAY_PUBLISH_ENABLED', '1')
    with pytest.raises(PublisherError, match='CONTRACT_UNVERIFIED'):
        c.boundary.submit_once(c.publisher, c.prepared, c.form, c.envelope)
    c.post.assert_not_called()
    assert c.publisher.outcome(c.draft['id']) is None


def test_private_boundary_also_rejects_unverified_contract_before_claim(exchange):
    c = exchange
    c.boundary = wire.NewOfferTransport()
    with pytest.raises(PublisherError, match='CONTRACT_UNVERIFIED'):
        send(c)
    c.post.assert_not_called()
    assert c.publisher.outcome(c.draft['id']) is None


def test_a_new_transport_instance_cannot_use_finished_attempt(exchange):
    c = exchange
    c.publisher._claim(c.prepared, c.form)
    c.publisher._finish(c.prepared, 'TRANSPORT_UNKNOWN')
    c.boundary = wire.NewOfferTransport()
    with pytest.raises(PublisherError, match='ALREADY_CLAIMED'):
        send(c)
    c.post.assert_not_called()


def test_runtime_has_no_call_to_dormant_transport():
    for path in Path('src/funpayflow').glob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert not (isinstance(node.func, ast.Attribute) and node.func.attr == '_exchange')
                assert not (isinstance(node.func, ast.Name) and node.func.id == 'NewOfferTransport')


@pytest.mark.parametrize('old,new,code', [
    ('method="post"', 'method="get"', 'FORM_STRUCTURE'),
    ('name="csrf_token"', 'name="unknown"', 'CSRF'),
    ('name="offer_id" value=""', 'name="offer_id" value="0"', 'NEW_IDENTITY'),
    ('</form>', '<input name="unknown"></form>', 'CONTROL_INVENTORY'),
])
def test_exact_structural_blocker_does_not_hide_currency_or_id_evidence(old, new, code):
    proof = prove_new_form_contract(new_html().replace(old, new),
                                    'https://funpay.com/lots/offerEdit?node=986')
    blockers = publication_blockers(proof, True)
    assert code in blockers
    assert 'CURRENCY_MAPPING' in blockers and 'MANUAL_MAPPING' in blockers
    assert 'RESPONSE_ID' in blockers and 'OWNER_READBACK' in blockers
    assert 'SELECTS' not in blockers
    assert 'SELECTS' in publication_blockers(proof, False)


def test_even_forged_green_matrix_cannot_remove_semantic_blockers():
    proof = prove_new_form_contract(new_html(), 'https://funpay.com/lots/offerEdit?node=986')
    assert proof['currency_control'] == 'absent'
    assert 'CURRENCY_MAPPING' in publication_blockers(proof, True)
    proof['publish_ready'] = True
    assert 'CURRENCY_MAPPING' in publication_blockers(proof, True)
    assert 'FORM_STRUCTURE' in publication_blockers(proof, True)
