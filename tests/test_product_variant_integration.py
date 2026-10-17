"""Offline variants through real registry, draft engine and owner Telegram UI."""
import asyncio
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from funpayflow.product_drafts import DraftService
from funpayflow.product_store import ProductStore
from funpayflow.product_variants import VariantService
from funpayflow import telegram_products as view
from funpayflow import telegram as ui
from test_telegram_products import context, seed_route, pick, callback, message, preview_action


def variant(ctx, route, name='Deluxe', **extra):
    return VariantService(ctx.store, ctx.routes).save({
        'template_id': ctx.template['id'], 'route_id': route['id'],
        'display_name': name, 'aliases': ['DE', 'Deluxe Edition'] if name == 'Deluxe' else [],
        'overrides': {'variables': {'edition': name + ' Edition'}, 'emoji': '⭐'}, **extra})


def test_standard_deluxe_same_route_and_full_inheritance(context):
    ctx = context
    route = seed_route(ctx)
    route = ctx.store.save_route(dict(route, overrides={'emoji': '⚔️', 'variables': {'edition': 'Route edition', 'region': 'EU'}}),
                                 expected_revision=route['revision'])
    standard = variant(ctx, route, 'Standard', overrides={'variables': {'edition': 'Standard Edition'}})
    deluxe = variant(ctx, route)
    basic = ctx.drafts.create_preview(ctx.template['id'], route['id'], price='29.99', variant_id=standard['id'])
    special = ctx.drafts.create_preview(ctx.template['id'], route['id'], price='39.99', variant_id=deluxe['id'])
    assert len(ctx.store.list_routes()) == 1
    assert basic['node'] == special['node'] == route['node_id']
    assert basic['emoji'] == '⚔️' and special['emoji'] == '⭐'
    for lang in ['ru', 'en']:
        assert 'Standard Edition' in basic['payload']['title_' + lang]
        assert 'Deluxe Edition' in special['payload']['title_' + lang]
        assert special['payload']['description_' + lang]
    assert special['variables']['region'] == 'EU'
    explicit = ctx.drafts.create_preview(ctx.template['id'], route['id'],
        variables={'edition': 'Draft Edition'}, price='45', overrides={'emoji': '🎁'}, variant_id=deluxe['id'])
    assert explicit['emoji'] == '🎁' and explicit['variables']['edition'] == 'Draft Edition'
    assert ctx.drafts.rebuild(special) == special
    edited = ctx.drafts.rebuild(special, variables={'edition': 'Edited Edition'}, overrides={'emoji': '💎'})
    assert edited['emoji'] == '💎' and edited['variables']['edition'] == 'Edited Edition'
    assert ctx.drafts.rebuild(edited) == edited


def test_variant_four_text_fields_and_draft_text_priority(context):
    ctx = context
    route = seed_route(ctx)
    fields = ('title_ru', 'title_en', 'description_ru', 'description_en')
    overrides = {key: 'Variant {{edition}}' for key in fields}
    overrides['variables'] = {'edition': 'Deluxe'}
    deluxe = variant(ctx, route, overrides=overrides)
    preview = ctx.drafts.create_preview(ctx.template['id'], route['id'], price='10', variant_id=deluxe['id'])
    for field in fields:
        assert 'Variant Deluxe' in preview['payload'][field]
    changed = ctx.drafts.rebuild(preview, overrides={key: 'Draft {{edition}}' for key in fields})
    for field in fields:
        assert 'Draft Deluxe' in changed['payload'][field]
    assert ctx.drafts.rebuild(changed) == changed


def test_verified_alternate_node_text_override_snapshot_and_restart(context):
    ctx = context
    route = seed_route(ctx)
    ctx.nodes.append(SimpleNamespace(id=999, name='DLC keys', type=ctx.nodes[0].type, category=ctx.nodes[0].category))
    deluxe = variant(ctx, route, route_override_url='https://funpay.com/lots/999/', overrides={
        'variables': {'edition': 'Deluxe'}, 'emoji': '⭐',
        'title_ru': '{{game}} | {{edition}}', 'description_en': 'Deluxe product {{game}}'})
    preview = ctx.drafts.create_preview(ctx.template['id'], route['id'], price='39', variant_id=deluxe['id'])
    assert preview['node'] == 999 and preview['route_id'] == route['id']
    assert preview['payload']['title_ru'] == '⭐ Elden Ring | Deluxe'
    assert preview['payload']['description_en'] == 'Deluxe product Elden Ring'
    assert preview['payload']['description_ru']
    batch = ctx.drafts.stage([preview])
    VariantService(ctx.store, ctx.routes).disable(deluxe['id'], deluxe['revision'])
    fresh_store = ProductStore(ctx.receipt, 1, 42)
    service = DraftService(fresh_store, ctx.routes)
    saved = service.confirm(batch['token'])['results'][0]
    assert fresh_store.get_draft(saved['id'])['variant_snapshot']['id'] == deluxe['id']
    assert service.rebuild(preview) == preview  # disabled variants preserve already reviewed drafts
    with pytest.raises(ValueError):
        service.create_preview(ctx.template['id'], route['id'], price='1', variant_id=deluxe['id'])
    ctx.nodes[-1].name = 'Changed section'
    with pytest.raises(ValueError):
        service.rebuild(preview)
    ctx.client.account.save_lot.assert_not_called()
    ctx.client.account.method.assert_not_called()


def test_bulk_aliases_legacy_mixed_variants_and_no_mutation(context):
    ctx = context
    route = seed_route(ctx)
    variant(ctx, route, 'Standard', overrides={'variables': {'edition': 'Standard Edition'}})
    variant(ctx, route)
    result = ctx.drafts.preview_bulk(ctx.template['id'],
        'Elden Ring | 29.99\nElden Ring | Standard | 30\nElden Ring | dE | 39.99\n'
        'Elden Ring | Unknown | 20\nElden Ring | Deluxe Edition | 40 | edition=Custom | emoji=🎁\n'
        'Missing | Deluxe | 1\nElden Ring | Deluxe | nope')
    assert len(result['previews']) == 4 and len(result['errors']) == 3
    assert 'variant_id' not in result['previews'][0]
    assert result['previews'][1]['variables']['edition'] == 'Standard Edition'
    assert result['previews'][2]['variables']['edition'] == 'Deluxe Edition'
    assert result['previews'][3]['variables']['edition'] == 'Custom'
    assert result['previews'][3]['emoji'] == '🎁'
    staged = ctx.drafts.stage(result['previews'])
    assert len(ctx.drafts.confirm(staged['token'])['results']) == 4
    assert len(ctx.drafts.confirm(staged['token'])['results']) == 4
    assert len(ctx.store.list_drafts()) == 4
    ctx.client.account.save_lot.assert_not_called()
    ctx.client.account.method.assert_not_called()


def test_bulk_distinct_variant_nodes_are_not_deduplicated(context):
    ctx = context
    route = seed_route(ctx)
    ctx.nodes.append(SimpleNamespace(id=999, name='Other section', type=ctx.nodes[0].type, category=ctx.nodes[0].category))
    variant(ctx, route, 'A', overrides={})
    variant(ctx, route, 'B', overrides={}, route_override_url='https://funpay.com/lots/999/')
    result = ctx.drafts.preview_bulk(ctx.template['id'], 'Elden Ring | A | 10\nElden Ring | B | 10')
    assert len(result['previews']) == 2
    assert {p['node'] for p in result['previews']} == {100, 999}
    variant(ctx, route, '2026', overrides={})
    numeric = ctx.drafts.preview_bulk(ctx.template['id'], 'Elden Ring | 2026 | 20')
    assert numeric['previews'][0]['variant_name'] == '2026'


def test_additive_upgrade_keeps_pre_variant_templates_drafts_and_pending_batches(context):
    ctx = context
    route = seed_route(ctx)
    preview = ctx.drafts.create_preview(ctx.template['id'], route['id'], price='10')
    saved = ctx.drafts.confirm(ctx.drafts.stage([preview])['token'])['results'][0]
    pending = ctx.drafts.stage([preview])
    with sqlite3.connect(ctx.receipt.path) as db:
        db.execute('DROP TABLE product_variants')
        tables = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
        before = {name: (schema, db.execute('SELECT * FROM ' + name).fetchall()) for name, schema in tables.items()}
    ctx.receipt.initialize()
    ctx.receipt.initialize()
    with sqlite3.connect(ctx.receipt.path) as db:
        after_tables = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
        assert set(after_tables) - set(before) == {'product_variants'}
        assert all((after_tables[name], db.execute('SELECT * FROM ' + name).fetchall()) == rows
                   for name, rows in before.items())
    assert ctx.store.get_draft(saved['id']) == saved
    assert ctx.drafts.rebuild(preview) == preview
    assert ctx.drafts.confirm(pending['token'])['status'] == 'confirmed'


def test_telegram_variant_pick_and_preview(context):
    ctx = context
    route = seed_route(ctx)
    deluxe = variant(ctx, route)

    async def run():
        cb = await pick(ctx)
        await view.products_callback(cb, 'prd:route:' + route['id'], 1)
        assert 'Выберите вариант' in str(cb.message.edit_text.call_args)
        assert not view._flows[1].get('step')
        await view.products_callback(cb, 'prd:vp:' + view._flows[1]['variant_picker']['token'] + ':pick:' + deluxe['id'], 1)
        for _ in range(5):
            await view.products_input(message('-'), 1)
        await view.products_input(message('39.99'), 1)
        preview = view._flows[1]['preview']
        assert preview['variant_id'] == deluxe['id']
        assert preview['variables']['edition'] == 'Deluxe Edition'
        assert preview['emoji'] == '⭐'
        assert 'Variant: Deluxe' in view._preview_screen(view._flows[1])[0]
        await asyncio.gather(*(view.products_callback(callback(), preview_action('prd:save'), 1) for _ in range(2)))
        assert len(ctx.store.list_drafts()) == 1
        await view.products_callback(cb, 'products', 1)
        await view.products_callback(cb, 'prd:variantpick:' + deluxe['id'], 1)
        assert not view._flows

    asyncio.run(run())


def test_telegram_management_confirm_cancel_disable_and_scope(context):
    ctx = context
    route = seed_route(ctx)
    other = seed_route(ctx, 1)
    foreign = variant(ctx, other)

    async def run():
        cb = await pick(ctx, 'routes')
        await view.products_callback(cb, 'prd:route:' + route['id'], 1)
        await view.products_callback(cb, f"prd:variants:{route['id']}:0", 1)
        await view.products_callback(cb, 'prd:variantnew', 1)
        body = {'template_id': ctx.template['id'], 'route_id': route['id'], 'display_name': 'Deluxe',
                'overrides': {'variables': {'edition': 'Deluxe Edition'}}, 'enabled': True}
        await view.products_input(message(json.dumps(body)), 1)
        assert not VariantService(ctx.store, ctx.routes).list(route['id'])
        await view.products_callback(cb, f"prd:variants:{route['id']}:0", 1)  # cancel preview
        await view.products_callback(cb, 'prd:variantsave', 1)
        assert not VariantService(ctx.store, ctx.routes).list(route['id'])
        await view.products_callback(cb, 'prd:variantnew', 1)
        await view.products_input(message(json.dumps(body)), 1)
        action = 'prd:variantsave:' + view._flows[1]['variant_token']
        await asyncio.gather(*(view.products_callback(callback(), action, 1) for _ in range(2)))
        rows = VariantService(ctx.store, ctx.routes).list(route['id'])
        assert len(rows) == 1
        row = rows[0]
        await view.products_callback(cb, 'prd:variantedit:' + foreign['id'], 1)
        assert view._flows[1].get('variant_edit') is None
        await view.products_callback(cb, 'prd:variantedit:' + row['id'], 1)
        await view.products_callback(cb, f"prd:variantdisable:{row['id']}:{row['revision']}", 1)
        assert not VariantService(ctx.store, ctx.routes).list(route['id'])
        assert not ctx.store.get_variant(row['id'])['enabled']

    asyncio.run(run())


def test_telegram_variant_pagination_and_implicit_default(context):
    ctx = context
    route = seed_route(ctx)
    for i in range(10):
        variant(ctx, route, 'Edition ' + str(i))

    async def run():
        cb = await pick(ctx)
        await view.products_callback(cb, 'prd:route:' + route['id'], 1)
        assert 'prd:vp:' + view._flows[1]['variant_picker']['token'] + ':page:8' in str(cb.message.edit_text.call_args)
        await view.products_callback(cb, 'prd:vp:' + view._flows[1]['variant_picker']['token'] + ':page:8', 1)
        assert 'prd:vp:' + view._flows[1]['variant_picker']['token'] + ':page:0' in str(cb.message.edit_text.call_args)
        await view.products_callback(cb, 'prd:vp:' + view._flows[1]['variant_picker']['token'] + ':pick:base', 1)
        for _ in range(5):
            await view.products_input(message('-'), 1)
        await view.products_input(message('29.99'), 1)
        assert 'variant_id' not in view._flows[1]['preview']
        assert view._flows[1]['preview']['variables']['edition'] == 'Standard'
        await view.products_callback(cb, 'products', 1)
        cb = await pick(ctx, 'bulk')
        for _ in range(5):
            await view.products_input(message('-'), 1)
        result = message('Elden Ring | Edition 1 | 20\nElden Ring | Unknown | 30')
        await view.products_input(result, 1)
        assert 'Edition 1' in str(result.answer.call_args)
        token = view._flows[1]['batch_token']
        await view.products_callback(cb, 'prd:cancel:' + token, 1)
        assert not ctx.store.list_drafts()

    asyncio.run(run())


def test_stale_variant_confirmation_cannot_save_newer_preview(context):
    ctx = context; route = seed_route(ctx)

    async def run():
        cb = await pick(ctx, 'routes')
        await view.products_callback(cb, f"prd:variants:{route['id']}:0", 1)
        body = {'template_id': ctx.template['id'], 'route_id': route['id'], 'display_name': 'First'}
        await view.products_callback(cb, 'prd:variantnew', 1)
        await view.products_input(message(json.dumps(body)), 1)
        stale = 'prd:variantsave:' + view._flows[1]['variant_token']
        await view.products_callback(cb, 'prd:variantnew', 1)
        body['display_name'] = 'Second'
        await view.products_input(message(json.dumps(body)), 1)
        await view.products_callback(cb, stale, 1)
        assert not VariantService(ctx.store, ctx.routes).list(route['id'])
        await view.products_callback(cb, 'prd:variantsave:' + view._flows[1]['variant_token'], 1)
        assert [v['display_name'] for v in VariantService(ctx.store, ctx.routes).list(route['id'])] == ['Second']

    asyncio.run(run())


def test_integrated_search_deluxe_and_disabled_variant_callback(context):
    ctx = context; route = seed_route(ctx)
    deluxe = variant(ctx, route)

    async def run():
        await pick(ctx)
        await ui.callback_handler(callback('prd:search'))
        await ui.text_handler(message('ELDEN RING'))
        cb = callback('prd:routepage:0')
        await ui.callback_handler(cb)
        assert 'Elden Ring' in str(cb.message.edit_text.call_args)
        await ui.callback_handler(callback('prd:route:' + route['id']))
        VariantService(ctx.store, ctx.routes).disable(deluxe['id'], deluxe['revision'])
        await ui.callback_handler(callback('prd:vp:' + view._flows[1]['variant_picker']['token'] + ':pick:' + deluxe['id']))
        assert 'variant_id' not in view._flows[1]
        assert not ctx.store.list_drafts()
        disabled = ctx.store.get_variant(deluxe['id'])
        VariantService(ctx.store, ctx.routes).save(dict(disabled, enabled=True), disabled['id'], disabled['revision'])
        await ui.callback_handler(callback('prd:route:' + route['id']))
        await ui.callback_handler(callback('prd:vp:' + view._flows[1]['variant_picker']['token'] + ':pick:' + deluxe['id']))
        for _ in range(5):
            await ui.text_handler(message('-'))
        await ui.text_handler(message('39.99'))
        assert view._flows[1]['preview']['variables']['edition'] == 'Deluxe Edition'
        await ui.callback_handler(callback(preview_action('prd:save')))
        assert len(ctx.store.list_drafts()) == 1
        ctx.client.account.save_lot.assert_not_called()
        ctx.client.account.method.assert_not_called()

    asyncio.run(run())
