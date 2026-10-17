"""Offline reports: bounded valid HTML and notification failures never repeat raises."""
import log_isolation
import ast
import asyncio
from html.parser import HTMLParser
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import requests
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMessage

from funpayflow import telegram as ui
from funpayflow.bump_reports import REPORT_MESSAGE_LIMIT, build_bump_report_chunks, format_seconds
from funpayflow.funpay import _AmbiguousRaiseOutcome
from funpayflow.state import ReviewReceiptStore
import test_autobump_resume as resume_checks


class ReportParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bold = False
        self.titles = []
        self.current = ''

    def handle_starttag(self, tag, attrs):
        assert tag == 'b' and not attrs and not self.bold
        self.bold = True
        self.current = ''

    def handle_endtag(self, tag):
        assert tag == 'b' and self.bold
        self.bold = False
        if self.current != 'Лоты подняты!':
            self.titles.append(self.current)

    def handle_data(self, data):
        if self.bold:
            self.current += data


def titles(chunks):
    result = []
    for chunk in chunks:
        assert len(chunk.encode('utf-16-le')) // 2 <= REPORT_MESSAGE_LIMIT
        parser = ReportParser()
        parser.feed(chunk); parser.close()
        assert not parser.bold
        result.extend(parser.titles)
    return result


def test_short_report_identical_and_no_empty_report():
    assert build_bump_report_chunks([], 60) == []
    chunks = build_bump_report_chunks([('Game', 2, 7200)], 60)
    assert chunks == ['🚀 <b>Лоты подняты!</b>\n\n🎮 <b>Game</b>\n✅ Лоты подняты\n'
                      '⏳ Повтор для этой игры: через 2 ч 00 мин\n\n'
                      '🔄 Следующая общая проверка: через 1 мин']
    assert titles(chunks) == ['Game']
    assert [format_seconds(n) for n in (-1, 45, 90, 7200)] == ['0 сек', '45 сек', '1 мин 30 сек', '2 ч 00 мин']


@pytest.mark.parametrize('size', [3456, 3466, 3467])
def test_single_report_near_safe_limit_is_not_unnecessarily_fragmented(size):
    name = 'a' * size
    chunks = build_bump_report_chunks([(name, 1, 7200)], 60)
    assert len(chunks) == 1
    assert titles(chunks) == [name]
    assert '(1/' not in chunks[0]


@pytest.mark.parametrize('size', [3400, 3498])
def test_long_report_keeps_whole_game_entry_that_fits_without_footer(size):
    names = ['first', 'a' * size, 'last']
    chunks = build_bump_report_chunks([(name, 1, 7200) for name in names], 60)
    assert titles(chunks) == names
    assert '(продолжение)' not in ''.join(chunks)


def test_numbering_width_expands_safely_beyond_nine_pages():
    names = [f'Game {index:02d} ' + 'x' * 3450 for index in range(12)]
    chunks = build_bump_report_chunks([(name, 1, 7200) for name in names], 60)
    assert len(chunks) >= 12
    assert titles(chunks) == names


def test_dozens_of_games_order_no_loss_duplicates_or_split_entries():
    names = [f'Game {i:03d} · ' + 'Long title ' * 4 for i in range(80)]
    chunks = build_bump_report_chunks([(name, 1, 7200) for name in names], 120)
    assert len(chunks) > 1
    assert titles(chunks) == names
    assert all(f'({i + 1}/{len(chunks)})' in chunk for i, chunk in enumerate(chunks))
    assert sum(chunk.count('✅ Лоты подняты') for chunk in chunks) == len(names)
    assert sum(chunk.count('Следующая общая проверка:') for chunk in chunks) == 1


def test_special_characters_are_escaped_once_and_oversized_name_is_lossless():
    names = ["Tom Clancy's The Division", 'App Store & iTunes', "Marvel's Wolverine",
             'Literal &amp; <b>text</b> "quoted"', '😀' * 100, 'Name\nwith newline']
    chunks = build_bump_report_chunks([(name, 1, 7200) for name in names], 60)
    assert titles(chunks) == names
    assert 'App Store &amp; iTunes' in ''.join(chunks)
    assert 'App Store &amp;amp;' not in ''.join(chunks)
    oversized = 'START' + '😀<&\'"' * 3000 + 'END'
    pages = build_bump_report_chunks([(oversized, 1, 7200)], 60)
    assert len(pages) > 1
    assert ''.join(titles(pages)) == oversized
    assert sum(page.count('✅ Лоты подняты') for page in pages) == 1
    assert '(продолжение)' in ''.join(pages)


@pytest.mark.parametrize('character', ['a', '&', '😀', '"', '\u200d'])
def test_encoded_boundaries_include_headers_footer_entities_and_astral_chars(character):
    names = [character * size for size in [1, 3300, 3400, 3500, 3600, 7000]]
    for name in names:
        chunks = build_bump_report_chunks([(name, 1, 7200)], 7200)
        assert ''.join(titles(chunks)) == name


@pytest.fixture
def resume_fixture(tmp_path):
    fixture = resume_checks.ResumeTests(methodName='runTest')
    fixture.path = tmp_path / 'state.sqlite3'
    fixture.store = ReviewReceiptStore(fixture.path)
    fixture.store.initialize()
    # Explicit contexts restore shared asyncio/time even outside unittest's runner.
    with patch('funpayflow.state.time.time', return_value=10000), \
            patch('funpayflow.funpay.asyncio.sleep', new=AsyncMock()):
        yield fixture


def worker(namespace):
    tree = ast.parse(Path('src/funpayflow/main.py').read_text(encoding='utf-8'))
    node = next(n for n in tree.body if getattr(n, 'name', None) == 'auto_bump_loop')
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<worker>', 'exec'), namespace)
    return namespace['auto_bump_loop']


@pytest.mark.parametrize('failure', ['bad_request', 'timeout', 'network', 'connection', None])
def test_report_error_isolated_from_successful_cycle_progress_and_other_recipients(resume_fixture, failure):
    fixture = resume_fixture
    client, raised = fixture.client(list(range(1, 71)))
    profile = client.account.get_user(1)
    for index, subcategory in enumerate(profile.get_sorted_lots(0)):
        subcategory.category.name = f"Game {index:03d} App Store & iTunes " + 'long ' * 10
    client.account.get_user = lambda _: profile
    client.runner_stop_requested = lambda: False
    client.get_runner_health = lambda: {'state': 'healthy'}
    actual_bump = client.bump_lots
    client.bump_lots = AsyncMock(wraps=actual_bump)
    delivered = {1: [], 2: []}
    attempts = {1: 0, 2: 0}
    logs = []

    async def send(recipient, text, **kwargs):
        assert kwargs == {'parse_mode': 'HTML'}
        assert len(text.encode('utf-16-le')) // 2 <= REPORT_MESSAGE_LIMIT
        attempts[recipient] += 1
        if recipient == 1 and attempts[recipient] == 2 and failure:
            method = SendMessage(chat_id=recipient, text='Synthetic')
            errors = {'bad_request': TelegramBadRequest(method=method, message='private-error'),
                      'network': TelegramNetworkError(method=method, message='private-error'),
                      'timeout': TimeoutError('private-error'),
                      'connection': requests.exceptions.ConnectionError('private-error')}
            raise errors[failure]
        delivered[recipient].append(text)

    async def stop_after_cycle(_):
        raise asyncio.CancelledError

    namespace = dict(Bot=object, FunPayClient=object,
        asyncio=SimpleNamespace(sleep=stop_after_cycle),
        _require_env=lambda _: '1', get_autobump_state=lambda: (True, 1),
        is_safe_mode_enabled=lambda: False, restart_requested=lambda: False,
        module_enabled=lambda _: True, bot_settings={'auto_bump': True},
        get_all_recipients=lambda: [1, 2], get_user_settings=lambda _: {},
        _strip_html=lambda text: re.sub(r'<[^>]*>', '', text),
        _is_ignorable_send_error=lambda error: False, _AmbiguousRaiseOutcome=_AmbiguousRaiseOutcome,
        logger=SimpleNamespace(bump=logs.append))

    async def run():
        with patch.object(ui, 'autobump_schedule_allows_now', return_value=True):
            with pytest.raises(asyncio.CancelledError):
                await worker(namespace)(SimpleNamespace(send_message=send), client)
        assert client.bump_lots.await_count == 1
        assert raised == list(range(1, 71))
        assert all(row['state'] == 'success' for row in fixture.rows().values())
        assert all(row['eligible_at'] == 17200 for row in fixture.rows().values())
        # A separate next cycle still honors saved cooldown despite notification failure.
        success, text, _ = await actual_bump(1)
        assert success and not text and raised == list(range(1, 71))

    asyncio.run(run())
    assert len(delivered[2]) > 2
    assert len(titles(delivered[2])) == 70
    if failure:
        assert attempts[1] == 2 and len(delivered[1]) == 1
        assert any('Не удалось отправить отчет:' in line for line in logs)
    else:
        assert delivered[1] == delivered[2]
    assert 'private-error' not in '\n'.join(logs)
    assert 'App Store &amp;' not in '\n'.join(logs)
