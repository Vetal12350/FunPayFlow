"""Offline restart UI, action-gate, and lifecycle checks; main.py is never imported."""

import log_isolation
import ast
import asyncio
import copy
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import telegram as ui
from funpay import FunPayClient, _AutobumpActionCancelled
from runtime_events import ActionEvent, ActionKind
from runtime_control import (RuntimeAction, automatic_action_gate,
                             begin_runtime_cycle, claim_restart, restart_requested,
                             signal_restart, wait_for_restart)
from state import ReviewReceiptStore


def main_function(name: str, namespace: dict):
    source = ast.parse(Path("main.py").read_text(encoding="utf-8"))
    node = next(node for node in source.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), namespace)
    return namespace[name]


class Callback:
    def __init__(self, action: str, user_id: int = 100):
        self.data = action
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []
        self.edits = []
        self.message = SimpleNamespace(edit_text=self.edit_text)

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


def actions(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class RestartUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        begin_runtime_cycle()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"ADMIN_ID": "100"})
        self.env.start()
        self.old_settings = copy.deepcopy(ui.bot_settings)
        self.old_effective = ui._effective_modules
        self.old_client = ui._runtime_client
        self.old_file = ui.SETTINGS_FILE
        self.old_import = ui._sales_import_session
        ui.SETTINGS_FILE = str(Path(self.tmp.name) / "settings.json")
        ui.bot_settings.clear()
        ui.bot_settings.update(copy.deepcopy(ui._DEFAULT_GLOBAL_SETTINGS))
        ui.configure_module_runtime(fresh_install=False)
        ui._module_drafts.clear()
        ui._interaction_state.clear()
        ui._restart_confirmations.clear()
        ui._sales_import_session = None
        self.store = ReviewReceiptStore(Path(self.tmp.name) / "state.sqlite3",
                                        Path(self.tmp.name) / "missing.json")
        self.store.initialize()
        ui._runtime_client = SimpleNamespace(review_state=self.store)
        ui._clear_problem("AUDIT_UNAVAILABLE")

    async def asyncTearDown(self):
        begin_runtime_cycle()
        ui._sales_import_session = self.old_import
        ui._runtime_client = self.old_client
        ui.SETTINGS_FILE = self.old_file
        ui.bot_settings.clear()
        ui.bot_settings.update(self.old_settings)
        ui._effective_modules = self.old_effective
        ui._module_drafts.clear()
        ui._interaction_state.clear()
        ui._restart_confirmations.clear()
        self.env.stop()
        self.tmp.cleanup()

    async def test_owner_confirmation_cancel_duplicate_and_audit(self):
        self.assertEqual(actions(ui.get_main_keyboard(100))[-2:],
                         ["modules_open", "restart_open"])
        self.assertNotIn("modules_open", actions(ui._system_screen(100)[1]))
        self.assertNotIn("restart_open", actions(ui.get_main_keyboard(101)))
        denied = Callback("restart_open", 101)
        self.assertTrue(await ui._restart_callback(denied, denied.data, 101))
        self.assertFalse(denied.edits)
        opened = Callback("restart_open")
        await ui._restart_callback(opened, opened.data, 100)
        choices = actions(opened.edits[-1][1]["reply_markup"])
        confirm = next(value for value in choices if value.startswith("restart_confirm:"))
        cancel = next(value for value in choices if value.startswith("restart_cancel:"))
        self.assertFalse(restart_requested())
        await ui._restart_callback(Callback(cancel), cancel, 100)
        self.assertFalse(restart_requested())
        await ui._restart_callback(Callback(confirm), confirm, 100)
        self.assertFalse(restart_requested())
        await ui._restart_callback(Callback("restart_open"), "restart_open", 100)
        confirm = "restart_confirm:" + ui._restart_confirmations[100]
        await ui._restart_callback(Callback(confirm), confirm, 100)
        self.assertTrue(restart_requested())
        await asyncio.wait_for(wait_for_restart(), 0.1)
        await ui._restart_callback(Callback(confirm), confirm, 100)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute(
                "SELECT action, target, result FROM audit_events"
            ).fetchall(), [("RESTART", "global", "REQUESTED")])
        self.assertNotIn("AUDIT_UNAVAILABLE",
                         {problem["code"] for problem in ui.get_active_problems()})

    async def test_active_import_blocks_restart_and_draft_is_disposable(self):
        await ui._restart_callback(Callback("restart_open"), "restart_open", 100)
        confirm = "restart_confirm:" + ui._restart_confirmations[100]
        ui._sales_import_session = {"phase": "importing", "owner": 100}
        busy = Callback(confirm)
        await ui._restart_callback(busy, confirm, 100)
        self.assertIn("операция с данными", str(busy.answers))
        self.assertFalse(restart_requested())
        ui._sales_import_session = None
        ui._module_drafts[100] = {"disposable": True}
        ui._interaction_state[100] = {"disposable": True}
        await ui._restart_callback(Callback(confirm), confirm, 100)
        self.assertFalse(ui._module_drafts)
        self.assertFalse(ui._interaction_state)

    async def test_module_save_restart_now_uses_same_confirmation(self):
        await ui._modules_callback(Callback("modules_open"), "modules_open", 100)
        await ui._modules_callback(Callback("modules_toggle:autobump"),
                                   "modules_toggle:autobump", 100)
        saved = Callback("modules_save")
        await ui._modules_callback(saved, saved.data, 100)
        self.assertIn("restart_open", actions(saved.edits[-1][1]["reply_markup"]))
        opened = Callback("restart_open")
        await ui._restart_callback(opened, opened.data, 100)
        self.assertIn("Перезапустить бота?", opened.edits[-1][0])
        self.assertFalse(restart_requested())

    async def test_no_user_facing_shutdown_action(self):
        menu = actions(ui.get_main_keyboard(100))
        system = actions(ui._system_screen(100)[1])
        self.assertFalse(any("shutdown" in action or "power" in action
                             for action in menu + system))
        self.assertFalse(any("Выключить бота" in str(markup)
                             for markup in (ui.get_main_keyboard(100),
                                            ui._system_screen(100)[1])))


class RestartLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        begin_runtime_cycle()

    async def test_action_claim_waits_for_inflight_and_blocks_new(self):
        begin_runtime_cycle()
        entered, release = threading.Event(), threading.Event()
        def active_send():
            with automatic_action_gate() as allowed:
                self.assertTrue(allowed)
                entered.set()
                release.wait(2)
        worker = asyncio.create_task(asyncio.to_thread(active_send))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        claim = asyncio.create_task(asyncio.to_thread(claim_restart))
        await asyncio.sleep(0.02)
        self.assertFalse(claim.done())
        release.set()
        await worker
        self.assertTrue(await claim)
        with automatic_action_gate() as allowed:
            self.assertFalse(allowed)
        self.assertFalse(claim_restart())

    async def test_all_three_automatic_http_paths_reject_after_claim(self):
        begin_runtime_cycle()
        network_calls = []
        class Account:
            is_initiated = True
            bot_character = "x"
            csrf_token = "synthetic"
            runner = None

            def __init__(self, *args, **kwargs):
                pass

            def method(self, *args, **kwargs):
                network_calls.append(args)
                return SimpleNamespace(json=lambda: {"response": {"ok": True}})

            def send_message(self, *args, **kwargs):
                network_calls.append(("night",))

        with patch("funpay._apply_funpay_cookie_patch", lambda: None), \
             patch("funpay.FunPayAPI.Account", Account):
            client = FunPayClient("synthetic")
        self.assertTrue(claim_restart())
        with self.assertRaises(_AutobumpActionCancelled):
            client.account.method("post", "lots/raise", {}, {})
        self.assertFalse(client.send_review_request_once(
            1, "synthetic", enabled_check=lambda: True))
        namespace = {
            "asyncio": asyncio, "FunPayClient": object,
            "ActionEvent": ActionEvent, "ActionKind": ActionKind,
            "module_enabled": lambda feature: True,
            "is_night_mode_enabled": lambda: True,
            "is_safe_mode_enabled": lambda: False,
            "restart_requested": restart_requested,
            "automatic_action_gate": automatic_action_gate,
            "get_night_mode_reply_text": lambda kind: "synthetic",
            "logger": SimpleNamespace(notify=lambda *args: None,
                                      warning=lambda *args: None),
        }
        night = main_function("_send_night_mode_reply", namespace)
        await night(client, ActionEvent(ActionKind.NIGHT_MESSAGE, chat_id=1))
        self.assertEqual(network_calls, [])

    async def test_restart_supervisor_tears_down_one_runner_and_workers(self):
        begin_runtime_cycle()
        started, errors, problems = [], [], []
        stopped = asyncio.Event()
        async def polling(*args, **kwargs):
            started.append("polling")
            await stopped.wait()
        async def worker(name, *args):
            started.append(name)
            await asyncio.Event().wait()
        async def stop_polling():
            stopped.set()
        client = SimpleNamespace(stop_count=0, join_count=0)
        def stop_runner():
            client.stop_count += 1
        async def join_runner():
            client.join_count += 1
            return True
        client.stop_runner = stop_runner
        client.join_runner = join_runner
        namespace = {
            "asyncio": asyncio, "Bot": object, "FunPayClient": object,
            "dp": SimpleNamespace(start_polling=polling, stop_polling=stop_polling),
            "notifications_loop": lambda *args: worker("runner", *args),
            "session_refresh_loop": lambda *args: worker("refresh", *args),
            "auto_bump_loop": lambda *args: worker("autobump", *args),
            "withdrawals_poll_loop": lambda *args: worker("withdrawals", *args),
            "module_enabled": lambda key: True,
            "wait_for_restart": wait_for_restart, "restart_requested": restart_requested,
            "RuntimeAction": RuntimeAction,
            "set_telegram_polling_state": lambda *args: None,
            "_set_problem": problems.append,
            "logger": SimpleNamespace(error=errors.append),
        }
        supervise = main_function("_supervise_tasks", namespace)
        task = asyncio.create_task(supervise(object(), client))
        await asyncio.sleep(0.02)
        self.assertTrue(claim_restart())
        signal_restart()
        self.assertIs(await asyncio.wait_for(task, 1), RuntimeAction.RESTART)
        self.assertEqual(started.count("runner"), 1)
        self.assertEqual(started.count("autobump"), 1)
        self.assertEqual((client.stop_count, client.join_count), (1, 1))
        self.assertEqual((errors, problems), ([], []))

    async def test_sigterm_style_cancellation_still_stops_normally(self):
        begin_runtime_cycle()
        stopped = asyncio.Event()
        async def polling(*args, **kwargs):
            await stopped.wait()
        async def worker(*args):
            await asyncio.Event().wait()
        async def stop_polling():
            stopped.set()
        client = SimpleNamespace(stop_count=0, join_count=0)
        def stop_runner():
            client.stop_count += 1
        async def join_runner():
            client.join_count += 1
            return True
        client.stop_runner = stop_runner
        client.join_runner = join_runner
        namespace = {
            "asyncio": asyncio, "Bot": object, "FunPayClient": object,
            "dp": SimpleNamespace(start_polling=polling, stop_polling=stop_polling),
            "notifications_loop": worker, "session_refresh_loop": worker,
            "module_enabled": lambda key: False,
            "wait_for_restart": wait_for_restart, "restart_requested": restart_requested,
            "RuntimeAction": RuntimeAction,
            "set_telegram_polling_state": lambda *args: None,
            "_set_problem": lambda *args: None,
            "logger": SimpleNamespace(error=lambda *args: None),
        }
        supervise = main_function("_supervise_tasks", namespace)
        task = asyncio.create_task(supervise(object(), client))
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((client.stop_count, client.join_count), (1, 1))
        self.assertFalse(restart_requested())

    async def test_outer_loop_serializes_cycles_and_keeps_sqlite_usable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            cycles = []
            async def cycle():
                store = ReviewReceiptStore(path, Path(temp) / "missing.json")
                store.initialize()
                cycles.append(store)
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute("SELECT 1").fetchone(), (1,))
                return RuntimeAction.RESTART if len(cycles) == 1 else RuntimeAction.NONE
            loop = main_function("_application_loop", {"main": cycle,
                                                        "asyncio": asyncio,
                                                        "ThreadPoolExecutor": ThreadPoolExecutor,
                                                        "RuntimeAction": RuntimeAction})
            await loop()
            self.assertEqual(len(cycles), 2)
            self.assertIsNot(cycles[0], cycles[1])

    async def test_restart_drains_cancelled_to_thread_before_next_cycle(self):
        started = threading.Event()
        finished = threading.Event()
        calls = 0

        def lingering_worker():
            started.set()
            time.sleep(0.05)
            finished.set()

        async def cycle():
            nonlocal calls
            calls += 1
            if calls == 1:
                pending = asyncio.create_task(asyncio.to_thread(lingering_worker))
                while not started.is_set():
                    await asyncio.sleep(0)
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
                self.assertFalse(finished.is_set())
                return RuntimeAction.RESTART
            self.assertTrue(finished.is_set())
            return RuntimeAction.NONE

        namespace = {"main": cycle, "asyncio": asyncio,
                     "ThreadPoolExecutor": ThreadPoolExecutor,
                     "RuntimeAction": RuntimeAction}
        await main_function("_application_loop", namespace)()
        self.assertEqual(calls, 2)

    async def test_main_recreates_one_account_and_session_per_cycle(self):
        with tempfile.TemporaryDirectory() as temp:
            clients, bots, notices, autobump = [], [], [], []
            db_path = Path(temp) / "state.sqlite3"
            class Client:
                def __init__(self, key):
                    if bots:
                        self.assert_previous_closed = bots[-1].session.closed
                    else:
                        self.assert_previous_closed = True
                    self.account = SimpleNamespace(username="synthetic")
                    self.initialized = 0
                    clients.append(self)

                def initialize_account(self):
                    self.initialized += 1

            class Bot:
                def __init__(self, token):
                    self.session = SimpleNamespace(closed=False, close=self.close)
                    bots.append(self)

                async def close(self):
                    self.session.closed = True

            async def supervise(bot, client):
                self.assertTrue(client.assert_previous_closed)
                return RuntimeAction.RESTART if len(clients) == 1 else RuntimeAction.NONE

            async def notice(bot, text, **kwargs):
                notices.append(text)

            namespace = {
                "asyncio": asyncio, "sys": sys,
                "ThreadPoolExecutor": ThreadPoolExecutor,
                "Path": Path, "DEFAULT_DB_PATH": db_path,
                "SETTINGS_FILE": str(Path(temp) / "settings.json"),
                "is_fresh_install": lambda *args: False,
                "configure_module_runtime": lambda **kwargs: None,
                "begin_runtime_cycle": begin_runtime_cycle,
                "_require_env": lambda key: "1",
                "FunPayClient": Client, "ReviewReceiptStore": lambda: ReviewReceiptStore(
                    db_path, Path(temp) / "missing.json"),
                "_validate_funpay_user_id": lambda *args: None,
                "Bot": Bot, "module_enabled": lambda key: key == "autobump",
                "enable_autobump_on_startup": lambda: autobump.append(True),
                "_send_runtime_notice": notice,
                "_supervise_tasks": supervise,
                "set_runtime_status_context": lambda *args: None,
                "clear_runtime_status_context": lambda: None,
                "os": SimpleNamespace(name="posix"),
                "logger": SimpleNamespace(info=lambda *args: None,
                                          success=lambda *args: None,
                                          error=lambda *args: None,
                                          banner=lambda *args: None),
                "RuntimeAction": RuntimeAction,
            }
            namespace["main"] = main_function("main", namespace)
            loop = main_function("_application_loop", namespace)
            await loop()
            self.assertEqual(len(clients), 2)
            self.assertEqual([client.initialized for client in clients], [1, 1])
            self.assertEqual(len(bots), 2)
            self.assertTrue(all(bot.session.closed for bot in bots))
            self.assertEqual(notices.count("🟢 Бот запущен."), 2)
            self.assertEqual(notices.count("⚠️ Бот остановлен из-за критической ошибки."), 0)
            self.assertEqual(len(autobump), 2)
            with closing(sqlite3.connect(db_path)) as db:
                self.assertEqual(db.execute("SELECT 1").fetchone(), (1,))

    async def test_real_process_lock_handle_can_be_reacquired_after_release(self):
        with tempfile.TemporaryDirectory() as temp:
            namespace = {"os": os, "atexit": SimpleNamespace(register=lambda *args: None),
                         "LOCK_FILE": str(Path(temp) / "bot.lock"),
                         "_lock_handle": None,
                         "logger": SimpleNamespace(error=lambda *args: None)}
            acquire = main_function("acquire_lock", namespace)
            release = main_function("release_lock", namespace)
            try:
                acquire()
                self.assertIsNotNone(namespace["_lock_handle"])
                with self.assertRaises(RuntimeError):
                    acquire()
                release()
                acquire()
                self.assertIsNotNone(namespace["_lock_handle"])
            finally:
                release()

    async def test_process_lock_wraps_full_outer_loop(self):
        calls = []
        async def loop():
            calls.append("loop")
        namespace = {"acquire_lock": lambda: calls.append("acquire"),
                     "release_lock": lambda: calls.append("release"),
                     "_application_loop": loop,
                     "asyncio": SimpleNamespace(run=lambda coro: asyncio.run(coro)),
                     "logger": SimpleNamespace(error=lambda *args: None)}
        run = main_function("run", namespace)
        await asyncio.to_thread(run)
        self.assertEqual(calls, ["acquire", "loop", "release"])


if __name__ == "__main__":
    unittest.main()
