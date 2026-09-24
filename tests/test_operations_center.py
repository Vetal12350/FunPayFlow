"""Offline Operations Center checks; no production runtime or network."""
import log_isolation
import asyncio
import ast
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import telegram as ui
from state import ReviewReceiptStore, StateError


class Callback:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.edits = []
        self.answers = []
        self.message = SimpleNamespace(edit_text=self.edit_text)

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


class OperationsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_settings = copy.deepcopy(ui.bot_settings)
        self.original_users = copy.deepcopy(ui._user_settings)
        self.original_file = ui.SETTINGS_FILE
        self.original_client = ui._runtime_client
        self.original_started = ui._runtime_started_at
        ui.SETTINGS_FILE = str(Path(self.tmp.name) / "settings.json")
        ui.bot_settings.clear()
        ui.bot_settings.update(copy.deepcopy(ui._DEFAULT_GLOBAL_SETTINGS))
        ui._user_settings.clear()
        ui._active_problems.clear()
        self.store = ReviewReceiptStore(Path(self.tmp.name) / "state.sqlite3",
                                        Path(self.tmp.name) / "missing.json")
        self.store.initialize()
        self.client = SimpleNamespace(
            review_state=self.store, _review_state_failed=False,
            get_runner_health=lambda: {"state": "healthy", "consecutive_errors": 0,
                                       "last_success_monotonic": time.monotonic()},
            event_queue=SimpleNamespace(qsize=lambda: 0, maxsize=100),
        )
        ui._runtime_client = self.client
        ui._runtime_started_at = time.monotonic() - 5

    async def asyncTearDown(self):
        ui.SETTINGS_FILE = self.original_file
        ui.bot_settings.clear()
        ui.bot_settings.update(self.original_settings)
        ui._user_settings.clear()
        ui._user_settings.update(self.original_users)
        ui._runtime_client = self.original_client
        ui._runtime_started_at = self.original_started
        ui._active_problems.clear()
        self.tmp.cleanup()

    async def test_status_and_problem_lifecycle(self):
        status = ui.get_runtime_status_text()
        for field in ("FunPay Runner:", "Telegram:", "Очередь событий:", "SQLite:",
                      "Автоподнятие:", "Night Mode:", "Запрос отзыва:", "SAFE_MODE:",
                      "Withdrawal polling:", "Ожидающих запросов отзыва:"):
            self.assertIn(field, status)
        self.assertNotIn("secret-raw-error", status)
        self.assertEqual(ui.get_active_problems(), [])
        ui.record_withdrawal_poll(False)
        self.assertNotIn("WITHDRAWAL_REPEATED", {p["code"] for p in ui.get_active_problems()})
        ui.record_withdrawal_poll(False)
        ui.record_withdrawal_poll(False)
        self.assertIn("WITHDRAWAL_REPEATED", {p["code"] for p in ui.get_active_problems()})
        ui.record_withdrawal_poll(True)
        self.assertNotIn("WITHDRAWAL_REPEATED", {p["code"] for p in ui.get_active_problems()})
        self.client.get_runner_health = lambda: {"state": "backoff", "consecutive_errors": 1}
        self.assertNotIn("RUNNER_RETRYING", {p["code"] for p in ui.get_active_problems()})
        self.client.get_runner_health = lambda: {"state": "backoff", "consecutive_errors": 3}
        self.assertIn("RUNNER_RETRYING", {p["code"] for p in ui.get_active_problems()})
        self.client.get_runner_health = lambda: {"state": "healthy", "consecutive_errors": 0}
        self.assertNotIn("RUNNER_RETRYING", {p["code"] for p in ui.get_active_problems()})

    async def test_db_probe_and_safe_problem_display(self):
        self.client.review_state = SimpleNamespace(
            count_pending_review_requests=lambda: (_ for _ in ()).throw(RuntimeError("secret-raw-error")))
        self.assertIn("SQLite: недоступно", ui.get_runtime_status_text())
        self.assertIn("DB_UNAVAILABLE", {p["code"] for p in ui.get_active_problems()})
        self.assertNotIn("secret-raw-error", ui._problems_screen()[0])
        self.client.review_state = self.store
        self.assertNotIn("DB_UNAVAILABLE", {p["code"] for p in ui.get_active_problems()})

    async def test_audit_sanitization_pagination_and_setting(self):
        ui._save_global_setting("review_request_enabled", True, actor="telegram:1")
        count, rows = self.store.list_audit_events()
        self.assertEqual((count, rows[0]["action"], rows[0]["result"]),
                         (1, "REVIEW_REQUEST", "ON"))
        self.assertEqual(rows[0]["actor"], "telegram:1")
        self.assertIsNone(rows[0]["details_safe"])
        with self.assertRaises(StateError):
            self.store.record_audit_event("telegram:1", "REVIEW_REQUEST_SEND", "order", "SUCCESS",
                                          "secret message body")
        for _ in range(25):
            self.store.record_audit_event("system", "SETTINGS", "global", "UPDATED")
        count, first = self.store.list_audit_events(0)
        _, second = self.store.list_audit_events(1)
        self.assertEqual((count, len(first), len(second)), (26, 20, 6))
        self.assertEqual(len({row["id"] for row in first + second}), 26)
        self.assertEqual(self.store.get_audit_event(first[0]["id"])["id"], first[0]["id"])

    async def test_safe_mode_persistence_rollback_confirmation_and_notifications(self):
        self.assertTrue(ui.toggle_safe_mode_saved(actor="telegram:1"))
        self.assertTrue(json.loads(Path(ui.SETTINGS_FILE).read_text(encoding="utf-8"))["safe_mode"])
        ui.bot_settings["safe_mode"] = False
        ui.load_settings()
        self.assertTrue(ui.is_safe_mode_enabled())
        callback = Callback("sys_safe_toggle")
        self.assertTrue(await ui._operations_callback(callback, callback.data, 1))
        self.assertIn("Разрешить автоматические действия", callback.edits[0][0])
        self.assertTrue(ui.is_safe_mode_enabled())
        confirm = Callback("sys_safe_off")
        self.assertTrue(await ui._operations_callback(confirm, confirm.data, 1))
        self.assertFalse(ui.is_safe_mode_enabled())
        with patch.object(ui, "save_settings", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                ui.toggle_safe_mode_saved()
        self.assertFalse(ui.is_safe_mode_enabled())
        ui.get_user_settings(1)["notify_system"] = False
        ui.save_settings(required=True)
        ui._user_settings.clear()
        ui.load_settings()
        self.assertFalse(ui.get_user_settings(1)["notify_system"])
        actions = [b.callback_data for row in ui.get_main_keyboard(1).inline_keyboard for b in row]
        self.assertIn("system", actions)
        self.assertIn("menu_status", actions)
        self.assertIn("menu_stats", actions)

    async def test_audit_failure_does_not_break_toggle(self):
        self.client.review_state = SimpleNamespace(record_audit_event=lambda *args: 1 / 0,
                                                   count_pending_review_requests=lambda: 0)
        self.assertTrue(ui.toggle_safe_mode_saved())
        self.assertIn("AUDIT_UNAVAILABLE", {p["code"] for p in ui.get_active_problems()})


    async def test_autobump_safe_gate_and_late_check(self):
        source = Path("main.py").read_text(encoding="utf-8")
        node = next(n for n in ast.parse(source).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "auto_bump_loop")
        namespace = {"Bot": object, "FunPayClient": object, "asyncio": asyncio,
                     "bot_settings": ui.bot_settings, "is_safe_mode_enabled": ui.is_safe_mode_enabled,
                     "_require_env": lambda key: "1", "get_all_recipients": lambda: [],
                     "_AmbiguousRaiseOutcome": type("_AmbiguousRaiseOutcome", (Exception,), {}),
                     "logger": SimpleNamespace(bump=lambda *args: None)}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), namespace)
        calls = []

        async def bump_lots(user_id, *, is_cancelled):
            calls.append(is_cancelled)
            await asyncio.sleep(10)

        ui.bot_settings["auto_bump"] = True
        ui.bot_settings["safe_mode"] = True
        task = asyncio.create_task(namespace["auto_bump_loop"](
            None, SimpleNamespace(bump_lots=bump_lots, runner_stop_requested=lambda: False)))
        await asyncio.sleep(0.05)
        self.assertEqual(calls, [])
        ui.bot_settings["safe_mode"] = False
        await asyncio.sleep(1.05)
        self.assertEqual(len(calls), 1)
        ui.bot_settings["safe_mode"] = True
        self.assertTrue(calls[0]())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
