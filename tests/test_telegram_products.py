"""Offline end-to-end product navigation with real temporary SQLite."""
import log_isolation
import asyncio
from copy import deepcopy
import json
from html import escape
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock, Mock

import pytest
from FunPayAPI.common.enums import SubCategoryTypes

from funpayflow import telegram as ui, telegram_products as view
from funpayflow.product_store import ProductStore
from funpayflow.game_routes import RouteService
from funpayflow.product_templates import TemplateService, default_template, BUILTIN_TYPES
from funpayflow.product_drafts import DraftService
from funpayflow.state import ReviewReceiptStore


@pytest.fixture
def context(tmp_path, monkeypatch):
    receipt = ReviewReceiptStore(tmp_path / 'state.sqlite3')
    receipt.initialize()
    names = ['Elden Ring', 'Helldivers 2', 'Cyberpunk 2077'] + [f'Game {i}' for i in range(3, 14)]
    nodes = [SimpleNamespace(id=100+i, name='Keys', type=SubCategoryTypes.COMMON,
                             category=SimpleNamespace(id=200+i, name=name)) for i, name in enumerate(names)]
    lots = [SimpleNamespace(id=11+i, subcategory=node) for i, node in enumerate(nodes)]
    account = SimpleNamespace(id=42, subcategories=nodes,
        get_subcategory=lambda kind, node_id: next((node for node in nodes if node.id == node_id and node.type is kind), None),
        get_user=Mock(return_value=SimpleNamespace(id=42, get_lots=lambda: lots)),
        save_lot=Mock(side_effect=AssertionError('No writes to FunPay')),
        method=Mock(side_effect=AssertionError('No generic HTTP')))
    def get_form(verb, endpoint, headers, payload, **kwargs):
        assert verb == 'get' and endpoint.startswith('lots/offerEdit?node=')
        node_id = int(endpoint.split('=')[1])
        options = {'platform': ['Steam', 'GOG', 'Epic'], 'region': ['Global', 'EU', 'US'],
                   'edition': ['Standard', 'Deluxe Edition', 'Standard Edition', 'Ultimate'], 'delivery': ['Digital key']}
        html = f'<form class="form-offer-editor"><input type="hidden" name="node_id" value="{node_id}"><input type="hidden" name="offer_id" value="0">'
        for name, values in options.items():
            html += f'<div class="form-group"><label for="{name}">{name}</label><select id="{name}" name="fields[{name}]">'
            html += ''.join(f'<option value="{escape(v,quote=True)}"'+(' selected' if i==0 else '')+'>'+escape(v)+'</option>' for i,v in enumerate(values))
            html += '</select></div>'
        html += '</form>'
        return SimpleNamespace(status_code=200, url='https://funpay.com/'+endpoint, content=html.encode())
    account.method = Mock(side_effect=get_form)
    client = SimpleNamespace(account=account, review_state=receipt, _account_lock=threading.RLock())
    monkeypatch.setattr(ui, '_runtime_client', client)
    monkeypatch.setattr(ui, '_owner_id', lambda user_id: user_id == 1)
    monkeypatch.setattr(ui, 'is_authorized', lambda user_id: user_id == 1)
    monkeypatch.setattr(ui, '_interaction_state', {})
    monkeypatch.setattr(view, '_flows', {})
    monkeypatch.setattr(view, '_locks', {})
    store = ProductStore(receipt, 1, 42)
    templates = TemplateService(store)
    template = templates.ensure_builtin()
    template = templates.save(dict(template, node_bindings={key: f'fields[{key}]' for key in ('platform','region','edition')}),template['id'],template['revision'])
    routes = RouteService(client, store)
    return SimpleNamespace(store=store, templates=templates, template=template, routes=routes,
                           drafts=DraftService(store, routes), client=client, nodes=nodes, receipt=receipt)


def callback(action='products'):
    return SimpleNamespace(data=action, from_user=SimpleNamespace(id=1), answer=AsyncMock(),
                           message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()))


def message(text):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=1), answer=AsyncMock())


def preview_action(action):
    return action + ':' + view._flows[1]['preview_token']


def seed_route(ctx, index=0):
    return ctx.store.save_route(ctx.routes.from_url(ctx.template['id'],
        f'https://funpay.com/lots/{ctx.nodes[index].id}/'))


async def route_form_button(ctx, route):
    cb = await pick(ctx, 'routes')
    await view.products_callback(cb, f"prd:route:{route['id']}", 1)
    token = view._flows[1]['route_form']['token']
    assert f'prd:routeform:{token}' in str(cb.message.edit_text.call_args)
    return cb, f'prd:routeform:{token}'


def database_snapshot(ctx):
    with ctx.receipt._connect() as db:
        return '\n'.join(db.iterdump())


def test_compact_menus_and_legacy_entries(context):
    ctx = context

    async def run():
        cb = callback()
        for action, expected in [
            ('products', [
                ('➕ Создать товар', 'prd:types'),
                ('📝 Мои черновики', 'prd:drafts:0'),
                ('⚙️ Дополнительно', 'prd:advanced'),
                ('🔙 Главное меню', 'menu_main'),
            ]),
            ('prd:advanced', [
                ('🗺 Категории и маршруты', 'prd:choose:routes:0'),
                ('🧩 Шаблоны оформления', 'prd:templates:0'),
                ('📚 Массовое создание черновиков', 'prd:choose:bulk:0'),
                ('➕ Расширенное создание', 'prd:choose:single:0'),
                ('📝 Из существующего лота', 'lot_templates'),
                ('🔙 Лоты', 'products'),
            ]),
        ]:
            await view.products_callback(cb, action, 1)
            shown = cb.message.edit_text.call_args
            rows = shown.kwargs['reply_markup'].inline_keyboard
            assert [[(b.text, b.callback_data) for b in row] for row in rows] == [
                [button] for button in expected]
            assert 'локальные черновики' in shown.args[0]
            assert 'публикация funpay пока отключена' in shown.args[0].lower()
            assert not any(word in shown.args[0] for word in ('Template', 'Route', 'variables'))
        for action, title in [('prd:drafts:0', 'drafts'),
                              ('prd:templates:0', 'Product Templates'),
                              ('prd:choose:single:0', 'Выберите Product Template'),
                              ('prd:choose:bulk:0', 'Выберите Product Template'),
                              ('prd:choose:routes:0', 'Выберите Product Template')]:
            await view.products_callback(cb, action, 1)
            assert title in cb.message.edit_text.call_args.args[0]
        assert not await view.products_callback(cb, 'lot_templates', 1)  # Existing lots handler owns it.
        await view.products_callback(cb, 'prd:types', 1)
        rows = cb.message.edit_text.call_args.kwargs['reply_markup'].inline_keyboard
        assert len(rows) == 8 and rows[0][0].callback_data.endswith(':GAME_KEY')
        ctx.client.account.method.assert_not_called()
        ctx.client.account.get_user.assert_not_called()
        ctx.client.account.save_lot.assert_not_called()
        assert ctx.store.list_drafts() == []

    asyncio.run(run())


@pytest.mark.parametrize('navigation', ['prd:advanced', 'products', 'prd:home'])
def test_menu_navigation_abandons_route_form_and_type_tokens(context, navigation):
    ctx = context
    route = seed_route(ctx)

    async def run():
        cb, action = await route_form_button(ctx, route)
        before = database_snapshot(ctx)
        await view.products_callback(cb, navigation, 1)
        assert 1 not in view._flows and 1 not in ui._interaction_state
        await view.products_callback(cb, action, 1)
        assert 'устарел' in str(cb.message.answer.call_args)
        await view.products_callback(cb, 'prd:types', 1)
        preset = f"prd:preset:{view._flows[1]['type_token']}:GAME_KEY"
        await view.products_callback(cb, navigation, 1)
        await view.products_callback(cb, preset, 1)
        assert 1 not in view._flows
        assert database_snapshot(ctx) == before
        ctx.client.account.method.assert_not_called()
        ctx.client.account.save_lot.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize('bulk', [False, True])
def test_advanced_abandons_draft_confirmation(context, bulk):
    ctx = context
    route = seed_route(ctx)

    async def run():
        if bulk:
            cb = await pick(ctx, 'bulk')
            preview = ctx.drafts.preview_bulk(ctx.template['id'], 'Elden Ring | 10', {})
            batch = ctx.drafts.stage(preview['previews'])
            view._flows[1]['batch_token'] = batch['token']
            action = 'prd:confirm:' + batch['token']
        else:
            cb = await fill_variables(ctx, route)
            await view.products_input(message('10'), 1)
            action = preview_action('prd:save')
        before = database_snapshot(ctx)
        ctx.client.account.method.reset_mock()
        await view.products_callback(cb, 'prd:advanced', 1)
        assert 1 not in view._flows and 1 not in ui._interaction_state
        assert not await view.products_input(message('20'), 1)
        await view.products_callback(cb, 'products', 1)
        cb.message.answer.reset_mock()
        await view.products_callback(cb, action, 1)
        cb.message.answer.assert_called_once()
        assert 'Ничего не опубликовано' in cb.message.answer.call_args.args[0]
        assert database_snapshot(ctx) == before and ctx.store.list_drafts() == []
        ctx.client.account.method.assert_not_called()
        ctx.client.account.save_lot.assert_not_called()

    asyncio.run(run())


def test_advanced_rejects_nonowner_without_discarding_owner_input(context):
    async def run():
        cb = await pick(context, 'bulk')
        flow = view._flows[1]
        pending = ui._interaction_state[1]
        for caller, target in [(2, 2), (2, 1)]:
            cb.from_user.id = caller
            await view.products_callback(cb, 'prd:advanced', target)
            assert cb.answer.call_args.kwargs.get('show_alert')
            assert view._flows[1] is flow and ui._interaction_state[1] is pending
            assert 2 not in view._flows
        context.client.account.method.assert_not_called()
        context.client.account.save_lot.assert_not_called()

    asyncio.run(run())


def test_route_form_explicit_get_sanitized_metadata_and_no_mutations(context):
    ctx = context
    route = seed_route(ctx, 1)
    original_account = ctx.client.account
    fake_get = original_account.method.side_effect
    canary = 'SYNTHETIC_PRIVATE_CANARY'
    def response(*args, **kwargs):
        assert ctx.client._account_lock._is_owned()
        result = fake_get(*args, **kwargs)
        result.content = result.content.replace(b'Global', canary.encode())
        result.content = result.content.replace(b'</form>',
            f'<input type="hidden" name="csrf_token" value="{canary}"></form>'.encode())
        return result
    original_account.method.side_effect = response
    before = database_snapshot(ctx)

    async def run():
        cb, action = await route_form_button(ctx, route)
        original_account.method.assert_not_called()
        assert '900' in str(cb.message.edit_text.call_args)
        await asyncio.gather(*(view.products_callback(cb, action, 1) for _ in range(2)))
        original_account.method.assert_called_once_with('get', 'lots/offerEdit?node=101',
            {'accept-language': 'ru'}, {}, raise_not_200=True)
        shown = str(cb.message.answer.call_args_list)
        assert 'Каталог 201 · node 101' in shown and 'доступна' in shown
        assert 'SELECT-полей: 4 (метаданные)' in shown
        assert 'публикация НЕ проверена' in shown
        assert 'редиректам' in shown and '900' in shown
        for forbidden in (canary, 'fields[', 'Steam', 'csrf_token', 'https://', 'Traceback'):
            assert forbidden not in shown
        assert 'route_form' not in view._flows[1]
        assert ctx.client.account is original_account
        original_account.save_lot.assert_not_called()
        original_account.get_user.assert_not_called()
        assert database_snapshot(ctx) == before
        await view.products_callback(cb, f"prd:route:{route['id']}", 1)
        assert 'route_form' in view._flows[1]

    asyncio.run(run())


@pytest.mark.parametrize('failure,reason', [
    ('login', 'URL_AUTH'), ('auth', 'HTTP_AUTH'), ('html', 'FIELD_NAME_UNSUPPORTED'),
    ('exception', 'REQUEST_FAILED'),
])
def test_route_form_safe_errors_no_persistence(context, failure, reason):
    ctx = context
    route = seed_route(ctx)
    canary = 'SYNTHETIC_PRIVATE_CANARY'
    fake_get = ctx.client.account.method.side_effect
    def response(*args, **kwargs):
        result = fake_get(*args, **kwargs)
        if failure == 'login':
            result.url = 'https://funpay.com/account/login?private=' + canary
        elif failure == 'auth':
            result.status_code = 403
            result.content = canary.encode()
        elif failure == 'html':
            result.content = result.content.replace(b'fields[platform]', b'unsupported[platform]')
        else:
            raise RuntimeError(canary)
        return result
    ctx.client.account.method.side_effect = response
    before = database_snapshot(ctx)

    async def run():
        cb, action = await route_form_button(ctx, route)
        await view.products_callback(cb, action, 1)
        shown = str(cb.message.answer.call_args)
        assert 'не удалось прочитать' in shown and reason in shown
        assert 'публикация НЕ проверена' in shown
        for forbidden in (canary, 'fields[', 'csrf_token', 'https://', 'RuntimeError', 'SELECT-полей:'):
            assert forbidden not in shown
        assert database_snapshot(ctx) == before
        assert 'route_form' not in view._flows[1]
        ctx.client.account.save_lot.assert_not_called()
        ctx.client.account.method.assert_called_once()
    asyncio.run(run())


@pytest.mark.parametrize('change', [
    'forged', 'account_id', 'account_object', 'client', 'template', 'template_revision',
    'route', 'route_revision', 'removed_node', 'navigation', 'cancel',
])
def test_route_form_rejects_changed_or_stale_context(context, monkeypatch, change):
    ctx = context
    route = seed_route(ctx)
    other = seed_route(ctx, 1)
    account = ctx.client.account

    async def run():
        cb, action = await route_form_button(ctx, route)
        flow = view._flows[1]
        if change == 'forged':
            action += ':100'
        elif change == 'account_id':
            account.id = 43
        elif change == 'account_object':
            ctx.client.account = SimpleNamespace(**vars(account))
        elif change == 'client':
            monkeypatch.setattr(ui, '_runtime_client', SimpleNamespace(**vars(ctx.client)))
        elif change == 'template':
            flow['template_id'] = 'DLC_KEY'
        elif change == 'template_revision':
            ctx.templates.save(dict(ctx.template, display_name='Changed'), ctx.template['id'], ctx.template['revision'])
        elif change == 'route':
            flow['route_id'] = other['id']
        elif change == 'route_revision':
            ctx.store.save_route(dict(route, game_aliases=['Changed']), expected_revision=route['revision'])
        elif change == 'removed_node':
            ctx.nodes.pop(0)  # No route enabled flag exists: unavailable catalog nodes fail closed.
        elif change == 'navigation':
            await view.products_callback(cb, 'prd:routepage:0', 1)
        else:
            view.discard_session(1)
        before = database_snapshot(ctx)
        await view.products_callback(cb, action, 1)
        account.method.assert_not_called()
        assert database_snapshot(ctx) == before
        assert 'устарел' in str(cb.message.answer.call_args)
    asyncio.run(run())


def test_route_form_other_user_and_old_button_preserve_active_wizard(context):
    ctx = context
    route = seed_route(ctx)

    async def run():
        cb, action = await route_form_button(ctx, route)
        stranger = callback(action)
        stranger.from_user.id = 2
        await view.products_callback(stranger, action, 2)
        await view.products_callback(stranger, action, 1)  # Mismatched caller identity.
        ctx.client.account.method.assert_not_called()
        assert view._flows[1].get('route_form')
        await fill_variables(ctx, route)
        flow = deepcopy(view._flows[1])
        pending = ui._interaction_state[1]
        calls = ctx.client.account.method.call_count
        cb.data = action
        await ui.callback_handler(cb)
        assert view._flows[1] == flow and ui._interaction_state[1] is pending
        assert ctx.client.account.method.call_count == calls
    asyncio.run(run())


def test_route_form_revalidates_account_after_waiting_for_shared_lock(context):
    ctx = context
    route = seed_route(ctx)

    async def run():
        cb, action = await route_form_button(ctx, route)
        # The event-loop thread holds the RLock; only the worker can wait for it.
        with ctx.client._account_lock:
            task = asyncio.create_task(view.products_callback(cb, action, 1))
            await asyncio.sleep(0.01)
            assert not task.done()
            ctx.client.account.id = 43
        await task
        ctx.client.account.method.assert_not_called()
        assert 'устарел' in str(cb.message.answer.call_args)
    asyncio.run(run())


def test_route_form_cache_reuse_and_expiry(context, monkeypatch):
    from funpayflow import node_form
    ctx = context
    route = seed_route(ctx)
    now = [10.0]
    monkeypatch.setattr(node_form.time, 'monotonic', lambda: now[0])

    async def run():
        cb, action = await route_form_button(ctx, route)
        await view.products_callback(cb, action, 1)
        now[0] += 899
        cb, action = await route_form_button(ctx, route)
        await view.products_callback(cb, action, 1)
        assert ctx.client.account.method.call_count == 1
        now[0] += 2
        cb, action = await route_form_button(ctx, route)
        ctx.client.account.method.side_effect = RuntimeError('Synthetic failure')
        await view.products_callback(cb, action, 1)
        assert ctx.client.account.method.call_count == 2
        assert 'не удалось прочитать' in str(cb.message.answer.call_args)
    asyncio.run(run())


@pytest.mark.parametrize('kind', [kind for kind, _ in BUILTIN_TYPES if kind != 'GAME_KEY'])
def test_route_form_not_offered_for_other_product_types(context, kind):
    ctx = context
    ctx.template = ctx.templates.ensure_builtin(kind)
    route = seed_route(ctx)

    async def run():
        cb = await pick(ctx, 'routes')
        await view.products_callback(cb, f"prd:route:{route['id']}", 1)
        assert 'routeform' not in str(cb.message.edit_text.call_args)
        assert not view._flows[1].get('route_form')
        ctx.client.account.method.assert_not_called()
    asyncio.run(run())


async def pick(ctx, purpose='single'):
    cb = callback()
    assert await view.products_callback(cb, 'products', 1)
    await view.products_callback(cb, f'prd:choose:{purpose}:0', 1)
    await view.products_callback(cb, f"prd:pick:{purpose}:{ctx.template['id']}", 1)
    return cb


async def fill_variables(ctx, route=None, bulk=False, quick=False):
    if quick:
        cb = callback()
        await view.products_callback(cb, 'prd:quick', 1)
    else:
        cb = await pick(ctx, 'bulk' if bulk else 'single')
    if not bulk:
        await view.products_callback(cb, f"prd:route:{route['id']}", 1)
    for value in ['Steam', 'Global', 'Standard', '-', '2']:
        assert await view.products_input(message(value), 1)
    return cb


def test_search_alias_pagination_recent_and_catalog(context):
    ctx = context
    routes = [seed_route(ctx, i) for i in range(14)]
    first = routes[0]; first['game_aliases'] = ['ELDEN_ALIAS']
    ctx.store.save_route(first, expected_revision=first['revision'])

    async def run():
        cb = await pick(ctx, 'routes')
        assert 'prd:routepage:8' in str(cb.message.edit_text.call_args)
        await view.products_callback(cb, 'prd:routepage:8', 1)
        assert 'prd:routepage:0' in str(cb.message.edit_text.call_args)
        await view.products_callback(cb, 'prd:search', 1)
        await view.products_input(message('elden_alias'), 1)
        await view.products_callback(cb, 'prd:routepage:0', 1)
        assert 'Elden Ring' in str(cb.message.edit_text.call_args)
        assert 'Helldivers' not in str(cb.message.edit_text.call_args)
        await view.products_callback(cb, 'prd:recent:0', 1)
        assert 'Недавние' in str(cb.message.edit_text.call_args)
        await view.products_callback(cb, 'prd:catalogsearch', 1)
        await view.products_input(message('HELldivers'), 1)
        await view.products_callback(cb, 'prd:catalog:0', 1)
        assert 'Helldivers' in str(cb.message.edit_text.call_args)
        ctx.client.account.get_user.assert_not_called()

    asyncio.run(run())


def test_own_lot_route_preview_confirm_double_click_and_no_source_id(context):
    ctx = context

    async def run():
        cb = await pick(ctx, 'routes')
        await view.products_callback(cb, 'prd:own:0', 1)
        await view.products_callback(cb, 'prd:source:11', 1)
        assert ctx.store.list_routes() == []
        token = view._flows[1]['batch_token']
        await asyncio.gather(*(view.products_callback(callback(), f'prd:confirm:{token}', 1) for _ in range(2)))
        rows = ctx.store.list_routes()
        assert len(rows) == 1
        assert rows[0]['node_id'] == 100
        assert not {'source_id', 'offer_id', 'csrf_token', 'form_created_at'} & set(rows[0])
        ctx.client.account.save_lot.assert_not_called()
        assert all(call.args[0] == 'get' and call.args[1].startswith('lots/offerEdit?node=') for call in ctx.client.account.method.call_args_list)

    asyncio.run(run())


def test_route_bulk_partial_success_preview_and_cancel(context):
    ctx = context

    async def run():
        cb = await pick(ctx, 'routes')
        await view.products_callback(cb, 'prd:routeimport', 1)
        source = 'Elden Ring | https://funpay.com/lots/100/\nUnknown\nHelldivers 2 | https://funpay.com/lots/101/'
        text = message(source)
        await view.products_input(text, 1)
        assert 'ready: 2' in str(text.answer.call_args)
        assert 'error: 1' in str(text.answer.call_args)
        token = view._flows[1]['batch_token']
        assert ctx.store.list_routes() == []
        await view.products_callback(cb, f'prd:cancel:{token}', 1)
        await view.products_callback(cb, f'prd:confirm:{token}', 1)
        assert ctx.store.list_routes() == []
        await view.products_callback(cb, 'prd:routeimport', 1)
        await view.products_input(message(source), 1)
        token = view._flows[1]['batch_token']
        await view.products_callback(cb, f'prd:confirm:{token}', 1)
        assert len(ctx.store.list_routes()) == 2

    asyncio.run(run())


@pytest.mark.parametrize('quick', [False, True])
def test_single_draft_wizard_edit_save_duplicate_and_restart(context, quick):
    ctx = context
    route = seed_route(ctx)

    async def run():
        cb = await fill_variables(ctx, route, quick=quick)
        invalid = message('NaN')
        await view.products_input(invalid, 1)
        assert ui._interaction_state[1]['flow']['field'] == 'price'
        await view.products_input(message('29.99'), 1)
        assert ctx.store.list_drafts() == []
        payload = view._flows[1]['preview']['payload']
        assert payload['price'] == '29.99' and payload['amount'] == 2
        for lang in ('ru', 'en'):
            assert 'Elden Ring' in payload['title_' + lang]
            assert payload['description_' + lang]
        assert 'RU + EN' in str(view._preview_screen(view._flows[1]))
        await view.products_callback(cb, preview_action('prd:edit:emoji'), 1)
        await view.products_input(message('⚔️'), 1)
        await view.products_callback(cb, preview_action('prd:edit:en'), 1)
        await view.products_input(message('English key\nEnglish description'), 1)
        await asyncio.gather(*(view.products_callback(callback(), preview_action('prd:save'), 1) for _ in range(2)))
        rows = ctx.store.list_drafts()
        assert len(rows) == 1
        saved = rows[0]
        reopened = ProductStore(ReviewReceiptStore(ctx.receipt.path), 1, 42)
        assert reopened.get_draft(saved['id']) == saved
        assert ProductStore(ctx.receipt, 2, 42).list_drafts() == []
        assert ProductStore(ctx.receipt, 1, 43).list_drafts() == []
        assert saved['payload']['title_en'].startswith('⚔️')
        assert saved['payload']['description_en'] == 'English description'
        assert saved['payload']['amount'] == 2
        view.discard_session(1); ui._interaction_state.clear()
        await view.products_callback(cb, 'prd:save', 1)  # Stale button after restart.
        assert len(ctx.store.list_drafts()) == 1
        await view.products_callback(cb, f"prd:open:{saved['id']}", 1)
        assert view._flows[1]['preview']['payload']['price'] == '29.99'
        await view.products_callback(cb, preview_action('prd:edit:price'), 1)
        await view.products_input(message('31.99'), 1)
        await view.products_callback(cb, preview_action('prd:save'), 1)
        assert ctx.store.get_draft(saved['id'])['payload']['price'] == '31.99'
        await view.products_callback(cb, f"prd:open:{saved['id']}", 1)
        await view.products_callback(cb, preview_action('prd:discard'), 1)
        assert ctx.store.get_draft(saved['id'])['status'] == 'cancelled'
        ctx.client.account.save_lot.assert_not_called()
        assert all(call.args[0] == 'get' and call.args[1].startswith('lots/offerEdit?node=') for call in ctx.client.account.method.call_args_list)

    asyncio.run(run())


def test_quick_entry_saved_routes_custom_template_and_cancel(context):
    ctx = context
    routes = [seed_route(ctx, i) for i in range(2)]
    customized = ctx.templates.save(dict(ctx.template, display_name='My keys', default_emoji='🎮'),
                                    ctx.template['id'], ctx.template['revision'])

    async def run():
        cb = callback()
        await view.products_callback(cb, 'products', 1)
        assert 'prd:quick' not in str(cb.message.edit_text.call_args)
        await view.products_callback(cb, 'prd:quick', 1)
        shown = str(cb.message.edit_text.call_args)
        assert all(route['game_name'] in shown for route in routes)
        assert 'Выберите Product Template' not in shown
        assert view._flows[1]['template_id'] == customized['id']
        assert ctx.templates.get(customized['id']) == customized
        ctx.client.account.method.assert_not_called()
        await view.products_callback(cb, f"prd:route:{routes[0]['id']}", 1)
        flow = view._flows[1]
        assert flow['step'] == 'nodefields' and not flow.get('preview')
        token = flow['node_state']['token']
        pending = ui._interaction_state[1]
        await view.products_callback(callback(), 'prd:quick', 2)
        assert view._flows[1] is flow and ui._interaction_state[1] is pending
        assert 2 not in view._flows
        await view.products_callback(cb, f'prd:nf:{token}:choose:1', 1)
        current = flow['node_state']['token']
        await view.products_callback(cb, f'prd:nf:{token}:choose:1', 1)
        assert flow['node_state']['token'] == current
        await view.products_callback(cb, f'prd:nf:{current}:cancel:0', 1)
        assert 1 not in view._flows and ctx.store.list_drafts() == []
        assert len(ctx.store.list_routes()) == 2
        ctx.client.account.save_lot.assert_not_called()

    asyncio.run(run())


def test_quick_without_routes_uses_confirmed_catalog_and_cancel(context):
    ctx = context

    async def run():
        cb = callback()
        await view.products_callback(cb, 'prd:quick', 1)
        flow = view._flows[1]
        assert flow['purpose'] == 'single' and 'catalog_state' in flow
        assert ctx.store.list_routes() == []
        state = flow['catalog_state']
        await view.products_callback(cb, f"prd:rt:{state['token']}:game:200", 1)
        state = flow['catalog_state']
        assert state['view'] == 'confirm'
        assert ctx.store.list_routes() == []
        old_confirm = f"prd:rt:{state['token']}:confirm:0"
        await view.products_callback(cb, f"prd:rt:{state['token']}:cancel:0", 1)
        await view.products_callback(cb, old_confirm, 1)
        assert ctx.store.list_routes() == [] and ctx.store.list_drafts() == []
        ctx.client.account.method.assert_not_called()
        ctx.client.account.save_lot.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize('kind,label', BUILTIN_TYPES)
def test_type_selector_confirmed_catalog_node_fields_preview_save_restart(context, kind, label):
    from funpayflow.product_variants import VariantService
    ctx = context
    # Synthetic catalogue labels deliberately differ from the item sold.
    ctx.nodes[0].category.name = 'A Service catalogue category'
    ctx.nodes[0].name = 'Owner-selected section'

    async def run():
        cb = callback()
        await view.products_callback(cb, 'prd:types', 1)
        screen = str(cb.message.edit_text.call_args)
        assert [screen.index(label) for _, label in BUILTIN_TYPES] == sorted(
            screen.index(label) for _, label in BUILTIN_TYPES)
        markup = cb.message.edit_text.call_args.kwargs['reply_markup']
        assert len(markup.inline_keyboard) == 8  # Seven readable choices plus Back.
        assert all(len(button.callback_data.encode()) <= 64
            for row in markup.inline_keyboard for button in row)
        action = f"prd:preset:{view._flows[1]['type_token']}:{kind}"
        await asyncio.gather(*(view.products_callback(callback(), action, 1) for _ in range(2)))
        flow = view._flows[1]
        assert flow['template_id'] == kind and flow['purpose'] == 'single'
        state = flow['catalog_state']
        await view.products_callback(cb, f"prd:rt:{state['token']}:game:200", 1)
        state = flow['catalog_state']
        assert state['view'] == 'confirm' and ctx.store.list_routes() == []
        assert 'legacy game_name' in str(cb.message.edit_text.call_args)
        await view.products_callback(cb, f"prd:rt:{state['token']}:confirm:0", 1)
        route = ctx.store.list_routes()[0]
        assert route['node_id'] == 100 and route['template_id'] == kind
        assert flow['step'] == 'nodefields'
        # Re-enter via saved route, including a real Variant and technical field value.
        variant = VariantService(ctx.store, ctx.routes).save({
            'template_id': kind, 'route_id': route['id'], 'display_name': 'Owner variant',
            'node_values': {'fields[region]': 'EU'}, 'overrides': {'emoji': '⭐'}})
        await view.products_callback(cb, 'prd:types', 1)
        await view.products_callback(cb, f"prd:preset:{view._flows[1]['type_token']}:{kind}", 1)
        await view.products_callback(cb, f"prd:route:{route['id']}", 1)
        flow = view._flows[1]
        await view.products_callback(cb, f"prd:vp:{flow['variant_picker']['token']}:pick:{variant['id']}", 1)
        while flow.get('step') == 'nodefields':
            await view.products_input(message('-'), 1)
        values = dict(quantity='2', sale_name='Actual product', sale_platform='Owner platform',
            availability='не указано / unspecified', delivery_method='Разрешённое приглашение / Permitted invitation',
            ps_platform='PS5', sale_edition='Owner edition', product_form='Код / Code',
            plan='Owner plan', duration='30 дней / 30 days',
            base_game='Required base game', currency_name='Crystals', denomination='500 crystals',
            gaming_product_type='Подарочная карта / Gift card', term_or_value='25 EUR',
            authorized_delivery='Подтверждаю разрешение провайдера / I confirm provider permission', price='12.34')
        for _ in range(15):
            if flow.get('preview'):
                break
            field = flow['field']
            if kind != 'GAME_KEY' and field == 'sale_name':
                await view.products_input(message('-'), 1)
                assert flow['field'] == 'sale_name'  # No default product inferred from category.
            await view.products_input(message(values[field]), 1)
        preview = flow['preview']
        assert preview['variant_id'] == variant['id']
        assert next(row for row in preview['node_fields'] if row['html_name'] == 'fields[region]')['value'] == 'EU'
        assert preview['payload']['price'] == '12.34'
        for lang in ('ru', 'en'):
            title = preview['payload']['title_' + lang]
            description = preview['payload']['description_' + lang]
            assert title.startswith('⭐')
            if kind != 'GAME_KEY':
                assert 'Actual product' in title and 'Service catalogue category' not in title + description
                assert values['delivery_method'] in description
            if kind == 'AI_SUBSCRIPTION':
                assert 'Owner plan' in title + description and '30 days' in title + description
            elif kind == 'PLAYSTATION_GAME':
                assert 'PS5' in title and 'Code' in description
            elif kind == 'GIFT':
                assert ('гифт' if lang == 'ru' else 'gift') in title
            elif kind == 'DLC_CONTENT':
                assert 'Required base game' in description and 'DLC' in title
            elif kind == 'GAME_CURRENCY':
                assert 'Crystals' in title and '500 crystals' in description
            elif kind == 'GAMING_SUBSCRIPTION':
                assert 'Gift card' in title and '25 EUR' in description
        save = preview_action('prd:save')
        await view.products_callback(cb, save, 1)
        saved = ctx.store.list_drafts()[0]
        view.discard_session(1); ui._interaction_state.clear()
        reopened = ProductStore(ReviewReceiptStore(ctx.receipt.path), 1, 42)
        assert reopened.get_draft(saved['id'])['payload'] == preview['payload']
        await view.products_callback(cb, action, 1)  # Pre-restart type button.
        assert 1 not in view._flows
        await view.products_callback(cb, f"prd:open:{saved['id']}", 1)
        assert view._flows[1]['preview']['payload'] == saved['payload']
        template = ctx.templates.get(kind)
        customized = ctx.templates.save(dict(template, name='Owner ' + kind,
            display_name='Owner preset', default_emoji='🧩', description_en='Owner text'),
            kind, template['revision'])
        await view.products_callback(cb, 'prd:types', 1)
        await view.products_callback(cb, f"prd:preset:{view._flows[1]['type_token']}:{kind}", 1)
        assert view._flows[1]['template_id'] == customized['id']
        assert ctx.templates.get(kind) == customized
        assert ctx.store.get_draft(saved['id']) == saved
        ctx.client.account.save_lot.assert_not_called()
        assert all(call.args[0] == 'get' for call in ctx.client.account.method.call_args_list)

    asyncio.run(run())


@pytest.mark.parametrize('kind', ['GIFT', 'DLC_CONTENT', 'GAME_CURRENCY', 'GAMING_SUBSCRIPTION'])
def test_type_selector_invalid_stale_nonowner_and_scope_do_not_change_input(context, kind):
    ctx = context

    async def run():
        cb = callback()
        await view.products_callback(cb, 'prd:types', 1)
        old = view._flows[1]['type_token']
        await view.products_callback(cb, 'prd:types', 1)
        flow = view._flows[1]
        token = flow['type_token']
        ui._interaction_state[1] = pending = {'sentinel': True}
        for action in (f'prd:preset:{old}:{kind}', f'prd:preset:{token}:arbitrary',
                       f'prd:preset:{token}:{kind}:extra'):
            await view.products_callback(cb, action, 1)
            assert view._flows[1] is flow and ui._interaction_state[1] is pending
        await view.products_callback(callback(), f'prd:preset:{token}:{kind}', 2)
        assert 2 not in view._flows
        ctx.client.account.id = 43
        await view.products_callback(cb, f'prd:preset:{token}:{kind}', 1)
        assert view._flows[1] is flow and ui._interaction_state[1] is pending
        assert ProductStore(ctx.receipt, 1, 43).list_templates() == []
        ctx.client.account.id = 42
        assert [row['id'] for row in ctx.templates.list()] == ['GAME_KEY']
        assert ctx.store.list_routes() == [] and ctx.store.list_drafts() == []
        ctx.client.account.method.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['AI_SUBSCRIPTION', 'DLC_CONTENT', 'GAME_CURRENCY', 'GAMING_SUBSCRIPTION'])
def test_type_selector_leaving_screen_and_missing_safe_category(context, kind):
    ctx = context

    async def run():
        cb = callback()
        await view.products_callback(cb, 'prd:types', 1)
        old = f"prd:preset:{view._flows[1]['type_token']}:GIFT"
        await view.products_callback(cb, 'prd:templates:0', 1)
        await view.products_callback(cb, old, 1)
        assert ctx.store.get_template('GIFT') is None
        ctx.client.account.subcategories = []
        await view.products_callback(cb, 'prd:types', 1)
        await view.products_callback(cb, f"prd:preset:{view._flows[1]['type_token']}:{kind}", 1)
        assert 'Без подходящей подтверждённой COMMON-категории' in str(cb.message.edit_text.call_args)
        flow = view._flows[1]
        assert not flow.get('preview') and not flow.get('route_id')
        await view.products_callback(cb, 'prd:save:invented', 1)
        assert ctx.store.list_drafts() == [] and ctx.store.list_routes() == []
        state = flow['catalog_state']
        await view.products_callback(cb, f"prd:rt:{state['token']}:cancel:0", 1)
        assert 1 not in view._flows
        ctx.client.account.method.assert_not_called()
        ctx.client.account.save_lot.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['DLC_CONTENT', 'GAME_CURRENCY', 'GAMING_SUBSCRIPTION'])
@pytest.mark.parametrize('unsafe_form', [False, True])
def test_second_wave_cancel_or_unsafe_form_never_saves_draft(context, kind, unsafe_form):
    ctx = context
    if unsafe_form:
        ctx.client.account.method = Mock(return_value=SimpleNamespace(status_code=200,
            url='https://funpay.com/lots/offerEdit?node=100', content=b'<html>No create form</html>'))

    async def run():
        cb = callback()
        await view.products_callback(cb, 'prd:types', 1)
        await view.products_callback(cb, f"prd:preset:{view._flows[1]['type_token']}:{kind}", 1)
        flow = view._flows[1]
        state = flow['catalog_state']
        await view.products_callback(cb, f"prd:rt:{state['token']}:game:200", 1)
        state = flow['catalog_state']
        await view.products_callback(cb, f"prd:rt:{state['token']}:confirm:0", 1)
        assert len(ctx.store.list_routes()) == 1  # Explicitly confirmed route survives cancellation.
        assert not flow.get('preview')
        if unsafe_form:
            assert flow['node_state']['phase'] == 'error'
            assert not flow.get('node_ready')
        else:
            while flow.get('step') == 'nodefields':
                await view.products_input(message('-'), 1)
            assert flow['field'] == 'quantity'
            await view.products_input(message('-'), 1)
            # Missing offer details cannot be replaced with catalogue labels/defaults.
            assert flow['field'] == 'sale_name'
            for value in ('-', '   '):
                await view.products_input(message(value), 1)
                assert flow['field'] == 'sale_name' and not flow.get('preview')
        await view.products_callback(cb, 'prd:save:invented', 1)
        assert ctx.store.list_drafts() == []
        await ui.text_handler(message('❌ Отмена'))
        assert 1 not in view._flows
        assert ctx.store.list_drafts() == [] and len(ctx.store.list_routes()) == 1
        ctx.client.account.save_lot.assert_not_called()
        assert all(call.args[0] == 'get' for call in ctx.client.account.method.call_args_list)

    asyncio.run(run())


def test_quick_variant_and_stop_preserve_saved_route(context):
    from funpayflow.product_variants import VariantService
    ctx = context
    route = seed_route(ctx)
    variant = VariantService(ctx.store, ctx.routes).save({
        'template_id': ctx.template['id'], 'route_id': route['id'],
        'display_name': 'Deluxe', 'overrides': {'variables': {'edition': 'Deluxe Edition'}}})

    async def run():
        cb = callback()
        await view.products_callback(cb, 'prd:quick', 1)
        await view.products_callback(cb, f"prd:route:{route['id']}", 1)
        flow = view._flows[1]
        assert 'node_state' not in flow
        token = flow['variant_picker']['token']
        await view.products_callback(cb, f"prd:vp:{token}:pick:{variant['id']}", 1)
        assert flow['variant_id'] == variant['id'] and flow['step'] == 'nodefields'
        for value in ('-', '-', '-', '-', '1', '39.99'):
            await view.products_input(message(value), 1)
        assert flow['preview']['variables']['edition'] == 'Deluxe Edition'
        assert flow['preview']['payload']['price'] == '39.99'
        old_save = preview_action('prd:save')
        await view.products_callback(cb, 'prd:quick', 1)
        await view.products_callback(cb, old_save, 1)
        assert view._flows[1].get('preview') is None
        await view.products_callback(cb, f"prd:route:{route['id']}", 1)
        token = view._flows[1]['variant_picker']['token']
        await view.products_callback(cb, f'prd:vp:{token}:pick:base', 1)
        await view.products_input(message('/stop'), 1)
        assert 1 not in view._flows and ctx.store.list_drafts() == []
        assert ctx.store.get_route(route['id']) == route
        ctx.client.account.save_lot.assert_not_called()

    asyncio.run(run())


def test_mass_drafts_common_defaults_per_row_and_duplicates(context):
    ctx = context
    seed_route(ctx, 0); seed_route(ctx, 1)

    async def run():
        text='Elden Ring | 29.99\nHelldivers 2 | 24.99 | region=EU\nElden Ring | 29.99\nUnknown | 1'
        legacy=ctx.drafts.preview_bulk(ctx.template['id'],text,{'variables':{'platform':'Steam'}})
        assert len(legacy['previews'])==2
        assert {r['payload']['price'] for r in legacy['previews']}=={'29.99','24.99'}
        assert any(r.get('status')=='duplicate' for r in legacy['errors'])
        cb=await pick(ctx,'bulk'); incoming=message(text)
        await view.products_input(incoming,1)
        assert 'Ready: 0' in str(incoming.answer.call_args)
        assert ctx.store.list_drafts()==[] and not view._flows[1].get('batch_token')
        # Already prepared legacy batches still confirm through the same durable engine.
        batch=ctx.drafts.stage(legacy['previews']); view._flows[1]['batch_token']=batch['token']
        await view.products_callback(cb,'prd:confirm:'+batch['token'],1)
        rows=ctx.store.list_drafts()
        assert len(rows)==2
        assert all(r['variables']['platform']=='Steam' for r in rows)
        assert next(r for r in rows if r['game']=='Helldivers 2')['variables']['region']=='EU'
        assert ctx.store.list_routes(recent=True)[0]['game_name']=='Helldivers 2'
        ctx.client.account.save_lot.assert_not_called()

    asyncio.run(run())


def test_global_cancel_back_stale_confirmation_and_owner_guard(context):
    ctx = context

    async def run():
        cb = await pick(ctx, 'routes')
        await view.products_callback(cb, 'prd:node:100', 1)
        token = view._flows[1]['batch_token']
        await ui.text_handler(message('❌ Отмена'))
        assert 1 not in view._flows
        await view.products_callback(cb, f'prd:confirm:{token}', 1)
        assert ctx.store.list_routes() == []
        await view.products_callback(cb, 'products', 2)
        assert cb.answer.call_args.kwargs.get('show_alert')
        await pick(ctx, 'routes')
        await ui.callback_handler(callback('menu_main'))
        assert 1 not in view._flows
        await view.products_callback(cb, 'prd:advanced', 1)
        assert 'lot_templates' in str(cb.message.edit_text.call_args)

    asyncio.run(run())


def test_template_json_edit_and_route_alias_emoji_override(context):
    ctx = context; route = seed_route(ctx)

    async def run():
        cb = await pick(ctx, 'routes')
        await view.products_callback(cb, f"prd:routeedit:{route['id']}", 1)
        await view.products_input(message('{"game_aliases":["ER"],"overrides":{"emoji":"⚔️"}}'), 1)
        await view.products_callback(cb, f"prd:confirm:{view._flows[1]['batch_token']}", 1)
        assert ctx.store.find_route(ctx.template['id'], 'er')['overrides']['emoji'] == '⚔️'
        await view.products_callback(cb, f"prd:template:{ctx.template['id']}", 1)
        await view.products_callback(cb, 'prd:templateedit', 1)
        definition = deepcopy(ctx.template)
        definition['display_name'] = 'Reusable game keys'
        await view.products_input(message(json.dumps(definition)), 1)
        assert ctx.templates.get(ctx.template['id'])['display_name'] == 'Reusable game keys'

    asyncio.run(run())


def test_long_preview_is_safe_and_legacy_drafts_untouched(context):
    ctx = context; route = seed_route(ctx)
    old = ctx.receipt.create_lot_template_draft('1', 42, 11, 100,
        {'title_ru': 'Old', 'title_en': '', 'description_ru': 'Old description',
         'description_en': '', 'price': '10', 'amount': '1'}, [11])
    preview = ctx.drafts.create_preview(ctx.template['id'], route['id'], price='10')
    for field in ('title_ru', 'title_en', 'description_ru', 'description_en'):
        preview['payload'][field] = '😀<&' * 3000
    preview['game'] = '<' * 200; preview['section'] = '😀' * 200
    text, markup = view._preview_screen({'preview': preview})
    assert len(text.encode('utf-16-le')) // 2 < 4096
    assert '&lt;' in text
    assert 'PUBLISH отсутствует' in text
    assert all(len(b.callback_data.encode()) <= 64 for row in markup.inline_keyboard for b in row)
    assert ctx.receipt.get_lot_template_draft('1', old['draft_id']) == old


def test_draft_route_wrong_template_and_cancelled_preview_not_saved(context):
    ctx = context; route = seed_route(ctx)
    definition = default_template(); definition['name'] = 'DLC_KEY'
    other = ctx.templates.save(definition)

    async def run():
        cb = callback()
        await view.products_callback(cb, f"prd:pick:single:{other['id']}", 1)
        await view.products_callback(cb, f"prd:route:{route['id']}", 1)
        assert 'route_id' not in view._flows[1]
        assert ctx.store.list_drafts() == []

    asyncio.run(run())


def test_stale_preview_actions_do_not_touch_another_active_draft(context):
    ctx = context; route = seed_route(ctx)
    previews = [ctx.drafts.create_preview(ctx.template['id'], route['id'], price=str(price)) for price in (10, 20)]
    rows = ctx.drafts.confirm(ctx.drafts.stage(previews)['token'])['results']

    async def run():
        cb = callback()
        await view.products_callback(cb, 'prd:open:' + rows[0]['id'], 1)
        old_actions = [preview_action(action) for action in ('prd:save', 'prd:discard', 'prd:edit:price', 'prd:text:ru')]
        await view.products_callback(cb, 'prd:open:' + rows[1]['id'], 1)
        current = deepcopy(view._flows[1])
        for action in old_actions:
            await view.products_callback(cb, action, 1)
            assert view._flows[1] == current
        assert ctx.store.get_draft(rows[0]['id']) == rows[0]
        assert ctx.store.get_draft(rows[1]['id']) == rows[1]
        await view.products_callback(cb, preview_action('prd:edit:price'), 1)
        pending = deepcopy(ui._interaction_state[1])
        await ui.callback_handler(callback(old_actions[0]))
        assert ui._interaction_state[1] == pending
        await view.products_input(message('21'), 1)
        old_save = preview_action('prd:save')
        await view.products_callback(cb, preview_action('prd:edit:price'), 1)
        await view.products_input(message('22'), 1)
        await view.products_callback(cb, old_save, 1)
        assert ctx.store.get_draft(rows[1]['id'])['payload']['price'] == '20'
        await view.products_callback(cb, preview_action('prd:save'), 1)
        assert ctx.store.get_draft(rows[1]['id'])['payload']['price'] == '22'

    asyncio.run(run())
