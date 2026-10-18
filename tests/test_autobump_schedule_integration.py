"""Offline schedule persistence, Telegram and worker gate regressions."""
import log_isolation
import ast
import asyncio
import copy
import json
import tempfile
import unittest
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from funpayflow import telegram as ui
from funpayflow.autobump_schedule import normalize_schedule, allows_now
from funpayflow.funpay import _AmbiguousRaiseOutcome
from funpayflow.state import ReviewReceiptStore
import test_autobump_resume as resume_checks


class ScheduleIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        settings = copy.deepcopy(ui.bot_settings)
        path, client, modules = ui.SETTINGS_FILE, ui._runtime_client, ui._effective_modules
        interactions = dict(ui._interaction_state)
        sessions, locks = dict(ui._schedule_sessions), dict(ui._schedule_locks)
        self.addCleanup(lambda: (ui._schedule_sessions.clear(), ui._schedule_sessions.update(sessions),
                                ui._schedule_locks.clear(), ui._schedule_locks.update(locks)))
        self.addCleanup(lambda: (ui.bot_settings.clear(), ui.bot_settings.update(settings),
                                setattr(ui, 'SETTINGS_FILE', path),
                                setattr(ui, '_runtime_client', client),
                                setattr(ui, '_effective_modules', modules),
                                ui._interaction_state.clear(),
                                ui._interaction_state.update(interactions)))
        ui.bot_settings.clear()
        ui.bot_settings.update(copy.deepcopy(ui._DEFAULT_GLOBAL_SETTINGS))
        ui.SETTINGS_FILE = str(Path(self.folder.name) / 'settings.json')
        ui.configure_module_runtime(fresh_install=False)

    def test_restart_and_legacy_settings(self):
        schedule = normalize_schedule({'enabled': True})
        ui.save_autobump_schedule(schedule)
        ui.bot_settings['autobump_schedule'] = normalize_schedule()
        ui.load_settings()
        self.assertEqual(ui.bot_settings['autobump_schedule'], schedule)
        saved = json.loads(Path(ui.SETTINGS_FILE).read_text(encoding='utf-8'))
        del saved['autobump_schedule']
        saved['auto_bump'] = False
        Path(ui.SETTINGS_FILE).write_text(json.dumps(saved), encoding='utf-8')
        ui.load_settings()
        self.assertEqual(ui.bot_settings['autobump_schedule'], normalize_schedule())
        self.assertFalse(ui.bot_settings['auto_bump'])

    def test_saved_0700_0400_window_across_midnight_and_restart(self):
        policy = normalize_schedule({'enabled': True, 'start': '07:00', 'stop': '04:00'})
        ui.bot_settings['auto_bump'] = False
        ui.save_autobump_schedule(policy)
        ui.bot_settings['autobump_schedule'] = normalize_schedule()
        ui.load_settings()
        self.assertEqual(ui.bot_settings['autobump_schedule'], policy)
        for day, hour, minute, second, expected in (
                (16, 20, 59, 59, True),   # 23:59:59 MSK
                (16, 21, 0, 0, True),     # local midnight
                (17, 0, 59, 59, True),    # 03:59:59 MSK
                (17, 1, 0, 0, False),     # stop is exclusive
                (17, 3, 59, 59, False),
                (17, 4, 0, 0, True)):     # start is inclusive
            instant = datetime(2026, 10, day, hour, minute, second, tzinfo=timezone.utc)
            with self.subTest(instant=instant), patch.object(
                    ui, 'allows_now', side_effect=lambda value: allows_now(value, instant)):
                self.assertEqual(ui.autobump_schedule_allows_now(), expected)
                self.assertFalse(ui.bot_settings['auto_bump'])
        self.assertEqual(ui.bot_settings['autobump_schedule'], policy)

    async def test_overnight_close_after_claim_releases_before_dispatch(self):
        fixture = resume_checks.ResumeTests(methodName='runTest')
        fixture.path = Path(self.folder.name) / 'overnight.sqlite3'
        fixture.store = ReviewReceiptStore(fixture.path)
        fixture.store.initialize()
        fixture.store.autobump_cycle([1])
        before = fixture.rows()
        client, sent = fixture.client([1])
        ui.save_autobump_schedule(normalize_schedule({
            'enabled': True, 'start': '07:00', 'stop': '04:00'}))
        clock = [datetime(2026, 10, 17, 0, 59, 59, tzinfo=timezone.utc)]
        original = fixture.store.claim_autobump_target

        def claim(*args):
            claimed = original(*args)
            self.assertTrue(claimed)
            clock[0] = datetime(2026, 10, 17, 1, tzinfo=timezone.utc)
            return claimed

        with patch.object(ui, 'allows_now', side_effect=lambda value: allows_now(value, clock[0])), \
                patch.object(fixture.store, 'claim_autobump_target', side_effect=claim), \
                patch('funpayflow.funpay.asyncio.sleep', new=AsyncMock()):
            result = await client.bump_lots(1, is_cancelled=lambda: not ui.autobump_schedule_allows_now())
        self.assertEqual(result[2], 0)
        self.assertEqual(sent, [])
        fixture.store = ReviewReceiptStore(fixture.path)
        fixture.store.initialize()
        fixture.store.recover_autobump_progress()
        self.assertEqual(fixture.rows(), before)

    def test_failed_save_rolls_back_without_version_change(self):
        ui.save_settings(required=True)
        original_json = Path(ui.SETTINGS_FILE).read_bytes()
        previous = ui.get_autobump_state()
        schedule = copy.deepcopy(ui.bot_settings['autobump_schedule'])
        for target, error in [('save_settings', RuntimeError), ('os.replace', OSError)]:
            with self.subTest(target=target), patch(
                    f'funpayflow.telegram.{target}', side_effect=error('synthetic')):
                with self.assertRaises(RuntimeError):
                    ui.save_autobump_schedule(normalize_schedule({'enabled': True}))
            self.assertEqual(ui.get_autobump_state(), previous)
            self.assertEqual(ui.bot_settings['autobump_schedule'], schedule)
            self.assertEqual(Path(ui.SETTINGS_FILE).read_bytes(), original_json)

    def test_invalid_persisted_policy_fails_closed(self):
        ui.save_settings(required=True)
        saved = json.loads(Path(ui.SETTINGS_FILE).read_text(encoding='utf-8'))
        before = copy.deepcopy(ui.bot_settings)
        for value in [None, [], '', False, {'enabled': True, 'start': 'invalid'},
                      {'enabled': True, 'timezone': 'America/New_York'}]:
            with self.subTest(value=value):
                saved['autobump_schedule'] = value
                Path(ui.SETTINGS_FILE).write_text(json.dumps(saved), encoding='utf-8')
                with self.assertRaises(RuntimeError):
                    ui.load_settings()
                self.assertEqual(ui.bot_settings, before)

    async def test_policy_changes_during_account_preparation_prevent_http(self):
        for change in ('schedule', 'manual_off', 'boundary'):
            with self.subTest(change=change):
                fixture = resume_checks.ResumeTests(methodName='runTest')
                fixture.path = Path(self.folder.name) / f'{change}.sqlite3'
                fixture.store = ReviewReceiptStore(fixture.path)
                fixture.store.initialize()
                entered, release = threading.Event(), threading.Event()
                try:
                    client, sent = fixture.client([1])
                    client.review_state.autobump_cycle([1])
                    progress = fixture.rows()
                    ui._runtime_client = client
                    ui.bot_settings['auto_bump'] = True
                    ui.save_autobump_schedule(normalize_schedule({
                        'enabled': True, 'start': '07:00', 'stop': '10:00'}))
                    version = ui.get_autobump_state()
                    clock = [datetime(2026, 10, 16, 6, 59, 59, tzinfo=timezone.utc)]

                    def prepare():
                        entered.set()
                        assert release.wait(2)

                    client.account.get = prepare
                    with patch.object(ui, 'allows_now', side_effect=lambda policy: allows_now(policy, clock[0])):
                        task = asyncio.create_task(client.bump_lots(1, is_cancelled=lambda:
                            ui.get_autobump_state() != version or not ui.autobump_schedule_allows_now()))
                        try:
                            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                            if change == 'schedule':
                                ui.save_autobump_schedule(normalize_schedule({
                                    'enabled': True, 'start': '07:00', 'stop': '09:00'}))
                            elif change == 'manual_off':
                                ui.toggle_autobump_saved()
                            else:
                                clock[0] = datetime(2026, 10, 16, 7, tzinfo=timezone.utc)
                        finally:
                            release.set()
                        self.assertEqual((await task)[2], 0)
                    self.assertEqual(sent, [])
                    self.assertEqual(fixture.rows(), progress)
                finally:
                    release.set()

    async def test_boundary_does_not_cancel_inflight_http_but_blocks_next_target(self):
        fixture = resume_checks.ResumeTests(methodName='runTest')
        fixture.path = Path(self.folder.name) / 'inflight.sqlite3'
        fixture.store = ReviewReceiptStore(fixture.path)
        fixture.store.initialize()
        sleep_patch = patch('funpayflow.funpay.asyncio.sleep', new=AsyncMock())
        sleep_patch.start()
        entered, release = threading.Event(), threading.Event()
        try:
            client, sent = fixture.client([1, 2])
            ui.save_autobump_schedule(normalize_schedule({
                'enabled': True, 'start': '07:00', 'stop': '10:00'}))
            clock = [datetime(2026, 10, 16, 6, 59, 59, tzinfo=timezone.utc)]
            original = client.account.raise_lots

            def http(game_id):
                original(game_id)
                entered.set()
                assert release.wait(2)

            client.account.raise_lots = http
            with patch.object(ui, 'allows_now', side_effect=lambda policy: allows_now(policy, clock[0])):
                task = asyncio.create_task(client.bump_lots(1, is_cancelled=lambda:
                    not ui.autobump_schedule_allows_now()))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    clock[0] = datetime(2026, 10, 16, 7, tzinfo=timezone.utc)
                    self.assertFalse(task.done())
                    self.assertEqual(fixture.rows()[1]['state'], 'claimed')
                finally:
                    release.set()
                await task
            self.assertEqual(sent, [1])
            self.assertEqual(fixture.rows()[1]['state'], 'success')
            self.assertEqual(fixture.rows()[2]['state'], 'pending')
        finally:
            release.set()
            sleep_patch.stop()

    async def test_telegram_owner_toggle_and_legacy_input_fails_closed(self):
        callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(
            answer=AsyncMock(), edit_text=AsyncMock()))
        with patch.object(ui, '_owner_id', return_value=True):
            await ui._autobump_schedule_callback(callback, 'bump_schedule', 1)
            action = ui._schedule_action(ui._schedule_sessions[1], 'toggle')
            await ui._autobump_schedule_callback(callback, action, 1)
            self.assertTrue(ui.bot_settings['autobump_schedule']['enabled'])
            ui._interaction_state[1] = {'action': 'bump_schedule_edit'}
            message = SimpleNamespace(text='09:00 18:00 Europe/Moscow', answer=AsyncMock())
            before = copy.deepcopy(ui.bot_settings['autobump_schedule'])
            self.assertTrue(await ui._autobump_schedule_input(message, 1))
            self.assertEqual(ui.bot_settings['autobump_schedule'], before)
            self.assertNotIn(1, ui._interaction_state)
        text, keyboard = ui._autobump_schedule_screen()
        self.assertIn('круглосуточно', text)
        self.assertIn('Europe/Moscow', text)
        self.assertIn('bump_schedule', str(keyboard))
        self.assertEqual(ui._action_feature('bump_schedule:edit'), 'autobump')

    async def test_non_owner_cannot_change_schedule(self):
        callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        with patch.object(ui, '_owner_id', return_value=False):
            await ui._autobump_schedule_callback(callback, 'bump_schedule:toggle', 2)
        self.assertFalse(ui.bot_settings['autobump_schedule']['enabled'])
        callback.message.edit_text.assert_not_awaited()

    async def test_worker_outer_and_late_gate_without_mutating_settings(self):
        tree = ast.parse(Path('src/funpayflow/main.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if getattr(n, 'name', '') == 'auto_bump_loop')
        for allowed in (False, True):
            with self.subTest(allowed=allowed):
                policy = normalize_schedule({'enabled': True})
                ui.bot_settings['autobump_schedule'] = policy
                ui.bot_settings['auto_bump'] = True
                called = []
                gate = [allowed]

                async def bump(*args, is_cancelled):
                    self.assertFalse(is_cancelled())
                    gate[0] = False
                    self.assertTrue(is_cancelled())
                    called.append(1)
                    return False, '', 60

                async def sleep(_):
                    raise asyncio.CancelledError

                namespace = dict(Bot=object, FunPayClient=object,
                                 asyncio=SimpleNamespace(sleep=sleep),
                                 _require_env=lambda _: '1', get_autobump_state=ui.get_autobump_state,
                                 is_safe_mode_enabled=lambda: False, restart_requested=lambda: False,
                                 module_enabled=lambda _: False, bot_settings=ui.bot_settings,
                                 logger=SimpleNamespace(bump=lambda *_: None))
                exec(compile(ast.Module(body=[node], type_ignores=[]), '<worker>', 'exec'), namespace)
                client = SimpleNamespace(bump_lots=bump, runner_stop_requested=lambda: False,
                                         get_runner_health=lambda: {'state': 'healthy'})
                with patch.object(ui, 'autobump_schedule_allows_now', side_effect=lambda: gate[0]):
                    with self.assertRaises(asyncio.CancelledError):
                        await namespace['auto_bump_loop'](None, client)
                self.assertEqual(called, [1] if allowed else [])
                self.assertTrue(ui.bot_settings['auto_bump'])
                self.assertEqual(ui.bot_settings['autobump_schedule'], policy)

    async def test_unknown_network_outcome_preserves_enabled_policy(self):
        policy = normalize_schedule({'enabled': True})
        ui.bot_settings['autobump_schedule'] = policy
        ui.bot_settings['auto_bump'] = True
        node = next(n for n in ast.parse(Path('src/funpayflow/main.py').read_text(
            encoding='utf-8')).body if getattr(n, 'name', '') == 'auto_bump_loop')

        async def sleep(_):
            raise asyncio.CancelledError

        namespace = dict(Bot=object, FunPayClient=object,
                         asyncio=SimpleNamespace(sleep=sleep, current_task=asyncio.current_task,
                                                 CancelledError=asyncio.CancelledError),
                         _require_env=lambda _: '1', get_autobump_state=ui.get_autobump_state,
                         is_safe_mode_enabled=lambda: False, restart_requested=lambda: False,
                         module_enabled=lambda _: False, bot_settings=ui.bot_settings,
                         _AmbiguousRaiseOutcome=_AmbiguousRaiseOutcome,
                         logger=SimpleNamespace(bump=lambda *_: None, warning=lambda *_: None))
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<worker>', 'exec'), namespace)
        client = SimpleNamespace(bump_lots=AsyncMock(side_effect=_AmbiguousRaiseOutcome),
                                 runner_stop_requested=lambda: False,
                                 get_runner_health=lambda: {'state': 'healthy'})
        with patch.object(ui, 'autobump_schedule_allows_now', return_value=True):
            with self.assertRaises(asyncio.CancelledError):
                await namespace['auto_bump_loop'](None, client)
        client.bump_lots.assert_awaited_once()
        self.assertTrue(ui.bot_settings['auto_bump'])
        self.assertEqual(ui.bot_settings['autobump_schedule'], policy)


if __name__ == '__main__':
    unittest.main()
