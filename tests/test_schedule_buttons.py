"""Offline schedule buttons, persistence and shared Product navigation regressions."""
import log_isolation
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from funpayflow import telegram as ui, telegram_products as products
from funpayflow.autobump_schedule import normalize_schedule


@pytest.fixture
def context(tmp_path, monkeypatch):
    settings = deepcopy(ui._DEFAULT_GLOBAL_SETTINGS)
    settings['autobump_schedule'] = normalize_schedule({
        'enabled': True, 'start': '06:00', 'stop': '04:00'})
    monkeypatch.setattr(ui, 'bot_settings', settings)
    monkeypatch.setattr(ui, 'SETTINGS_FILE', str(tmp_path / 'settings.json'))
    monkeypatch.setattr(ui, '_autobump_state_version', 0)
    monkeypatch.setattr(ui, '_schedule_sessions', {})
    monkeypatch.setattr(ui, '_schedule_locks', {})
    monkeypatch.setattr(ui, '_interaction_state', {})
    monkeypatch.setattr(ui, '_owner_id', lambda user_id: user_id == 1)
    monkeypatch.setattr(ui, 'is_authorized', lambda user_id: user_id == 1)
    monkeypatch.setattr(ui, 'module_enabled', lambda _: True)
    monkeypatch.setattr(products, '_flows', {})
    monkeypatch.setattr(products, '_locks', {})
    return tmp_path / 'settings.json'


def callback(action):
    return SimpleNamespace(data=action, from_user=SimpleNamespace(id=1), answer=AsyncMock(),
                           message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()))


def message(text):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=1), answer=AsyncMock())


async def dispatch(action):
    cb = callback(action)
    await ui.callback_handler(cb)
    return cb


def action(operation):
    return ui._schedule_action(ui._schedule_sessions[1], operation)


async def open_editor(field='start'):
    await dispatch('bump_schedule')
    return await dispatch(action(field))


def test_schedule_screen_and_overnight(context):
    async def run():
        cb = await dispatch('bump_schedule')
        text = cb.message.edit_text.call_args.args[0]
        keyboard = cb.message.edit_text.call_args.kwargs['reply_markup']
        assert '06:00 → 04:00 следующего дня (22 ч)' in text
        assert 'Перерыв: 04:00 → 06:00' in text
        assert 'Europe/Moscow' in text
        assert 'Включено' in text
        assert [button.text for row in keyboard.inline_keyboard for button in row] == [
            '🌅 Начало: 06:00', '🌙 Конец: 04:00', '✅ Расписание: ВКЛ', '↩️ Назад']
        assert not context.exists()
    asyncio.run(run())


@pytest.mark.parametrize('field,heading', [('start', '🌅 Начало'), ('stop', '🌙 Конец')])
def test_independent_editors_and_button_set(context, field, heading):
    async def run():
        cb = await open_editor(field)
        assert heading in cb.message.edit_text.call_args.args[0]
        keyboard = cb.message.edit_text.call_args.kwargs['reply_markup']
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        assert len(buttons) == 13
        assert all(len(b.callback_data.encode('utf-8')) <= 64 for b in buttons)
        assert ui._schedule_sessions[1]['field'] == field
        assert not ui._interaction_state
    asyncio.run(run())


@pytest.mark.parametrize('start,operation,expected', [
    ('06:37', 'add60', '07:37'), ('06:37', 'add-60', '05:37'),
    ('06:37', 'add15', '06:52'), ('06:37', 'add-15', '06:22'),
    ('06:37', 'add5', '06:42'), ('06:37', 'add-5', '06:32'),
    ('06:37', 'minute0', '06:00'), ('06:37', 'minute15', '06:15'),
    ('06:37', 'minute30', '06:30'), ('06:37', 'minute45', '06:45'),
    ('23:00', 'add60', '00:00'), ('00:00', 'add-60', '23:00'),
    ('23:55', 'add5', '00:00'), ('00:05', 'add-15', '23:50'),
])
def test_adjustments_are_temporary_and_wrap(context, start, operation, expected):
    async def run():
        ui.bot_settings['autobump_schedule']['start'] = start
        await open_editor()
        before = deepcopy(ui.bot_settings)
        cb = await dispatch(action(operation))
        assert f'<b>{expected}</b>' in cb.message.edit_text.call_args.args[0]
        assert ui.bot_settings == before
        assert not context.exists()
    asyncio.run(run())


@pytest.mark.parametrize('field', ['start', 'stop'])
def test_save_only_changes_selected_field_and_persists_once(context, monkeypatch, field):
    original_save = ui.save_settings
    save = Mock(wraps=original_save)
    monkeypatch.setattr(ui, 'save_settings', save)

    async def run():
        await open_editor(field)
        before = deepcopy(ui.bot_settings['autobump_schedule'])
        await dispatch(action('add60'))
        save_action = action('save')
        await asyncio.gather(dispatch(save_action), dispatch(save_action))
        expected = dict(before)
        expected[field] = '07:00' if field == 'start' else '05:00'
        assert ui.bot_settings['autobump_schedule'] == expected
        assert json.loads(context.read_text(encoding='utf-8'))['autobump_schedule'] == expected
        assert save.call_count == 1
        assert ui._autobump_state_version == 1
    asyncio.run(run())


@pytest.mark.parametrize('navigation', ['cancel', 'back', 'menu_main', 'reply_main', 'reply_cancel', 'start'])
def test_cancel_and_navigation_discard_without_saving(context, navigation):
    async def run():
        await open_editor()
        before = deepcopy(ui.bot_settings['autobump_schedule'])
        await dispatch(action('add60'))
        stale_save = action('save')
        if navigation in {'cancel', 'back'}:
            await dispatch(action(navigation))
        elif navigation.startswith('reply_'):
            await ui.text_handler(message('🛠 Главное меню' if navigation == 'reply_main' else '❌ Отмена'))
        elif navigation == 'start':
            await ui.cmd_start(message('/start'))
        else:
            await dispatch(navigation)
        await dispatch(stale_save)
        assert ui.bot_settings['autobump_schedule'] == before
        assert not context.exists()
    asyncio.run(run())


def test_old_revision_actions_and_save_cannot_affect_editor(context):
    async def run():
        await open_editor()
        old_add, old_save = action('add60'), action('save')
        await dispatch(old_add)
        session = deepcopy(ui._schedule_sessions[1])
        await asyncio.gather(dispatch(old_add), dispatch(old_save))
        assert ui._schedule_sessions[1] == session
        assert ui.bot_settings['autobump_schedule']['start'] == '06:00'
        assert not context.exists()
        await dispatch(action('save'))
        assert ui.bot_settings['autobump_schedule']['start'] == '07:00'
    asyncio.run(run())


def test_previous_editor_cannot_change_other_field(context):
    async def run():
        await open_editor('start')
        stale_add, stale_save = action('add60'), action('save')
        await open_editor('stop')
        session = deepcopy(ui._schedule_sessions[1])
        await dispatch(stale_add)
        await dispatch(stale_save)
        assert ui._schedule_sessions[1] == session
        assert not context.exists()
        await dispatch(action('add15'))
        await dispatch(action('save'))
        assert ui.bot_settings['autobump_schedule']['start'] == '06:00'
        assert ui.bot_settings['autobump_schedule']['stop'] == '04:15'
    asyncio.run(run())


@pytest.mark.parametrize('change', ['schedule', 'manual', 'aba'])
def test_concurrent_setting_changes_do_not_get_overwritten(context, change):
    async def run():
        await open_editor()
        await dispatch(action('add60'))
        original = deepcopy(ui.bot_settings['autobump_schedule'])
        if change == 'manual':
            ui.toggle_autobump_saved()
        else:
            ui.save_autobump_schedule({**original, 'stop': '05:00'})
            if change == 'aba':
                ui.save_autobump_schedule(original)
        before = deepcopy(ui.bot_settings)
        persisted = context.read_bytes()
        cb = await dispatch(action('save'))
        assert cb.answer.call_args.kwargs.get('show_alert')
        assert ui.bot_settings == before
        assert context.read_bytes() == persisted
        assert 1 not in ui._schedule_sessions
    asyncio.run(run())


def test_toggle_is_persisted_once_and_keeps_times(context):
    async def run():
        await dispatch('bump_schedule')
        toggle = action('toggle')
        await asyncio.gather(dispatch(toggle), dispatch(toggle))
        policy = ui.bot_settings['autobump_schedule']
        assert policy == normalize_schedule({'start': '06:00', 'stop': '04:00', 'enabled': False})
        assert ui._autobump_state_version == 1
        assert json.loads(context.read_text(encoding='utf-8'))['autobump_schedule'] == policy
    asyncio.run(run())


def test_equal_times_and_failed_save_are_rejected(context, monkeypatch):
    async def run():
        await open_editor()
        await dispatch(action('add-60'))
        await dispatch(action('add-60'))
        cb = await dispatch(action('save'))
        assert cb.answer.call_args.kwargs.get('show_alert')
        assert ui.bot_settings['autobump_schedule']['start'] == '06:00'
        assert not context.exists()
        await open_editor()
        await dispatch(action('add60'))
        monkeypatch.setattr(ui, 'save_settings', Mock(side_effect=RuntimeError('synthetic')))
        cb = await dispatch(action('save'))
        assert cb.answer.call_args.kwargs.get('show_alert')
        assert ui.bot_settings['autobump_schedule']['start'] == '06:00'
        assert ui._autobump_state_version == 0
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['bump_schedule:edit', 'bump_schedule:toggle', 'bump_schedule:invalid:0:save'])
def test_invalid_global_schedule_callbacks_preserve_product_input(context, operation):
    async def run():
        pending = {'action': 'product_variables', 'draft': 'synthetic'}
        flow = {'phase': 'search', 'purpose': 'routes'}
        ui._interaction_state[1] = pending
        products._flows[1] = flow
        await dispatch(operation)
        assert ui._interaction_state[1] is pending
        assert products._flows[1] is flow
        assert not context.exists()
    asyncio.run(run())


def test_stale_editor_after_product_navigation_preserves_product_session(context, monkeypatch):
    async def run():
        await open_editor()
        stale_save = action('save')

        async def product_navigation(cb, operation, user_id):
            if operation == 'products':
                products._flows[user_id] = {'phase': 'search'}
                ui._interaction_state[user_id] = {'action': 'synthetic_product_input'}
                return True
            return False

        monkeypatch.setattr(products, 'products_callback', product_navigation)
        await dispatch('products')
        flow = products._flows[1]
        pending = ui._interaction_state[1]
        assert 1 not in ui._schedule_sessions
        await dispatch(stale_save)
        assert products._flows[1] is flow
        assert ui._interaction_state[1] is pending
        assert not context.exists()
    asyncio.run(run())


def test_legacy_text_input_is_cleared_and_never_persisted(context):
    async def run():
        ui._interaction_state[1] = {'action': 'bump_schedule_edit'}
        before = deepcopy(ui.bot_settings)
        msg = message('09:00 18:00 Europe/Moscow')
        await ui.text_handler(msg)
        assert 'кнопками' in msg.answer.call_args.args[0]
        assert not ui._interaction_state
        assert ui.bot_settings == before
        assert not context.exists()
    asyncio.run(run())


@pytest.mark.parametrize('gate', ['owner', 'module', 'authorization'])
def test_gates_block_all_schedule_writes(context, monkeypatch, gate):
    async def run():
        await open_editor()
        await dispatch(action('add60'))
        save = action('save')
        monkeypatch.setattr(ui, {'owner': '_owner_id', 'module': 'module_enabled',
                                'authorization': 'is_authorized'}[gate], lambda _: False)
        await dispatch(save)
        assert ui.bot_settings['autobump_schedule']['start'] == '06:00'
        assert not context.exists()
    asyncio.run(run())
