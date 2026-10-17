"""Offline realtime resolver journeys; SQLite writes require explicit confirmation."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from requests.exceptions import Timeout
from FunPayAPI.common.enums import SubCategoryTypes

from test_telegram_products import context, pick, callback, message, seed_route, preview_action
from funpayflow import telegram as ui, telegram_products as view
from funpayflow.game_catalog import GameCatalog
from funpayflow.product_store import ProductStore


class Account:
    def __init__(self, ctx):
        self.id = 42
        self.nodes = ctx.nodes
        self.reads = 0
        self.error = None
        self.save_lot = ctx.client.account.save_lot
        self.method = ctx.client.account.method
        self.get_user = ctx.client.account.get_user

    @property
    def subcategories(self):
        self.reads += 1
        if self.error:
            raise self.error
        return self.nodes

    def get_subcategory(self, kind, node_id):
        return next((n for n in self.nodes if n.id == node_id and n.type is kind), None)


@pytest.fixture
def ctx(context):
    context.client.account = Account(context)
    return context


def action(operation, value=0):
    return view._catalog_action(view._flows[1]['catalog_state'], operation, value)


async def dispatch(data):
    cb = callback(data)
    await ui.callback_handler(cb)
    return cb


async def search(ctx, query):
    await dispatch('prd:search')
    await ui.text_handler(message(query))
    return await dispatch('prd:find:' + view._flows[1]['search_token'])


@pytest.mark.parametrize('query,game', [('  cYBERpunk  ', 'Cyberpunk 2077'),
                                       ('  ELDEN   ring ', 'Elden Ring')])
def test_complete_create_saved_lookup_and_restart(ctx, query, game):
    async def run():
        await pick(ctx)
        cb = await search(ctx, query)
        assert 'Подтвердите GameRoute' in str(cb.message.edit_text.call_args)
        assert ctx.store.list_routes() == []
        confirm = action('confirm')
        await asyncio.gather(dispatch(confirm), dispatch(confirm))
        routes = ctx.store.list_routes()
        assert len(routes) == 1 and routes[0]['game_name'] == game
        assert routes[0]['game_aliases'] == []  # Query is not an invented alias.
        for value in ['-', '-', '-', '-', '-', '29.99']:
            await ui.text_handler(message(value))
        assert 'preview' in view._flows[1]
        await dispatch(preview_action('prd:save'))
        assert len(ctx.store.list_drafts()) == 1
        assert ctx.store.list_routes(recent=True)[0]['id'] == routes[0]['id']
        # Simulate process-memory restart; persisted route needs no catalog.
        view.discard_session(1)
        ctx.client._game_catalog_cache.clear()
        ctx.client.account.error = AssertionError('Catalog must not be read')
        reads = ctx.client.account.reads
        assert len(ProductStore(ctx.receipt, 1, 42).list_routes()) == 1
        await pick(ctx)
        await search(ctx, query)
        await dispatch('prd:route:' + routes[0]['id'])
        assert ctx.client.account.reads == reads
        assert ui._interaction_state[1]['flow']['step'] == 'nodefields'
        ctx.client.account.save_lot.assert_not_called()
        assert all(call.args[0] == 'get' and call.args[1].startswith('lots/offerEdit?node=') for call in ctx.client.account.method.call_args_list)
        ctx.client.account.get_user.assert_not_called()
    asyncio.run(run())


def test_saved_alias_never_reads_catalog(ctx):
    row = seed_route(ctx, 2)
    row['game_aliases'] = ['CP2077']
    ctx.store.save_route(row, expected_revision=row['revision'])
    ctx.client.account.error = AssertionError('Saved route first')
    async def run():
        await pick(ctx)
        cb = await search(ctx, ' cp2077 ')
        assert 'Cyberpunk 2077' in str(cb.message.edit_text.call_args)
        assert ctx.client.account.reads == 0
    asyncio.run(run())


def test_multiple_games_sections_pagination_and_no_guess(ctx):
    category = ctx.nodes[2].category
    ctx.nodes.extend(SimpleNamespace(id=500+i, name=f'Actual section {i}',
        type=SubCategoryTypes.COMMON, category=category) for i in range(10))
    async def run():
        cb = await pick(ctx)
        assert ':gpage:8' in str(cb.message.edit_text.call_args)
        cb = await dispatch(action('gpage', 8))
        assert ':gpage:0' in str(cb.message.edit_text.call_args)
        cb = await search(ctx, 'game')
        assert view._flows[1]['catalog_state']['view'] == 'games'
        assert len(view._flows[1]['catalog_state']['games']) > 1
        cb = await search(ctx, 'Cyberpunk')
        state = view._flows[1]['catalog_state']
        assert state['view'] == 'sections'
        assert ':spage:8' in str(cb.message.edit_text.call_args)
        assert 'Actual section' in str(cb.message.edit_text.call_args)
        cb = await dispatch(action('spage', 8))
        state = view._flows[1]['catalog_state']
        node = state['nodes'][0]
        await dispatch(action('node', node['node_id']))
        assert view._flows[1]['catalog_state']['candidate']['node_id'] == node['node_id']
        assert ctx.store.list_routes() == []
        await dispatch(action('confirm'))
        assert ctx.store.list_routes()[0]['node_id'] == node['node_id']
    asyncio.run(run())


def test_no_results_cancel_back_and_old_buttons(ctx):
    async def run():
        await pick(ctx)
        cb = await search(ctx, 'No such game')
        assert 'Совпадений' in str(cb.message.edit_text.call_args)
        await search(ctx, 'Elden')
        old = action('confirm')
        await dispatch(action('back'))
        await dispatch(old)
        assert ctx.store.list_routes() == []
        await search(ctx, 'Elden')
        old = action('confirm')
        await dispatch(action('cancel'))
        await dispatch(old)
        assert ctx.store.list_routes() == [] and 1 not in view._flows
    asyncio.run(run())


def test_refresh_rejects_old_confirmation_and_external_generation(ctx):
    async def run():
        await pick(ctx)
        await search(ctx, 'Cyberpunk')
        old = action('confirm')
        reads = ctx.client.account.reads
        await dispatch(action('refresh'))
        assert ctx.client.account.reads == reads + 1
        new = action('confirm')
        await dispatch(old)
        assert ctx.store.list_routes() == []
        GameCatalog(ctx.client, ctx.store).snapshot(force=True)
        await dispatch(new)
        assert ctx.store.list_routes() == []
        assert view._flows[1]['catalog_state']['view'] == 'games'
    asyncio.run(run())


def test_stale_search_account_template_and_other_draft(ctx):
    async def run():
        await pick(ctx)
        await dispatch('prd:search')
        await ui.text_handler(message('Elden'))
        old_search = 'prd:find:' + view._flows[1]['search_token']
        await dispatch('prd:search')
        await ui.text_handler(message('Cyberpunk'))
        current = view._flows[1]['search_token']
        await dispatch(old_search)
        assert view._flows[1]['search_token'] == current
        await dispatch('prd:find:' + current)
        old = action('confirm')
        previous = ctx.client.account
        ctx.client.account = Account(ctx)
        await dispatch(old)
        assert ctx.store.list_routes() == []
        ctx.client.account = previous
        await search(ctx, 'Cyberpunk')
        old = action('confirm')
        view._flows[1]['template_id'] = 'another-template'
        await dispatch(old)
        assert ctx.store.list_routes() == []
        await pick(ctx)
        await search(ctx, 'Elden')
        old = action('confirm')
        await dispatch(action('confirm'))
        pending = ui._interaction_state[1]
        await dispatch(old)
        assert ui._interaction_state[1] is pending
        for value in ['-', '-', '-', '-', '-', '5']:
            await ui.text_handler(message(value))
        token = view._flows[1]['preview_token']
        await dispatch(old)
        assert view._flows[1]['preview_token'] == token
        await dispatch(preview_action('prd:discard'))
        assert len(ctx.store.list_routes()) == 1
    asyncio.run(run())


@pytest.mark.parametrize('conflict', [False, True])
def test_concurrent_saved_route_reuse_or_conflict(ctx, conflict):
    async def run():
        await pick(ctx)
        await search(ctx, 'Elden')
        if conflict:
            ctx.nodes.append(SimpleNamespace(id=999, name='Different real section',
                type=SubCategoryTypes.COMMON, category=ctx.nodes[0].category))
            body = ctx.routes.from_url(ctx.template['id'], 'https://funpay.com/lots/999/')
            existing = ctx.store.save_route(body)
        else:
            existing = seed_route(ctx)
        before = deepcopy(existing)
        await dispatch(action('confirm'))
        assert ctx.store.list_routes() == [before]
        assert ('route_id' in view._flows[1]) is not conflict
    asyncio.run(run())


@pytest.mark.parametrize('change', ['wrong_game', 'non_common', 'missing'])
def test_current_node_validation_prevents_catalog_save(ctx, change):
    async def run():
        await pick(ctx)
        await search(ctx, 'Elden')
        if change == 'wrong_game':
            ctx.nodes[0].category = ctx.nodes[1].category
        elif change == 'non_common':
            ctx.nodes[0].type = SubCategoryTypes.CURRENCY
        else:
            ctx.nodes.pop(0)
        await dispatch(action('confirm'))
        assert ctx.store.list_routes() == []
    asyncio.run(run())


def test_cold_error_retry_and_stale_warning_require_confirmation(ctx):
    async def run():
        ctx.client.account.error = Timeout('sensitive synthetic error')
        cb = await pick(ctx)
        assert 'Каталог недоступен' in str(cb.message.edit_text.call_args)
        assert 'sensitive' not in str(cb.message.edit_text.call_args)
        ctx.client.account.error = None
        await dispatch(action('refresh'))
        await search(ctx, 'Elden')
        ctx.client.account.error = Timeout('sensitive synthetic error')
        cb = await dispatch(action('refresh'))
        assert 'сохранённая в памяти' in str(cb.message.edit_text.call_args)
        assert ctx.store.list_routes() == []
        await dispatch(action('confirm'))
        assert len(ctx.store.list_routes()) == 1
    asyncio.run(run())


def test_owner_guard_and_bulk_remains_saved_only(ctx):
    async def run():
        await pick(ctx)
        await search(ctx, 'Elden')
        cb = callback(action('confirm'))
        cb.from_user.id = 2
        await view.products_callback(cb, cb.data, 2)
        assert ctx.store.list_routes() == []
        reads = ctx.client.account.reads
        await pick(ctx, 'bulk')
        for value in ['-', '-', '-', '-', '-']:
            await ui.text_handler(message(value))
        text = message('Elden Ring | 2')
        await ui.text_handler(text)
        assert ctx.store.list_routes() == []
        assert ctx.client.account.reads == reads
        ctx.client.account.save_lot.assert_not_called()
    asyncio.run(run())
