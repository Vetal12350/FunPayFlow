"""Owner-only local product workflows. No FunPay modifying operation exists here."""
import asyncio
from copy import deepcopy
from html import escape
import json
import re
import sqlite3
import uuid

from .game_routes import RouteService, RouteError
from .game_catalog import GameCatalog, saved_routes
from .product_store import ProductStore
from .product_templates import TemplateService, default_template, validate_overrides, validate_variable_value, BUILTIN_TYPES
from .product_drafts import validate_price
from .node_products import NodeDraftService, LEGACY_NODE_VARIABLES, LEGACY_WARNING, STALE_WARNING, NodeSelectionError, NodeVariantConflictError, VARIANT_WARNING
from .node_form import NodeFormError, NodeFormService, safe_form_evidence, safe_contract_proof
from . import telegram_node_fields as node_ui
from .product_variants import VariantService
from .state import StateError
from .lot_publisher import LotPublisher, PublisherError, PUBLICATION_BLOCKERS, _fresh


_flows: dict[int, dict] = {}
_locks: dict[int, tuple] = {}
_PAGE = 8
_TITLES = {'platform': 'Platform', 'region': 'Region', 'edition': 'Edition',
           'quantity': 'Quantity', 'product_name': 'Product name', 'price': 'Price',
           'sale_name': 'Фактическое название игры / DLC / сервиса / карты · Actual offer name',
           'base_game': 'Необходимая базовая игра (не включена) / Required base game (not included)',
           'currency_name': 'Название внутриигровой валюты / In-game currency name',
           'denomination': 'Объём валюты на единицу товара / Currency amount per item',
           'gaming_product_type': 'Тип: подписка / подарочная карта / код · Product type',
           'term_or_value': 'Укажите срок подписки или номинал карты / кода · Duration or face value',
           'sale_platform': 'Платформа / Platform', 'ps_platform': 'PlayStation',
           'sale_edition': 'Издание / Edition', 'product_form': 'Форма товара / Product form',
           'plan': 'Тариф / Plan', 'duration': 'Срок / Duration',
           'availability': 'Регион / доступность (или «не указано / unspecified»)',
           'delivery_method': 'Фактический разрешённый способ передачи / Authorized delivery method',
           'authorized_delivery': 'Подтвердите разрешение провайдера для указанного способа передачи'}


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
    return store, TemplateService(store), routes, NodeDraftService(store, routes)


def _menu(rows):
    ui = _ui()
    return ui._menu([[ui._button(text, action) for text, action in row] for row in rows])


def _home():
    return ('📦 <b>Лоты · локальные черновики</b>\n'
            'Черновики товаров · публикация FunPay пока отключена.\n'
            'Существующие лоты не изменяются.', _menu([
                [('➕ Создать товар', 'prd:types')],
                [('📝 Мои черновики', 'prd:drafts:0')],
                [('⚙️ Дополнительно', 'prd:advanced')],
                [('🔙 Главное меню', 'menu_main')],
            ]))


def _advanced():
    return ('⚙️ <b>Дополнительно · локальные черновики</b>\n'
            'Публикация FunPay пока отключена.', _menu([
                [('🗺 Категории и маршруты', 'prd:choose:routes:0')],
                [('🧩 Шаблоны оформления', 'prd:templates:0')],
                [('📚 Массовое создание черновиков', 'prd:choose:bulk:0')],
                [('➕ Расширенное создание', 'prd:choose:single:0')],
                [('📝 Из существующего лота', 'lot_templates')],
                [('🔙 Лоты', 'products')],
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


async def _route_list(callback, user_id, store, offset=0, recent=False, catalog_fallback=True):
    flow = _active(user_id)
    flow.pop('catalog_state', None)
    rows = await asyncio.to_thread(saved_routes, store, template_id=flow['template_id'],
                                   query='' if recent else flow.get('query', ''), limit=_PAGE + 1,
                                   offset=offset, recent=recent)
    if not rows and offset == 0 and not recent and catalog_fallback and flow['purpose'] == 'single':
        _, _, routes, _ = _kit(user_id)
        await _catalog_results(callback, user_id, store, routes)
        return
    keyboard = [[(f"{row['game_name'][:32]} · {row['section_name'][:20]}",
                  f"prd:route:{row['id']}")] for row in rows[:_PAGE]]
    keyboard += _page_buttons('prd:recent' if recent else 'prd:routepage', offset,
                              len(rows) > _PAGE)
    keyboard += [[('🔎 Поиск категории / alias', 'prd:search')],
                 [('🕘 Недавние', 'prd:recent:0')]]
    if flow['purpose'] == 'routes':
        keyboard += [[('➕ URL / массовый import', 'prd:routeimport')],
                     [('🎮 Каталог FunPay', 'prd:catalog:0')],
                     [('Из собственного лота', 'prd:own:0')]]
    keyboard.append([('🔙 Лоты', 'products')])
    title = '🕘 Недавние / последние routes' if recent else '🗺 Выберите маршрут / категорию'
    await _show(callback, f'<b>{title}</b>\n'
                f"Найденные маршруты · страница {offset // _PAGE + 1}.\n"
                'Поиск сначала использует ваши routes, затем каталог FunPay. '
                'game_name — внутреннее legacy-имя маршрута, не название продаваемого товара. '
                'Выберите подходящую COMMON-категорию явно; если её нет, создание заблокировано.', _menu(keyboard))


def _catalog_action(state, operation, value=0):
    return f"prd:rt:{state['token']}:{operation}:{value}"


def _catalog_context(routes, store, template_id):
    return (id(routes.client), id(routes.client.account), store.owner_id, store.account_id, template_id)


async def _catalog_error(callback, flow):
    state = flow['catalog_state']
    state.update(token=uuid.uuid4().hex[:12], view='games')
    await _show(callback, 'Каталог недоступен или выбор устарел. Обновите каталог и повторите выбор. '
                'Ничего не сохранено.', _menu([
                    [('🔄 Обновить каталог', _catalog_action(state, 'refresh'))],
                    [('🔍 Другой поиск', _catalog_action(state, 'search'))],
                    [('❌ Отмена', _catalog_action(state, 'cancel'))]]))


async def _catalog_results(callback, user_id, store, routes, *, offset=0, category_id=None,
                           force=False, auto=True):
    flow = _active(user_id)
    previous = flow.get('catalog_state', {})
    state = {'token': uuid.uuid4().hex[:12], 'generation': previous.get('generation'),
             'context': _catalog_context(routes, store, flow['template_id']),
             'view': 'games' if category_id is None else 'sections', 'category_id': category_id}
    flow['catalog_state'] = state
    catalog = GameCatalog(routes.client, store)
    try:
        if category_id is None:
            result = await asyncio.to_thread(catalog.games, flow.get('query', ''),
                                             limit=_PAGE + 1, offset=offset, force=force)
            if _flows.get(user_id) is not flow:
                return
            rows = result['games']
            state.update(generation=result['generation'], games=rows[:_PAGE])
            if auto and offset == 0 and len(rows) == 1:
                await _catalog_results(callback, user_id, store, routes, category_id=rows[0]['category_id'])
                return
            keyboard = [[(row['game_name'][:60], _catalog_action(state, 'game', row['category_id']))]
                        for row in rows[:_PAGE]]
            operation = 'gpage'
            text = ('🔎 <b>Найдено в FunPay</b>\nВыберите категорию каталога.' if rows else
                    '🔎 Совпадений в каталоге FunPay нет. Без подходящей подтверждённой COMMON-категории черновик создать нельзя.')
        else:
            result = await asyncio.to_thread(catalog.sections, category_id, state['generation'],
                                             limit=_PAGE + 1, offset=offset)
            if _flows.get(user_id) is not flow:
                return
            rows = result['rows']
            state['nodes'] = rows[:_PAGE]
            if auto and offset == 0 and len(rows) == 1:
                await _catalog_confirmation(callback, user_id, store, routes, rows[0]['node_id'])
                return
            keyboard = [[(row['section_name'][:60], _catalog_action(state, 'node', row['node_id']))]
                        for row in rows[:_PAGE]]
            operation = 'spage'
            game = rows[0]['game_name'] if rows else 'Категория каталога'
            text = (f"🔎 <b>{escape(game[:150])}</b>\nВыберите реальный раздел FunPay. "
                    'Тип товара не определяется автоматически.')
        if _flows.get(user_id) is not flow:
            return
        if offset:
            keyboard.append([('⬅️ Назад', _catalog_action(state, operation, max(0, offset - _PAGE)))])
        if len(rows) > _PAGE:
            keyboard.append([('Далее ➡️', _catalog_action(state, operation, offset + _PAGE))])
        text += '\n' + (result['warning'] if result['stale'] else 'Источник: каталог текущего Account.')
        keyboard += [[('🔄 Обновить каталог', _catalog_action(state, 'refresh'))],
                     [('🔍 Другой поиск', _catalog_action(state, 'search'))],
                     [('🔙 Назад', _catalog_action(state, 'back'))],
                     [('❌ Отмена', _catalog_action(state, 'cancel'))]]
        await _show(callback, text, _menu(keyboard))
    except RouteError:
        if _flows.get(user_id) is flow:
            await _catalog_error(callback, flow)


async def _catalog_confirmation(callback, user_id, store, routes, node_id):
    flow = _active(user_id); state = flow['catalog_state']
    if node_id not in {row['node_id'] for row in state['nodes']}:
        raise ValueError('Раздел не был показан.')
    catalog = GameCatalog(routes.client, store)
    candidate = await asyncio.to_thread(catalog.candidate, state['category_id'], node_id, state['generation'])
    snapshot = await asyncio.to_thread(catalog.snapshot)
    if _flows.get(user_id) is not flow:
        return
    if snapshot['generation'] != state['generation']:
        raise RouteError('Каталог изменился.')
    state.update(token=uuid.uuid4().hex[:12], view='confirm', candidate=candidate)
    await _show(callback, '👁 <b>Подтвердите GameRoute</b>\n'
        f"Категория каталога (legacy game_name): {escape(candidate['game_name'][:150])}\n"
        f"Раздел: {escape(candidate['section_name'][:150])} · node {candidate['node_id']}\n"
        'Подтверждение сохранит только локальное соответствие и продолжит создание draft.\n'
        'Подтвердите пригодность этого COMMON-раздела для вашего товара. Тип шаблона её не гарантирует.\n'
        + (snapshot['warning'] if snapshot['stale'] else 'Публикация FunPay отсутствует.'), _menu([
            [('✅ Использовать', _catalog_action(state, 'confirm'))],
            [('🔄 Обновить каталог', _catalog_action(state, 'refresh'))],
            [('🔍 Другой поиск', _catalog_action(state, 'search'))],
            [('🔙 Назад', _catalog_action(state, 'back'))],
            [('❌ Отмена', _catalog_action(state, 'cancel'))]]))


def _save_catalog_route(store, routes, template_id, state):
    catalog = GameCatalog(routes.client, store)
    candidate = catalog.candidate(state['category_id'], state['candidate']['node_id'], state['generation'])
    if candidate != state['candidate']:
        raise ValueError('Каталог изменился.')
    body = routes.from_url(template_id, f"https://funpay.com/lots/{candidate['node_id']}/")
    if any(body[key] != candidate[key] for key in ('game_name', 'node_id', 'section_name', 'metadata')):
        raise ValueError('Раздел изменился.')
    def compatible():
        existing = store.find_route(template_id, candidate['game_name'])
        if not existing:
            return None
        verified = routes.resolve_route(existing['id'])
        if any(verified[key] != body[key] for key in ('template_id', 'game_name', 'node_id', 'section_name', 'metadata')):
            raise ValueError('Существует конфликтующий route.')
        return verified
    existing = compatible()
    if existing:
        return existing
    try:
        return store.save_route(body)
    except ValueError:
        existing = compatible()  # A concurrent compatible insert can be reused, never overwritten.
        if existing:
            return existing
        raise


async def _start_route_draft(callback, user_id, row, store, routes, templates):
    flow = _active(user_id)
    for key in ('catalog_state', 'search_token', 'variant_id', 'preview', 'preview_token',
                'stored_id', 'stored_revision', 'batch_token', 'price', 'node_state', 'node_ready'):
        flow.pop(key, None)
    flow.update(route_id=row['id'], variables={}, field_index=0)
    variants = VariantService(store, routes)
    if await asyncio.to_thread(variants.list, row['id'], limit=1):
        await _variant_picker(callback, user_id, variants)
    else:
        await _wizard_prompt(callback.message, user_id, flow, templates)


def _draft_label(row):
    values = row.get('variables', {})
    parts = [values.get('sale_name', row['game'])[:22], row.get('variant_name', values.get('edition', 'defaults'))[:16],
             str(values.get('region', ''))[:12], str(values.get('platform', ''))[:12],
             row['payload']['price'][:12] + ' ×' + str(row['payload']['amount'])]
    return ' · '.join(part for part in parts if part)[:64]


def _publication_candidate(body):
    """Display filter only; the service revalidates the saved rows and scope."""
    return (body.get('node') == 986 and body.get('game', '').casefold() == 'elden ring'
            and body.get('template_snapshot', {}).get('name') == 'GAME_KEY'
            and not body.get('variant_id') and body.get('status') != 'cancelled'
            and body.get('payload', {}).get('amount') == 1 and len(body.get('node_fields', [])) == 3)


async def _publication_screen(callback, state):
    review = state['review']
    token = state['token']
    text = ('👁 <b>PREVIEW · новый лот Elden Ring</b>\n'
        'GAME_KEY · category 274 · COMMON node 986\n'
        f'Draft revision: {review.revision}\n'
        + '\n'.join(escape(label[:65]) + ': ' + escape(option[:120])
                    for _, _, label, option in review.selections)
        + f'\nЦена: {escape(review.price)} {review.currency or "— выберите валюту"}'
        '\nКоличество: 1\nВыдача: ручная (запрошена; соответствие форме ещё не доказано).\n'
        'Видимость: не доказана.\n\n'
        '<b>Live POST отключён.</b> Следующий шаг — один свежий GET NEW-формы '
        'без редиректов. Проверка ничего не публикует. '
        '«Опубликовать 1 лот» недоступно до доказательства NEW-контракта и read-back.')
    rows = [[(currency, f'prd:pub:{token}:{currency}') for currency in ('RUB', 'USD', 'EUR')],
            [('Полный RU', f'prd:pub:{token}:ru'), ('Полный EN', f'prd:pub:{token}:en')]]
    if review.currency:
        rows.append([('🔎 Проверить NEW-форму (GET)', f'prd:pub:{token}:inspect')])
    rows += [[('❌ Отмена проверки', f'prd:pub:{token}:cancel')], [('📝 Мои черновики', 'prd:drafts:0')]]
    if any(len(label) > 65 or len(option) > 120 for _, _, label, option in review.selections):
        await _plain_chunks(callback.message, '\n'.join(label + ': ' + option
            for _, _, label, option in review.selections))
    await _show(callback, text, _menu(rows))


async def _publication_callback(callback, user_id, parts, store, routes):
    """Read-only fallback. No live submission capability is reachable here."""
    command = parts[1]
    if command in {'pubopen', 'pubpreview'}:
        if command == 'pubopen':
            if len(parts) != 4:
                raise ValueError('Экран устарел.')
            draft_id, revision = parts[2], int(parts[3])
        else:
            flow = _active(user_id)
            if (len(parts) != 3 or parts[2] != flow.get('preview_token')
                    or not flow.get('stored_id') or flow.get('dirty')):
                raise ValueError('Экран устарел.')
            draft_id, revision = flow['stored_id'], flow['stored_revision']
        publisher = LotPublisher(routes.client, store)
        review = await asyncio.to_thread(publisher.review, draft_id, revision, None)
        if await asyncio.to_thread(publisher.outcome, draft_id) is not None:
            raise PublisherError('ALREADY_CLAIMED')
        state = {'review': review, 'token': uuid.uuid4().hex[:12],
                 'context': _catalog_context(routes, store, review.template_id)}
        _flows[user_id] = {'publication': state}
        _ui()._interaction_state.pop(user_id, None)
        await _publication_screen(callback, state)
        return
    flow = _active(user_id)
    state = flow.get('publication')
    if (len(parts) != 4 or not state or not state['token'] or parts[2] != state['token']
            or state['context'] != _catalog_context(routes, store, state['review'].template_id)):
        raise ValueError('Экран устарел.')
    operation = parts[3]
    if operation == 'cancel':
        discard_session(user_id)
        await _show(callback, *_home())
        return
    review = state['review']
    def current():
        return (_flows.get(user_id) is flow and flow.get('publication') is state
            and _ui()._runtime_client is routes.client and _ui()._owner_id(user_id)
            and state['context'] == _catalog_context(routes, store, review.template_id)
            and not _ui().is_safe_mode_enabled())
    publisher = LotPublisher(routes.client, store, late_check=current)
    _fresh(review.fetched_at, publisher.clock())
    if await asyncio.to_thread(publisher.review, review.draft_id, review.revision, review.currency) != review:
        raise PublisherError('REVISION')
    if operation in {'RUB', 'USD', 'EUR'}:
        state['review'] = await asyncio.to_thread(publisher.review, review.draft_id, review.revision, operation)
        state['token'] = uuid.uuid4().hex[:12]
        await _publication_screen(callback, state)
    elif operation in {'ru', 'en'}:
        content = dict(review.content)
        await _plain_chunks(callback.message, content['title_' + operation] + '\n\n' + content['description_' + operation])
    elif operation == 'inspect':
        # Consume before waiting for the Account lock or making any request.
        state['token'] = None
        proof, blockers = await asyncio.to_thread(publisher.inspect_new, review)
        if not current():
            return
        lines = ['🔒 Публикация заблокирована. Выполнена только свежая GET-проверка.',
                 'Наблюдения формы (verified = структура, не разрешение на POST):']
        lines += [f'{key}: {proof[key]}' for key in (
            'editor_url', 'document_markup', 'form_subtree', 'submission', 'blank_identity',
            'node_identity', 'csrf_presence', 'control_inventory', 'select_names')]
        lines += [PUBLICATION_BLOCKERS[key] for key in blockers]
        lines += ['POST не выполнялся; попытка публикации не создавалась. '
                  'Не используйте save_lot или offer_id=0 как обход. Нужен review NEW-контракта.']
        await _show(callback, escape('\n'.join(lines)), _menu([[('📝 Мои черновики', 'prd:drafts:0')]]))
    else:
        raise ValueError('Экран устарел.')


def _route_screen(row, check_token=None):
    text = ('🗺 <b>Game Route</b>\n'
            f"Категория каталога (legacy game_name): {escape(row['game_name'][:150])}\n"
            f"Раздел: {escape(row['section_name'][:150])} · node {row['node_id']}\n"
            f"Aliases: {escape(', '.join(row.get('game_aliases', []))[:250])}\n"
            f"Emoji: {escape(row.get('overrides', {}).get('emoji', 'template default'))}")
    check = []
    if check_token:
        text += ('\nПроверка — только чтение, публикация НЕ проверена. '
                 'get может вернуть кеш до 900 с; клиент следует редиректам. '
                 'Это не гарантированно одна ограниченная HTTP-транзакция.')
        check = [[('🧪 Проверить форму раздела (только чтение)', f'prd:routeform:{check_token}')]]
    return text, _menu(check + [[('Aliases / emoji', f"prd:routeedit:{row['id']}")],
                        [('🎚 Варианты / Editions', f"prd:variants:{row['id']}:0")],
                        [('🔙 Routes', 'prd:routepage:0')], [('Лоты', 'products')]])


async def _check_route_form(callback, user_id, parts, store, templates, routes):
    """Consume one owner intent before I/O; never enter draft/input machinery."""
    flow = _flows.get(user_id)
    state = flow.pop('route_form', None) if flow else None

    def current():
        if (not state or len(parts) != 3 or parts[2] != state['token']
                or _flows.get(user_id) is not flow or flow.get('purpose') != 'routes'
                or flow.get('template_id') != state['template']['id']
                or flow.get('route_id') != state['route']['id']
                or state['context'] != _catalog_context(routes, store, flow['template_id'])
                or _ui()._runtime_client is not routes.client
                or not _ui()._owner_id(user_id)):
            raise ValueError('Stale route form intent')
        if (templates.get(flow['template_id']) != state['template']
                or routes.resolve_route(flow['route_id']) != state['route']):
            raise ValueError('Changed route form context')

    def read():
        # Validate again after acquiring the same lock used by all Account work.
        with routes.client._account_lock:
            current()
            row = state['route']
            proof = {} if (row['node_id'] == 986 and row['metadata']['category_id'] == 274
                           and row['metadata']['type'] == 'COMMON'
                           and state['template']['id'] == 'GAME_KEY') else None
            schema, evidence = NodeFormService(routes.client, store).get_with_evidence(
                row['node_id'], contract_proof=proof)
            current()
            return min(100, len(schema['fields'])), safe_form_evidence(evidence), proof

    try:
        count, evidence, proof = await asyncio.to_thread(read)
        status = f'доступна\nВидимых SELECT-полей: {count} (метаданные).'
        status += '\n' + _route_form_evidence_text(evidence)
        if proof is not None:
            status += '\n' + _route_contract_proof_text(proof)
    except NodeFormError as cause:
        status = 'не удалось прочитать\n' + node_ui.safe_error_text(cause)
    except (ValueError, KeyError, IndexError, StateError, sqlite3.Error):
        await callback.message.answer('Проверка недоступна: экран устарел. Откройте маршрут заново.')
        return
    row = state['route']
    await callback.message.answer(
        f"🧪 Каталог {row['metadata']['category_id']} · node {row['node_id']}\nФорма: {status}\n"
        'публикация НЕ проверена\n'
        'get может вернуть кеш до 900 с; клиент следует редиректам. '
        'Это не гарантированно одна ограниченная HTTP-транзакция.',
        reply_markup=_menu([[('🔙 Маршрут', f"prd:route:{row['id']}")], [('Лоты', 'products')]]))


def _route_contract_proof_text(value):
    proof = safe_contract_proof(value)
    # Keys and values are revalidated; no response-derived text is interpolated.
    rows = [('URL', 'editor_url'), ('Документ', 'document_markup'), ('Поддерево', 'form_subtree'),
            ('Action/POST/encoding', 'submission'), ('Blank identity', 'blank_identity'),
            ('NEW semantics', 'new_semantics'), ('CSRF presence', 'csrf_presence'),
            ('Manual control', 'manual_control'), ('Manual semantics', 'manual_delivery'),
            ('Node', 'node_identity'), ('Currency control', 'currency_control'),
            ('Currency semantics', 'currency'), ('Controls', 'control_inventory'),
            ('3 SELECT names', 'select_names'), ('Response ID', 'response_id'),
            ('No-retry POST', 'no_retry_post')]
    return ('Contract proof: ' + proof['availability'] + '\n'
            + '\n'.join(label + ': ' + proof[key] for label, key in rows)
            + '\nControls/hidden/duplicates/unknown: '
            + '/'.join(str(proof[k]) for k in (
                'control_count', 'hidden_count', 'duplicate_count', 'unknown_count'))
            + '\npublish_ready: false; публикация OFF.')


def _route_form_evidence_text(evidence):
    evidence = safe_form_evidence(evidence)
    source = {'response': 'ответ этого GET (свежесть не доказана)',
              'cache': 'кеш до 900 с; текущая структура неизвестна',
              'unknown': 'неизвестен'}[evidence['source']]
    controls = {'recognized': 'распознана ограниченная структура',
                'unknown': 'неизвестна / неоднозначна'}[evidence['controls']]
    context = {'candidate': 'кандидат NEW; создание НЕ доказано',
               'unknown': 'неизвестен'}[evidence['new_form_context']]
    csrf = {'present': 'признак есть', 'absent': 'распознаваемый признак отсутствует',
            'ambiguous': 'неизвестно / неоднозначно'}[evidence['csrf_input_presence']]
    blocker = ''
    if evidence['controls'] == 'unknown':
        reason = {
            'unknown': 'причина не установлена',
            'cache_unavailable': 'структурные данные сетевого ответа недоступны в кеше',
            'response_url': 'неоднозначный адрес ответа',
            'duplicate_attributes': 'повторяющиеся атрибуты разметки',
            'document_markup': 'неоднозначная разметка HTML',
            'document_tag_mismatch': 'несовпадение закрывающего тега со строгим стеком HTML',
            'document_unclosed_tags': 'непустой строгий стек тегов в конце HTML',
            'document_self_closing': 'самозакрывающийся обычный тег HTML',
            'document_text': 'неоднозначный текст HTML',
            'editor_markup': 'неподдерживаемая разметка формы',
            'control_conflict': 'конфликт элементов формы',
            'unsupported_controls': 'неподдерживаемые элементы формы',
            'ambiguous_context': 'неоднозначный контекст формы',
        }[evidence['evidence_blocker']]
        blocker = f'Почему неизвестно: {reason}.\n'
    return (f'Источник: {source}.\nСтруктура: {controls}.\n{blocker}Контекст NEW: {context}.\n'
            f'CSRF: {csrf}; свежесть и авторизация НЕ доказаны.\n'
            'Ручная выдача: НЕ проверена.\nКонтракт отправки: НЕ проверен.\n'
            'ID результата: НЕ проверен.')


def _preview_screen(flow):
    body = flow['preview']; p = body['payload']
    token = flow.setdefault('preview_token', uuid.uuid4().hex[:12])
    clip = _ui()._escaped_preview
    text = ('👁 <b>RU + EN draft</b>\n'
            f"Route label (legacy game_name): {clip(body['game'], 80)}\n"
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
    if 'node_fields' in body:
        field_lines = [f"{row['display_label'][:65]}: {row['option_label'][:90]}" for row in body['node_fields']]
        text = '📋 <b>Параметры FunPay</b>\n' + escape('\n'.join(field_lines[:8])) + '\n\n' + text
        if len(field_lines) > 8:
            text += '\nПолные параметры доступны отдельной кнопкой.'
        if body.get('node_warning'):
            text += '\n⚠ ' + escape(body['node_warning'])
    else:
        text += '\n⚠ ' + LEGACY_WARNING
    rows = [[('💾 Сохранить draft', 'prd:save')],
            [('💵 Цена', 'prd:edit:price'), ('Emoji', 'prd:edit:emoji')],
            [('Изменить RU', 'prd:edit:ru'), ('Изменить EN', 'prd:edit:en')],
            [('Variables', 'prd:edit:variables')],
            [('Полный RU', 'prd:text:ru'), ('Полный EN', 'prd:text:en')],
            [('❌ Cancel', 'prd:discard')], [('🔙 Лоты', 'products')]]
    if body.get('status') == 'cancelled':
        rows = [[('🔙 Лоты', 'products')]]
    if 'node_fields' in body:
        rows.insert(0, [('📋 Все параметры', 'prd:text:node')])
        if body.get('status') != 'cancelled':
            rows.insert(0, [('🔗 Связать поля', 'prd:bindings')])
    if flow.get('stored_id') and not flow.get('dirty') and body.get('status') != 'cancelled':
        rows.insert(0, [('📑 Создать похожий', 'prd:clone')])
        if _publication_candidate(body):
            rows.insert(0, [('👁 PREVIEW публикации', 'prd:pubpreview:' + token)])
    if flow.get('similar') and not flow.get('stored_id'):
        rows.insert(0, [('📝 Параметры', _local_action(flow, 'menu'))])
    rows = [[(label, action + ':' + token if action.startswith('prd:') and action.split(':')[1] in {'save', 'edit', 'text', 'discard', 'clone', 'bindings'}
              else action) for label, action in row] for row in rows]
    return text, _menu(rows)


def _edit_context(user_id, flow):
    store, _, routes, _ = _kit(user_id)
    return _catalog_context(routes, store, flow.get('template_id'))


def _local_action(flow, operation, value=0):
    return f"prd:sim:{flow['similar_token']}:{operation}:{value}"


async def _similar_screen(message, user_id, flow, templates, drafts):
    _ui()._interaction_state.pop(user_id, None)
    flow.pop('step', None)
    flow.pop('node_state', None)
    body = flow['preview']
    template = await asyncio.to_thread(templates.get, body['template_id'])
    flow.pop('picker', None)
    flow.pop('similar_variants', None)
    flow['similar_token'] = uuid.uuid4().hex[:12]
    flow['edit_context'] = _edit_context(user_id, flow)
    dynamic = 'node_fields' in body
    flow['similar_fields'] = [key for key in template['allowed_variables'] if key != 'game'
        and (not dynamic or key not in LEGACY_NODE_VARIABLES | set(template.get('node_bindings', {})))]
    rows = []
    variants = await asyncio.to_thread(drafts.variants.list, body['route_id'], limit=1)
    if variants:
        rows.append([('🎭 Variant / Edition', _local_action(flow, 'variant'))])
    if dynamic:
        selected = {row['html_name']: row for row in body['node_fields']}
        flow['node_edit_fields'] = [f['html_name'] for f in body['node_schema']['fields'] if not f['disabled']]
        for index, field in enumerate(f for f in body['node_schema']['fields'] if not f['disabled']):
            value = selected.get(field['html_name'], {}).get('option_label', '—')
            rows.append([((field['display_label'] + ': ' + value)[:60], _local_action(flow, 'node', index))])
        rows.append([('🔄 Обновить параметры', _local_action(flow, 'node_refresh'))])
    for index, key in enumerate(flow['similar_fields']):
        value = str(body['variables'].get(key, '—'))
        rows.append([(f"{_TITLES.get(key, key)}: {value}"[:55], _local_action(flow, 'variable', index))])
    rows += [[('💰 Price: ' + body['payload']['price'][:30], _local_action(flow, 'price'))],
             [('👁 Preview', _local_action(flow, 'preview'))],
             [('💾 Сохранить', _local_action(flow, 'save'))],
             [('❌ Отмена', _local_action(flow, 'cancel'))]]
    warnings = flow.get('clone_warnings', [])
    await message.answer('📑 <b>Похожий draft</b>\n'
        + escape(body['game'][:120]) + ' · ' + escape(body['section'][:100]) + '\n'
        + 'Variant: ' + escape(body.get('variant_name', 'defaults')[:100]) + '\n'
        + 'Используются текущие template/route/variant. Исходный draft не меняется.\n'
        + escape(' '.join(warnings)), reply_markup=_menu(rows), parse_mode='HTML')


async def _variable_picker(message, user_id, flow, template, field, current, mode, *, offset=0, numeric=None):
    rule = template['validation_rules'].get(field, {})
    choices = list(dict.fromkeys(rule.get('choices', [])))
    state = {'token': uuid.uuid4().hex[:12], 'field': field, 'current': current,
             'mode': mode, 'choices': choices, 'revision': template['revision'],
             'context': _edit_context(user_id, flow), 'offset': offset}
    flow['picker'] = state
    flow['edit_context'] = state['context']
    def action(op, value=0):
        return f"prd:pv:{state['token']}:{op}:{value}"
    rows = []
    if field == 'quantity' and not choices:
        state['numeric'] = max(1, min(999999999, int(numeric if numeric is not None else current or 1)))
        rows += [[('-10', action('delta', -10)), ('-1', action('delta', -1)),
                  ('+1', action('delta', 1)), ('+10', action('delta', 10))],
                 [(str(n), action('number', n)) for n in (1, 5, 10, 50)],
                 [('✅ Готово', action('done'))]]
        label = str(state['numeric'])
    else:
        label = str(current or '—')
        if current is not None:
            rows.append([('✅ Оставить ' + str(current)[:38], action('keep'))])
        rows += [[(str(value)[:50], action('choice', i))]
                 for i, value in enumerate(choices[offset:offset + _PAGE], offset)]
        if offset:
            rows.append([('⬅️ Страница', action('page', max(0, offset - _PAGE)))])
        if offset + _PAGE < len(choices):
            rows.append([('Далее ➡️', action('page', offset + _PAGE))])
    rows += [[('✏️ Другое', action('custom'))], [('⬅️ Назад', action('back'))],
             [('❌ Отмена', action('cancel'))]]
    await message.answer(f"<b>{escape(_TITLES.get(field, field))}</b>\nТекущее значение: {escape(label[:500])}",
                         reply_markup=_menu(rows), parse_mode='HTML')


async def _advance_variables(message, user_id, flow, templates, drafts):
    flow.pop('picker', None)
    flow['field_index'] = flow.get('field_index', 0) + 1
    if await _wizard_prompt(message, user_id, flow, templates):
        if flow['purpose'] == 'bulk':
            _pending(user_id, flow, 'massdrafts')
            await message.answer('До 100 строк: Game | price или Game | variant | price\n'
                'Дополнительно: | region=EU | emoji=⚔️\nСначала summary, затем явное сохранение.')
        else:
            state = flow['node_state']
            preview = await asyncio.to_thread(drafts.create_node_preview, flow['template_id'], flow['route_id'],
                state['values'], variables=flow['variables'], price=flow.get('price'),
                variant_id=flow.get('variant_id'), schema=state['schema'], input_values=state['explicit'])
            flow.update(preview=preview, dirty=True)
            _ui()._interaction_state.pop(user_id, None)
            text, markup = _preview_screen(flow)
            await message.answer(text, reply_markup=markup, parse_mode='HTML')


async def _accept_variable(message, user_id, flow, templates, drafts, state, value, keep=False):
    template = await asyncio.to_thread(templates.get, flow['template_id'])
    value = validate_variable_value(template, state['field'], value)
    if state['mode'] == 'wizard':
        if not keep:
            flow['variables'][state['field']] = value
        await _advance_variables(message, user_id, flow, templates, drafts)
    else:
        if not keep:
            flow['preview'] = await asyncio.to_thread(drafts.rebuild, flow['preview'], variables={state['field']: value})
        await _similar_screen(message, user_id, flow, templates, drafts)


async def _picker_callback(callback, user_id, flow, state, operation, value, templates, drafts):
    message = callback.message
    template = await asyncio.to_thread(templates.get, flow['template_id'])
    if operation not in {'back', 'cancel'} and template['revision'] != state['revision']:
        raise ValueError('Template изменён; откройте поле заново.')
    _ui()._interaction_state.pop(user_id, None)
    if operation == 'cancel':
        discard_session(user_id)
        await _show(callback, *_home())
    elif operation == 'back':
        flow.pop('picker', None)
        if state['mode'] == 'similar':
            await _similar_screen(message, user_id, flow, templates, drafts)
        else:
            flow['field_index'] = max(0, flow['field_index'] - 1)
            await _wizard_prompt(message, user_id, flow, templates)
    elif operation == 'custom':
        state = dict(state, token=uuid.uuid4().hex[:12])
        flow['picker'] = state
        if state['mode'] == 'wizard':
            _pending(user_id, flow, 'variables')
        else:
            flow['similar_field'] = state['field']
            _pending(user_id, flow, 'similarinput')
        await message.answer('Введите ' + state['field'] + '. Значение проверяется по правилам template.', reply_markup=_menu([[('⬅️ Назад', f"prd:pv:{state['token']}:back:0")], [('❌ Отмена', f"prd:pv:{state['token']}:cancel:0")]]))
    elif operation == 'page':
        offset = int(value)
        if offset < 0 or offset % _PAGE or offset >= len(state['choices']):
            raise ValueError('Страница устарела.')
        await _variable_picker(message, user_id, flow, template, state['field'], state['current'], state['mode'], offset=offset)
    elif operation in {'delta', 'number'} and 'numeric' in state:
        number = int(value)
        if operation == 'delta' and number not in {-10, -1, 1, 10}:
            raise ValueError('Некорректный шаг.')
        if operation == 'number' and number not in {1, 5, 10, 50}:
            raise ValueError('Некорректное количество.')
        number = state['numeric'] + number if operation == 'delta' else number
        await _variable_picker(message, user_id, flow, template, state['field'], state['current'], state['mode'], numeric=number)
    elif operation in {'keep', 'done', 'choice'}:
        keep = operation == 'keep'
        if keep:
            selected = state['current']
        elif operation == 'done' and 'numeric' in state:
            selected = str(state['numeric'])
            keep = selected == str(state['current'])
        elif operation == 'choice' and state['offset'] <= int(value) < min(len(state['choices']), state['offset'] + _PAGE):
            selected = state['choices'][int(value)]
        else:
            raise ValueError('Выбор устарел.')
        flow.pop('picker', None)  # Consume before any awaited render/save.
        await _accept_variable(message, user_id, flow, templates, drafts, state, selected, keep)
    else:
        raise ValueError('Выбор устарел.')


async def _similar_text_prompt(message, user_id, flow, templates, field):
    template = await asyncio.to_thread(templates.get, flow['template_id'])
    token = uuid.uuid4().hex[:12]
    flow['picker'] = {'token': token, 'field': field, 'mode': 'similar',
                      'context': _edit_context(user_id, flow), 'revision': template['revision']}
    flow['similar_field'] = field
    _pending(user_id, flow, 'similarinput')
    await message.answer('Введите ' + _TITLES.get(field, field) + '.', reply_markup=_menu([
        [('⬅️ Назад', f'prd:pv:{token}:back:0')], [('❌ Отмена', f'prd:pv:{token}:cancel:0')]]))


async def _variable_model(user_id, flow, templates):
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
    if flow.get('node_state'):
        fields = [key for key in fields if key not in LEGACY_NODE_VARIABLES | set(template.get('node_bindings', {})) | {'product_name'}]
    return template, defaults, fields


async def _wizard_prompt(message, user_id, flow, templates):
    if flow['purpose'] == 'single' and flow.get('route_id'):
        state = flow.get('node_state')
        if not flow.get('node_ready') or not state or state.get('variant_id') != flow.get('variant_id'):
            _, _, _, drafts = _kit(user_id)
            return await node_ui.begin(message, user_id, flow, drafts)
    template, defaults, fields = await _variable_model(user_id, flow, templates)
    if flow['purpose'] == 'single':
        fields.append('price')
    index = flow.get('field_index', 0)
    if index < len(fields):
        field = fields[index]
        flow['field'] = field
        _pending(user_id, flow, 'variables')
        default = defaults.get(field, 'название игры' if field == 'product_name' else 'обязательно')
        flow.pop('picker', None)
        if field != 'price' and (field == 'quantity' or field in defaults or template['validation_rules'].get(field, {}).get('choices')):
            await _variable_picker(message, user_id, flow, template, field,
                                   flow['variables'].get(field, defaults.get(field)), 'wizard')
            return False
        await message.answer(f"{_TITLES.get(field, field)} [{default}]\n"
                             'Введите значение; «-» — оставить default. ❌ Отмена — выйти. '
                             'Только публичное описание товара: не вводите логины, пароли, токены, cookies или API keys.',
                             parse_mode=None)
        return False
    return True


async def _variant_picker(callback, user_id, variants, offset=0):
    flow = _active(user_id)
    rows = await asyncio.to_thread(variants.list, flow['route_id'], limit=_PAGE + 1, offset=offset)
    if flow.get('similar'):
        token = uuid.uuid4().hex[:12]
        flow['similar_token'] = token
        flow['similar_variants'] = {'token': token, 'ids': [r['id'] for r in rows[:_PAGE]]}
        keyboard = [[(r['display_name'][:50], f"prd:sv:{token}:{r['id']}")] for r in rows[:_PAGE]]
        if offset:
            keyboard.append([('⬅️ Страница', _local_action(flow, 'variant', max(0, offset - _PAGE)))])
        if len(rows) > _PAGE:
            keyboard.append([('Далее ➡️', _local_action(flow, 'variant', offset + _PAGE))])
        keyboard += [[('Без варианта / defaults', f'prd:sv:{token}:base')],
                     [('⬅️ Назад', _local_action(flow, 'menu'))],
                     [('❌ Отмена', _local_action(flow, 'cancel'))]]
        await _show(callback, '🎭 <b>Вариант похожего draft</b>', _menu(keyboard))
        return
    token = uuid.uuid4().hex[:12]
    flow['variant_picker'] = {'token': token, 'context': _edit_context(user_id, flow),
                              'route_id': flow['route_id'], 'ids': [r['id'] for r in rows[:_PAGE]]}
    keyboard = [[(row['display_name'][:50], f"prd:vp:{token}:pick:{row['id']}")] for row in rows[:_PAGE]]
    if offset:
        keyboard.append([('⬅️ Страница', f'prd:vp:{token}:page:{max(0, offset - _PAGE)}')])
    if len(rows) > _PAGE:
        keyboard.append([('Далее ➡️', f'prd:vp:{token}:page:{offset + _PAGE}')])
    keyboard += [[('Без варианта / defaults', f'prd:vp:{token}:pick:base')], [('🔙 Маршруты', 'prd:routepage:0')]]
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
    if callback.from_user.id != user_id or not ui._owner_id(user_id):
        await callback.answer('Доступно только владельцу.', show_alert=True)
        return True
    await callback.answer()
    catalog_valid = False
    try:
        store, templates, routes, drafts = _kit(user_id)
        parts = action.split(':')
        command = parts[1] if len(parts) > 1 else 'home'
        if command == 'routeform':
            await _check_route_form(callback, user_id, parts, store, templates, routes)
            return True
        if user_id in _flows:
            _flows[user_id].pop('route_form', None)
        if command in {'pubopen', 'pubpreview', 'pub'}:
            await _publication_callback(callback, user_id, parts, store, routes)
            return True
        if user_id in _flows:
            _flows[user_id].pop('publication', None)
        if command == 'preset':
            flow = _active(user_id)
            if (len(parts) != 4 or parts[2] != flow.get('type_token')
                    or flow.get('type_context') != _catalog_context(routes, store, None)
                    or parts[3] not in dict(BUILTIN_TYPES)):
                raise ValueError('Выбор типа устарел.')
        builtin = await asyncio.to_thread(templates.ensure_builtin)
        if command == 'nb':
            await node_ui.bindings_callback(callback, user_id, _active(user_id), parts, drafts, templates)
            return True
        if command == 'bindings':
            flow = _active(user_id)
            if len(parts) != 3 or parts[2] != flow.get('preview_token'):
                raise ValueError('Предпросмотр устарел.')
            await node_ui.begin_bindings(callback, user_id, flow, drafts)
            return True
        if command == 'rt':
            flow = _active(user_id); state = flow.get('catalog_state')
            if (len(parts) != 5 or not state or parts[2] != state['token']
                    or flow.get('purpose') != 'single'
                    or state['context'] != _catalog_context(routes, store, flow['template_id'])):
                raise ValueError('Результаты устарели.')
            catalog_valid = True
        elif command == 'find':
            flow = _active(user_id)
            if len(parts) != 3 or parts[2] != flow.get('search_token'):
                raise ValueError('Поиск устарел.')
        if command == 'nf':
            flow = _active(user_id)
            state = await node_ui.validate(user_id, flow, parts)
        if command in {'variantpick', 'variantpage'}:
            raise ValueError('Старая кнопка варианта; выберите игру заново.')
        if command == 'vp':
            flow = _active(user_id); state = flow.get('variant_picker')
            if (len(parts) != 5 or not state or parts[2] != state['token']
                    or state['context'] != _edit_context(user_id, flow)
                    or state['route_id'] != flow.get('route_id')):
                raise ValueError('Варианты устарели.')
            operation, value = parts[3:5]
            if operation == 'pick' and (value == 'base' or value in state['ids']):
                command, parts = 'variantpick', ['prd', 'variantpick', value]
            elif operation == 'page' and int(value) >= 0 and int(value) % _PAGE == 0:
                command, parts = 'variantpage', ['prd', 'variantpage', value]
            else:
                raise ValueError('Выбор варианта устарел.')
            flow.pop('variant_picker', None)
        if command in {'pv', 'sim', 'sv'}:
            flow = _active(user_id)
            if command == 'pv':
                state = flow.get('picker')
                valid = (len(parts) == 5 and state and parts[2] == state['token']
                         and state['context'] == _edit_context(user_id, flow))
            else:
                state = flow.get('similar_variants') if command == 'sv' else None
                expected = state['token'] if state else flow.get('similar_token')
                valid = (len(parts) == (4 if command == 'sv' else 5) and flow.get('similar')
                         and not flow.get('stored_id') and parts[2] == expected
                         and flow.get('edit_context') == _edit_context(user_id, flow))
            if not valid:
                raise ValueError('Экран редактирования устарел.')
        if command in {'save', 'edit', 'text', 'discard', 'clone'}:
            flow = _active(user_id)
            index = 3 if command in {'edit', 'text'} else 2
            if not flow.get('preview_token') or len(parts) != index + 1 or parts[index] != flow['preview_token']:
                raise ValueError('Предпросмотр устарел.')
            if command == 'edit':
                flow.pop('preview_token', None)
        elif command in {'confirm', 'cancel'}:
            flow = _active(user_id)
            if len(parts) != 3 or not flow.get('batch_token') or parts[2] != flow['batch_token']:
                raise ValueError('Предпросмотр устарел.')
        elif command == 'variantsave':
            flow = _active(user_id)
            if len(parts) != 3 or parts[2] != flow.get('variant_token') or 'variant_pending' not in flow:
                raise ValueError('Предпросмотр устарел.')
        elif user_id in _flows:
            _flows[user_id].pop('preview_token', None)
        if command != 'types' and user_id in _flows:
            _flows[user_id].pop('type_token', None)
            _flows[user_id].pop('type_context', None)
        # Leaving a batch confirmation abandons its controls even when navigation
        # reuses the same flow dict. Keep the token for repeated confirm/cancel only.
        if command not in {'confirm', 'cancel'} and user_id in _flows:
            flow = _flows[user_id]
            if flow.get('kind') in {'routes', 'drafts'}:
                flow.pop('batch_token', None)
                flow.pop('kind', None)
        if command not in {'rt', 'find'} and user_id in _flows:
            _flows[user_id].pop('catalog_state', None)
            _flows[user_id].pop('search_token', None)
        if command not in {'pv', 'sim', 'sv'} and user_id in _flows:
            _flows[user_id].pop('picker', None)
            _flows[user_id].pop('similar_token', None)
            _flows[user_id].pop('similar_variants', None)
        if command not in {'variantpick', 'variantpage'} and user_id in _flows:
            _flows[user_id].pop('variant_picker', None)
        if command != 'pv':
            ui._interaction_state.pop(user_id, None)
        if user_id in _flows:
            _flows[user_id].pop('binding_state', None)
        if command == 'home':
            discard_session(user_id)
            await _show(callback, *_home())
        elif command == 'advanced':
            discard_session(user_id)
            await _show(callback, *_advanced())
        elif command == 'choose':
            await _template_picker(callback, parts[2], int(parts[3]), templates)
        elif command == 'types':
            token = uuid.uuid4().hex[:12]
            _flows[user_id] = {'type_token': token, 'type_context': _catalog_context(routes, store, None)}
            await _show(callback, '🧩 <b>Тип товара · LOCAL DRAFT</b>\n'
                'Далее явно выберите категорию FunPay и фактические параметры. '
                'Не вводите логины, пароли, токены или одноразовые коды. '
                'Указывайте только фактически разрешённую передачу; заявление владельца не является проверкой правил провайдера.',
                _menu([[(label, f'prd:preset:{token}:{kind}')] for kind, label in BUILTIN_TYPES]
                      + [[('🔙 Лоты', 'products')]]))
        elif command in {'pick', 'quick', 'preset'}:
            # One-tap GAME_KEY entry into the same scoped, confirmed draft wizard.
            if command == 'preset':
                chosen = await asyncio.to_thread(templates.ensure_builtin, parts[3])
                purpose, template_id = 'single', chosen['id']
            else:
                purpose, template_id = ('single', builtin['id']) if command == 'quick' else parts[2:4]
            await asyncio.to_thread(templates.get, template_id)
            if purpose not in {'single', 'bulk', 'routes'}:
                raise ValueError('Недоступное действие.')
            flow = {'purpose': purpose, 'template_id': template_id, 'variables': {}, 'query': ''}
            _flows[user_id] = flow
            if purpose == 'bulk':
                _pending(user_id, flow, 'massdrafts')
                await callback.message.answer('До 100 строк: Game | price. Параметры FunPay потребуют отдельного resolution; готовые legacy batches сохраняются как прежде.')
            else:
                await _route_list(callback, user_id, store)
        elif command in {'routepage', 'recent'}:
            await _route_list(callback, user_id, store, int(parts[2]), command == 'recent')
        elif command == 'find':
            await _route_list(callback, user_id, store)
        elif command == 'rt':
            operation, value = parts[3:5]
            catalog = GameCatalog(routes.client, store)
            if operation not in {'cancel', 'back', 'search', 'refresh'}:
                snapshot = await asyncio.to_thread(catalog.snapshot)
                if snapshot['generation'] != state['generation']:
                    raise RouteError('Каталог изменился.')
            if operation == 'cancel':
                discard_session(user_id)
                await _show(callback, *_home())
            elif operation == 'search':
                flow.pop('catalog_state', None)
                _pending(user_id, flow, 'search')
                await callback.message.answer('Введите название категории каталога или сохранённый alias маршрута.')
            elif operation == 'refresh':
                await _catalog_results(callback, user_id, store, routes, force=True)
            elif operation == 'back':
                if state['view'] == 'confirm':
                    await _catalog_results(callback, user_id, store, routes,
                                           category_id=state['category_id'], auto=False)
                elif state['view'] == 'sections':
                    await _catalog_results(callback, user_id, store, routes, auto=False)
                else:
                    await _route_list(callback, user_id, store, catalog_fallback=False)
            elif operation == 'gpage' and state['view'] == 'games':
                await _catalog_results(callback, user_id, store, routes, offset=int(value), auto=False)
            elif operation == 'game' and state['view'] == 'games':
                if int(value) not in {row['category_id'] for row in state['games']}:
                    raise ValueError('Игра не была показана.')
                await _catalog_results(callback, user_id, store, routes, category_id=int(value))
            elif operation == 'spage' and state['view'] == 'sections':
                await _catalog_results(callback, user_id, store, routes, category_id=state['category_id'],
                                       offset=int(value), auto=False)
            elif operation == 'node' and state['view'] == 'sections':
                await _catalog_confirmation(callback, user_id, store, routes, int(value))
            elif operation == 'confirm' and state['view'] == 'confirm':
                row = await asyncio.to_thread(_save_catalog_route, store, routes, flow['template_id'], state)
                if _flows.get(user_id) is flow:
                    await _start_route_draft(callback, user_id, row, store, routes, templates)
            else:
                raise ValueError('Действие устарело.')
        elif command == 'nf':
            await node_ui.handle(callback, user_id, flow, state, parts[3], parts[4], drafts)
        elif command == 'clone':
            flow = _active(user_id)
            if not flow.get('stored_id') or flow.get('dirty'):
                raise ValueError('Сначала сохраните исходный draft.')
            source = await asyncio.to_thread(store.get_draft, flow['stored_id'])
            warnings = drafts.input_warnings(source) if source else []
            try:
                preview = await asyncio.to_thread(drafts.clone_preview, flow['stored_id'], flow['stored_revision'])
            except (NodeFormError, NodeSelectionError, NodeVariantConflictError):
                raise
            except ValueError:
                await callback.message.answer(STALE_WARNING + ' Исходный draft или его конфигурация изменились. '
                    'Откройте его заново либо выберите актуальный route через «Создать».',
                    reply_markup=_menu([[('➕ Создать / выбрать route', 'prd:choose:single:0')], [('Drafts', 'prd:drafts:0')]]))
                return True
            flow = {'purpose': 'single', 'similar': True, 'preview': preview, 'dirty': True,
                    'template_id': preview['template_id'], 'route_id': preview['route_id'], 'clone_warnings': warnings}
            _flows[user_id] = flow
            if 'node_fields' not in preview:
                flow['clone_warnings'] = [LEGACY_WARNING, *warnings]
                await callback.message.answer(LEGACY_WARNING)
                await node_ui.begin(callback.message, user_id, flow, drafts, mode='similar')
            else:
                await _similar_screen(callback.message, user_id, flow, templates, drafts)
        elif command == 'pv':
            await _picker_callback(callback, user_id, flow, state, parts[3], parts[4], templates, drafts)
        elif command == 'sim':
            operation, value = parts[3:5]
            flow.pop('similar_token', None)
            flow.pop('picker', None)
            flow.pop('similar_variants', None)
            if operation == 'cancel':
                discard_session(user_id)
                await _show(callback, *_home())
            elif operation == 'menu':
                await _similar_screen(callback.message, user_id, flow, templates, drafts)
            elif operation == 'variable':
                index = int(value)
                if not 0 <= index < len(flow['similar_fields']):
                    raise ValueError('Поле устарело.')
                field = flow['similar_fields'][index]
                template = await asyncio.to_thread(templates.get, flow['template_id'])
                current = flow['preview']['variables'].get(field)
                if field in template['defaults'] or field == 'quantity' or template['validation_rules'].get(field, {}).get('choices'):
                    await _variable_picker(callback.message, user_id, flow, template, field, current, 'similar')
                else:
                    await _similar_text_prompt(callback.message, user_id, flow, templates, field)
            elif operation in {'node', 'node_refresh'}:
                index = int(value) if operation == 'node' else None
                if index is not None and not 0 <= index < len(flow['node_edit_fields']):
                    raise ValueError('Поле устарело.')
                await node_ui.begin(callback.message, user_id, flow, drafts, mode='similar',
                                    force=operation == 'node_refresh', field_index=index)
            elif operation == 'price':
                await _similar_text_prompt(callback.message, user_id, flow, templates, 'price')
            elif operation == 'variant':
                await _variant_picker(callback, user_id, drafts.variants, int(value))
            elif operation == 'preview':
                flow['similar_token'] = uuid.uuid4().hex[:12]
                await _show(callback, *_preview_screen(flow))
            elif operation == 'save':
                await _save_product_draft(callback, user_id, flow, store, drafts)
            else:
                raise ValueError('Экран устарел.')
        elif command == 'sv':
            variant_id = None if parts[3] == 'base' else parts[3]
            if variant_id is not None and variant_id not in state['ids']:
                raise ValueError('Вариант не был показан.')
            flow.pop('similar_variants', None)
            flow.pop('similar_token', None)
            flow['preview'] = await asyncio.to_thread(drafts.switch_variant, flow['preview'], variant_id)
            await _similar_screen(callback.message, user_id, flow, templates, drafts)
        elif command == 'search':
            flow = _active(user_id); _pending(user_id, flow, 'search')
            await callback.message.answer('Введите название категории каталога или alias маршрута. ❌ Отмена — выйти.')
        elif command == 'route':
            flow = _active(user_id)
            row = await asyncio.to_thread(routes.resolve_route, parts[2])
            if row['template_id'] != flow['template_id']:
                raise ValueError('Маршрут относится к другому шаблону.')
            if flow['purpose'] == 'routes':
                flow['route_id'] = row['id']
                template = await asyncio.to_thread(templates.get, flow['template_id'])
                token = None
                if template['name'] == 'GAME_KEY':
                    token = uuid.uuid4().hex[:12]
                    flow['route_form'] = dict(token=token, route=deepcopy(row), template=deepcopy(template),
                        context=_catalog_context(routes, store, flow['template_id']))
                await _show(callback, *_route_screen(row, token))
            else:
                await _start_route_draft(callback, user_id, row, store, routes, templates)
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
                'подтверждённый раздел той же категории каталога. enabled=false отключает variant.\n'
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
            if len(parts) != 3 or parts[2] != flow.get('variant_token'):
                raise ValueError('Предпросмотр устарел.')
            body = flow['variant_pending']; old = flow.get('variant_edit')
            if body['route_id'] != flow['route_id']:
                raise ValueError('Вариант другого маршрута.')
            saved = await asyncio.to_thread(VariantService(store, routes).save, body,
                old['id'] if old else None, old['revision'] if old else None)
            flow.pop('variant_pending', None)
            flow.pop('variant_token', None)
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
                    'Можно отправить только URL либо точное имя категории каталога; несколько разделов дадут warning.\n'
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
            await callback.message.answer('Название категории для поиска по каталогу FunPay:')
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
            keyboard = []
            for row in rows[:_PAGE]:
                keyboard.append([(_draft_label(row), f"prd:open:{row['id']}")])
                if _publication_candidate(row):
                    keyboard.append([('👁 PREVIEW · Elden Ring', f"prd:pubopen:{row['id']}:{row['revision']}")])
            keyboard += _page_buttons('prd:drafts', offset, len(rows) > _PAGE)
            keyboard.append([('Лоты', 'products')])
            await _show(callback, '📝 <b>Сохранённые product drafts</b>', _menu(keyboard))
        elif command == 'open':
            body = await asyncio.to_thread(store.get_draft, parts[2])
            if body is None:
                raise ValueError('Черновик не найден.')
            original = body
            if 'node_fields' in body:
                body = await asyncio.to_thread(drafts.refresh_preview, body)
            flow = {'preview': body, 'stored_id': original['id'], 'stored_revision': original['revision'], 'dirty': False}
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
            if 'node_fields' in flow['preview']:
                template = await asyncio.to_thread(templates.get, flow['preview']['template_id'])
                content_fields = [key for key in template['allowed_variables']
                    if key not in LEGACY_NODE_VARIABLES | set(template.get('node_bindings', {})) | {'game'}]
                prompts['variables'] = ('Content variables, по одной строке key=value: '
                    + ', '.join(content_fields) + '. Параметры FunPay меняются через «Параметры» похожего draft.')
            if field not in prompts:
                raise ValueError('Неподдерживаемое поле.')
            flow['edit_field'] = field; _pending(user_id, flow, 'draftedit')
            await callback.message.answer(prompts[field] + '\n❌ Отмена — выйти без сохранения.')
        elif command == 'text':
            body = _active(user_id)['preview']['payload']; language = parts[2]
            if language == 'node' and 'node_fields' in _active(user_id)['preview']:
                await _plain_chunks(callback.message, '\n'.join(row['display_label'] + ': ' + row['option_label'] for row in _active(user_id)['preview']['node_fields']))
                return True
            if language not in {'ru', 'en'}:
                raise ValueError('Неподдерживаемый язык.')
            await _plain_chunks(callback.message, body[f'title_{language}'] + '\n\n' + body[f'description_{language}'])
        elif command == 'save':
            await _save_product_draft(callback, user_id, _active(user_id), store, drafts)
        elif command in {'confirm', 'cancel'}:
            flow = _active(user_id)
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
    except PublisherError as cause:
        if cause.reason == 'ALREADY_CLAIMED':
            text = ('🔒 Для этого draft уже есть постоянный барьер попытки. Результат может быть UNKNOWN. '
                    'Не повторяйте отправку и не создавайте копию для обхода. Проверьте свои лоты '
                    'на сайте только чтением: ID, владельца, node 986, цену, количество и выдачу. '
                    'Не очищайте lot_publish_attempts; автоматического retry нет.')
        else:
            text = ('🔒 PREVIEW / проверка отклонены: ' + cause.reason
                    + '. Откройте сохранённый draft заново; проверьте owner/account, route, '
                    'revision, три выбора, quantity=1 и SAFE_MODE. POST не выполнялся.')
        await callback.message.answer(text)
    except NodeSelectionError:
        if user_id in _flows and _flows[user_id].get('node_state'):
            await node_ui.error(callback.message, user_id, _flows[user_id], STALE_WARNING)
        else:
            await callback.message.answer(STALE_WARNING, reply_markup=_menu([
                [('➕ Создать / выбрать параметры', 'prd:choose:single:0')], [('Drafts', 'prd:drafts:0')]]))
    except NodeVariantConflictError:
        await callback.message.answer(VARIANT_WARNING, reply_markup=_menu([
            [('➕ Выбрать route / Variant заново', 'prd:choose:single:0')]]))
    except NodeFormError as cause:
        if user_id in _flows and _flows[user_id].get('node_state'):
            await node_ui.error(callback.message, user_id, _flows[user_id], diagnostic=cause)
        else:
            await callback.message.answer(node_ui.safe_error_text(cause))
    except (ValueError, KeyError, IndexError, StateError, sqlite3.Error):
        if catalog_valid and _flows.get(user_id) is flow and flow.get('catalog_state'):
            await _catalog_error(callback, flow)
        else:
            await callback.message.answer('Не удалось выполнить действие. Проверьте данные или откройте «Лоты» заново. Ничего не опубликовано.')
    return True


async def _save_product_draft(callback, user_id, flow, store, drafts):
    if flow['preview'].get('status') == 'cancelled':
        raise ValueError('Черновик отменён.')
    if flow.get('stored_id'):
        if not flow.get('dirty'):
            await callback.message.answer('Этот draft уже сохранён.')
            return
        if 'node_fields' in flow['preview']:
            reviewed = await asyncio.to_thread(drafts.rebuild, flow['preview'])
            if reviewed != flow['preview']:
                raise NodeSelectionError()
        row = await asyncio.to_thread(store.edit_draft, flow['stored_id'], flow['preview'], flow['stored_revision'])
    else:
        if not flow.get('batch_token'):
            batch = await asyncio.to_thread(drafts.stage, [flow['preview']])
            flow['batch_token'] = batch['token']
            if _flows.get(user_id) is not flow:
                await asyncio.to_thread(store.cancel_batch, batch['token'])
                return
        result = await asyncio.to_thread(drafts.confirm, flow['batch_token'])
        if not result['results']:
            raise ValueError('Draft не сохранён: пакет отменён или устарел.')
        row = result['results'][0]
    flow.update(preview=row, stored_id=row['id'], stored_revision=row['revision'], dirty=False)
    flow.pop('preview_token', None)
    flow.pop('similar_token', None)
    flow.pop('picker', None)
    _preview_screen(flow)
    await callback.message.answer('✅ Draft сохранён локально.', reply_markup=_menu([[('📑 Создать похожий', 'prd:clone:' + flow['preview_token'])], [('Открыть', f"prd:open:{row['id']}")], [('Лоты', 'products')]]))


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
        if (step in {'variables', 'similarinput', 'draftedit'} and flow.get('edit_context')
                and flow['edit_context'] != _edit_context(user_id, flow)):
            raise ValueError('Контекст ввода устарел.')
        if not value.strip() or len(value) > 100000:
            raise ValueError('Пустой или слишком длинный ввод.')
        if step == 'nodefields':
            await node_ui.text(message, user_id, flow, value, drafts)
        elif step in {'search', 'catalogsearch'}:
            if len(value) > 200:
                raise ValueError('Слишком длинный запрос.')
            flow['query' if step == 'search' else 'catalog_query'] = value.strip()
            ui._interaction_state.pop(user_id, None)
            action = 'prd:routepage:0' if step == 'search' else 'prd:catalog:0'
            if step == 'search' and flow.get('purpose') == 'single':
                flow.pop('catalog_state', None)
                flow['search_token'] = uuid.uuid4().hex[:12]
                action = 'prd:find:' + flow['search_token']
            await message.answer('🔎 Поиск готов.', reply_markup=_menu([[('Показать результаты', action)], [('Лоты', 'products')]]))
        elif step == 'variables':
            field = flow['field']
            if value != '-':
                if field == 'price':
                    flow['price'] = validate_price(value)
                else:
                    template = await asyncio.to_thread(templates.get, flow['template_id'])
                    value = validate_variable_value(template, field, value)
                    flow['variables'][field] = value
            elif field == 'price' and not flow.get('price'):
                raise ValueError('Цена обязательна.')
            elif field != 'price':
                _, defaults, _ = await _variable_model(user_id, flow, templates)
                if field not in flow['variables'] and field not in defaults:
                    raise ValueError('У поля нет default; укажите фактическое значение или отмените создание.')
            await _advance_variables(message, user_id, flow, templates, drafts)
        elif step == 'similarinput':
            field = flow['similar_field']
            kwargs = {'price': value} if field == 'price' else {'variables': {field: value}}
            if field != 'price':
                template = await asyncio.to_thread(templates.get, flow['template_id'])
                validate_variable_value(template, field, value)
            flow['preview'] = await asyncio.to_thread(drafts.rebuild, flow['preview'], **kwargs)
            ui._interaction_state.pop(user_id, None)
            await _similar_screen(message, user_id, flow, templates, drafts)
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
            flow['variant_token'] = uuid.uuid4().hex[:12]
            ui._interaction_state.pop(user_id, None)
            await _plain_chunks(message, json.dumps(body, ensure_ascii=False, indent=2))
            await message.answer('Сохранить variant локально?', reply_markup=_menu([
                [('✅ Сохранить variant', f"prd:variantsave:{flow['variant_token']}")],
                [('❌ Cancel', f"prd:variants:{flow['route_id']}:0")]]))
        elif step == 'draftedit':
            field = flow['edit_field']; overrides = {}; variables = None; price = None
            if field == 'price':
                price = value
            elif field == 'emoji':
                overrides['emoji'] = value
            elif field == 'variables':
                variables = _mapping(value)
                if ('node_fields' in flow['preview']
                        and set(variables) & (LEGACY_NODE_VARIABLES | set(flow['preview'].get('node_bindings', {})))):
                    raise ValueError('Используйте editor параметров FunPay.')
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
    except NodeSelectionError:
        await message.answer(STALE_WARNING)
    except NodeVariantConflictError:
        await message.answer(VARIANT_WARNING)
    except NodeFormError as cause:
        if flow.get('node_state'):
            await node_ui.error(message, user_id, flow, diagnostic=cause)
        else:
            await message.answer(node_ui.safe_error_text(cause))
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
