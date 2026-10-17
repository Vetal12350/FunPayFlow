"""Token/index UI for real visible node selects. No FunPay mutation actions."""
import asyncio
from html import escape
import uuid

from .node_form import NodeFormError, NodeFormService
from .node_products import LEGACY_NODE_VARIABLES, STALE_WARNING, NodeSelectionError, selected_fields


def _view():
    from . import telegram_products
    return telegram_products


def action(state, operation, value=0):
    return f"prd:nf:{state['token']}:{operation}:{value}"


def _context(user_id, flow):
    return _view()._edit_context(user_id, flow)


async def error(message, user_id, flow, text='Не удалось получить актуальные параметры раздела FunPay.'):
    view = _view()
    state = flow['node_state']
    state.update(token=uuid.uuid4().hex[:12], phase='error')
    view._ui()._interaction_state.pop(user_id, None)
    await message.answer(text,
        reply_markup=view._menu([[('🔄 Обновить параметры', action(state, 'refresh'))],
                                 [('❌ Отмена', action(state, 'cancel'))]]))


async def begin(message, user_id, flow, drafts, *, mode='wizard', force=False, field_index=None):
    view = _view()
    old = flow.get('node_state', {})
    state = {'token': uuid.uuid4().hex[:12], 'context': _context(user_id, flow),
             'variant_id': flow.get('variant_id') if mode == 'wizard' else flow['preview'].get('variant_id'),
             'mode': mode, 'index': field_index or 0, 'single': field_index is not None, 'offset': 0, 'phase': 'fields'}
    flow['node_state'] = state
    flow.pop('picker', None)
    flow.pop('node_ready', None)
    try:
        template, route, variant, schema, defaults = await asyncio.to_thread(drafts.node_setup,
            flow['template_id'], flow['route_id'], state['variant_id'], force)
        if view._flows.get(user_id) is not flow:
            return False
        values = ({row['html_name']: row['value'] for row in flow['preview'].get('node_fields', [])}
                  if mode == 'similar' else old.get('values', {}))
        chosen = values if mode == 'similar' and 'node_fields' in flow['preview'] else {**defaults, **values}
        state.update(schema=schema, values=chosen, explicit=old.get('explicit', {}) if mode == 'wizard'
                     else flow['preview'].get('node_input_values', {}))
        known = {f['html_name']: f for f in schema['fields'] if not f['disabled']}
        state['values'] = {key: value for key, value in state['values'].items() if key in known
            and any(o['value'] == value and not o['disabled'] for o in known[key]['options'])}
        state['explicit'] = {k:v for k,v in state['explicit'].items() if k in state['values'] and state['values'][k] == v}
        if values and state['values'] != values:
            await message.answer(STALE_WARNING)
        return await show(message, user_id, flow, drafts)
    except NodeSelectionError:
        if view._flows.get(user_id) is flow:
            await error(message, user_id, flow, STALE_WARNING)
        return False
    except NodeFormError:
        if view._flows.get(user_id) is flow:
            await error(message, user_id, flow)
        return False


async def show(message, user_id, flow, drafts):
    view = _view(); state = flow['node_state']
    fields = [f for f in state['schema']['fields'] if not f['disabled']]
    if state['index'] >= len(fields):
        selected_fields(state['schema'], state['values'])
        if state['mode'] == 'wizard':
            flow['node_ready'] = True
            flow['field_index'] = 0
            _, templates, _, _ = view._kit(user_id)
            return await view._wizard_prompt(message, user_id, flow, templates)
        body = flow['preview']
        template = await asyncio.to_thread(drafts.templates.get, body['template_id'])
        node_variables = LEGACY_NODE_VARIABLES | set(template.get('node_bindings', {}))
        variables = {k:v for k,v in drafts.variable_input(body).items() if k not in node_variables}
        overrides = dict(body.get('overrides', {}))
        if 'variables' in overrides:
            overrides['variables'] = {k:v for k,v in overrides['variables'].items() if k not in node_variables}
        preview = await asyncio.to_thread(drafts.create_node_preview, body['template_id'], body['route_id'], state['values'],
            variables=variables, price=body['payload']['price'], overrides=overrides, variant_id=body.get('variant_id'),
            schema=state['schema'], input_values=state['explicit'])
        flow['preview'] = preview
        _, templates, _, _ = view._kit(user_id)
        return await view._similar_screen(message, user_id, flow, templates, drafts)
    field = fields[state['index']]
    state.update(token=uuid.uuid4().hex[:12], field=field['html_name'], phase='fields')
    options = field['options']; start = state.get('offset', 0); rows = []
    for i, option in enumerate(options[start:start+8], start):
        if not option['disabled'] and (option['value'] or not field['required']):
            rows.append([(option['label'][:60] or '(пусто)', action(state, 'choose', i))])
    if start:
        rows.append([('⬅️ Страница', action(state, 'page', max(0, start-8)))])
    if start+8 < len(options):
        rows.append([('Далее ➡️', action(state, 'page', start+8))])
    current = next((o for o in options if o['value'] == state['values'].get(field['html_name']) and not o['disabled']), None)
    if current and (current['value'] or not field['required']):
        rows.insert(0, [('✅ Оставить ' + (current['label'][:35] or '(пусто)'), action(state, 'keep'))])
    if not field['required']:
        rows.append([('Пропустить необязательное', action(state, 'skip'))])
    rows += [[('🔄 Обновить параметры', action(state, 'refresh'))],
             [('⬅️ Назад', action(state, 'back'))], [('❌ Отмена', action(state, 'cancel'))]]
    view._pending(user_id, flow, 'nodefields')
    await message.answer('<b>' + escape(field['display_label']) + '</b>\n'
        + ('Обязательный параметр FunPay.' if field['required'] else 'Параметр FunPay.'),
        parse_mode='HTML', reply_markup=view._menu(rows))
    return False


async def validate(user_id, flow, parts):
    state = flow.get('node_state')
    if len(parts) != 5 or not state or parts[2] != state['token'] or state['context'] != _context(user_id, flow):
        raise ValueError('Экран параметров устарел.')
    if parts[3] not in {'refresh','cancel','back'}:
        store, _, routes, _ = _view()._kit(user_id)
        schema = await asyncio.to_thread(NodeFormService(routes.client,store).get, state['schema']['node_id'])
        if schema['generation'] != state['schema']['generation']:
            raise NodeFormError()
    return state


async def handle(callback, user_id, flow, state, operation, value, drafts):
    view = _view(); message = callback.message
    if operation == 'cancel':
        view.discard_session(user_id)
        await view._show(callback, *view._home())
    elif operation == 'refresh':
        await begin(message, user_id, flow, drafts, mode=state['mode'], force=True)
    elif operation == 'back':
        if state['index']:
            state.update(index=state['index']-1, offset=0)
            await show(message,user_id,flow,drafts)
        elif state['mode'] == 'similar' and 'node_fields' in flow['preview']:
            _,templates,_,_=view._kit(user_id)
            await view._similar_screen(message,user_id,flow,templates,drafts)
        else:
            view.discard_session(user_id)
            await view._show(callback,*view._home())
    elif state['phase'] == 'fields':
        fields = [f for f in state['schema']['fields'] if not f['disabled']]
        field = fields[state['index']]
        if field['html_name'] != state['field']:
            raise ValueError('Поле устарело.')
        if operation == 'page':
            page = int(value)
            if page < 0 or page % 8 or page >= len(field['options']):
                raise ValueError('Страница устарела.')
            state['offset'] = page
            await show(message,user_id,flow,drafts)
            return
        if operation == 'keep':
            selected = state['values'].get(field['html_name'])
            if selected is None and field['required']:
                raise ValueError('Выберите обязательный параметр.')
        elif operation == 'skip' and not field['required']:
            state['values'].pop(field['html_name'],None)
            state['explicit'].pop(field['html_name'],None)
            selected = None
        elif operation == 'choose' and state['offset'] <= int(value) < min(len(field['options']),state['offset']+8):
            selected = field['options'][int(value)]['value']
        else:
            raise ValueError('Выбор устарел.')
        if selected is not None:
            option = next((o for o in field['options'] if o['value'] == selected and not o['disabled']),None)
            if option is None or (field['required'] and not selected):
                raise ValueError('Недоступный option.')
            state['values'][field['html_name']] = selected
            if operation == 'choose': state['explicit'][field['html_name']] = selected
        state.update(index=len(fields) if state.get('single') else state['index']+1,offset=0,token=uuid.uuid4().hex[:12])
        await show(message,user_id,flow,drafts)
    else:
        raise ValueError('Экран устарел.')


async def text(message,user_id,flow,value,drafts):
    """Compatibility input accepts only an actual enabled option or '-' default."""
    state=flow['node_state']
    await validate(user_id,flow,['prd','nf',state['token'],'keep','0'])
    if value == '-':
        operation,index='keep','0'
    else:
        fields=[f for f in state['schema']['fields'] if not f['disabled']]
        field=fields[state['index']]
        matches=[i for i,o in enumerate(field['options']) if not o['disabled'] and value in {o['value'],o['label']}]
        if len(matches)!=1: raise ValueError('Выберите реальный option.')
        index=matches[0]
        state['offset']=(index//8)*8
        operation,index='choose',str(index)
    class Callback:
        pass
    cb=Callback(); cb.message=message
    await handle(cb,user_id,flow,state,operation,index,drafts)
