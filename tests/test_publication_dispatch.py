"""Integrated owner confirmation; all wire proof is explicitly test-only."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from test_telegram_products import context
from test_publication_preview import saved, no_network, preview, send, action
from test_own_offer_readback import inventory, editor, changed_field
from funpayflow import telegram as ui, telegram_products as view, runtime_control
from funpayflow.create_wire_contract import NewOfferTransport, NewFormFacts, assemble_new_request
from funpayflow.lot_publisher import FormContext, LotPublisher, PublisherError, TEXT_FIELDS
from funpayflow.product_store import ProductStore
from funpayflow.state import ReviewReceiptStore


@pytest.fixture
def dispatch(inventory, monkeypatch):
    c = inventory
    def fresh(transport, publisher, review):
        assert c.client._account_lock._is_owned()
        form = FormContext('1', 42, id(c.client.account), c.draft['node_schema'],
                           'USD', 'synthetic-v1')
        prepared = publisher.prepare(review.draft_id, review.revision, form,
                                     price=review.price, currency=review.currency)
        business = dict(prepared.business)
        options = tuple((name, value) for name, value, *_ in review.selections)
        controls = dict(business, offer_id='', node_id='986', csrf_token='PRIVATE-CSRF',
            auto_delivery='SYNTHETIC_MANUAL', location='trade', active='', deactivate_after_sale='')
        controls.update({k: '' for k in (*TEXT_FIELDS.values(), 'price')})
        facts = NewFormFacts('synthetic-new-v1', 'https://funpay.com/lots/offerEdit?node=986',
            'https://funpay.com/lots/offerSave', 'POST', 'application/x-www-form-urlencoded',
            prepared.fetched_at, 986, prepared.fingerprint, 'USD', 'SYNTHETIC_MANUAL',
            'PRIVATE-CSRF', tuple(controls.items()), options)
        return prepared, form, assemble_new_request(prepared, facts, now=c.now)
    c.fresh = Mock(side_effect=fresh)
    monkeypatch.setattr(NewOfferTransport, '_fresh_contract', lambda *args: c.fresh(*args))
    monkeypatch.setattr(NewOfferTransport, '_require_live_contract', lambda *args: None)
    c.after_post = lambda: None
    c.response = SimpleNamespace(status_code=200, history=[],
        url='https://funpay.com/lots/offerSave',
        iter_content=lambda size: [b'{"success":true,"id":999}'], close=Mock())
    def post(url, **kwargs):
        assert c.client._account_lock._is_owned()
        assert url == 'https://funpay.com/lots/offerSave'
        assert kwargs['allow_redirects'] is False
        assert c.get.call_count >= 3  # independent baseline precedes dispatch
        assert c.publisher.outcome(c.draft['id']) == {
            'state': 'UNKNOWN', 'reason': 'CLAIMED', 'remote_id': None}
        c.remote[72] = {}
        c.after_post()
        return c.response
    c.post = Mock(side_effect=post)
    monkeypatch.setattr(requests, 'post', c.post)
    return c


def persisted(c):
    receipt = ReviewReceiptStore(c.receipt.path)
    receipt.initialize()
    return LotPublisher(c.client, ProductStore(receipt, 1, 42)).outcome(c.draft['id'])


def test_telegram_final_full_summary_one_post_independent_id_permanent_claim(dispatch):
    c = dispatch
    # The ordinary GET inspection still runs. Both private live proof
    # boundaries are replaced only in this test's dispatch fixture.
    async def run():
        await preview(c)
        cb = await send(action('inspect'))
        assert 'Опубликовать 1 лот' in str(cb.message.edit_text.call_args)
        summary = cb.message.answer.call_args.args[0]
        for text in (*c.draft['payload'].values(), '274', '986', '42', 'USD'):
            assert str(text) in summary
        request = action('publish')
        c.publisher = view._flows[1]['publication']['publisher']
        cb = await send(request)
        assert 'ID 72' in cb.message.edit_text.call_args.args[0]  # ignores JSON id=999
        await send(request)
        assert c.post.call_count == 1
    asyncio.run(run())
    assert c.fresh.call_count == 2  # final dispatch reacquires transient form
    assert persisted(c) == {'state': 'CONFIRMED', 'reason': 'CONFIRMED', 'remote_id': 72}
    c.response.close.assert_called_once()


@pytest.mark.parametrize('failure', ['timeout', 'error_json', 'unknown_json', 'redirect',
    'wrong_currency', 'automatic', 'wrong_id', 'earlier_changed', 'incomplete'])
def test_ambiguous_result_permanent_unknown_no_retry(dispatch, failure):
    c = dispatch
    c.publisher.stage_new(c.review)
    if failure == 'timeout': c.post.side_effect = requests.Timeout('PRIVATE')
    elif failure == 'error_json':
        c.response.iter_content = lambda size: [b'{"success":true,"error":"PRIVATE"}']
    elif failure == 'unknown_json': c.response.iter_content = lambda size: [b'{}']
    elif failure == 'redirect': c.response.status_code = 302
    elif failure == 'wrong_currency': c.after_post = lambda: c.remote[72].update(currency='EUR')
    elif failure == 'automatic': c.after_post = lambda: c.remote[72].update(auto=True)
    elif failure == 'wrong_id': c.after_post = lambda: c.editors.update({72: editor(c, 999)})
    elif failure == 'earlier_changed':
        c.after_post = lambda: c.editors.update({71: changed_field(c, editor(c, 71),
            'fields[desc][ru]', 'changed')})
    elif failure == 'incomplete':
        c.after_post = lambda: setattr(c, 'trade_filter', lambda html: html + '<div class="pagination"></div>')
    assert c.publisher.confirm_new(c.review)['state'] == 'UNKNOWN'
    assert persisted(c)['state'] == 'UNKNOWN'
    with pytest.raises(PublisherError): c.publisher.confirm_new(c.review)
    with pytest.raises(PublisherError, match='ALREADY_CLAIMED'): c.publisher.stage_new(c.review)
    assert c.post.call_count == 1


@pytest.mark.parametrize('failure', ['stale', 'non_owner', 'safe_mode', 'restart', 'draft',
                                    'route', 'currency', 'manual', 'incomplete_baseline'])
def test_late_guard_zero_post(dispatch, failure):
    c = dispatch
    c.publisher.stage_new(c.review)
    if failure == 'stale': c.now += 301
    elif failure == 'non_owner': c.store.owner_id = '2'
    elif failure == 'safe_mode': c.publisher.late_check = lambda: False
    elif failure == 'restart': runtime_control.claim_restart()
    elif failure == 'draft': c.store.edit_draft(c.draft['id'], c.draft, c.draft['revision'])
    elif failure == 'route': c.store.save_route(c.route, c.route['revision'])
    elif failure in {'currency', 'manual'}:
        c.fresh.side_effect = PublisherError('PRICE' if failure == 'currency' else 'MANUAL_MODE')
    else: c.trade_filter = lambda html: html + '<div class="pagination"></div>'
    with pytest.raises(PublisherError): c.publisher.confirm_new(c.review)
    c.post.assert_not_called()
    assert c.publisher.outcome(c.draft['id']) is None


@pytest.mark.parametrize('failure', ['non_owner', 'safe_mode', 'lost_session'])
def test_telegram_late_owner_safe_mode_and_restart_reject_final_button(dispatch, monkeypatch, failure):
    c = dispatch
    async def run():
        await preview(c)
        await send(action('inspect'))
        request = action('publish')
        if failure == 'non_owner': monkeypatch.setattr(ui, '_owner_id', lambda _: False)
        elif failure == 'safe_mode': monkeypatch.setattr(ui, 'is_safe_mode_enabled', lambda: True)
        else: view.discard_session(1)
        await send(request)
    asyncio.run(run())
    c.post.assert_not_called()
    assert persisted(c) is None


def test_unpatched_runtime_profile_cannot_arm(inventory):
    c = inventory
    with pytest.raises(PublisherError, match='CONTRACT_UNVERIFIED'):
        c.publisher.stage_new(c.review)
    with pytest.raises(PublisherError): c.publisher.confirm_new(c.review)
    assert c.publisher.outcome(c.draft['id']) is None
    c.get.assert_not_called()


def test_gate_closes_after_durable_claim_without_dispatch(dispatch, monkeypatch):
    c = dispatch
    c.publisher.stage_new(c.review)
    claim = c.publisher._claim
    def close_gate(*args):
        claim(*args)
        c.publisher.late_check = lambda: False
    monkeypatch.setattr(c.publisher, '_claim', close_gate)
    assert c.publisher.confirm_new(c.review)['state'] == 'UNKNOWN'
    assert persisted(c)['state'] == 'UNKNOWN'
    c.post.assert_not_called()
