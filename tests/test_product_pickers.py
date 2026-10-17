"""Hybrid content pickers remain generic after the real node-select stage."""
import asyncio
from copy import deepcopy, copy
import pytest
from test_telegram_products import context, seed_route, callback, message, pick, preview_action
from funpayflow import telegram as ui, telegram_products as view, telegram_node_fields as node_ui
from funpayflow.node_products import NodeDraftService


async def send(data):
    cb=callback(data); await ui.callback_handler(cb); return cb


def pv(op,value=0):
    return 'prd:pv:'+view._flows[1]['picker']['token']+':'+op+':'+str(value)


def sim(op,value=0): return view._local_action(view._flows[1],op,value)


def variable_action(field): return sim('variable',view._flows[1]['similar_fields'].index(field))


async def node_defaults():
    while view._flows[1].get('step')=='nodefields':
        await send(node_ui.action(view._flows[1]['node_state'],'keep'))


@pytest.fixture
def ctx(context):
    ctx=context
    template=ctx.templates.get(ctx.template['id'])
    template['allowed_variables']+=['activation','language','notes']
    template['defaults'].update(activation='Online',language='English')
    template['validation_rules'].update(activation={'choices':['Online','Offline']},language={'choices':['English','Русский']})
    for lang in ('ru','en'): template['description_'+lang]+=' {{activation}} {{language}} {{notes}}'
    ctx.template=ctx.templates.save(template,template['id'],template['revision'])
    ctx.route=seed_route(ctx); ctx.nd=NodeDraftService(ctx.store,ctx.routes)
    return ctx


async def start(ctx):
    await pick(ctx); await send('prd:route:'+ctx.route['id']); await node_defaults()


def persist(ctx,**kwargs):
    *_,schema,defaults=ctx.nd.node_setup(ctx.template['id'],ctx.route['id'])
    body=ctx.nd.create_node_preview(ctx.template['id'],ctx.route['id'],defaults,price='12.50',
        variables={'notes':'Content'},input_values={},**kwargs)
    return ctx.nd.confirm(ctx.nd.stage([body])['token'])['results'][0]


async def clone(source):
    await send('prd:open:'+source['id']); return await send(preview_action('prd:clone'))


def test_enum_default_custom_free_text_numeric_and_save(ctx):
    async def run():
        await start(ctx)
        assert view._flows[1]['picker']['field']=='quantity'  # Original allowed order is retained.
        await send(pv('done'))
        assert view._flows[1]['picker']['field']=='activation'
        await send(pv('keep'))
        await send(pv('custom')); await ui.text_handler(message('Русский'))
        assert view._flows[1]['field']=='notes' and 'picker' not in view._flows[1]
        await ui.text_handler(message('Content'))
        await ui.text_handler(message('12.50'))
        body=view._flows[1]['preview']
        assert body['variables']['language']=='Русский' and body['variables']['activation']=='Online'
        assert body['input_variables']=={'language':'Русский','notes':'Content'}
        assert ctx.store.list_drafts()==[]
        await send(preview_action('prd:save'))
        assert len(ctx.store.list_drafts())==1
    asyncio.run(run())


def test_content_choices_pagination_special_values_and_validation(ctx):
    template=ctx.templates.get(ctx.template['id'])
    template['validation_rules']['activation']['choices']=['Online']+[f'<Option & {i}>'+('x'*40) for i in range(18)]
    ctx.templates.save(template,template['id'],template['revision'])
    async def run():
        await start(ctx); await send(pv('done'))
        stale=pv('choice',0); cb=await send(pv('page',8))
        markup=cb.message.answer.call_args.kwargs['reply_markup']
        assert all(len(b.callback_data.encode())<=64 and 'Option' not in b.callback_data for row in markup.inline_keyboard for b in row)
        await send(stale)
        assert view._flows[1]['picker']['offset']==8
        await send(pv('choice',8))
        assert view._flows[1]['variables']['activation'].startswith('<Option & 7>')
        await send(pv('custom'))
        bad=message('Not valid'); await ui.text_handler(bad)
        assert 'Не сохранено' in str(bad.answer.call_args)
        assert view._flows[1]['field']=='language'
        await ui.text_handler(message('Русский'))
        assert view._flows[1]['variables']['language']=='Русский'
    asyncio.run(run())


@pytest.mark.parametrize('invalid',['0','-1','1.2','True','1000000000'])
def test_similar_quantity_custom_validation_bounds_source_immutable(ctx,invalid):
    source=persist(ctx)
    async def run():
        await clone(source); await send(variable_action('quantity'))
        await send(pv('delta',-10)); assert view._flows[1]['picker']['numeric']==1
        await send(pv('custom')); await ui.text_handler(message(invalid))
        assert view._flows[1]['preview']['payload']['amount']==1
        assert ui._interaction_state[1]['flow']['step']=='similarinput'
        await ui.text_handler(message('999999999'))
        await send(variable_action('quantity')); await send(pv('delta',10))
        assert view._flows[1]['picker']['numeric']==999999999
        await send(pv('cancel'))
        assert ctx.store.get_draft(source['id'])==source
    asyncio.run(run())


def test_content_picker_back_cancel_stale_and_source_revision(ctx):
    source=persist(ctx)
    async def run():
        await clone(source); await send(variable_action('language'))
        old=pv('keep'); await send(pv('custom')); await send(pv('back'))
        assert 'picker' not in view._flows[1]
        await send(old)
        await send(variable_action('activation')); stale=pv('keep')
        await start(ctx)
        pending=ui._interaction_state[1]; token=view._flows[1]['picker']['token']
        await send(stale)
        assert ui._interaction_state[1] is pending and view._flows[1]['picker']['token']==token
        await send(pv('cancel')); assert ctx.store.get_draft(source['id'])==source
        await send('prd:open:'+source['id']); old=preview_action('prd:clone')
        ctx.store.edit_draft(source['id'],ctx.nd.rebuild(source,price='20'),source['revision'])
        await send(old)
        assert len(ctx.store.list_drafts())==1
    asyncio.run(run())


def test_similar_template_variable_price_multiple_clones_and_repeated_save(ctx):
    source=persist(ctx)
    async def run():
        await clone(source); await send(variable_action('activation')); await send(pv('choice',1))
        assert view._flows[1]['preview']['variables']['activation']=='Offline'
        await send(sim('price')); await send(pv('back'))
        assert view._flows[1]['preview']['payload']['price']=='12.50'
        await send(sim('price')); await ui.text_handler(message('22.25'))
        save=sim('save'); await asyncio.gather(send(save),send(save))
        assert len(ctx.store.list_drafts())==2
        assert ctx.store.get_draft(source['id'])==source
        await send(preview_action('prd:clone')); await send(sim('cancel'))
        assert len(ctx.store.list_drafts())==2
    asyncio.run(run())


def test_quantity_without_default_and_template_stale_pending(ctx):
    template=ctx.templates.get(ctx.template['id']); template['defaults'].pop('quantity')
    ctx.templates.save(template,template['id'],template['revision'])
    async def run():
        await start(ctx)
        assert view._flows[1]['picker']['numeric']==1
        pending=ui._interaction_state[1]; state=deepcopy(view._flows[1]['picker'])
        old=pv('done')
        template=ctx.templates.get(ctx.template['id']); ctx.templates.save(dict(template,display_name='Updated'),template['id'],template['revision'])
        await send(old)
        assert ui._interaction_state[1] is pending and view._flows[1]['picker']==state
        await send(pv('back'))
        assert view._flows[1]['picker']['revision']!=state['revision']
    asyncio.run(run())


def test_replaced_account_text_cannot_edit_another_context(ctx):
    source=persist(ctx)
    async def run():
        await clone(source); await send(sim('price'))
        original=ctx.client.account; ctx.client.account=copy(original)
        await ui.text_handler(message('99'))
        assert view._flows[1]['preview']['payload']['price']=='12.50'
        ctx.client.account=original; await send(pv('cancel'))
        assert ctx.store.get_draft(source['id'])==source
    asyncio.run(run())


def test_legacy_open_warns_and_clone_resolves_real_fields(ctx):
    preview=ctx.drafts.create_preview(ctx.template['id'],ctx.route['id'],variables={'notes':'Legacy'},price='15')
    source=ctx.drafts.confirm(ctx.drafts.stage([preview])['token'])['results'][0]
    async def run():
        cb=await send('prd:open:'+source['id'])
        assert 'Черновик создан до динамических параметров' in str(cb.message.edit_text.call_args)
        assert ctx.client.account.method.call_count==0
        await send(preview_action('prd:clone'))
        assert view._flows[1]['step']=='nodefields'
        await node_defaults()
        assert 'node_fields' in view._flows[1]['preview']
        assert ctx.store.get_draft(source['id'])==source and len(ctx.store.list_drafts())==1
    asyncio.run(run())
