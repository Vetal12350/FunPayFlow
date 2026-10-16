"""Owner-only local product workflows. No FunPay modifying operation exists here."""
import asyncio
from copy import deepcopy
from html import escape
import json
import re
import sqlite3

from .game_routes import RouteService
from .product_store import ProductStore
from .product_templates import TemplateService, default_template, validate_overrides
from .product_drafts import DraftService, validate_price
from .product_variants import VariantService
from .state import StateError


_flows: dict[int, dict] = {}
_locks: dict[int, tuple] = {}
_PAGE = 8
_TITLES = {'platform': 'Platform', 'region': 'Region', 'edition': 'Edition',
           'quantity': 'Quantity', 'product_name': 'Product name', 'price': 'Price'}


def _ui():
    from . import telegram
    return telegram


def discard_session(user_id):
    """Abandon input; old confirmation buttons cannot revive this session."""
    _flows.pop(user_id, None)


def _lock(user_id):
    loop = asyncio.get_running_loop()
    if user_id not in _locks or _locks[user_id][0] is not loop:
        _locks[user_id] = (loop, asyncio.Lock())
    return _locks[user_id][1]


def _kit(user_id):
    client = _ui()._runtime_client
    if client is None or client.review_state is None:
        raise ValueError('Хранилище недоступно.')
    store = ProductStore(client.review_state, user_id, client.account.id)
    routes = RouteService(client, store)
    return store, TemplateService(store), routes, DraftService(store, routes)


def _menu(rows):
    ui = _ui()
    return ui._menu([[ui._button(text, action) for text, action in row] for row in rows])


def _home():
    return ('📦 <b>Лоты · локальные черновики</b>\n'
            'Product Template + Game Route + variables = RU/EN draft.\n'
            'Публикация FunPay недоступна. Существующие лоты не изменяются.', _menu([
                [('➕ Создать', 'prd:choose:single:0')],
                [('📚 Массовые drafts', 'prd:choose:bulk:0')],
                [('📝 Сохранённые drafts', 'prd:drafts:0')],
                [('🗺 Game Routes', 'prd:choose:routes:0')],
                [('🧩 Product Templates', 'prd:templates:0')],
                [('📝 Черновики из собственного лота', 'lot_templates')],
                [('🔙 Главное меню', 'menu_main')],
            ]))


async def _show(callback, text, markup):
    await _ui()._edit_or_keep_menu(callback.message, text, markup)


def _pending(user_id, flow, step):
    if _flows.get(user_id) is not flow:
        raise ValueError('Экран устарел.')
    flow['step'] = step
    _ui()._interaction_state[user_id] = {'action': 'product_input', 'flow': flow}


def _active(user_id):
    flow = _flows.get(user_id)
    if flow is None:
        raise ValueError('Экран устарел. Откройте раздел «Лоты» заново.')
    return flow


def _page_buttons(command, offset, more):
    rows = []
    if offset:
        rows.append([('⬅️ Назад', f'{command}:{max(0, offset - _PAGE)}')])
    if more:
        rows.append([('Далее ➡️', f'{command}:{offset + _PAGE}')])
    return rows


async def _template_picker(callback, purpose, offset, templates):
    if purpose not in {'single', 'bulk', 'routes'}:
        raise ValueError('Неизвестный раздел.')
    rows = await asyncio.to_thread(templates.list, limit=_PAGE + 1, offset=offset)
    keyboard = [[(row['display_name'][:45], f"prd:pick:{purpose}:{row['id']}")]
                for row in rows[:_PAGE]]
    keyboard += _page_buttons(f'prd:choose:{purpose}', offset, len(rows) > _PAGE)
    keyboard.append([('🔙 Лоты', 'products')])
    await _show(callback, '🧩 <b>Выберите Product Template</b>', _menu(keyboard))


async def _route_list(callback, user_id, store, offset=0, recent=False):
    flow = _active(user_id)
    rows = await asyncio.to_thread(store.list_routes, template_id=flow['template_id'],
                                   query='' if recent else flow.get('query', ''), limit=_PAGE + 1,
                                   offset=offset, recent=recent)
    keyboard = [[(f"{row['game_name'][:32]} · {row['section_name'][:20]}",
                  f"prd:route:{row['id']}")] for row in rows[:_PAGE]]
    keyboard += _page_buttons('prd:recent' if recent else 'prd:routepage', offset,
                              len(rows) > _PAGE)
    keyboard += [[('🔎 Поиск игры / alias', 'prd:search')],
                 [('🕘 Недавние', 'prd:recent:0')]]
    if flow['purpose'] == 'routes':
        keyboard += [[('➕ URL / массовый import', 'prd:routeimport')],
                     [('🎮 Каталог FunPay', 'prd:catalog:0')],
                     [('Из собственного лота', 'prd:own:0')]]
    keyboard.append([('🔙 Лоты', 'products')])
    title = '🕘 Недавние / последние routes' if recent else '🗺 Выберите Game Route'
    await _show(callback, f'<b>{title}</b>\n'
                f"Найденные маршруты · страница {offset // _PAGE + 1}.\n"
                'Нет игры? Добавьте соответствие в Game Routes.', _menu(keyboard))


def _route_screen(row):
    text = ('🗺 <b>Game Route</b>\n'
            f"Игра: {escape(row['game_name'][:150])}\n"
            f"Раздел: {escape(row['section_name'][:150])} · node {row['node_id']}\n"
            f"Aliases: {escape(', '.join(row.get('game_aliases', []))[:250])}\n"
            f"Emoji: {escape(row.get('overrides', {}).get('emoji', 'template default'))}")
    return text, _menu([[('Aliases / emoji', f"prd:routeedit:{row['id']}")],
                        [('🎚 Варианты / Editions', f"prd:variants:{row['id']}:0")],
                        [('🔙 Routes', 'prd:routepage:0')], [('Лоты', 'products')]])


def _preview_screen(flow):
    body = flow['preview']; p = body['payload']
    clip = _ui()._escaped_preview
    text = ('👁 <b>RU + EN draft</b>\n'
            f"Game: {clip(body['game'], 80)}\n"
            f"Template: {clip(body['template_name'], 60)}\n"
            + (f"Variant: {clip(body['variant_name'], 80)}\n" if body.get('variant_id') else '') +
            f"Section: {clip(body['section'], 80)} · node {body['node']}\n"
            f"Emoji: {clip(body['emoji'], 64)}\n"
            f"RU title: {clip(p['title_ru'], 200)}\n"
            f"EN title: {clip(p['title_en'], 200)}\n"
            f"RU description: {clip(p['description_ru'], 200)}\n"
            f"EN description: {clip(p['description_en'], 200)}\n"
            f"Price: {escape(p['price'])} · Quantity: {p['amount']}\n"
            f"Variables: {clip(json.dumps(body['variables'], ensure_ascii=False), 200)}\n\n"
            'Тексты здесь сокращены; полный RU/EN доступен ниже.\n'
            'Сохраняется только локальный draft. PUBLISH отсутствует.')
    rows = [[('💾 Сохранить draft', 'prd:save')],
            [('💵 Цена', 'prd:edit:price'), ('Emoji', 'prd:edit:emoji')],
            [('Изменить RU', 'prd:edit:ru'), ('Изменить EN', 'prd:edit:en')],
            [('Variables', 'prd:edit:variables')],
            [('Полный RU', 'prd:text:ru'), ('Полный EN', 'prd:text:en')],
            [('❌ Cancel', 'prd:discard')], [('🔙 Лоты', 'products')]]
    if body.get('status') == 'cancelled':
        rows = [[('🔙 Лоты', 'products')]]
    return text, _menu(rows)


async def _wizard_prompt(message, user_id, flow, templates):
    template = await asyncio.to_thread(templates.get, flow['template_id'])
    defaults = dict(template['defaults'])
    texts = {key: template[key] for key in ('title_ru', 'title_en', 'description_ru', 'description_en')}
    if flow.get('route_id'):
        store, _, routes, _ = _kit(user_id)
        route = await asyncio.to_thread(routes.resolve_route, flow['route_id'])
        inherited = [route.get('overrides', {})]
        if flow.get('variant_id'):
            variant = await asyncio.to_thread(VariantService(store, routes).get, flow['variant_id'])
            if variant['route_id'] != route['id']:
                raise ValueError('Вариант другого маршрута.')
            inherited.append(variant.get('overrides', {}))
        for override in inherited:
            defaults.update(override.get('variables', {}))
            texts.update({key: override[key] for key in texts if key in override})
    used = set(re.findall(r'\{\{([a-z][a-z0-9_]*)\}\}', ' '.join(
        texts.values()))) | {'quantity'}
    fields = [key for key in template['allowed_variables'] if key != 'game' and key in used]
    if flow['purpose'] == 'single':
        fields.append('price')
    index = flow.get('field_index', 0)
    if index < len(fields):
        field = fields[index]
        flow['field'] = field
        _pending(user_id, flow, 'variables')
        default = defaults.get(field, 'название игры' if field == 'product_name' else 'обязательно')
        await message.answer(f"{_TITLES.get(field, field)} [{default}]\n"
                             'Введите значение; «-» — оставить default. ❌ Отмена — выйти.',
                             parse_mode=None)
        return False
    return True


async def _variant_picker(callback, user_id, variants, offset=0):
    flow = _active(user_id)
    rows = await asyncio.to_thread(variants.list, flow['route_id'], limit=_PAGE + 1, offset=offset)
    keyboard = [[(row['display_name'][:50], f"prd:variantpick:{row['id']}")] for row in rows[:_PAGE]]
    keyboard += _page_buttons('prd:variantpage', offset, len(rows) > _PAGE)
    keyboard += [[('Без варианта / defaults', 'prd:variantpick:base')], [('🔙 Игры', 'prd:routepage:0')]]
    await _show(callback, '🎚 <b>Выберите вариант</b>', _menu(keyboard))


async def _stage_screen(message, user_id, flow, kind, bodies, details, store, drafts):
    if not bodies:
        await message.answer(details + '\nНет готовых строк для сохранения.', parse_mode=None)
        return
    batch = await asyncio.to_thread(store.stage_batch if kind == 'routes' else drafts.stage,
                                    *([kind, bodies] if kind == 'routes' else [bodies]))
    if _flows.get(user_id) is not flow:
        await asyncio.to_thread(store.cancel_batch, batch['token'])
        return
    flow.update(batch_token=batch['token'], kind=kind)
    _ui()._interaction_state.pop(user_id, None)
    await message.answer(details + '\nСохранить готовые строки только локально?',
                         parse_mode=None, reply_markup=_menu([
                             [('✅ Подтвердить', f"prd:confirm:{batch['token']}")],
                             [('❌ Cancel', f"prd:cancel:{batch['token']}")],
                             [('Лоты', 'products')]]))


def _mapping(text):
    if len(text) > 10000:
        raise ValueError('Слишком длинный ввод.')
    result = {}
    for line in text.splitlines():
        key, separator, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if not separator or not key or key in result or not value:
            raise ValueError('Ожидаются уникальные строки variable=value.')
        result[key] = value
    return result


async def products_callback(callback, action, user_id):
    if action != 'products' and not action.startswith('prd:'):
        return False
    async with _lock(user_id):
        return await _products_callback(callback, action, user_id)


async def _products_callback(callback, action, user_id):
    ui = _ui()
    if not ui._owner_id(user_id):
        await callback.answer('Доступно только владельцу.', show_alert=True)
        return True
    await callback.answer()
    ui._interaction_state.pop(user_id, None)
    try:
        store, templates, routes, drafts = _kit(user_id)
        await asyncio.to_thread(templates.ensure_builtin)
        parts = action.split(':')
        command = parts[1] if len(parts) > 1 else 'home'
        if command == 'home':
            discard_session(user_id)
            await _show(callback, *_home())
        elif command == 'choose':
            await _template_picker(callback, parts[2], int(parts[3]), templates)
        elif command == 'pick':
            purpose, template_id = parts[2:4]
            await asyncio.to_thread(templates.get, template_id)
            if purpose not in {'single', 'bulk', 'routes'}:
                raise ValueError('Недоступное действие.')
            flow = {'purpose': purpose, 'template_id': template_id, 'variables': {}, 'query': ''}
            _flows[user_id] = flow
            if purpose == 'bulk':
                await _wizard_prompt(callback.message, user_id, flow, templates)
            else:
                await _route_list(callback, user_id, store)
        elif command in {'routepage', 'recent'}:
            await _route_list(callback, user_id, store, int(parts[2]), command == 'recent')
        elif command == 'search':
            flow = _active(user_id); _pending(user_id, flow, 'search')
            await callback.message.answer('Введите название игры или alias. ❌ Отмена — выйти.')
        elif command == 'route':
            flow = _active(user_id)
            row = await asyncio.to_thread(routes.resolve_route, parts[2])
            if row['template_id'] != flow['template_id']:
                raise ValueError('Маршрут относится к другому шаблону.')
            if flow['purpose'] == 'routes':
                flow['route_id'] = row['id']
                await _show(callback, *_route_screen(row))
            else:
                flow.update(route_id=row['id'], variables={}, field_index=0)
                flow.pop('variant_id', None)
                variants = VariantService(store, routes)
                if await asyncio.to_thread(variants.list, row['id'], limit=1):
                    await _variant_picker(callback, user_id, variants)
                else:
                    await _wizard_prompt(callback.message, user_id, flow, templates)
        elif command in {'variantpage', 'variantpick'}:
            flow = _active(user_id)
            if flow.get('purpose') != 'single' or not flow.get('route_id'):
                raise ValueError('Выберите игру.')
            variants = VariantService(store, routes)
            if command == 'variantpage':
                await _variant_picker(callback, user_id, variants, int(parts[2]))
            else:
                variant = None if parts[2] == 'base' else await asyncio.to_thread(variants.get, parts[2])
                if variant is not None and variant['route_id'] != flow['route_id']:
                    raise ValueError('Вариант другого маршрута.')
                flow.update(variables={}, field_index=0)
                flow.pop('variant_id', None)
                if variant is not None:
                    flow['variant_id'] = variant['id']
                await _wizard_prompt(callback.message, user_id, flow, templates)
        elif command == 'variants':
            flow = _active(user_id)
            flow.pop('variant_pending', None)
            row = await asyncio.to_thread(routes.resolve_route, parts[2])
            if flow.get('purpose') != 'routes' or row['template_id'] != flow['template_id']:
                raise ValueError('Откройте Game Routes.')
            flow['route_id'] = row['id']
            offset = int(parts[3])
            rows = await asyncio.to_thread(VariantService(store, routes).list, row['id'],
                                           limit=_PAGE + 1, offset=offset, enabled_only=False)
            keyboard = [[(('✅ ' if v['enabled'] else '⏸ ') + v['display_name'][:45],
                          f"prd:variantedit:{v['id']}")] for v in rows[:_PAGE]]
            for buttons, variant in zip(keyboard, rows):
                if variant['enabled']:
                    buttons.append(('⏸', f"prd:variantdisable:{variant['id']}:{variant['revision']}"))
            keyboard += _page_buttons(f"prd:variants:{row['id']}", offset, len(rows) > _PAGE)
            keyboard += [[('➕ Новый variant', 'prd:variantnew')], [('🔙 Route', f"prd:route:{row['id']}")]]
            await _show(callback, '🎚 <b>Варианты / Editions</b>\n'
                        'Без варианта используются прежние defaults. Отключение сохраняет старые drafts.', _menu(keyboard))
        elif command in {'variantedit', 'variantnew'}:
            flow = _active(user_id)
            if flow.get('purpose') != 'routes' or not flow.get('route_id'):
                raise ValueError('Откройте Game Routes.')
            old = None if command == 'variantnew' else await asyncio.to_thread(
                VariantService(store, routes).get, parts[2], enabled_only=False)
            if old is not None and old['route_id'] != flow['route_id']:
                raise ValueError('Вариант другого маршрута.')
            flow['variant_edit'] = old
            flow.pop('variant_pending', None)
            body = old or {'template_id': flow['template_id'], 'route_id': flow['route_id'],
                'display_name': 'Deluxe', 'aliases': ['Deluxe Edition', 'DE'], 'enabled': True,
                'overrides': {'variables': {'edition': 'Deluxe Edition'}, 'emoji': '⭐'}}
            _pending(user_id, flow, 'variantedit')
            await callback.message.answer('Отправьте полный JSON variant. overrides: variables, emoji, '
                'title_ru/en, description_ru/en. Необязательный route_override_url — '
                'подтверждённый раздел той же игры. enabled=false отключает variant.\n'
                'Сначала preview, затем подтверждение.', parse_mode=None)
            await _plain_chunks(callback.message, json.dumps(body, ensure_ascii=False, indent=2))
            if old is not None and old['enabled']:
                await callback.message.answer('Отключить variant для новых drafts?', reply_markup=_menu([
                    [('⏸ Отключить', f"prd:variantdisable:{old['id']}:{old['revision']}")]]))
        elif command == 'variantdisable':
            flow = _active(user_id)
            old = await asyncio.to_thread(store.get_variant, parts[2])
            if (not old or flow.get('purpose') != 'routes' or old['route_id'] != flow.get('route_id')
                    or old['revision'] != int(parts[3])):
                raise ValueError('Экран устарел.')
            saved = await asyncio.to_thread(VariantService(store, routes).disable, old['id'], old['revision'])
            flow.pop('variant_edit', None)
            flow.pop('variant_pending', None)
            await callback.message.answer('⏸ Variant отключён; сохранённые drafts остаются.', reply_markup=_menu([
                [('Варианты', f"prd:variants:{saved['route_id']}:0")]]))
        elif command == 'variantsave':
            flow = _active(user_id)
            body = flow['variant_pending']; old = flow.get('variant_edit')
            if body['route_id'] != flow['route_id']:
                raise ValueError('Вариант другого маршрута.')
            saved = await asyncio.to_thread(VariantService(store, routes).save, body,
                old['id'] if old else None, old['revision'] if old else None)
            flow.pop('variant_pending', None)
            ui._interaction_state.pop(user_id, None)
            await callback.message.answer('✅ Variant сохранён локально.', reply_markup=_menu([
                [('Варианты', f"prd:variants:{saved['route_id']}:0")]]))
        elif command in {'routeimport', 'catalog', 'own'}:
            flow = _active(user_id)
            if flow['purpose'] != 'routes':
                raise ValueError('Откройте Game Routes.')
            if command == 'routeimport':
                _pending(user_id, flow, 'routeimport')
                await callback.message.answer('До 100 строк: Game | https://funpay.com/lots/ID/\n'
                    'Можно отправить только URL либо точное имя игры; несколько разделов дадут warning.\n'
                    'Сначала preview, сохранение — после подтверждения.')
            else:
                offset = int(parts[2])
                if command == 'catalog':
                    rows = await asyncio.to_thread(routes.catalog, query=flow.get('catalog_query', ''),
                                                    limit=_PAGE + 1, offset=offset)
                    keyboard = [[(f"{r['game_name'][:28]} · {r['section_name'][:24]}",
                                  f"prd:node:{r['node_id']}")] for r in rows[:_PAGE]]
                    keyboard += _page_buttons('prd:catalog', offset, len(rows) > _PAGE)
                    keyboard.append([('🔎 Поиск каталога', 'prd:catalogsearch')])
                else:
                    if not 0 <= offset <= 1_000_000:
                        raise ValueError('Некорректная страница.')
                    rows = await asyncio.to_thread(routes.own_lots)
                    keyboard = [[(f"{r['game_name'][:30]} · {r['section_name'][:20]}",
                                  f"prd:source:{r['source_id']}")] for r in rows[offset:offset + _PAGE]]
                    keyboard += _page_buttons('prd:own', offset, len(rows) > offset + _PAGE)
                keyboard.append([('🔙 Routes', 'prd:routepage:0')])
                await _show(callback, '🎮 <b>Выберите подтверждённый раздел</b>', _menu(keyboard))
        elif command == 'catalogsearch':
            flow = _active(user_id); _pending(user_id, flow, 'catalogsearch')
            await callback.message.answer('Название игры для поиска по каталогу FunPay:')
        elif command in {'node', 'source'}:
            flow = _active(user_id)
            if flow['purpose'] != 'routes':
                raise ValueError('Откройте Game Routes.')
            body = await asyncio.to_thread(routes.from_url if command == 'node' else routes.from_lot,
                flow['template_id'], f'https://funpay.com/lots/{int(parts[2])}/' if command == 'node' else int(parts[2]))
            await _stage_screen(callback.message, user_id, flow, 'routes', [body],
                f"✅ {body['game_name']} → {body['section_name']} · node {body['node_id']}", store, drafts)
        elif command == 'routeedit':
            flow = _active(user_id)
            row = await asyncio.to_thread(routes.resolve_route, parts[2])
            if row['template_id'] != flow['template_id']:
                raise ValueError('Откройте соответствующий шаблон.')
            flow['route_edit'] = row
            _pending(user_id, flow, 'routeedit')
            await callback.message.answer('JSON с aliases и/или overrides, например:\n'
                '{"game_aliases":["ER"],"overrides":{"emoji":"⚔️"}}\n'
                'Для сброса overrides используйте {}. Сохранение после preview/confirm.', parse_mode=None)
        elif command == 'templates':
            offset = int(parts[2]); rows = await asyncio.to_thread(templates.list, limit=_PAGE + 1, offset=offset)
            keyboard = [[(r['display_name'][:50], f"prd:template:{r['id']}")] for r in rows[:_PAGE]]
            keyboard += _page_buttons('prd:templates', offset, len(rows) > _PAGE)
            keyboard += [[('➕ Новый template (JSON)', 'prd:templatenew')], [('Лоты', 'products')]]
            await _show(callback, '🧩 <b>Reusable Product Templates</b>\nRU и EN обязательны.', _menu(keyboard))
        elif command in {'template', 'templatenew'}:
            body = default_template() if command == 'templatenew' else await asyncio.to_thread(templates.get, parts[2])
            if command == 'template':
                flow = {'template_edit': body}; _flows[user_id] = flow
                await _show(callback, f"🧩 <b>{escape(body['display_name'])}</b>\n"
                    f"Internal name: {escape(body['name'])}\nEmoji: {escape(body['default_emoji'])}\n"
                    'Для редактирования отправляется полный JSON. Существующие drafts хранят snapshot.',
                    _menu([[('Редактировать JSON', 'prd:templateedit')], [('Templates', 'prd:templates:0')]]))
            else:
                flow = {'template_edit': None}; _flows[user_id] = flow
                _pending(user_id, flow, 'templateedit')
                await callback.message.answer('Отправьте новый template JSON, изменив internal name. Пример:', parse_mode=None)
                await _plain_chunks(callback.message, json.dumps(body, ensure_ascii=False, indent=2))
        elif command == 'templateedit':
            flow = _active(user_id); _pending(user_id, flow, 'templateedit')
            await _plain_chunks(callback.message, json.dumps(flow['template_edit'], ensure_ascii=False, indent=2))
        elif command == 'drafts':
            offset = int(parts[2]); rows = await asyncio.to_thread(store.list_drafts, limit=_PAGE + 1, offset=offset)
            keyboard = [[(f"{r['game'][:36]} · {r.get('status', 'draft')}", f"prd:open:{r['id']}")] for r in rows[:_PAGE]]
            keyboard += _page_buttons('prd:drafts', offset, len(rows) > _PAGE)
            keyboard.append([('Лоты', 'products')])
            await _show(callback, '📝 <b>Сохранённые product drafts</b>', _menu(keyboard))
        elif command == 'open':
            body = await asyncio.to_thread(store.get_draft, parts[2])
            if body is None:
                raise ValueError('Черновик не найден.')
            flow = {'preview': body, 'stored_id': body['id'], 'stored_revision': body['revision'], 'dirty': False}
            _flows[user_id] = flow
            await _show(callback, *_preview_screen(flow))
        elif command == 'edit':
            flow = _active(user_id)
            if flow['preview'].get('status') == 'cancelled':
                raise ValueError('Черновик отменён.')
            field = parts[2]
            prompts = {'price': 'Новая цена, например 29.99:', 'emoji': 'Новый emoji:',
                       'ru': 'RU: первая строка — название, остальные — описание.',
                       'en': 'EN: first line — title, remaining lines — description.',
                       'variables': 'Variables, по одной строке platform=Steam, region=Global и т. д.:'}
            if field not in prompts:
                raise ValueError('Неподдерживаемое поле.')
            flow['edit_field'] = field; _pending(user_id, flow, 'draftedit')
            await callback.message.answer(prompts[field] + '\n❌ Отмена — выйти без сохранения.')
        elif command == 'text':
            body = _active(user_id)['preview']['payload']; language = parts[2]
            if language not in {'ru', 'en'}:
                raise ValueError('Неподдерживаемый язык.')
            await _plain_chunks(callback.message, body[f'title_{language}'] + '\n\n' + body[f'description_{language}'])
        elif command == 'save':
            flow = _active(user_id)
            if flow['preview'].get('status') == 'cancelled':
                raise ValueError('Черновик отменён.')
            if flow.get('stored_id'):
                if not flow.get('dirty'):
                    await callback.message.answer('Этот draft уже сохранён.')
                    return True
                row = await asyncio.to_thread(store.edit_draft, flow['stored_id'], flow['preview'], flow['stored_revision'])
            else:
                if not flow.get('batch_token'):
                    batch = await asyncio.to_thread(drafts.stage, [flow['preview']])
                    flow['batch_token'] = batch['token']
                    if _flows.get(user_id) is not flow:
                        await asyncio.to_thread(store.cancel_batch, batch['token'])
                        return True
                result = await asyncio.to_thread(drafts.confirm, flow['batch_token'])
                if not result['results']:
                    raise ValueError('Draft не сохранён: пакет отменён или устарел.')
                row = result['results'][0]
            flow.update(preview=row, stored_id=row['id'], stored_revision=row['revision'], dirty=False)
            await callback.message.answer('✅ Draft сохранён локально.', reply_markup=_menu([[('Открыть', f"prd:open:{row['id']}")], [('Лоты', 'products')]]))
        elif command in {'confirm', 'cancel'}:
            flow = _active(user_id)
            if flow.get('batch_token') != parts[2]:
                raise ValueError('Предпросмотр устарел.')
            result = await asyncio.to_thread(store.confirm_batch if command == 'confirm' else store.cancel_batch, parts[2])
            await callback.message.answer(f"Пакет: {result['status']}.\n"
                f"Сохранено: {len(result['results'])}; ошибок: {len(result['errors'])}.\n"
                'Внешних публикаций нет.', reply_markup=_menu([[('Лоты', 'products')]]))
        elif command == 'discard':
            flow = _active(user_id)
            if flow.get('batch_token'):
                await asyncio.to_thread(store.cancel_batch, flow['batch_token'])
            if flow.get('stored_id'):
                await asyncio.to_thread(store.cancel_draft, flow['stored_id'], flow['stored_revision'])
            discard_session(user_id)
            await _show(callback, *_home())
        else:
            raise ValueError('Действие устарело.')
    except (ValueError, KeyError, IndexError, StateError, sqlite3.Error):
        await callback.message.answer('Не удалось выполнить действие. Проверьте данные или откройте «Лоты» заново. Ничего не опубликовано.')
    return True


async def _plain_chunks(message, text):
    # Even astral symbols stay comfortably below Telegram's UTF-16 limits.
    for offset in range(0, len(text), 1500):
        await message.answer(text[offset:offset + 1500], parse_mode=None)


async def products_input(message, user_id):
    async with _lock(user_id):
        return await _products_input(message, user_id)


async def _products_input(message, user_id):
    ui = _ui(); pending = ui._interaction_state.get(user_id, {})
    if pending.get('action') != 'product_input':
        return False
    flow = pending.get('flow')
    if not ui._owner_id(user_id) or _flows.get(user_id) is not flow:
        ui._interaction_state.pop(user_id, None)
        await message.answer('Экран устарел.')
        return True
    value = message.text or ''
    if value.startswith('/'):
        discard_session(user_id); ui._interaction_state.pop(user_id, None)
        await message.answer('Ввод отменён. Команда не сохранена.')
        return True
    try:
        store, templates, routes, drafts = _kit(user_id)
        step = flow['step']
        if not value.strip() or len(value) > 100000:
            raise ValueError('Пустой или слишком длинный ввод.')
        if step in {'search', 'catalogsearch'}:
            if len(value) > 200:
                raise ValueError('Слишком длинный запрос.')
            flow['query' if step == 'search' else 'catalog_query'] = value.strip()
            ui._interaction_state.pop(user_id, None)
            action = 'prd:routepage:0' if step == 'search' else 'prd:catalog:0'
            await message.answer('🔎 Поиск готов.', reply_markup=_menu([[('Показать результаты', action)], [('Лоты', 'products')]]))
        elif step == 'variables':
            field = flow['field']
            if value != '-':
                if field == 'price':
                    flow['price'] = validate_price(value)
                else:
                    template = await asyncio.to_thread(templates.get, flow['template_id'])
                    validate_overrides({'variables': {field: value}}, template['allowed_variables'])
                    flow['variables'][field] = value
            elif field == 'price' and not flow.get('price'):
                raise ValueError('Цена обязательна.')
            flow['field_index'] = flow.get('field_index', 0) + 1
            if await _wizard_prompt(message, user_id, flow, templates):
                if flow['purpose'] == 'bulk':
                    _pending(user_id, flow, 'massdrafts')
                    await message.answer('До 100 строк: Game | price или Game | variant | price\n'
                        'Дополнительно: | region=EU | emoji=⚔️\n'
                        'Пример: Elden Ring | 29.99\nСначала summary, затем явное сохранение.')
                else:
                    preview = await asyncio.to_thread(drafts.create_preview, flow['template_id'], flow['route_id'],
                                                      flow['variables'], flow.get('price'), variant_id=flow.get('variant_id'))
                    flow.update(preview=preview, dirty=True)
                    ui._interaction_state.pop(user_id, None)
                    text, markup = _preview_screen(flow)
                    await message.answer(text, reply_markup=markup, parse_mode='HTML')
        elif step == 'routeimport':
            text = value.strip()
            if text.startswith('https://') and '\n' not in text:
                body = await asyncio.to_thread(routes.from_url, flow['template_id'], text)
                preview = {'rows': [{'status': 'ready', 'game_name': body['game_name']}], 'bodies': [body]}
            else:
                preview = await asyncio.to_thread(routes.preview_bulk, flow['template_id'], value)
            details = _route_summary(preview['rows'])
            await _stage_screen(message, user_id, flow, 'routes', preview['bodies'], details, store, drafts)
        elif step == 'massdrafts':
            preview = await asyncio.to_thread(drafts.preview_bulk, flow['template_id'], value, {'variables': flow['variables']})
            warnings = sum(row.get('status') in {'duplicate', 'warning'} for row in preview['errors'])
            details = (f"Строк: {len(preview['previews']) + len(preview['errors'])}\n"
                       f"Ready: {len(preview['previews'])}; warnings: {warnings}; errors: {len(preview['errors']) - warnings}\n" +
                       '\n'.join(f"✅ {row['game'][:35]} · {row.get('variant_name', 'defaults')[:30]} · {row['payload']['price']} · node {row['node']}"
                                 for row in preview['previews'][:6]) + '\n' +
                       '\n'.join(f"⚠ Строка {row['line']}: {row['error'][:70]}" for row in preview['errors'][:6]))
            if len(preview['previews']) > 6 or len(preview['errors']) > 6:
                details += '\nОстальные строки учтены в итогах.'
            await _stage_screen(message, user_id, flow, 'drafts', preview['previews'], details, store, drafts)
        elif step == 'routeedit':
            edit = json.loads(value)
            if not isinstance(edit, dict) or set(edit) - {'game_aliases', 'overrides'}:
                raise ValueError('Только aliases и overrides.')
            if 'game_aliases' in edit and (not isinstance(edit['game_aliases'], list)
                    or len(edit['game_aliases']) > 30
                    or any(not isinstance(alias, str) or not alias.strip() or len(alias) > 200
                           for alias in edit['game_aliases'])):
                raise ValueError('Некорректные aliases.')
            body = deepcopy(flow['route_edit'])
            template = await asyncio.to_thread(templates.get, body['template_id'])
            if 'overrides' in edit:
                edit['overrides'] = validate_overrides(edit['overrides'], template['allowed_variables'])
            body.update(edit); body['expected_revision'] = body['revision']
            await _plain_chunks(message, json.dumps(edit, ensure_ascii=False, indent=2))
            await _stage_screen(message, user_id, flow, 'routes', [body],
                                f"Изменить route {body['game_name']}?", store, drafts)
        elif step == 'templateedit':
            body = json.loads(value); old = flow.get('template_edit')
            saved = await asyncio.to_thread(templates.save, body, old['id'] if old else None,
                                            old['revision'] if old else None)
            ui._interaction_state.pop(user_id, None)
            await message.answer(f"✅ Template сохранён: {saved['name']}", parse_mode=None,
                                 reply_markup=_menu([[('Templates', 'prd:templates:0')]]))
        elif step == 'variantedit':
            body = json.loads(value)
            if not isinstance(body, dict) or body.get('route_id') != flow['route_id'] or body.get('template_id') != flow['template_id']:
                raise ValueError('Вариант другого маршрута.')
            # Verify overrides and the optional catalogue node before asking to confirm.
            body = await asyncio.to_thread(VariantService(store, routes).prepare, body)
            flow['variant_pending'] = body
            ui._interaction_state.pop(user_id, None)
            await _plain_chunks(message, json.dumps(body, ensure_ascii=False, indent=2))
            await message.answer('Сохранить variant локально?', reply_markup=_menu([
                [('✅ Сохранить variant', 'prd:variantsave')],
                [('❌ Cancel', f"prd:variants:{flow['route_id']}:0")]]))
        elif step == 'draftedit':
            field = flow['edit_field']; overrides = {}; variables = None; price = None
            if field == 'price':
                price = value
            elif field == 'emoji':
                overrides['emoji'] = value
            elif field == 'variables':
                variables = _mapping(value)
            else:
                title, separator, description = value.partition('\n')
                if not separator:
                    raise ValueError('Название и описание обязательны.')
                overrides = {f'title_{field}': title, f'description_{field}': description}
            preview = await asyncio.to_thread(drafts.rebuild, flow['preview'], price=price,
                                               variables=variables, overrides=overrides)
            if flow.get('batch_token'):
                await asyncio.to_thread(store.cancel_batch, flow.pop('batch_token'))
            flow.update(preview=preview, dirty=True)
            ui._interaction_state.pop(user_id, None)
            text, markup = _preview_screen(flow)
            await message.answer(text, reply_markup=markup, parse_mode='HTML')
        else:
            raise ValueError('Экран устарел.')
    except (ValueError, KeyError, TypeError, StateError, sqlite3.Error):
        await message.answer('Не сохранено. Проверьте формат, переменные, маршрут и обязательные RU/EN поля. Можно исправить ввод или выйти через ❌ Отмена.')
    return True


def _route_summary(rows):
    icons = {'ready': '✅', 'warning': '⚠', 'duplicate': '⚠', 'error': '❌'}
    counts = {status: sum(row['status'] == status for row in rows) for status in icons}
    text = (f"Строк: {len(rows)} · ready: {counts['ready']} · "
            f"warning/duplicate: {counts['warning'] + counts['duplicate']} · error: {counts['error']}\n")
    text += '\n'.join(f"{icons[row['status']]} {row['game_name'][:60]}" +
                      (f" — {row['message'][:80]}" if row.get('message') else '') for row in rows[:12])
    if len(rows) > 12:
        text += '\nОстальные строки учтены в итогах.'
    return text
