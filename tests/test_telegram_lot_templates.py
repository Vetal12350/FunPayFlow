"""Draft Telegram navigation without real Telegram/FunPay calls."""
import log_isolation
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from funpayflow import telegram as ui
from funpayflow import telegram_lots as view
from test_lot_templates import setup_service  # Explicit shared offline fixture.


def callback():
    return SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(
        answer=AsyncMock(), edit_text=AsyncMock()))


def test_owner_navigation_edit_preview_cancel_and_restart(setup_service):
    service, client, _ = setup_service

    async def run():
        cb = callback()
        with patch.object(ui, '_owner_id', return_value=True), \
                patch.object(ui, '_runtime_client', client), \
                patch.object(ui, '_interaction_state', {}):
            assert await view.templates_callback(cb, 'lt:discover', 101)
            assert 'lt:select:11' in str(cb.message.edit_text.call_args)
            await view.templates_callback(cb, 'lt:select:11', 101)
            draft = service.list_drafts(101)[0]
            token = draft['draft_id']
            await view.templates_callback(cb, f'lt:edit:{token}:price', 101)
            message = SimpleNamespace(text='14.25', answer=AsyncMock())
            assert await view.templates_input(message, 101)
            assert service.get_draft(101, token)['payload']['price'] == '14.25'
            await view.templates_callback(cb, f'lt:preview:{token}', 101)
            assert service.get_draft(101, token)['state'] == 'previewed'
            ui._interaction_state.clear()  # Simulated process interaction reset.
            await view.templates_callback(cb, 'lot_templates', 101)
            assert token in str(cb.message.edit_text.call_args)
            assert 'Публикация пока недоступна' in str(cb.message.edit_text.call_args)
            await view.templates_callback(cb, f'lt:cancel:{token}', 101)
            assert service.get_draft(101, token)['state'] == 'cancelled'
            client.account.save_lot.assert_not_called()
            assert cb.answer.await_count == 6

    asyncio.run(run())


def test_non_owner_cannot_discover_or_edit(setup_service):
    _, client, _ = setup_service

    async def run():
        cb = callback()
        with patch.object(ui, '_owner_id', return_value=False), \
                patch.object(ui, '_runtime_client', client):
            assert await view.templates_callback(cb, 'lt:discover', 202)
            cb.message.edit_text.assert_not_awaited()
            client.account.get_user.assert_not_called()

    asyncio.run(run())


def test_preview_escapes_html_and_has_no_publish_button(setup_service):
    service, _, _ = setup_service
    draft = service.create_draft(101, 11)
    draft['payload']['title_ru'] = '<' * 500
    draft['payload']['description_ru'] = '<' * 20000
    text, markup = view._screen(draft)
    assert len(text) < 4096
    assert '&lt;' in text
    assert 'lt:confirm' not in str(markup)
    assert 'заблокирована' in text


def test_pagination_restores_oldest_of_eleven_drafts(setup_service):
    service, client, _ = setup_service
    oldest = service.create_draft(101, 11)['draft_id']
    for _ in range(10):
        service.create_draft(101, 11)

    async def run():
        cb = callback()
        with patch.object(ui, '_owner_id', return_value=True), \
                patch.object(ui, '_runtime_client', client), \
                patch.object(ui, '_interaction_state', {}):
            await view.templates_callback(cb, 'lot_templates', 101)
            assert oldest not in str(cb.message.edit_text.call_args)
            assert 'lt:list:10' in str(cb.message.edit_text.call_args)
            await view.templates_callback(cb, 'lt:list:10', 101)
            assert oldest in str(cb.message.edit_text.call_args)
            assert 'lt:list:0' in str(cb.message.edit_text.call_args)
            await view.templates_callback(cb, f'lt:open:{oldest}', 101)
            assert 'Черновик лота' in str(cb.message.edit_text.call_args)
            client.account.save_lot.assert_not_called()

    asyncio.run(run())


def test_repeated_navigation_acknowledges_unchanged_menu(setup_service):
    _, client, _ = setup_service

    async def run():
        cb = callback()
        cb.message.edit_text.side_effect = [None, TelegramBadRequest(
            method=EditMessageText(text='same menu', chat_id=1, message_id=1),
            message='Bad Request: message is not modified')]
        with patch.object(ui, '_owner_id', return_value=True), \
                patch.object(ui, '_runtime_client', client):
            await view.templates_callback(cb, 'lot_templates', 101)
            await view.templates_callback(cb, 'lot_templates', 101)
        assert cb.answer.await_count == 2
        client.account.save_lot.assert_not_called()

    asyncio.run(run())
