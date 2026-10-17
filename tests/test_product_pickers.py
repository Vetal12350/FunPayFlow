"""Offline picker and similar-draft Telegram journeys with real temporary SQLite."""
import asyncio
from copy import deepcopy

import pytest
from test_telegram_products import context, pick, seed_route, callback, message, preview_action
from test_product_variant_integration import variant
from funpayflow import telegram as ui, telegram_products as view


async def send(data):
    cb=callback(data)
    await ui.callback_handler(cb)
    return cb


def pv(op, value=0):
    return 'prd:pv:' + view._flows[1]['picker']['token'] + ':' + op + ':' + str(value)


def sim(op, value=0):
    return view._local_action(view._flows[1],op,value)


def variable_action(field):
    return sim('variable',view._flows[1]['similar_fields'].index(field))


def choices(ctx, values=None):
    template=ctx.templates.get(ctx.template['id'])
    template['validation_rules']={'platform':{'choices':values or ['Steam','GOG']},
                                  'region':{'choices':['Global','EU']}}
    ctx.template=ctx.templates.save(template,template['id'],template['revision'])


def persist(ctx, route, **kwargs):
    body=ctx.drafts.create_preview(ctx.template['id'],route['id'],price='12.50',**kwargs)
    return ctx.drafts.confirm(ctx.drafts.stage([body])['token'])['results'][0]


async def open_clone(source):
    await send('prd:open:'+source['id'])
    return await send(preview_action('prd:clone'))


def test_game_key_defaults_buttons_free_text_and_numeric(context):
    ctx=context; route=seed_route(ctx)
    async def run():
        await pick(ctx)
        cb=await send('prd:route:'+route['id'])
        assert view._flows[1]['picker']['field']=='platform'
        await send(pv('keep'))
        assert view._flows[1]['picker']['field']=='region'
        await send(pv('keep'))
        await send(pv('keep'))  # Edition default.
        assert view._flows[1]['field']=='product_name' and 'picker' not in view._flows[1]
        await ui.text_handler(message('-'))
        assert 'numeric' in view._flows[1]['picker']
        await send(pv('done'))
        await ui.text_handler(message('12.50'))
        assert view._flows[1]['preview']['input_variables']=={}
        assert ctx.store.list_drafts()==[]
    asyncio.run(run())


def test_end_to_end_catalog_create_then_similar_region_quantity(context):
    ctx=context; choices(ctx)
    async def run():
        await pick(ctx)
        await send('prd:search'); await ui.text_handler(message('Cyberpunk'))
        await send('prd:find:'+view._flows[1]['search_token'])
        await send(view._catalog_action(view._flows[1]['catalog_state'],'confirm'))
        await send(pv('keep')); await send(pv('keep')); await send(pv('keep'))
        await ui.text_handler(message('-')); await send(pv('done'))
        await ui.text_handler(message('12.50'))
        await send(preview_action('prd:save'))
        source=deepcopy(ctx.store.list_drafts()[0])
        cb=await send(preview_action('prd:clone'))
        assert 'Похожий draft' in str(cb.message.answer.call_args)
        assert len(ctx.store.list_drafts())==1
        await send(variable_action('region')); await send(pv('choice',1))
        await send(variable_action('quantity')); await send(pv('number',5)); await send(pv('done'))
        await send(sim('preview'))
        result=view._flows[1]['preview']
        assert result['variables']['platform']=='Steam'
        assert result['variables']['region']=='EU' and result['payload']['amount']==5
        assert 'EU' in result['payload']['title_ru'] and 'EU' in result['payload']['title_en']
        save=preview_action('prd:save')
        await asyncio.gather(send(save),send(save))
        rows=ctx.store.list_drafts()
        assert len(rows)==2 and len({r['id'] for r in rows})==2
        assert ctx.store.get_draft(source['id'])==source
        await send(preview_action('prd:clone'))
        assert len(ctx.store.list_drafts())==2
        await send(sim('cancel'))
        assert len(ctx.store.list_drafts())==2
        ctx.client.account.save_lot.assert_not_called(); ctx.client.account.method.assert_not_called()
    asyncio.run(run())


def test_choices_pagination_special_values_and_custom_validation(context):
    ctx=context; choices(ctx,['Steam']+[f'<Option & {i}> '+('x'*40) for i in range(18)])
    route=seed_route(ctx)
    async def run():
        await pick(ctx); await send('prd:route:'+route['id'])
        old=pv('choice',0)
        cb=await send(pv('page',8))
        markup=cb.message.answer.call_args.kwargs['reply_markup']
        assert all(len(b.callback_data.encode())<=64 for row in markup.inline_keyboard for b in row)
        assert all('Option' not in b.callback_data for row in markup.inline_keyboard for b in row)
        await send(old)
        assert view._flows[1]['picker']['offset']==8
        await send(pv('choice',8))
        assert view._flows[1]['variables']['platform'].startswith('<Option & 7>')
        await send(pv('custom'))
        bad=message('Not an allowed region'); await ui.text_handler(bad)
        assert view._flows[1]['field']=='region' and 'Не сохранено' in str(bad.answer.call_args)
        await ui.text_handler(message('EU'))
        assert view._flows[1]['variables']['region']=='EU'
        assert ctx.store.list_drafts()==[]
    asyncio.run(run())


@pytest.mark.parametrize('direction',[False,True])
def test_variant_switch_clone_does_not_freeze_inherited_values(context,direction):
    ctx=context; route=seed_route(ctx)
    standard=variant(ctx,route,'Standard',overrides={'variables':{'edition':'Standard'},'emoji':'🔑'})
    deluxe=variant(ctx,route,'Deluxe')
    first,second=(deluxe,standard) if direction else (standard,deluxe)
    source=persist(ctx,route,variant_id=first['id'])
    async def run():
        await open_clone(source)
        await send(sim('variant'))
        state=view._flows[1]['similar_variants']; old='prd:sv:'+state['token']+':'+second['id']
        await send(old)
        result=view._flows[1]['preview']
        assert result['variant_id']==second['id']
        assert result['emoji']==second['overrides']['emoji']
        assert result['variables']['edition']==second['overrides']['variables']['edition']
        for lang in ('ru','en'):
            assert result['variables']['edition'] in result['payload']['title_'+lang]
        token=view._flows[1]['similar_token']
        await send(old)
        assert view._flows[1]['similar_token']==token
        assert ctx.store.get_draft(source['id'])==source
    asyncio.run(run())


def test_picker_back_cancel_stale_on_another_draft(context):
    ctx=context; route=seed_route(ctx); source=persist(ctx,route)
    async def run():
        await open_clone(source)
        await send(variable_action('region'))
        old=pv('keep')
        await send(pv('custom'))
        await send(pv('back'))
        assert 'picker' not in view._flows[1]
        await send(old)
        assert ctx.store.get_draft(source['id'])==source
        await send(variable_action('platform'))
        stale=pv('keep')
        await pick(ctx); await send('prd:route:'+route['id'])
        pending=ui._interaction_state[1]; token=view._flows[1]['picker']['token']
        await send(stale)
        assert ui._interaction_state[1] is pending and view._flows[1]['picker']['token']==token
        await send(pv('cancel'))
        assert not view._flows and ctx.store.get_draft(source['id'])==source
    asyncio.run(run())


@pytest.mark.parametrize('invalid',['0','-1','1.2','True','1000000000'])
def test_quantity_custom_validation(context,invalid):
    ctx=context; route=seed_route(ctx); source=persist(ctx,route)
    async def run():
        await open_clone(source); await send(variable_action('quantity'))
        await send(pv('delta',-10))
        assert view._flows[1]['picker']['numeric']==1
        await send(pv('custom'))
        await ui.text_handler(message(invalid))
        assert view._flows[1]['preview']['payload']['amount']==1
        assert ui._interaction_state[1]['flow']['step']=='similarinput'
        await ui.text_handler(message('999999999'))
        await send(variable_action('quantity')); await send(pv('delta',10))
        assert view._flows[1]['picker']['numeric']==999999999
        assert ctx.store.get_draft(source['id'])==source
    asyncio.run(run())


def test_dynamic_variables_and_source_revision_guard(context):
    ctx=context
    template=ctx.templates.get(ctx.template['id']); template['allowed_variables'].append('activation')
    template['defaults']['activation']='Online'; template['validation_rules']['activation']={'choices':['Online','Offline']}
    template['description_ru']+=' {{activation}}'; template['description_en']+=' {{activation}}'
    ctx.template=ctx.templates.save(template,template['id'],template['revision'])
    route=seed_route(ctx); source=persist(ctx,route)
    async def run():
        await send('prd:open:'+source['id'])
        old=preview_action('prd:clone')
        ctx.store.edit_draft(source['id'],ctx.drafts.rebuild(source,price='20'),source['revision'])
        cb=await send(old)
        assert 'изменились' in str(cb.message.answer.call_args)
        assert len(ctx.store.list_drafts())==1
        current=ctx.store.get_draft(source['id'])
        await open_clone(current)
        await send(variable_action('activation'))
        await send(pv('choice',1))
        assert view._flows[1]['preview']['variables']['activation']=='Offline'
        assert 'Offline' in view._flows[1]['preview']['payload']['description_en']
        await send(sim('price')); await ui.text_handler(message('22.25'))
        await send(sim('save'))
        assert len(ctx.store.list_drafts())==2
    asyncio.run(run())


def test_old_variant_picker_cannot_change_new_input(context):
    ctx=context; route=seed_route(ctx); deluxe=variant(ctx,route)
    async def run():
        await pick(ctx); await send('prd:route:'+route['id'])
        old='prd:vp:'+view._flows[1]['variant_picker']['token']+':pick:'+deluxe['id']
        await send(old)
        pending=ui._interaction_state[1]
        await send(old); await send('prd:variantpick:'+deluxe['id'])
        assert ui._interaction_state[1] is pending
        assert view._flows[1]['field']=='platform'
    asyncio.run(run())



def test_quantity_without_default_still_numeric(context):
    ctx=context
    template=ctx.templates.get(ctx.template['id']); template['defaults'].pop('quantity')
    ctx.templates.save(template,template['id'],template['revision'])
    route=seed_route(ctx)
    async def run():
        await pick(ctx); await send('prd:route:'+route['id'])
        for _ in range(3): await send(pv('keep'))
        await ui.text_handler(message('-'))
        assert view._flows[1]['picker']['field']=='quantity'
        assert view._flows[1]['picker']['numeric']==1
        await send(pv('done')); await ui.text_handler(message('5'))
        assert view._flows[1]['preview']['payload']['amount']==1
    asyncio.run(run())


def test_template_revision_stale_picker_preserves_pending_input(context):
    ctx=context; route=seed_route(ctx)
    async def run():
        await pick(ctx); await send('prd:route:'+route['id'])
        pending=ui._interaction_state[1]; state=deepcopy(view._flows[1]['picker'])
        stale=pv('keep')
        template=ctx.templates.get(ctx.template['id'])
        ctx.templates.save(dict(template,display_name='Updated'),template['id'],template['revision'])
        await send(stale)
        assert ui._interaction_state[1] is pending
        assert view._flows[1]['picker']==state
        await send(pv('back'))
        assert view._flows[1]['picker']['revision']!=state['revision']
        assert ctx.store.list_drafts()==[]
    asyncio.run(run())


def test_similar_price_back_and_replaced_account_input(context):
    from copy import copy
    ctx=context; route=seed_route(ctx); source=persist(ctx,route)
    async def run():
        await open_clone(source); await send(sim('price'))
        await send(pv('back'))
        assert view._flows[1]['preview']['payload']['price']=='12.50'
        await send(sim('price'))
        original=ctx.client.account; ctx.client.account=copy(original)
        await ui.text_handler(message('99'))
        assert view._flows[1]['preview']['payload']['price']=='12.50'
        ctx.client.account=original
        await send(pv('cancel'))
        assert ctx.store.get_draft(source['id'])==source
    asyncio.run(run())
