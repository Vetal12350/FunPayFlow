"""Owner-like product flow smoke tests. All IO is mocked; no FunPay POST."""

import asyncio
from copy import deepcopy

import pytest

from test_node_products import node, start, send, nf
from test_telegram_products import context, callback, message, preview_action, pick, seed_route
from funpayflow import telegram as ui, telegram_products as view
from funpayflow.product_store import ProductStore


def test_qa_create_save_reopen_similar_with_technical_fields(node):
    ctx = node

    async def owner_flow():
        await start(ctx)
        observed = []
        while view._flows[1].get("step") == "nodefields":
            state = view._flows[1]["node_state"]
            field = state["field"]
            observed.append(field)
            if field in {"fields[edition]", "fields[edition_extra]"}:
                await send(nf("choose", 1))
            else:
                await send(nf("keep"))
        schema = view._flows[1]["node_state"]["schema"]
        assert observed == [field["html_name"] for field in schema["fields"] if not field["disabled"]]
        if ctx.node_id == 976:
            assert "fields[region]" not in observed
        else:
            assert "fields[edition]" in observed and "fields[edition_extra]" in observed

        picker = view._flows[1]["picker"]
        await send("prd:pv:" + picker["token"] + ":done:0")
        await ui.text_handler(message("15"))
        assert not ctx.store.list_drafts()
        await send(preview_action("prd:save"))
        drafts = ctx.store.list_drafts()
        assert len(drafts) == 1
        source = deepcopy(drafts[0])

        await send("prd:open:" + source["id"])
        assert view._flows[1]["preview"]["node_fields"] == source["node_fields"]
        await send(preview_action("prd:clone"))
        flow = view._flows[1]
        target = "fields[region]" if ctx.node_id == 986 else "fields[platform]"
        await send(view._local_action(flow, "node", flow["node_edit_fields"].index(target)))
        assert view._flows[1]["node_state"]["field"] == target
        await send(nf("choose", 2 if ctx.node_id == 976 else 1))
        new_preview = view._flows[1]["preview"]
        original_fields = {f["html_name"]: f["value"] for f in source["node_fields"]}
        cloned_fields = {f["html_name"]: f["value"] for f in new_preview["node_fields"]}
        assert cloned_fields[target] != original_fields[target]
        assert all(cloned_fields[key] == value for key, value in original_fields.items() if key != target)
        assert len(ctx.store.list_drafts()) == 1
        assert ctx.store.get_draft(source["id"]) == source

        await send(view._local_action(view._flows[1], "save"))
        assert len(ctx.store.list_drafts()) == 2
        assert ctx.store.get_draft(source["id"]) == source

    asyncio.run(owner_flow())
    assert ctx.client.account.method.call_count >= 1
    assert all(call.args[0] == "get" and call.args[1] == f"lots/offerEdit?node={ctx.node_id}"
               for call in ctx.client.account.method.call_args_list)
    ctx.client.account.save_lot.assert_not_called()


@pytest.mark.parametrize('operation', ['confirm', 'cancel'])
@pytest.mark.parametrize('editing', [False, True])
def test_qa_old_batch_callback_preserves_another_draft(context, operation, editing):
    ctx = context
    route = seed_route(ctx)
    preview = ctx.drafts.create_preview(ctx.template['id'], route['id'], price='10')
    source = ctx.drafts.confirm(ctx.drafts.stage([preview])['token'])['results'][0]

    async def owner_flow():
        await pick(ctx, 'routes')
        await send('prd:node:101')
        stale = f"prd:{operation}:" + view._flows[1]['batch_token']
        await send('prd:open:' + source['id'])
        if editing:
            await send(preview_action('prd:edit:price'))
        flow = view._flows[1]
        snapshot = deepcopy(flow)
        pending = ui._interaction_state.get(1)
        await send(stale)
        assert view._flows[1] is flow and flow == snapshot
        assert ui._interaction_state.get(1) is pending
        if editing:
            await ui.text_handler(message('21'))
        await send(preview_action('prd:save'))
        fresh = ProductStore(ctx.receipt, 1, 42)
        assert fresh.get_draft(source['id'])['payload']['price'] == ('21' if editing else '10')
        assert len(fresh.list_routes()) == 1

    asyncio.run(owner_flow())
    ctx.client.account.method.assert_not_called()
    ctx.client.account.save_lot.assert_not_called()


@pytest.mark.parametrize('operation', ['confirm', 'cancel'])
def test_qa_old_batch_callback_after_resuming_import(context, operation):
    ctx = context

    async def owner_flow():
        await pick(ctx, 'routes')
        await send('prd:node:100')
        stale = f"prd:{operation}:" + view._flows[1]['batch_token']
        await send('prd:routeimport')
        flow = view._flows[1]
        snapshot = deepcopy(flow)
        pending = ui._interaction_state[1]
        await send(stale)
        assert view._flows[1] is flow and flow == snapshot
        assert ui._interaction_state[1] is pending
        assert not ProductStore(ctx.receipt, 1, 42).list_routes()
        await ui.text_handler(message('https://funpay.com/lots/101/'))
        await send('prd:confirm:' + view._flows[1]['batch_token'])
        routes = ProductStore(ctx.receipt, 1, 42).list_routes()
        assert len(routes) == 1 and routes[0]['node_id'] == 101

    asyncio.run(owner_flow())
    ctx.client.account.method.assert_not_called()
    ctx.client.account.save_lot.assert_not_called()


@pytest.mark.parametrize('quantity', ['1', '999999999'])
def test_qa_quantity_save_reopen_validate_and_edit(node, quantity):
    ctx = node

    async def owner_flow():
        await start(ctx)
        while view._flows[1].get('step') == 'nodefields':
            await send(nf('keep'))
        token = view._flows[1]['picker']['token']
        await send(f'prd:pv:{token}:custom:0')
        await ui.text_handler(message(quantity))
        await ui.text_handler(message('15'))
        save = preview_action('prd:save')
        await asyncio.gather(send(save), send(save))
        source = ctx.store.list_drafts()[0]
        assert source['payload']['amount'] == int(quantity)
        view.discard_session(1)
        ui._interaction_state.clear()
        await send('prd:open:' + source['id'])
        await send(preview_action('prd:edit:variables'))
        before = deepcopy(view._flows[1]['preview'])
        for invalid in ('0', '1000000000', '1.5'):
            await ui.text_handler(message('quantity=' + invalid))
            assert view._flows[1]['preview'] == before
            assert ctx.store.get_draft(source['id']) == source
            assert ui._interaction_state[1]['flow'] is view._flows[1]
        replacement = '999999999' if quantity == '1' else '1'
        await ui.text_handler(message('quantity=' + replacement))
        await send(preview_action('prd:save'))
        fresh = ProductStore(ctx.receipt, 1, 42)
        saved = fresh.get_draft(source['id'])
        assert len(fresh.list_drafts()) == 1
        assert saved['payload']['amount'] == int(replacement)
        assert saved['revision'] == source['revision'] + 1

    asyncio.run(owner_flow())
    assert all(call.args[0] == 'get' for call in ctx.client.account.method.call_args_list)
    ctx.client.account.save_lot.assert_not_called()


def test_qa_non_owner_cannot_use_owner_draft_controls(context):
    ctx = context
    route = seed_route(ctx)
    preview = ctx.drafts.create_preview(ctx.template['id'], route['id'], price='10')
    source = ctx.drafts.confirm(ctx.drafts.stage([preview])['token'])['results'][0]

    async def owner_flow():
        await pick(ctx, 'routes')
        await send('prd:node:101')
        batch = view._flows[1]['batch_token']
        await send('prd:open:' + source['id'])
        actions = [preview_action(action) for action in
                   ('prd:save', 'prd:discard', 'prd:clone', 'prd:edit:price')]
        actions += ['prd:confirm:' + batch, 'prd:cancel:' + batch,
                    'prd:open:' + source['id'], 'prd:routeedit:' + route['id']]
        await send(preview_action('prd:edit:price'))
        snapshot = deepcopy(view._flows[1])
        pending = ui._interaction_state[1]
        for action in actions:
            cb = callback(action)
            cb.from_user.id = 2
            await ui.callback_handler(cb)
            assert cb.answer.call_args.kwargs['show_alert']
            # Also exercise the product module's own owner gate.
            await view.products_callback(cb, action, 2)
            assert cb.answer.call_args.kwargs['show_alert']
            assert view._flows[1] == snapshot and 2 not in view._flows
            assert ui._interaction_state[1] is pending
        fresh = ProductStore(ctx.receipt, 1, 42)
        assert fresh.get_draft(source['id']) == source
        assert fresh.list_routes() == [route]

    asyncio.run(owner_flow())
    ctx.client.account.method.assert_not_called()
    ctx.client.account.save_lot.assert_not_called()
