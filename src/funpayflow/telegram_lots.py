"""Owner-only draft UI. Publication is visibly blocked by the library adapter."""
import asyncio
from html import escape

from .lot_templates import EDITABLE_FIELDS, LotTemplateError, LotTemplateService
from .state import StateError


_LABELS = {
    'title_ru': 'Название RU', 'title_en': 'Название EN',
    'description_ru': 'Описание RU', 'description_en': 'Описание EN',
    'price': 'Цена', 'amount': 'Количество',
}


def _ui():
    from . import telegram
    return telegram


def _service():
    ui = _ui()
    if ui._runtime_client is None or ui._runtime_client.review_state is None:
        raise LotTemplateError('Хранилище черновиков недоступно.')
    return LotTemplateService(ui._runtime_client,
                              late_check=lambda: not ui.is_safe_mode_enabled()
                              and not ui.restart_requested())


def _screen(draft):
    ui = _ui()
    payload = draft['payload']
    text = ('📝 <b>Черновик лота</b>\n'
            f"Категория: {draft['subcategory_id']} · состояние: {escape(draft['state'])}\n"
            f"Название RU: {escape(payload['title_ru'][:80])}\n"
            f"Название EN: {escape(payload['title_en'][:80])}\n"
            f"Цена: {escape(payload['price'])} · количество: {escape(payload['amount'])}\n"
            f"Описание RU: {escape(payload['description_ru'][:150])}\n"
            f"Описание EN: {escape(payload['description_en'][:150])}\n\n"
            'Длинные тексты в этом сообщении сокращены; черновик хранится полностью.\n'
            '⚠️ Публикация заблокирована: установленная FunPayAPI не подтверждает '
            'безопасную схему создания нового лота. Исходный лот не изменяется.')
    token = draft['draft_id']
    rows = []
    if draft['state'] in {'draft', 'previewed'}:
        rows.extend([[ui._button(label, f'lt:edit:{token}:{field}')]
                     for field, label in _LABELS.items()])
        rows.extend([[ui._button('👁 Предпросмотр', f'lt:preview:{token}')],
                     [ui._button('❌ Отменить черновик', f'lt:cancel:{token}')]])
    rows.append([ui._button('🔙 Шаблоны', 'lot_templates')])
    return text, ui._menu(rows)


async def templates_callback(callback, action, user_id):
    if action != 'lot_templates' and not action.startswith('lt:'):
        return False
    ui = _ui()
    if not ui._owner_id(user_id):
        await callback.answer('Доступно только владельцу.', show_alert=True)
        return True
    await callback.answer()
    try:
        service = _service()
        if action == 'lot_templates' or action.startswith('lt:list:'):
            offset = 0 if action == 'lot_templates' else int(action.removeprefix('lt:list:'))
            rows = []
            drafts = await asyncio.to_thread(service.list_drafts, user_id, offset)
            for draft in drafts:
                rows.append([ui._button(
                    f"📝 {draft['payload']['title_ru'][:35] or draft['payload']['title_en'][:35]}",
                    f"lt:open:{draft['draft_id']}")])
            if offset:
                rows.append([ui._button('⬅️ Предыдущие', f'lt:list:{max(0, offset - 10)}')])
            if len(drafts) == 10:
                rows.append([ui._button('Следующие ➡️', f'lt:list:{offset + 10}')])
            rows.append([ui._button('🔎 Собственные лоты', 'lt:discover')])
            rows.append([ui._button('🔙 Главное меню', 'menu_main')])
            text = ('📝 <b>Шаблоны лотов</b>\n'
                    'Собственный лот → редактируемый черновик → предпросмотр.\n'
                    'Публикация пока недоступна: схема создания не подтверждена '
                    'установленной библиотекой.\n'
                    f'Сохранённые черновики · страница {offset // 10 + 1}:')
            markup = ui._menu(rows)
        elif action == 'lt:discover':
            lots = await asyncio.to_thread(service.discover)
            # Bounded list plus direct ID selection keeps large profiles usable.
            rows = [[ui._button(f"{lot['source_id']} · {lot['title'][:35]}",
                                f"lt:select:{lot['source_id']}")] for lot in lots[:20]]
            rows.extend([[ui._button('Выбрать по ID', 'lt:source')],
                         [ui._button('🔙 Шаблоны', 'lot_templates')]])
            text = (f'🔎 <b>Собственные обычные лоты: {len(lots)}</b>\n'
                    'Первые 20; остальные можно выбрать по ID. Формы с '
                    'неподдерживаемыми параметрами будут отклонены.')
            markup = ui._menu(rows)
        elif action == 'lt:source':
            ui._interaction_state[user_id] = {'action': 'lot_source'}
            await callback.message.answer('Отправьте ID собственного лота. ❌ Отмена — выйти.')
            return True
        else:
            parts = action.split(':')
            command, token = parts[1:3]
            if command == 'select':
                draft = await asyncio.to_thread(service.create_draft, user_id, int(token))
            elif command == 'edit' and len(parts) == 4 and parts[3] in EDITABLE_FIELDS:
                await asyncio.to_thread(service.get_draft, user_id, token)
                ui._interaction_state[user_id] = {
                    'action': 'lot_edit', 'draft_id': token, 'field': parts[3]}
                await callback.message.answer(
                    f'Новое значение «{_LABELS[parts[3]]}». Для пустого EN поля отправьте «-».\n'
                    '❌ Отмена — выйти без изменения.')
                return True
            elif command == 'preview':
                draft = await asyncio.to_thread(service.preview, user_id, token)
            elif command == 'cancel':
                draft = await asyncio.to_thread(service.cancel, user_id, token)
            elif command == 'open':
                draft = await asyncio.to_thread(service.get_draft, user_id, token)
            else:
                raise LotTemplateError('Действие больше недоступно.')
            text, markup = _screen(draft)
        await ui._edit_or_keep_menu(callback.message, text, markup)
    except LotTemplateError as error:
        await callback.message.answer(str(error))  # Only fixed service messages.
    except (StateError, ValueError, IndexError):
        await callback.message.answer('Не удалось прочитать или сохранить черновик.')
    return True


async def templates_input(message, user_id):
    ui = _ui()
    pending = ui._interaction_state.get(user_id, {})
    if pending.get('action') not in {'lot_source', 'lot_edit'}:
        return False
    if not ui._owner_id(user_id):
        ui._interaction_state.pop(user_id, None)
        await message.answer('Доступно только владельцу.')
        return True
    try:
        service = _service()
        value = message.text or ''
        if value.startswith('/'):
            ui._interaction_state.pop(user_id, None)
            await message.answer('Изменение отменено. Команда не сохранена.')
            return True
        if pending['action'] == 'lot_source':
            draft = await asyncio.to_thread(service.create_draft, user_id, int(value))
        else:
            if value == '-' and pending['field'].endswith('_en'):
                value = ''
            draft = await asyncio.to_thread(service.edit_draft, user_id,
                                            pending['draft_id'], pending['field'], value)
        ui._interaction_state.pop(user_id, None)
        text, markup = _screen(draft)
        await message.answer(text, reply_markup=markup, parse_mode='HTML')
    except LotTemplateError as error:
        await message.answer(str(error))
    except (StateError, ValueError):
        await message.answer('Не удалось прочитать или сохранить черновик. Проверьте ввод.')
    return True
