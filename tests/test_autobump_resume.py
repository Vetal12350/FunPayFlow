"""Durable lots/raise safety using synthetic accounts and temporary SQLite only."""
import log_isolation
import ast
import asyncio
import copy
import sqlite3
import tempfile
import threading
import sys
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import requests
import FunPayAPI
from funpayflow import telegram as ui
from funpayflow.funpay import FunPayClient, _AmbiguousRaiseOutcome, _AutobumpActionCancelled
from funpayflow.state import ReviewReceiptStore, StateError
from funpayflow.runtime_control import RuntimeAction


def main_function(name, namespace):
    tree = ast.parse(Path('src/funpayflow/main.py').read_text(encoding='utf-8'))
    node = next(node for node in tree.body if getattr(node, 'name', None) == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'main.py', 'exec'), namespace)
    return namespace[name]


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'state.sqlite3'
        self.store = ReviewReceiptStore(self.path)
        self.store.initialize()
        self.clock = patch('funpayflow.state.time.time', return_value=10000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.sleep = patch('funpayflow.funpay.asyncio.sleep', new=AsyncMock())
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def client(self, ids, errors=None, before_dispatch=False):
        errors = errors or {}
        client = object.__new__(FunPayClient)
        client.review_state = self.store
        client.raise_time = {}
        client._account_lock = threading.RLock()
        client._raise_action_gate = threading.local()
        calls = []

        class Subcategory:
            type = object()

            def __init__(self, game_id):
                self.id = game_id
                self.category = SimpleNamespace(id=game_id, name='Synthetic', position=0)

        lots = {Subcategory(game_id): [] for game_id in ids}

        def raise_lots(game_id):
            if before_dispatch and game_id in errors:
                raise errors[game_id]
            if not client._raise_action_gate.is_allowed():
                raise _AutobumpActionCancelled()
            calls.append(game_id)
            # A second SQLite connection proves no transaction spans HTTP.
            with closing(sqlite3.connect(self.path, timeout=0)) as db, db:
                db.execute('BEGIN IMMEDIATE')
            if game_id in errors:
                raise errors[game_id]

        client.account = SimpleNamespace(
            get=lambda: None,
            get_user=lambda _: SimpleNamespace(get_sorted_lots=lambda _: lots),
            raise_lots=raise_lots)
        return client, calls

    def rows(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return {row['game_id']: dict(row) for row in db.execute(
                'SELECT * FROM autobump_progress')}

    async def test_sqlite_snapshot_abort_rolls_back_before_retry(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER abort_snapshot BEFORE INSERT ON autobump_progress "
                       "WHEN NEW.game_id = 2 BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
        client, sent = self.client([1, 2])
        with self.assertRaises(StateError):
            await client.bump_lots(1)
        self.assertEqual(sent, [])
        self.store = ReviewReceiptStore(self.path)
        self.store.initialize()
        self.assertEqual(self.rows(), {})  # no partially committed snapshot
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TRIGGER abort_snapshot')
        client.review_state = self.store
        await client.bump_lots(1)
        self.assertEqual(sent, [1, 2])

    async def test_sqlite_claim_abort_blocks_dispatch_and_survives_reopen(self):
        self.store.autobump_cycle([1])
        before = self.rows()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER abort_claim BEFORE UPDATE ON autobump_progress "
                       "WHEN NEW.state = 'claimed' BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
        client, sent = self.client([1])
        with self.assertRaises(StateError):
            await client.bump_lots(1)
        self.assertEqual(sent, [])
        self.store = ReviewReceiptStore(self.path)
        self.store.initialize()
        self.store.recover_autobump_progress()
        self.assertEqual(self.rows(), before)

    async def test_competing_store_claims_and_stale_attempts_are_fenced(self):
        barrier = threading.Barrier(2)

        def worker(attempt):
            store = ReviewReceiptStore(self.path)
            cycle = store.autobump_cycle([1])
            barrier.wait(timeout=2)
            return cycle[0]['cycle_id'], attempt, store.claim_autobump_target(
                cycle[0]['cycle_id'], 1, attempt)

        # SQLite-only workers: no additional Account, Runner or HTTP client.
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker, attempt) for attempt in ('a', 'b')]
            results = [future.result(timeout=3) for future in futures]
        self.assertEqual(len({cycle for cycle, _, _ in results}), 1)
        self.assertEqual(sum(won for _, _, won in results), 1)
        cycle_id, old_attempt, _ = next(result for result in results if result[2])
        self.now.return_value = 20000  # a claim is not a stealable TTL lease
        reopened = ReviewReceiptStore(self.path)
        self.assertFalse(reopened.claim_autobump_target(cycle_id, 1, 'steal'))
        reopened.recover_autobump_progress()
        self.assertEqual(self.rows()[1]['eligible_at'], 27200)
        reopened.complete_autobump_cycle(cycle_id)
        self.now.return_value = 27200
        new_cycle = reopened.autobump_cycle([1])[0]['cycle_id']
        self.assertTrue(reopened.claim_autobump_target(new_cycle, 1, 'new'))
        before = self.rows()
        reopened.release_autobump_claim(1, old_attempt)
        with self.assertRaises(StateError):
            reopened.finish_autobump_target(1, old_attempt, 'success', 1)
        self.assertFalse(reopened.claim_autobump_target(cycle_id, 1, 'stale'))
        self.assertEqual(self.rows(), before)
        reopened.finish_autobump_target(1, 'new', 'success', 7200)
        self.assertEqual(self.rows()[1]['eligible_at'], 34400)

    async def test_121_saved_successes_survive_interrupted_next_target(self):
        cycle_id = self.store.autobump_cycle(list(range(1, 124)))[0]['cycle_id']
        for game_id in range(1, 122):
            attempt = f'saved-{game_id}'
            self.assertTrue(self.store.claim_autobump_target(cycle_id, game_id, attempt))
            self.store.finish_autobump_target(game_id, attempt, 'success', 7200)
        saved = self.rows()
        self.assertTrue(self.store.claim_autobump_target(cycle_id, 122, 'interrupted'))
        self.now.return_value = 11000
        self.store = ReviewReceiptStore(self.path)
        self.store.initialize()
        self.store.recover_autobump_progress()
        client, sent = self.client(list(range(123, 0, -1)))
        await client.bump_lots(1)
        self.assertEqual(sent, [123])
        rows = self.rows()
        for game_id in range(1, 122):
            self.assertEqual(rows[game_id], {**saved[game_id], 'cycle_id': None, 'position': None})
        self.assertEqual(rows[122]['state'], 'ambiguous')
        self.assertEqual(rows[122]['eligible_at'], 18200)
        self.assertTrue(all(row['cycle_id'] is None for row in rows.values()))

    async def test_unknown_repeated_recovery_preserves_deadline_until_exact_boundary(self):
        client, sent = self.client([1], {1: requests.exceptions.ReadTimeout('synthetic')})
        with self.assertRaises(_AmbiguousRaiseOutcome):
            await client.bump_lots(1)
        self.assertEqual(sent, [1])
        for instant in (11000, 17199):
            self.now.return_value = instant
            self.store = ReviewReceiptStore(self.path)
            self.store.initialize()
            self.store.recover_autobump_progress()
            resumed, sent = self.client([1])
            await resumed.bump_lots(1)
            self.assertEqual(sent, [])
            self.assertEqual(self.rows()[1]['eligible_at'], 17200)
        self.now.return_value = 17200
        await resumed.bump_lots(1)
        self.assertEqual(sent, [1])

    async def test_partial_cycle_restart_reorder_unknown_and_safe_window(self):
        client, sent = self.client([1, 2, 3, 4], {3: requests.exceptions.ReadTimeout('synthetic')})
        with self.assertRaises(_AmbiguousRaiseOutcome):
            await client.bump_lots(1)
        self.assertEqual(sent, [1, 2, 3])
        self.assertEqual([self.rows()[n]['state'] for n in (1, 2, 3, 4)],
                         ['success', 'success', 'ambiguous', 'pending'])
        self.store = ReviewReceiptStore(self.path)
        self.store.initialize()
        self.store.recover_autobump_progress()
        resumed, sent = self.client([4, 3, 2, 1])
        await resumed.bump_lots(1)
        self.assertEqual(sent, [4])
        self.assertTrue(all(row['cycle_id'] is None for row in self.rows().values()))
        await resumed.bump_lots(1)
        self.assertEqual(sent, [4])
        self.now.return_value = 17201
        await resumed.bump_lots(1)
        self.assertEqual(sent, [4, 4, 3, 2, 1])

    async def test_interrupted_claim_survives_process_restart_without_resend(self):
        cycle = self.store.autobump_cycle([1, 2])
        self.assertTrue(self.store.claim_autobump_target(cycle[0]['cycle_id'], 1, 'attempt'))
        self.now.return_value = 11000
        self.store = ReviewReceiptStore(self.path)
        self.store.initialize()
        self.store.recover_autobump_progress()
        client, sent = self.client([1, 2])
        await client.bump_lots(1)
        self.assertEqual(sent, [2])
        self.assertEqual(self.rows()[1]['eligible_at'], 18200)
        self.assertEqual(self.rows()[1]['state'], 'ambiguous')

    async def test_pretransport_failure_retains_pending_cursor(self):
        client, sent = self.client([1, 2], {1: requests.exceptions.ConnectionError('synthetic')}, True)
        result = await client.bump_lots(1)
        self.assertFalse(result[0])
        self.assertEqual(sent, [])
        self.assertEqual(self.rows()[1]['state'], 'pending')
        client, sent = self.client([2, 1])
        await client.bump_lots(1)
        self.assertEqual(sent, [1, 2])

    async def test_claim_failure_blocks_http(self):
        client, sent = self.client([1])
        with patch.object(self.store, 'claim_autobump_target', side_effect=StateError('synthetic')):
            with self.assertRaises(StateError):
                await client.bump_lots(1)
        self.assertEqual(sent, [])
        self.assertEqual(self.rows()[1]['state'], 'pending')

    async def test_off_after_claim_commit_prevents_http(self):
        client, sent = self.client([1])
        cancelled = threading.Event()
        original = self.store.claim_autobump_target

        def claim(*args):
            result = original(*args)
            cancelled.set()
            return result

        with patch.object(self.store, 'claim_autobump_target', side_effect=claim):
            result = await client.bump_lots(1, is_cancelled=cancelled.is_set)
        self.assertEqual(result[2], 0)
        self.assertEqual(sent, [])
        self.assertEqual(self.rows()[1]['state'], 'pending')
        self.assertEqual(self.rows()[1]['eligible_at'], 0)

    async def test_result_save_failure_keeps_durable_claim(self):
        client, sent = self.client([1])
        with patch.object(self.store, 'finish_autobump_target', side_effect=StateError('synthetic')):
            with self.assertRaises(StateError):
                await client.bump_lots(1)
        self.assertEqual(sent, [1])
        self.assertEqual(self.rows()[1]['state'], 'claimed')
        self.store.recover_autobump_progress()
        await client.bump_lots(1)
        self.assertEqual(sent, [1])

    async def test_raise_error_cooldown_survives_restart(self):
        error = FunPayAPI.exceptions.RaiseError.__new__(FunPayAPI.exceptions.RaiseError)
        error.wait_time = 120
        client, sent = self.client([1], {1: error})
        await client.bump_lots(1)
        self.assertEqual(sent, [1])
        self.assertEqual(self.rows()[1]['eligible_at'], 10120)
        self.store = ReviewReceiptStore(self.path)
        self.store.initialize()
        client, sent = self.client([1])
        await client.bump_lots(1)
        self.assertEqual(sent, [])
        self.now.return_value = 10121
        await client.bump_lots(1)
        self.assertEqual(sent, [1])

    async def test_profile_failure_preserves_cycle_and_missing_target_is_skipped(self):
        self.store.autobump_cycle([1, 2])
        client, sent = self.client([2])
        with patch.object(client.account, 'get', side_effect=requests.exceptions.SSLError('synthetic')):
            await client.bump_lots(1)
        self.assertEqual(self.rows()[1]['state'], 'pending')
        await client.bump_lots(1)
        self.assertEqual(sent, [2])
        self.assertEqual(self.rows()[1]['state'], 'skipped')

    async def test_off_on_during_account_preparation_cancels_old_pass(self):
        old_settings, old_client, old_path = copy.deepcopy(ui.bot_settings), ui._runtime_client, ui.SETTINGS_FILE
        client, sent = self.client([1, 2])
        ui._runtime_client = client
        ui.SETTINGS_FILE = str(Path(self.folder.name) / 'settings.json')
        ui.bot_settings['auto_bump'] = True
        state = ui.get_autobump_state()
        entered, release = threading.Event(), threading.Event()
        original = client.account.get_user

        def blocked(user_id):
            entered.set()
            if not release.wait(2):
                raise AssertionError('Worker release missing')
            return original(user_id)

        task = None
        try:
            client.account.get_user = blocked
            task = asyncio.create_task(client.bump_lots(
                1, is_cancelled=lambda: ui.get_autobump_state() != state))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertFalse(ui.toggle_autobump_saved())
            self.assertTrue(ui.toggle_autobump_saved())
            release.set()
            self.assertEqual((await task)[2], 0)
            self.assertEqual(sent, [])
        finally:
            release.set()
            if task is not None and not task.done():
                await task
            ui.bot_settings.clear()
            ui.bot_settings.update(old_settings)
            ui._runtime_client, ui.SETTINGS_FILE = old_client, old_path

    async def test_cancel_inflight_claim_is_ambiguous_and_not_repeated(self):
        client, sent = self.client([1])
        entered, release = threading.Event(), threading.Event()
        original = client.account.raise_lots

        def blocked(game_id):
            original(game_id)  # durable claim before the simulated HTTP completion
            entered.set()
            release.wait(2)

        client.account.raise_lots = blocked
        task = asyncio.create_task(client.bump_lots(1))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            with self.assertRaises(_AmbiguousRaiseOutcome):
                await task
            self.assertEqual(self.rows()[1]['state'], 'ambiguous')
        finally:
            release.set()
        client.account.raise_lots = original
        await client.bump_lots(1)
        self.assertEqual(sent, [1])


class LoopPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = copy.deepcopy(ui.bot_settings)
        self.addCleanup(lambda: (ui.bot_settings.clear(), ui.bot_settings.update(self.settings)))

    async def test_outer_and_late_safety_gates_preserve_user_preference(self):
        for blocker in ('safe_mode', 'manual_off', 'restart', 'backoff'):
            for initially_blocked in (True, False):
                with self.subTest(blocker=blocker, initially_blocked=initially_blocked):
                    blocked = [initially_blocked]
                    ui.bot_settings['auto_bump'] = True
                    preference = copy.deepcopy(ui.bot_settings)

                    def active(name):
                        return blocked[0] and blocker == name

                    def state():
                        return (not active('manual_off'), 1)

                    async def bump(*args, is_cancelled):
                        self.assertFalse(is_cancelled())
                        blocked[0] = True
                        self.assertTrue(is_cancelled())
                        return False, '', 60

                    async def sleep(_):
                        raise asyncio.CancelledError

                    client = SimpleNamespace(
                        bump_lots=AsyncMock(side_effect=bump), runner_stop_requested=lambda: False,
                        get_runner_health=lambda: {'state': 'backoff' if active('backoff') else 'healthy'})
                    namespace = {
                        'Bot': object, 'FunPayClient': object,
                        'asyncio': SimpleNamespace(sleep=sleep),
                        '_require_env': lambda _: '1', 'bot_settings': ui.bot_settings,
                        'get_autobump_state': state,
                        'is_safe_mode_enabled': lambda: active('safe_mode'),
                        'restart_requested': lambda: active('restart'),
                        'module_enabled': lambda _: False,
                        'logger': SimpleNamespace(bump=lambda *_: None),
                    }
                    with patch.object(ui, 'autobump_schedule_allows_now', return_value=True):
                        with self.assertRaises(asyncio.CancelledError):
                            await main_function('auto_bump_loop', namespace)(None, client)
                    self.assertEqual(client.bump_lots.await_count, 0 if initially_blocked else 1)
                    self.assertEqual(ui.bot_settings, preference)

    async def test_unknown_keeps_on_and_uses_pause_without_technical_notice(self):
        ui.bot_settings['auto_bump'] = True
        sent, attempts, delays = [], [], []

        async def bump(*args, **kwargs):
            attempts.append(1)
            raise _AmbiguousRaiseOutcome()

        async def sleep(delay):
            delays.append(delay)
            raise asyncio.CancelledError

        namespace = {
            'Bot': object, 'FunPayClient': object,
            'asyncio': SimpleNamespace(sleep=sleep, current_task=asyncio.current_task,
                                       CancelledError=asyncio.CancelledError),
            '_require_env': lambda _: '1', 'bot_settings': ui.bot_settings,
            'get_autobump_state': ui.get_autobump_state,
            'is_safe_mode_enabled': lambda: False, 'restart_requested': lambda: False,
            '_AmbiguousRaiseOutcome': _AmbiguousRaiseOutcome,
            'logger': SimpleNamespace(bump=lambda *_: None, warning=lambda *_: None),
            'get_all_recipients': lambda: [1], 'module_enabled': lambda _: True,
        }
        client = SimpleNamespace(bump_lots=bump, runner_stop_requested=lambda: False,
                                 get_runner_health=lambda: {'state': 'healthy'})
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=lambda *args: sent.append(args)))
        loop = main_function('auto_bump_loop', namespace)
        with self.assertRaises(asyncio.CancelledError):
            await loop(bot, client)
        self.assertTrue(ui.bot_settings['auto_bump'])
        self.assertEqual(attempts, [1])
        self.assertEqual(delays, [1])  # first second of the stop-aware 60s pause
        self.assertEqual(sent, [])

    async def test_transient_notifications_removed_from_consumer(self):
        source = Path('src/funpayflow/main.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        consumer = next(n for n in tree.body if getattr(n, 'name', '') == 'notifications_loop')
        calls = {n.func.id for n in ast.walk(consumer)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertNotIn('_notify_funpay_connection', calls)
        self.assertNotIn('_send_runtime_notice', calls)
        self.assertNotIn('Бот запущен.', source)
        self.assertNotIn('Соединение с FunPay восстановлено.', source)

    async def test_profile_or_pretransport_error_is_not_a_business_report(self):
        ui.bot_settings['auto_bump'] = True

        async def sleep(_):
            raise asyncio.CancelledError

        namespace = {
            'Bot': object, 'FunPayClient': object,
            'asyncio': SimpleNamespace(sleep=sleep, current_task=asyncio.current_task,
                                       CancelledError=asyncio.CancelledError),
            '_require_env': lambda _: '1', 'bot_settings': ui.bot_settings,
            'get_autobump_state': ui.get_autobump_state,
            'is_safe_mode_enabled': lambda: False, 'restart_requested': lambda: False,
            '_AmbiguousRaiseOutcome': _AmbiguousRaiseOutcome,
            'logger': SimpleNamespace(bump=lambda *_: None),
            'get_all_recipients': lambda: [1], 'module_enabled': lambda _: True,
            'get_user_settings': lambda _: {}, '_strip_html': lambda text: text,
        }
        client = SimpleNamespace(bump_lots=AsyncMock(return_value=(False, 'Temporary read error', 600)),
                                 runner_stop_requested=lambda: False,
                                 get_runner_health=lambda: {'state': 'healthy'})
        bot = SimpleNamespace(send_message=AsyncMock())
        with self.assertRaises(asyncio.CancelledError):
            await main_function('auto_bump_loop', namespace)(bot, client)
        bot.send_message.assert_not_awaited()

    async def test_only_fatal_main_sends_one_best_effort_notice(self):
        response = SimpleNamespace(status_code=403, request=SimpleNamespace(
            url='https://example.invalid/', headers={}, body=None))
        cases = [('runtime', error) for error in (
            None, FunPayAPI.exceptions.UnauthorizedError(response),
            requests.exceptions.SSLError('CERTIFICATE_VERIFY_FAILED'),
            StateError('synthetic'), RuntimeError('synthetic'))]
        cases += [('init', requests.exceptions.ReadTimeout('synthetic')),
                  ('init', FunPayAPI.exceptions.UnauthorizedError(response)),
                  ('init', requests.exceptions.SSLError('CERTIFICATE_VERIFY_FAILED')),
                  ('state', StateError('synthetic'))]
        for stage, error in cases:
            with self.subTest(stage=stage, error=type(error).__name__), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'state.sqlite3'
                sent, closed = [], []

                async def send(*args, **kwargs):
                    sent.append(args)
                    raise TimeoutError('synthetic-private-text')

                async def close():
                    closed.append(True)

                async def supervise(*args):
                    if error is not None:
                        raise error
                    return RuntimeAction.NONE

                def initialize():
                    if stage == 'init':
                        raise error

                def store_factory():
                    store = ReviewReceiptStore(path)
                    if stage == 'state':
                        def fail():
                            raise error
                        store.initialize = fail
                    return store

                namespace = {
                    'asyncio': asyncio, 'sys': sys, 'Path': Path,
                    'Bot': lambda **kwargs: SimpleNamespace(
                        send_message=send, session=SimpleNamespace(close=close)),
                    'DEFAULT_DB_PATH': path, 'SETTINGS_FILE': str(Path(folder) / 'settings.json'),
                    'is_fresh_install': lambda *args: False,
                    'configure_module_runtime': lambda **kwargs: None,
                    'begin_runtime_cycle': lambda: None, '_require_env': lambda _: '1',
                    'FunPayClient': lambda _: SimpleNamespace(
                        account=SimpleNamespace(username='Synthetic'), initialize_account=initialize,
                        _classify_runner_error=FunPayClient._classify_runner_error,
                        _certificate_verification_failed=FunPayClient._certificate_verification_failed,
                        get_runner_health=lambda: {'state': 'healthy'}),
                    'StateError': StateError, 'claim_critical_notice': lambda code: True,
                    '_validate_funpay_user_id': lambda _: None,
                    'ReviewReceiptStore': store_factory,
                    'logger': SimpleNamespace(**{name: lambda *args: None for name in
                                                ('info', 'banner', 'success', 'error', 'warning')}),
                    'os': SimpleNamespace(name='posix'), 'RuntimeAction': RuntimeAction,
                    'set_runtime_status_context': lambda _: None,
                    'clear_runtime_status_context': lambda: None,
                    '_supervise_tasks': supervise, 'get_all_recipients': lambda: [1],
                }
                main_function('_send_runtime_notice', namespace)
                main = main_function('main', namespace)
                if error is None:
                    await main()
                    self.assertEqual(sent, [])
                else:
                    with self.assertRaises(type(error) if stage == 'runtime' else RuntimeError) as raised:
                        await main()
                    if stage == 'runtime':
                        self.assertIs(raised.exception, error)
                    if stage == 'init' and isinstance(error, requests.exceptions.ReadTimeout):
                        self.assertEqual(sent, [])
                    else:
                        self.assertEqual(len(sent), 1)
                        self.assertIn('критической ошибки', sent[0][1])
                self.assertEqual(closed, [True])


if __name__ == '__main__':
    unittest.main()
