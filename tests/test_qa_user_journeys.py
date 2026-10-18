"""Owner-like product flow smoke tests. All IO is mocked; no FunPay POST."""

import asyncio
from copy import deepcopy

from test_node_products import node, start, send, nf
from test_telegram_products import message, preview_action
from funpayflow import telegram as ui, telegram_products as view


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
