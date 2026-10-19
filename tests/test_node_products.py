"""Real node-schema contracts with synthetic HTML; no live account or POST."""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_telegram_products import context, seed_route, callback, message, pick, preview_action
from funpayflow import telegram as ui, telegram_products as view, telegram_node_fields as fields_ui
from funpayflow.node_form import NodeFormService, NodeFormError
from funpayflow.node_products import (NodeDraftService, selected_fields, STALE_WARNING,
                                     NodeSelectionError, NodeVariantConflictError, VARIANT_WARNING)
from funpayflow.product_variants import VariantService
from funpayflow.product_templates import ProductValidationError, validate_node_bindings


@pytest.fixture(params=['cyberpunk','elden_ring'])
def node(context,request):
    ctx=context
    ctx.node_id=976 if request.param=='cyberpunk' else 986
    index=2 if request.param=='cyberpunk' else 0
    ctx.nodes[index].id=ctx.node_id
    ctx.html=(Path(__file__).parent/'fixtures'/'node_forms'/(request.param+'.html')).read_text(encoding='utf-8')
    ctx.client.account.method.side_effect=lambda verb,url,*args,**kwargs: SimpleNamespace(status_code=200,url='https://funpay.com/'+url,content=ctx.html.encode())
    template=ctx.templates.get(ctx.template['id'])
    template['node_bindings']={'platform':'fields[platform]','edition':'fields[edition]'}
    if request.param=='elden_ring': template['node_bindings']['region']='fields[region]'
    ctx.template=ctx.templates.save(template,template['id'],template['revision'])
    ctx.route=seed_route(ctx,index)
    ctx.nd=NodeDraftService(ctx.store,ctx.routes)
    return ctx


async def send(data):
    cb=callback(data); await ui.callback_handler(cb); return cb


def nf(op,value=0):
    return fields_ui.action(view._flows[1]['node_state'],op,value)


async def start(ctx):
    await pick(ctx); return await send('prd:route:'+ctx.route['id'])


async def complete_fields():
    while view._flows[1].get('step')=='nodefields':
        await send(nf('keep'))


def make(ctx,values=None,**kwargs):
    *_,schema,defaults=ctx.nd.node_setup(ctx.template['id'],ctx.route['id'])
    return ctx.nd.create_node_preview(ctx.template['id'],ctx.route['id'],defaults if values is None else values,price='15',**kwargs)


def save(ctx,body):
    return ctx.nd.confirm(ctx.nd.stage([body])['token'])['results'][0]


@pytest.fixture
def plain_node(node):
    ctx = node
    # Conceptual form, not captured live HTML. Deliberately no region field and
    # no semantic display labels: only the owner's technical mapping can bind.
    ctx.html = (f'<form class="form-offer-editor"><input name="node_id" value="{ctx.node_id}">'
                '<div class="form-group"><label>Choice A</label><select name="platform" required>'
                '<option value="pc" selected>PC</option><option value="ps">PlayStation</option></select></div>'
                '<div class="form-group"><label>Choice B</label><select name="edition">'
                '<option value="standard" selected>Standard Edition</option>'
                '<option value="deluxe">Deluxe Edition</option></select></div></form>')
    template = ctx.templates.get(ctx.template['id'])
    template['node_bindings'] = {'platform': 'platform', 'edition': 'edition'}
    ctx.template = ctx.templates.save(template, template['id'], template['revision'])
    return ctx


@pytest.mark.parametrize('name', [
    'platform', 'edition', 'category_type', 'game-mode', 'region.us', 'a:b', 'A' * 64,
    'fields[platform]', 'fields[game][edition]', 'fields[a][b][c][d]',
])
def test_explicit_binding_accepts_safe_technical_grammar(name):
    assert validate_node_bindings({'platform': name}, ['platform']) == {'platform': name}


@pytest.mark.parametrize('name', [
    '', None, 1, 'A' * 65, '1platform', 'bad@field', ' platform', 'platform\n',
    'fields', 'fieldsx', 'other[edition]', 'fields[]', 'fields[edition]suffix',
    'fields[a][b][c][d][e]', 'fields[' + 'a' * 65 + ']',
    'price', 'amount', 'currency', 'active', 'auto_delivery', 'locale',
    'account_type', 'authorization', 'secret', 'csrf_token', 'session_id',
    'seller_token', 'password', 'cookies', 'api_key', 'node_id', 'offer_id',
    'fields[summary]', 'fields[desc][en]', 'fields[token_type]', 'fields[session_id]',
    'fields[request]', 'fields[node_id]', 'fields[secret]', 'fields[account_type]',
    'fields[cookies]', 'fields[price]', 'fields[amount]', 'fields[currency]',
    'fields[game][price]',
])
def test_binding_rejects_malformed_standard_and_service_names(name):
    with pytest.raises(ProductValidationError, match='node_bindings:'):
        validate_node_bindings({'platform': name}, ['platform'])


def test_plain_binding_renders_option_labels_and_safe_lifecycle(plain_node):
    ctx = plain_node
    body = make(ctx)
    assert body['variables']['platform'] == 'PC'
    assert body['variables']['edition'] == 'Standard Edition'
    assert body['variables']['region'] == '—'
    assert 'node_bindings' in body['node_warning']
    for language in ('ru', 'en'):
        for field in ('title_', 'description_'):
            text = body['payload'][field + language]
            assert 'PC' in text and 'Standard Edition' in text and '—' in text
            assert 'Global' not in text and 'Steam' not in text
    text, _ = view._preview_screen({'preview': body})
    assert 'PC' in text and 'Standard Edition' in text
    assert ctx.store.list_drafts() == []
    assert ctx.nd.rebuild(body) == body
    source = save(ctx, body)
    assert ctx.nd.refresh_preview(source)['node_fields'] == source['node_fields']
    clone = ctx.nd.clone_preview(source['id'], source['revision'])
    changed = ctx.nd.create_node_preview(ctx.template['id'], ctx.route['id'],
                                        {'platform': 'ps', 'edition': 'deluxe'}, price='25')
    assert changed['variables']['platform'] == 'PlayStation'
    assert changed['variables']['edition'] == 'Deluxe Edition'
    assert clone['node_bindings'] == {'platform': 'platform', 'edition': 'edition'}
    saved_clone = save(ctx, clone)
    assert saved_clone['id'] != source['id']
    assert ctx.store.get_draft(source['id']) == source
    assert all(c.args[0] == 'get' for c in ctx.client.account.method.call_args_list)
    ctx.client.account.save_lot.assert_not_called()


def test_plain_names_without_bindings_stay_neutral(plain_node):
    ctx = plain_node
    template = ctx.templates.get(ctx.template['id'])
    template.pop('node_bindings')
    ctx.templates.save(template, template['id'], template['revision'])
    body = make(ctx)
    assert all(body['variables'][key] == '—' for key in ('platform', 'edition', 'region'))
    assert [row['option_label'] for row in body['node_fields']] == ['PC', 'Standard Edition']
    assert 'node_bindings' in body['node_warning']
    for language in ('ru', 'en'):
        for field in ('title_', 'description_'):
            text = body['payload'][field + language]
            assert '—' in text
            assert all(value not in text for value in ('PC', 'Steam', 'Global', 'Standard'))


async def binding_screen(ctx, *, unbound=True):
    if unbound:
        template = ctx.templates.get(ctx.template['id'])
        template['node_bindings'] = {}
        ctx.templates.save(template, template['id'], template['revision'])
    body = make(ctx)
    view._flows[1] = {'preview': body, 'template_id': body['template_id'], 'route_id': body['route_id']}
    view._preview_screen(view._flows[1])
    outdated_save = preview_action('prd:save')
    cb = await send(preview_action('prd:bindings'))
    assert 'binding_state' in view._flows[1]
    state = view._flows[1]['binding_state']
    await send(outdated_save)
    assert view._flows[1].get('binding_state') is state
    return cb, deepcopy(body)


def nb(op, index=0):
    return fields_ui.binding_action(view._flows[1]['binding_state'], op, index)


def test_binding_wizard_explicit_confirm_and_safe_preview(plain_node, monkeypatch):
    async def run():
        ctx = plain_node
        from unittest.mock import Mock
        import FunPayAPI
        account = Mock(side_effect=AssertionError('No second Account'))
        monkeypatch.setattr(FunPayAPI, 'Account', account)
        cb, body = await binding_screen(ctx)
        old = ctx.templates.get(ctx.template['id'])
        for key, source in [('platform', 0), ('edition', 1)]:
            state = view._flows[1]['binding_state']
            await send(nb('variable', state['variables'].index(key)))
            # Every source callback carries only token/operation/index.
            data = nb('source', source)
            assert len(data.encode()) <= 64 and data.split(':')[-1].isdigit()
            assert all(value not in data for value in ('platform', 'edition', 'PC', 'standard', 'fields['))
            await send(data)
            assert ctx.templates.get(old['id']) == old
        cb = await send(nb('back'))
        text = '\n'.join(call.args[0] for call in cb.message.answer.call_args_list)
        assert 'platform → Choice A: PC' in text
        assert 'edition → Choice B: Standard Edition' in text
        assert 'region → Не связано' in text
        replay = nb('confirm')
        await send(replay)
        saved = ctx.templates.get(old['id'])
        assert saved['revision'] == old['revision'] + 1
        assert saved['node_bindings'] == {'platform': 'platform', 'edition': 'edition'}
        reopened = type(ctx.templates)(type(ctx.store)(ctx.receipt, 1, 42)).get(old['id'])
        assert reopened == saved
        updated = view._flows[1]['preview']
        for lang in ('ru', 'en'):
            for field in ('title_', 'description_'):
                rendered = updated['payload'][field + lang]
                assert 'PC' in rendered and 'Standard Edition' in rendered and '—' in rendered
                assert 'Global' not in rendered
        assert updated['node_fields'] == body['node_fields']
        assert updated['payload']['amount'] == body['payload']['amount']
        assert updated['payload']['price'] == body['payload']['price']
        await send(replay)
        assert ctx.templates.get(old['id']) == saved
        assert ctx.store.list_drafts() == []
        assert all(c.args[0] == 'get' for c in ctx.client.account.method.call_args_list)
        ctx.client.account.save_lot.assert_not_called()
        account.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('confirm', [False, True])
def test_binding_clear_or_cancel_retains_draft(plain_node, confirm):
    async def run():
        ctx = plain_node
        _, body = await binding_screen(ctx, unbound=False)
        old = ctx.templates.get(ctx.template['id'])
        state = view._flows[1]['binding_state']
        await send(nb('variable', state['variables'].index('platform')))
        await send(nb('clear'))
        assert ctx.templates.get(old['id']) == old
        await send(nb('confirm' if confirm else 'cancel'))
        saved = ctx.templates.get(old['id'])
        if confirm:
            assert saved['node_bindings'] == {'edition': 'edition'}
            assert view._flows[1]['preview']['variables']['platform'] == '—'
        else:
            assert saved == old
            assert view._flows[1]['preview'] == body
    asyncio.run(run())


@pytest.mark.parametrize('change', ['template', 'route', 'generation', 'account', 'flow', 'token', 'owner'])
def test_binding_rejects_outdated_context(plain_node, change):
    async def run():
        ctx = plain_node
        await binding_screen(ctx)
        old = ctx.templates.get(ctx.template['id'])
        action = nb('confirm')
        if change == 'template':
            edited = dict(old, display_name='Changed elsewhere')
            ctx.templates.save(edited, old['id'], old['revision'])
        elif change == 'route':
            route = ctx.store.get_route(ctx.route['id'])
            ctx.store.save_route(dict(route, overrides={'emoji': 'X'}), expected_revision=route['revision'])
        elif change == 'generation':
            NodeFormService(ctx.client, ctx.store).get(ctx.node_id, force=True)
        elif change == 'account':
            ctx.client.account = deepcopy(ctx.client.account)
        elif change == 'flow':
            view._flows[1]['preview']['route_id'] = 'wrong-route'
        elif change == 'token':
            action = action.replace(view._flows[1]['binding_state']['token'], 'oldtoken')
        cb = callback(action)
        if change == 'owner':
            cb.from_user.id = 2
        await ui.callback_handler(cb)
        assert ctx.templates.get(old['id'])['node_bindings'] == {}
        assert ctx.templates.get(old['id'])['revision'] == old['revision'] + (change == 'template')
        assert ctx.store.list_drafts() == []
    asyncio.run(run())


def test_binding_conflict_does_not_write(plain_node):
    async def run():
        ctx = plain_node
        await binding_screen(ctx)
        flow = view._flows[1]
        # Custom explicit content must not be discarded to make a binding fit.
        flow['preview']['input_variables']['platform'] = 'Custom'
        flow['binding_state']['body'] = deepcopy(flow['preview'])
        state = flow['binding_state']
        await send(nb('variable', state['variables'].index('platform')))
        await send(nb('source', 0))
        await send(nb('confirm'))
        assert ctx.templates.get(ctx.template['id'])['node_bindings'] == {}
        assert flow['preview']['input_variables']['platform'] == 'Custom'
    asyncio.run(run())


def test_binding_same_labels_custom_variable_and_unavailable_sources(plain_node):
    async def run():
        ctx = plain_node
        ctx.html = ctx.html.replace('Choice A', '&lt;b&gt;Choice&lt;/b&gt;').replace('Choice B', '&lt;b&gt;Choice&lt;/b&gt;')
        ctx.html = ctx.html.replace('</form>', '<div class="form-group"><label>Optional</label>'
            '<select name="optional"><option value="" selected>Choose</option></select></div>'
            '<div class="form-group"><label>Disabled</label><select name="disabled_choice" disabled>'
            '<option value="x" selected>X</option></select></div></form>')
        template = ctx.templates.get(ctx.template['id'])
        template['allowed_variables'].append('custom')
        ctx.templates.save(template, template['id'], template['revision'])
        await binding_screen(ctx)
        state = view._flows[1]['binding_state']
        assert not set(state['variables']) & {'game', 'quantity', 'product_name'}
        await send(nb('variable', state['variables'].index('custom')))
        stale = nb('source', 0)
        for index in (2, 3, 100, -1):
            await send(nb('source', index))
            assert 'custom' not in state['bindings']
        cb = await send(nb('source', 1))
        text = '\n'.join(call.args[0] for call in cb.message.answer.call_args_list)
        assert 'custom → <b>Choice</b> (2): Standard Edition' in text
        assert all(call.kwargs.get('parse_mode') is None for call in cb.message.answer.call_args_list)
        await send(stale)
        assert state['bindings']['custom'] == 'edition'
        await send(nb('confirm'))
        assert ctx.templates.get(template['id'])['node_bindings'] == {'custom': 'edition'}
        text, _ = view._preview_screen(view._flows[1])
        assert '&lt;b&gt;Choice&lt;/b&gt;' in text
    asyncio.run(run())


def test_binding_compare_and_swap_race(plain_node, monkeypatch):
    async def run():
        ctx = plain_node
        await binding_screen(ctx)
        state = view._flows[1]['binding_state']
        await send(nb('variable', state['variables'].index('platform')))
        await send(nb('source', 0))
        from funpayflow.product_templates import TemplateService
        original = TemplateService.save
        winner = []
        def racing_save(service, body, template_id=None, expected_revision=None):
            current = service.get(template_id)
            winner.append(original(service, dict(current, display_name='Concurrent winner'), template_id, current['revision']))
            return original(service, body, template_id, expected_revision)
        monkeypatch.setattr(TemplateService, 'save', racing_save)
        old_preview = deepcopy(view._flows[1]['preview'])
        await send(nb('confirm'))
        assert ctx.templates.get(ctx.template['id']) == winner[0]
        assert winner[0]['node_bindings'] == {}
        assert view._flows[1]['preview'] == old_preview
    asyncio.run(run())


def test_binding_variant_conflict_and_navigation(plain_node):
    async def run():
        ctx = plain_node
        variant = VariantService(ctx.store, ctx.routes).save({
            'template_id': ctx.template['id'], 'route_id': ctx.route['id'],
            'display_name': 'PC preset', 'overrides': {'variables': {'platform': 'PC'}}})
        body = make(ctx, variant_id=variant['id'])
        flow = {'preview': body, 'template_id': body['template_id'], 'route_id': body['route_id']}
        view._flows[1] = flow
        view._preview_screen(flow)
        await send(preview_action('prd:bindings'))
        state = flow['binding_state']
        await send(nb('variable', state['variables'].index('platform')))
        await send(nb('source', 1))  # Edition cannot satisfy the explicit PC preset.
        old = ctx.templates.get(ctx.template['id'])
        cb = await send(nb('confirm'))
        assert VARIANT_WARNING in cb.message.answer.call_args.args[0]
        assert ctx.templates.get(old['id']) == old
        assert flow['preview'] == body
        outdated = nb('confirm')
        await send('products')
        await send(outdated)
        assert ctx.templates.get(old['id']) == old
        assert ctx.store.list_drafts() == []
    asyncio.run(run())


@pytest.mark.parametrize('name', ['Platform', 'fields[platform]', 'region'])
def test_plain_binding_requires_exact_field_present_in_schema(plain_node, name):
    ctx = plain_node
    template = ctx.templates.get(ctx.template['id'])
    template['node_bindings']['platform'] = name
    ctx.templates.save(template, template['id'], template['revision'])
    with pytest.raises(NodeSelectionError):
        ctx.nd.node_setup(ctx.template['id'], ctx.route['id'])
    assert ctx.store.list_drafts() == []


def test_plain_binding_content_and_variant_conflicts(plain_node):
    ctx = plain_node
    with pytest.raises(NodeVariantConflictError):
        make(ctx, variables={'platform': 'Steam'})
    with pytest.raises(NodeVariantConflictError):
        make(ctx, overrides={'variables': {'edition': 'Deluxe Edition'}})
    variant = VariantService(ctx.store, ctx.routes).save({
        'template_id': ctx.template['id'], 'route_id': ctx.route['id'],
        'display_name': 'Incompatible', 'overrides': {'variables': {'edition': 'Missing Edition'}}})
    with pytest.raises(NodeVariantConflictError):
        ctx.nd.node_setup(ctx.template['id'], ctx.route['id'], variant['id'])
    variant['overrides']['variables']['edition'] = 'Deluxe Edition'
    variant = VariantService(ctx.store, ctx.routes).save(variant, variant['id'], variant['revision'])
    *_, values = ctx.nd.node_setup(ctx.template['id'], ctx.route['id'], variant['id'])
    assert values['edition'] == 'deluxe'
    assert make(ctx, values, variant_id=variant['id'])['variables']['edition'] == 'Deluxe Edition'
    assert ctx.store.list_drafts() == []


def test_dynamic_draft_roundtrip_no_hidden_and_identity(node):
    ctx=node; body=make(ctx); snapshot=deepcopy(body)
    assert ctx.nd.rebuild(body)==body
    row=save(ctx,body)
    assert body==snapshot
    assert row['node_schema']['node_id']==ctx.node_id
    assert all(r['html_name'].startswith('fields[') for r in row['node_fields'])
    assert all(key not in str(row) for key in ['SYNTHETIC_CSRF','form_created_at','offer_id','raw_html'])
    clone=ctx.nd.clone_preview(row['id'],row['revision'])
    assert 'id' not in clone and clone['node_fields']==row['node_fields']
    new=save(ctx,clone)
    assert new['id']!=row['id'] and ctx.store.get_draft(row['id'])==row
    assert all(c.args[0]=='get' and c.args[1]==f'lots/offerEdit?node={ctx.node_id}' for c in ctx.client.account.method.call_args_list)
    ctx.client.account.save_lot.assert_not_called()


def test_node_selection_and_optional_binding_not_label_guess(node):
    ctx=node
    template=ctx.templates.get(ctx.template['id']); template.pop('node_bindings')
    ctx.templates.save(template,template['id'],template['revision'])
    body=make(ctx)
    assert body['variables']['platform']==body['variables']['region']==body['variables']['edition']=='—'
    assert body['node_fields'][0]['option_label']=='PC'
    assert body['node_warning']
    assert 'Steam' not in body['payload']['title_en']
    assert body['variables']['product_name']==ctx.route['game_name']


def test_duplicate_labels_independent_clone_and_preview(node):
    ctx=node
    if ctx.node_id!=986: return
    *_,schema,values=ctx.nd.node_setup(ctx.template['id'],ctx.route['id'])
    values.update({'fields[edition]':'shadow','fields[edition_extra]':'deluxe'})
    source=save(ctx,make(ctx,values))
    clone=ctx.nd.clone_preview(source['id'],source['revision'])
    clone_values={r['html_name']:r['value'] for r in clone['node_fields']}
    clone_values['fields[region]']='us'
    changed=ctx.nd.create_node_preview(ctx.template['id'],ctx.route['id'],clone_values,price='15')
    selected={r['html_name']:r for r in changed['node_fields']}
    assert selected['fields[edition]']['value']=='shadow'
    assert selected['fields[edition_extra]']['value']=='deluxe'
    flow={'preview':changed}
    text,_=view._preview_screen(flow)
    assert 'Тип издания:' in text and 'Тип издания (2):' in text
    assert 'Shadow of the Erdtree' in text and 'Deluxe Edition' in text
    assert ctx.store.get_draft(source['id'])==source


def test_new_ui_dom_order_no_phantom_region_and_autofill(node):
    ctx=node
    async def run():
        await start(ctx)
        seen=[]
        while view._flows[1].get('step')=='nodefields':
            state=view._flows[1]['node_state']; seen.append(state['field'])
            await send(nf('keep'))
        assert seen==[f['html_name'] for f in view._flows[1]['node_state']['schema']['fields'] if not f['disabled']]
        assert view._flows[1]['field']=='quantity'
        if ctx.node_id==976: assert 'fields[region]' not in seen
        picker=view._flows[1]['picker']
        await send('prd:pv:'+picker['token']+':done:0')
        await ui.text_handler(message('15'))
        body=view._flows[1]['preview']
        assert body['variables']['product_name']==ctx.route['game_name']
        assert ctx.store.list_drafts()==[]
        await send(preview_action('prd:save'))
        assert len(ctx.store.list_drafts())==1
    asyncio.run(run())


def test_schema_refresh_old_callbacks_and_other_draft(node):
    ctx=node
    async def run():
        await start(ctx); old=nf('choose',1)
        old_generation=view._flows[1]['node_state']['schema']['generation']
        await send(nf('refresh'))
        assert view._flows[1]['node_state']['schema']['generation']!=old_generation
        pending=ui._interaction_state[1]; token=view._flows[1]['node_state']['token']
        await send(old)
        assert view._flows[1]['node_state']['token']==token and ui._interaction_state[1] is pending
        old=nf('keep')
        await start(ctx)
        pending=ui._interaction_state[1]
        await send(old)
        assert ui._interaction_state[1] is pending
        await send(nf('cancel')); await send(old)
        assert not view._flows and ctx.store.list_drafts()==[]
    asyncio.run(run())


def test_failed_fetch_and_refresh_never_generic_fallback(node):
    ctx=node
    async def run():
        await start(ctx)
        ctx.client.account.method.side_effect=TimeoutError('sensitive synthetic server text')
        cb=await send(nf('refresh'))
        assert 'Не удалось получить актуальные параметры' in str(cb.message.answer.call_args)
        assert 'Код диагностики: REQUEST_FAILED' in str(cb.message.answer.call_args)
        assert 'sensitive' not in str(cb.message.answer.call_args)
        assert view._flows[1]['node_state']['phase']=='error'
        assert 1 not in ui._interaction_state
        await send(nf('cancel'))
        cb=await start(ctx)
        assert 'Не удалось получить актуальные параметры' in str(cb.message.answer.call_args)
        assert ctx.store.list_drafts()==[]
    asyncio.run(run())


def test_changed_options_fail_closed_labels_can_refresh(node):
    ctx=node; source=save(ctx,make(ctx))
    ctx.html=ctx.html.replace('>PC<','>Personal computer<')
    NodeFormService(ctx.client,ctx.store).get(ctx.node_id,force=True)
    refreshed=ctx.nd.refresh_preview(source)
    assert refreshed['node_fields'][0]['value']=='pc'
    assert refreshed['node_fields'][0]['option_label']=='Personal computer'
    ctx.html=ctx.html.replace('value="pc"','value="new-pc"')
    NodeFormService(ctx.client,ctx.store).get(ctx.node_id,force=True)
    with pytest.raises(ValueError,match='FunPay изменил'):
        ctx.nd.clone_preview(source['id'],source['revision'])
    with pytest.raises(ValueError): ctx.nd.rebuild(source)
    assert ctx.store.get_draft(source['id'])==source


def test_variant_preset_conflict_and_legacy_binding(node):
    ctx=node
    second='fields[edition_extra]' if ctx.node_id==986 else 'fields[edition]'
    new_value='deluxe' if ctx.node_id==986 else 'ultimate'
    variant=VariantService(ctx.store,ctx.routes).save({'template_id':ctx.template['id'],'route_id':ctx.route['id'],
        'display_name':'New edition','node_values':{second:new_value},'overrides':{'emoji':'⭐'}})
    source=save(ctx,make(ctx,input_values={}))
    special=ctx.nd.switch_variant(source,variant['id'])
    assert next(r for r in special['node_fields'] if r['html_name']==second)['value']==new_value
    assert special['emoji']=='⭐'
    base=ctx.nd.switch_variant(special,None)
    assert base['node_fields']==source['node_fields']
    assert ctx.store.get_draft(source['id'])==source
    # A content Edition override cannot disagree with an explicitly bound node field.
    legacy=VariantService(ctx.store,ctx.routes).save({'template_id':ctx.template['id'],'route_id':ctx.route['id'],
        'display_name':'Incompatible','overrides':{'variables':{'edition':'Impossible edition'}}})
    with pytest.raises(ValueError): ctx.nd.node_setup(ctx.template['id'],ctx.route['id'],legacy['id'])


def test_clone_ui_edits_one_technical_field_keeps_source(node):
    ctx=node; source=save(ctx,make(ctx))
    async def run():
        await send('prd:open:'+source['id']); await send(preview_action('prd:clone'))
        flow=view._flows[1]; names=flow['node_edit_fields']
        field='fields[region]' if ctx.node_id==986 else 'fields[platform]'
        await send(view._local_action(flow,'node',names.index(field)))
        state=view._flows[1]['node_state']; fields=[f for f in state['schema']['fields'] if not f['disabled']]
        assert fields[state['index']]['html_name']==field
        old=nf('choose',1); await send(old)
        assert view._flows[1].get('similar_token')
        result=view._flows[1]['preview']
        assert len(ctx.store.list_drafts())==1
        assert all(row['value']==next(r for r in source['node_fields'] if r['html_name']==row['html_name'])['value']
                   for row in result['node_fields'] if row['html_name']!=field)
        save_action=view._local_action(view._flows[1],'save')
        await asyncio.gather(send(save_action),send(save_action))
        assert len(ctx.store.list_drafts())==2
        assert ctx.store.get_draft(source['id'])==source
        await send(old)
        assert len(ctx.store.list_drafts())==2
    asyncio.run(run())


def test_new_mass_needs_resolution_legacy_store_untouched(node):
    ctx=node; source=save(ctx,make(ctx))
    result=ctx.nd.preview_bulk(ctx.template['id'],'Game | 1\nGame | 2')
    assert result['previews']==[]
    assert all(r['status']=='needs_resolution' for r in result['errors'])
    assert ctx.store.list_drafts()==[source]


def test_review_technical_variant_value_and_cancelled_lifecycle(node):
    ctx=node
    value='base' if ctx.node_id==986 else 'standard'
    variant=VariantService(ctx.store,ctx.routes).save({'template_id':ctx.template['id'],'route_id':ctx.route['id'],
        'display_name':'Technical','overrides':{'variables':{'edition':value}}})
    *_,schema,defaults=ctx.nd.node_setup(ctx.template['id'],ctx.route['id'],variant['id'])
    body=ctx.nd.create_node_preview(ctx.template['id'],ctx.route['id'],defaults,price='15',variant_id=variant['id'])
    source=save(ctx,body)
    cancelled=ctx.store.cancel_draft(source['id'],source['revision'])
    assert ctx.nd.refresh_preview(cancelled)['status']=='cancelled'
    async def run():
        cb=await send('prd:open:'+source['id'])
        assert 'prd:clone:' not in str(cb.message.edit_text.call_args)
        save_action=preview_action('prd:save')
        await send(preview_action('prd:edit:price'))
        await send(save_action)
        assert ctx.store.get_draft(source['id'])==cancelled
    asyncio.run(run())


def test_review_existing_dirty_save_rechecks_schema(node):
    ctx=node; source=save(ctx,make(ctx))
    async def run():
        await send('prd:open:'+source['id']); await send(preview_action('prd:edit:price'))
        await ui.text_handler(message('20'))
        ctx.html=ctx.html.replace('value="pc"','value="new-pc"')
        NodeFormService(ctx.client,ctx.store).get(ctx.node_id,force=True)
        await send(preview_action('prd:save'))
        assert ctx.store.get_draft(source['id'])==source
    asyncio.run(run())


def test_review_optional_omission_survives_similar_other_edit(node):
    ctx=node
    *_,schema,values=ctx.nd.node_setup(ctx.template['id'],ctx.route['id'])
    values.pop('fields[edition]')
    source=save(ctx,make(ctx,values,input_values={}))
    async def run():
        await send('prd:open:'+source['id']); await send(preview_action('prd:clone'))
        flow=view._flows[1]
        await send(view._local_action(flow,'node',flow['node_edit_fields'].index('fields[platform]')))
        await send(nf('choose',1))
        assert 'fields[edition]' not in {r['html_name'] for r in view._flows[1]['preview']['node_fields']}
        assert ctx.store.get_draft(source['id'])==source
    asyncio.run(run())


def test_verified_variant_other_node_uses_current_schema(node):
    ctx=node
    alternate=SimpleNamespace(id=9000,name='Other section',type=ctx.nodes[0].type,
        category=next(n.category for n in ctx.nodes if n.id==ctx.node_id))
    ctx.nodes.append(alternate)
    original=ctx.client.account.method.side_effect
    def get(verb,url,*args,**kwargs):
        response=original(verb,url,*args,**kwargs)
        if url.endswith('=9000'): response.content=ctx.html.replace('value="'+str(ctx.node_id)+'"','value="9000"').encode()
        return response
    ctx.client.account.method.side_effect=get
    variant=VariantService(ctx.store,ctx.routes).save({'template_id':ctx.template['id'],'route_id':ctx.route['id'],
        'display_name':'Other section','route_override_url':'https://funpay.com/lots/9000/'})
    base=make(ctx)
    other=ctx.nd.switch_variant(base,variant['id'])
    assert other['node']==9000 and other['node_schema']['node_id']==9000
    assert other['node_fields']==base['node_fields']


@pytest.mark.parametrize('bad',[{'csrf_token':'synthetic'}, {'fields[platform]':'wrong'}])
def test_unreviewed_provenance_cannot_be_persisted(node,bad):
    ctx=node
    with pytest.raises(ValueError): make(ctx,input_values=bad)
    assert ctx.store.list_drafts()==[]


def test_dynamic_variable_prompt_excludes_fake_generic_input(node):
    ctx=node; source=save(ctx,make(ctx))
    async def run():
        await send('prd:open:'+source['id'])
        cb=await send(preview_action('prd:edit:variables'))
        text=cb.message.answer.call_args.args[0]
        assert 'platform=Steam' not in text and 'region=Global' not in text
        before=deepcopy(view._flows[1]['preview'])
        await ui.text_handler(message('region=EU'))
        assert view._flows[1]['preview']==before
        await ui.text_handler(message('quantity=5'))
        assert view._flows[1]['preview']['payload']['amount']==5
        assert ctx.store.get_draft(source['id'])==source
    asyncio.run(run())


def test_custom_binding_legacy_clone_can_change_real_select(node):
    ctx=node
    ctx.html=ctx.html.replace('</form>','<div class="form-group"><label for="custom">Activation</label><select id="custom" name="fields[activation]"><option value="online" selected>Online</option><option value="offline">Offline</option></select></div></form>')
    template=ctx.templates.get(ctx.template['id'])
    template['allowed_variables'].append('activation')
    template['defaults']['activation']='Online'
    template['node_bindings']['activation']='fields[activation]'
    for language in ('ru','en'): template['description_'+language]+=' {{activation}}'
    ctx.template=ctx.templates.save(template,template['id'],template['revision'])
    legacy=ctx.drafts.create_preview(ctx.template['id'],ctx.route['id'],variables={'activation':'Online'},price='15')
    source=ctx.drafts.confirm(ctx.drafts.stage([legacy])['token'])['results'][0]
    async def run():
        await send('prd:open:'+source['id']); await send(preview_action('prd:clone'))
        while view._flows[1].get('step')=='nodefields':
            if view._flows[1]['node_state']['field']=='fields[activation]':
                await send(nf('choose',1))
            else: await send(nf('keep'))
        assert view._flows[1]['preview']['variables']['activation']=='Offline'
        assert 'Offline' in view._flows[1]['preview']['payload']['description_en']
        assert ctx.store.get_draft(source['id'])==source
    asyncio.run(run())


def test_real_select_callbacks_use_technical_identity_and_preview(node):
    ctx=node
    async def run():
        await start(ctx)
        previous=None
        while view._flows[1].get('step')=='nodefields':
            state=view._flows[1]['node_state']
            key=state['field']
            if previous:
                pending=ui._interaction_state[1]
                await send(previous)
                assert ui._interaction_state[1] is pending
            previous=nf('choose',1)
            if key in {'fields[edition]','fields[edition_extra]'}:
                await send(previous)
            else: await send(nf('keep'))
        await send('prd:pv:'+view._flows[1]['picker']['token']+':done:0')
        await ui.text_handler(message('15'))
        result=view._flows[1]['preview']
        values={r['html_name']:r['value'] for r in result['node_fields']}
        assert values['fields[edition]']==('shadow' if ctx.node_id==986 else 'ultimate')
        if ctx.node_id==986:
            assert values['fields[edition_extra]']=='deluxe'
            text,_=view._preview_screen(view._flows[1])
            assert 'Тип издания (2)' in text and 'Shadow of the Erdtree' in text and 'Deluxe Edition' in text
    asyncio.run(run())


def test_reopen_stale_options_show_specific_resolution_message(node):
    ctx=node; source=save(ctx,make(ctx))
    ctx.html=ctx.html.replace('value="pc"','value="new-pc"')
    NodeFormService(ctx.client,ctx.store).get(ctx.node_id,force=True)
    async def run():
        cb=await send('prd:open:'+source['id'])
        assert STALE_WARNING in str(cb.message.answer.call_args)
        assert ctx.store.get_draft(source['id'])==source
    asyncio.run(run())


@pytest.mark.parametrize('error,expected', [(NodeFormError, 'Не удалось получить актуальные параметры'),
                                          (NodeSelectionError, STALE_WARNING),
                                          (NodeVariantConflictError, VARIANT_WARNING)])
def test_clone_preserves_specific_node_error_message(node, monkeypatch, error, expected):
    ctx=node; source=save(ctx,make(ctx))
    async def run():
        await send('prd:open:'+source['id'])
        def fail(*args, **kwargs): raise error()
        monkeypatch.setattr(NodeDraftService, 'clone_preview', fail)
        cb=await send(preview_action('prd:clone'))
        assert expected in str(cb.message.answer.call_args)
        assert ctx.store.get_draft(source['id'])==source
    asyncio.run(run())


def test_save_changed_review_requires_specific_resolution(node, monkeypatch):
    ctx=node; source=save(ctx,make(ctx))
    async def run():
        await send('prd:open:'+source['id'])
        flow=view._flows[1]; flow['dirty']=True
        def changed(*args): return {**flow['preview'], 'node_warning':'changed'}
        monkeypatch.setattr(NodeDraftService, 'rebuild', changed)
        cb=await send(preview_action('prd:save'))
        assert STALE_WARNING in str(cb.message.answer.call_args)
        assert ctx.store.get_draft(source['id'])==source
    asyncio.run(run())
