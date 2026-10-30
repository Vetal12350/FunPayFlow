"""Owner-facing publication fallback, real SQLite, synthetic HTML, no network."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import socket
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from FunPayAPI import Account

from test_telegram_products import context, callback, seed_route, database_snapshot
from funpayflow import node_form, runtime_control
from funpayflow import telegram as ui, telegram_products as view
from funpayflow.lot_publisher import LotPublisher, PublisherError, PUBLICATION_BLOCKERS
from funpayflow.node_products import NodeDraftService
from funpayflow.state import ReviewReceiptStore
from funpayflow.product_store import ProductStore


def new_html():
    fields = [('platform', 'Платформа', 'pc', 'PC'), ('region', 'Регион', 'eu', 'Европа'),
              ('edition', 'Издание', 'base', 'Elden Ring')]
    return ('<html><head></head><body data-app-data=\'{"userId":42}\'>'
        '<form class="form-offer-editor" action="/lots/offerSave" method="post">'
        '<input type="hidden" name="node_id" value="986">'
        '<input type="hidden" name="offer_id" value="">'
        '<input type="hidden" name="csrf_token" value="SYNTHETIC-PRIVATE-CANARY">'
        + ''.join(f'<div class="form-group"><label for="{name}">{label}</label>'
            f'<select id="{name}" name="fields[{name}]" required>'
            f'<option value="{value}" selected>{text}</option></select></div>'
            for name, label, value, text in fields)
        + '<input name="price"><input name="amount" value="1">'
        '<input type="checkbox" name="auto_delivery" value="on">'
        '<button type="submit">Save</button></form></body></html>')


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    denied = Mock(side_effect=AssertionError('External IO forbidden'))
    monkeypatch.setattr(requests.sessions.Session, 'request', denied)
    monkeypatch.setattr(requests, 'post', denied)
    original_connect = socket.socket.connect
    def local_asyncio_pipe(sock, address):
        # Windows asyncio builds its internal socketpair over loopback.
        if isinstance(address, tuple) and address[0] in {'127.0.0.1', '::1'}:
            return original_connect(sock, address)
        return denied(address)
    monkeypatch.setattr(socket.socket, 'connect', local_asyncio_pipe)
    runtime_control.begin_runtime_cycle()
    monkeypatch.setattr(ui, 'is_safe_mode_enabled', lambda: False)
    yield denied
    assert denied.call_count == 0
    runtime_control.begin_runtime_cycle()


@pytest.fixture
def saved(context, monkeypatch):
    c = context
    c.nodes[0].id = 986
    c.nodes[0].category.id = 274
    c.client.account.is_initiated = True
    c.html = new_html()
    c.client.account.method = Mock(return_value=SimpleNamespace(status_code=200,
        url='https://funpay.com/lots/offerEdit?node=986', content=c.html.encode()))
    c.route = seed_route(c)
    nd = NodeDraftService(c.store, c.routes)
    preview = nd.create_node_preview(c.template['id'], c.route['id'],
        {'fields[platform]': 'pc', 'fields[region]': 'eu', 'fields[edition]': 'base'}, price='15')
    c.draft = nd.confirm(nd.stage([preview])['token'])['results'][0]
    c.client.account.method.reset_mock()
    def get(account):
        assert account is c.client.account
        assert c.client._account_lock._is_owned()
        return c.html.encode()
    c.get = Mock(side_effect=get)
    monkeypatch.setattr(node_form, '_fresh_new_editor', c.get)
    yield c
    c.client.account.method.assert_not_called()
    c.client.account.save_lot.assert_not_called()
    c.client.account.get_user.assert_not_called()


async def send(action):
    cb = callback(action)
    await view.products_callback(cb, action, 1)
    return cb


def action(operation):
    return f"prd:pub:{view._flows[1]['publication']['token']}:{operation}"


async def preview(c, currency=True):
    cb = await send('prd:drafts:0')
    assert 'PREVIEW' in str(cb.message.edit_text.call_args)
    cb = await send(f"prd:pubopen:{c.draft['id']}:{c.draft['revision']}")
    assert 'выберите валюту' in cb.message.edit_text.call_args.args[0]
    assert 'inspect' not in str(cb.message.edit_text.call_args.kwargs['reply_markup'])
    if currency:
        cb = await send(action('USD'))
    return cb


def test_saved_preview_explicit_currency_fresh_inspection_no_false_success(saved, caplog):
    c = saved
    before = database_snapshot(c)
    async def run():
        cb = await preview(c)
        text = cb.message.edit_text.call_args.args[0]
        for expected in ('Elden Ring', '274', '986', 'PC', 'Европа', c.draft['payload']['price'] + ' USD', 'Количество: 1', 'ручная'):
            assert expected in text
        assert 'Live POST отключён' in text
        c.get.assert_not_called()
        cb = await send(action('ru'))
        assert c.draft['payload']['title_ru'] in cb.message.answer.call_args.args[0]
        request = action('inspect')
        cb = await send(request)
        text = cb.message.edit_text.call_args.args[0]
        assert 'Публикация заблокирована' in text
        assert 'POST не выполнялся' in text
        for key in ('NEW_SEMANTICS', 'MANUAL_MAPPING', 'CURRENCY_MAPPING', 'VISIBILITY_MAPPING',
                    'SUBMIT_CONTROLS', 'RESPONSE_ID', 'OWNER_READBACK'):
            assert PUBLICATION_BLOCKERS[key] in text
        assert c.get.call_count == 1
        await send(request)
        assert c.get.call_count == 1
    asyncio.run(run())
    assert database_snapshot(c) == before
    assert 'SYNTHETIC-PRIVATE-CANARY' not in caplog.text


@pytest.mark.parametrize('change', ['cancel', 'navigate', 'stale', 'revision', 'route', 'template',
                                  'account', 'instance', 'catalog', 'owner', 'safe_mode', 'restart', 'unauthorized'])
def test_stale_changed_cancelled_unauthorized_never_get_or_submit(saved, monkeypatch, change):
    c = saved
    async def run():
        await preview(c)
        old = action('inspect')
        if change == 'cancel': await send(action('cancel'))
        elif change == 'navigate': await send('prd:drafts:0')
        elif change == 'stale':
            state = view._flows[1]['publication']
            state['review'] = replace(state['review'], fetched_at=time.time() - 301)
        elif change == 'revision':
            c.store.edit_draft(c.draft['id'], deepcopy(c.draft), c.draft['revision'])
        elif change == 'route':
            c.store.save_route(dict(c.route, game_aliases=['changed']), c.route['revision'])
        elif change == 'template':
            c.templates.save(dict(c.template, display_name='Changed'), c.template['id'], c.template['revision'])
        elif change == 'account': c.client.account.id = 43
        elif change == 'instance': c.client.account = SimpleNamespace(**vars(c.client.account))
        elif change == 'catalog': c.nodes[0].category.id = 275
        elif change == 'owner': monkeypatch.setattr(ui, '_owner_id', lambda user: False)
        elif change == 'safe_mode': monkeypatch.setattr(ui, 'is_safe_mode_enabled', lambda: True)
        elif change == 'restart': runtime_control.claim_restart()
        if change == 'unauthorized':
            cb = callback(old); cb.from_user.id = 2
            await view.products_callback(cb, old, 1)
        else: await send(old)
        c.get.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['zero_identity', 'missing_identity', 'missing_csrf', 'submit_override',
                                  'unknown_control', 'duplicate_control', 'schema', 'currency', 'manual', 'submits'])
def test_form_failures_return_fixed_observations_never_ready(saved, change, caplog):
    c = saved
    if change == 'zero_identity': c.html = c.html.replace('name="offer_id" value=""', 'name="offer_id" value="0"')
    elif change == 'missing_identity': c.html = c.html.replace('<input type="hidden" name="offer_id" value="">', '')
    elif change == 'missing_csrf': c.html = c.html.replace('name="csrf_token"', 'name="unknown"')
    elif change == 'submit_override': c.html = c.html.replace('<button ', '<button formaction="/other" ')
    elif change == 'unknown_control': c.html = c.html.replace('</form>', '<input name="unknown"></form>')
    elif change == 'duplicate_control': c.html = c.html.replace('</form>', '<input name="price"></form>')
    elif change == 'schema': c.html = c.html.replace('>PC</option>', '>Other</option>')
    elif change == 'currency': c.html = c.html.replace('</form>', '<input type="hidden" name="currency" value="opaque"></form>')
    elif change == 'manual': c.html = c.html.replace('name="auto_delivery" value="on"', 'name="auto_delivery" value="opaque" checked')
    elif change == 'submits': c.html = c.html.replace('</form>', '<button type="submit">Other</button></form>')
    publisher = LotPublisher(c.client, c.store, late_check=lambda: True)
    review = publisher.review(c.draft['id'], c.draft['revision'], 'USD')
    proof, blockers = publisher.inspect_new(review)
    assert proof['publish_ready'] is False
    assert 'OWNER_READBACK' in blockers
    if change == 'schema': assert 'SELECTS' in blockers
    elif change == 'zero_identity': assert proof['blank_identity'] == 'ambiguous'
    elif change == 'missing_identity': assert proof['blank_identity'] == 'absent'
    elif change == 'missing_csrf': assert proof['csrf_presence'] == 'absent'
    elif change == 'submit_override': assert proof['submission'] != 'verified'
    elif change == 'manual': assert proof['manual_delivery'] == 'unverified'
    elif change == 'submits': assert 'SUBMIT_CONTROLS' in blockers
    else: assert proof['control_inventory'] == 'ambiguous'
    assert 'SYNTHETIC-PRIVATE-CANARY' not in json.dumps((proof, blockers)) + caplog.text


@pytest.mark.parametrize('change,reason', [('owner', 'APP_USER_SCOPE'), ('edit', 'OFFER_CONTEXT'),
    ('node', 'NODE_CONTEXT'), ('appdata', 'AUTH_CONTEXT_MISSING'), ('timeout', 'REQUEST_TRANSPORT')])
def test_invalid_form_or_transport_consumes_intent_without_leaking(saved, change, reason):
    c = saved
    if change == 'owner': c.html = c.html.replace('"userId":42', '"userId":43')
    elif change == 'edit': c.html = c.html.replace('name="offer_id" value=""', 'name="offer_id" value="71"')
    elif change == 'node': c.html = c.html.replace('name="node_id" value="986"', 'name="node_id" value="976"')
    elif change == 'appdata': c.html = c.html.replace(' data-app-data=\'{"userId":42}\'', '')
    else: c.get.side_effect = node_form.NodeFormError('REQUEST_TRANSPORT')
    async def run():
        await preview(c)
        old = action('inspect')
        cb = await send(old)
        assert reason in str(cb.message.answer.call_args)
        assert 'SYNTHETIC-PRIVATE-CANARY' not in str(cb.message.answer.call_args)
        await send(old)
        assert c.get.call_count == 1
    asyncio.run(run())


def test_unknown_guard_survives_restart_and_revision_without_get(saved):
    c = saved
    with c.receipt._connect() as db, db:
        db.execute("INSERT INTO lot_publish_attempts VALUES (?,?,?,?,'UNKNOWN','CLAIMED',NULL,?)",
                   ('1', 42, c.draft['id'], c.draft['revision'], time.time()))
    restarted = ReviewReceiptStore(c.receipt.path)
    restarted.initialize()
    c.client.review_state = restarted
    c.draft = c.store.edit_draft(c.draft['id'], deepcopy(c.draft), c.draft['revision'])
    async def run():
        cb = await send(f"prd:pubopen:{c.draft['id']}:{c.draft['revision']}")
        assert 'UNKNOWN' in str(cb.message.answer.call_args)
        assert 'Не очищайте lot_publish_attempts' in str(cb.message.answer.call_args)
        c.get.assert_not_called()
    asyncio.run(run())
    store = ProductStore(restarted, 1, 42)
    publisher = LotPublisher(c.client, store, late_check=lambda: True)
    assert publisher.outcome(c.draft['id'])['state'] == 'UNKNOWN'


def test_currency_token_rotation_and_simultaneous_double_click(saved):
    c = saved
    async def run():
        await preview(c, currency=False)
        old = action('USD')
        await send(old)
        token = view._flows[1]['publication']['token']
        await send(old)
        assert view._flows[1]['publication']['token'] == token
        intent = action('inspect')
        await asyncio.gather(send(intent), send(intent))
        assert c.get.call_count == 1
    asyncio.run(run())


@pytest.mark.parametrize('change', ['safe_mode', 'account', 'revision', 'navigation', 'claim'])
def test_late_changes_during_get_cannot_return_a_valid_result(saved, monkeypatch, change):
    c = saved
    def get(account):
        if change == 'safe_mode': monkeypatch.setattr(ui, 'is_safe_mode_enabled', lambda: True)
        elif change == 'account': c.client.account = SimpleNamespace(**vars(account))
        elif change == 'revision': c.store.edit_draft(c.draft['id'], deepcopy(c.draft), c.draft['revision'])
        elif change == 'navigation': view.discard_session(1)
        elif change == 'claim':
            with c.receipt._connect() as db, db:
                db.execute("INSERT INTO lot_publish_attempts VALUES (?,?,?,?,'UNKNOWN','CLAIMED',NULL,?)",
                           ('1', 42, c.draft['id'], c.draft['revision'], time.time()))
        return c.html.encode()
    c.get.side_effect = get
    async def run():
        await preview(c)
        cb = await send(action('inspect'))
        assert cb.message.edit_text.call_count == 0
        assert c.get.call_count == 1
    asyncio.run(run())


@pytest.mark.parametrize('change', ['quantity', 'selects', 'currency', 'variant', 'cancelled'])
def test_review_rejects_ineligible_saved_data(saved, change):
    c = saved
    currency = 'USD'
    draft = deepcopy(c.draft)
    if change == 'quantity': draft['payload']['amount'] = 2
    elif change == 'selects': draft['node_fields'].pop()
    elif change == 'currency': currency = 'UNKNOWN'
    elif change == 'variant': draft['variant_id'] = 'other'
    elif change == 'cancelled': draft['status'] = 'cancelled'
    if change != 'currency':
        with c.receipt._connect() as db, db:
            db.execute('UPDATE product_drafts SET body=? WHERE id=?', (json.dumps(draft), draft['id']))
    with pytest.raises(PublisherError):
        LotPublisher(c.client, c.store).review(c.draft['id'], c.draft['revision'], currency)
    c.get.assert_not_called()


def test_existing_authenticated_account_get_policy_and_no_cached_response(monkeypatch):
    account = Account('SYNTHETIC-AUTH', user_agent='SYNTHETIC-UA')
    account.id = 42
    account._Account__initiated = True
    account.phpsessid = 'SYNTHETIC-SESSION'
    response = SimpleNamespace(status_code=200, history=[],
        url='https://funpay.com/lots/offerEdit?node=986',
        iter_content=Mock(return_value=iter([b'fresh'])), close=Mock())
    get = Mock(return_value=response)
    monkeypatch.setattr(requests, 'get', get)
    assert node_form._fresh_new_editor(account) == b'fresh'
    assert get.call_count == 1
    args, kwargs = get.call_args
    assert args == ('https://funpay.com/lots/offerEdit?node=986',)
    assert kwargs['allow_redirects'] is False and kwargs['verify'] is True
    assert kwargs['stream'] is True and kwargs['timeout'] == (5, 10)
    assert kwargs['headers']['cookie'] == 'golden_key=SYNTHETIC-AUTH; PHPSESSID=SYNTHETIC-SESSION'
    response.close.assert_called_once()


def test_runtime_account_method_wrappers_and_extra_cookies_are_preserved(monkeypatch):
    from funpayflow import funpay
    response = SimpleNamespace(status_code=200, history=[],
        url='https://funpay.com/lots/offerEdit?node=986', cookies=SimpleNamespace(get_dict=lambda: {}),
        iter_content=Mock(return_value=iter([b'fresh'])), close=Mock())
    transport = Mock(return_value=response)
    monkeypatch.setattr(requests.sessions.Session, 'request', transport)
    monkeypatch.setattr(funpay, '_funpay_cookie_patch_applied', False)
    monkeypatch.setattr(funpay, '_funpay_extra_cookies', {'extra': 'SYNTHETIC-EXTRA'})
    client = funpay.FunPayClient('SYNTHETIC-AUTH')
    client.account._Account__initiated = True
    client.account.id = 42
    # FunPayClient installs action_gated_method; it is not a bound Account.method.
    wrapped = client.account.method
    assert getattr(wrapped, '__func__', None) is not Account.method
    with client._account_lock:
        assert node_form._fresh_new_editor(client.account) == b'fresh'
    assert client.account.method is wrapped
    assert transport.call_count == 1
    kwargs = transport.call_args.kwargs
    assert kwargs['allow_redirects'] is False
    assert 'extra=SYNTHETIC-EXTRA' in kwargs['headers']['cookie']
    response.close.assert_called_once()


@pytest.mark.parametrize('failure,reason', [('redirect', 'HTTP_REDIRECT'), ('history', 'HTTP_REDIRECT'),
    ('url', 'HTTP_REDIRECT'), ('timeout', 'REQUEST_TRANSPORT'), ('oversize', 'CONTENT_SIZE'),
    ('version', 'TRANSPORT_UNSUPPORTED')])
def test_fresh_transport_fails_once_closes_and_sanitizes(monkeypatch, failure, reason, caplog):
    account = Account('SYNTHETIC-SECRET-CANARY')
    account.id = 42
    account._Account__initiated = True
    response = SimpleNamespace(status_code=200, history=[],
        url='https://funpay.com/lots/offerEdit?node=986',
        iter_content=Mock(return_value=iter([b'body'])), close=Mock())
    get = Mock(return_value=response)
    monkeypatch.setattr(requests, 'get', get)
    if failure == 'redirect': response.status_code = 302
    elif failure == 'history': response.history = [object()]
    elif failure == 'url': response.url += '&offer=71'
    elif failure == 'timeout': get.side_effect = requests.Timeout('SYNTHETIC-SECRET-CANARY')
    elif failure == 'oversize': response.iter_content.return_value = iter([b'x' * (node_form._MAX_HTML + 1)])
    elif failure == 'version': monkeypatch.setattr(node_form, 'version', lambda name: '2.0')
    with pytest.raises(node_form.NodeFormError) as raised:
        node_form._fresh_new_editor(account)
    assert raised.value.reason == reason
    assert 'SYNTHETIC-SECRET-CANARY' not in str(raised.value) + caplog.text
    assert get.call_count == (0 if failure == 'version' else 1)
    if failure not in {'timeout', 'version'}: response.close.assert_called_once()
