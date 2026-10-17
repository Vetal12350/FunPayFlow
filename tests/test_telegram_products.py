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
from funpayflow.product_templates import TemplateService, default_template
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


async def pick(ctx, purpose='single'):
    cb = callback()
    assert await view.products_callback(cb, 'products', 1)
    await view.products_callback(cb, f'prd:choose:{purpose}:0', 1)
    await view.products_callback(cb, f"prd:pick:{purpose}:{ctx.template['id']}", 1)
    return cb


async def fill_variables(ctx, route=None, bulk=False):
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


def test_single_draft_wizard_edit_save_duplicate_and_restart(context):
    ctx = context
    route = seed_route(ctx)

    async def run():
        cb = await fill_variables(ctx, route)
        invalid = message('NaN')
        await view.products_input(invalid, 1)
        assert ui._interaction_state[1]['flow']['field'] == 'price'
        await view.products_input(message('29.99'), 1)
        assert ctx.store.list_drafts() == []
        assert 'RU + EN' in str(view._preview_screen(view._flows[1]))
        await view.products_callback(cb, preview_action('prd:edit:emoji'), 1)
        await view.products_input(message('⚔️'), 1)
        await view.products_callback(cb, preview_action('prd:edit:en'), 1)
        await view.products_input(message('English key\nEnglish description'), 1)
        await asyncio.gather(*(view.products_callback(callback(), preview_action('prd:save'), 1) for _ in range(2)))
        rows = ctx.store.list_drafts()
        assert len(rows) == 1
        saved = rows[0]
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
        assert 'lot_templates' in str(view._home()[1])

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
