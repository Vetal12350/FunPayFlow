"""Offline pre-live integration cases for settings and schema migrations."""
import log_isolation
import copy
import ast
import asyncio
import hashlib
import json
import re
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import FunPayAPI
import logger
import telegram as ui
from funpay import FunPayClient
from runtime_events import html_preview
from state import ReviewReceiptStore, StateError


class SettingsCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_file = ui.SETTINGS_FILE
        self.previous_settings = copy.deepcopy(ui.bot_settings)
        self.previous_users = copy.deepcopy(ui._user_settings)
        ui.SETTINGS_FILE = str(Path(self.tmp.name) / "settings.json")
        ui.bot_settings.clear()
        ui.bot_settings.update(copy.deepcopy(ui._DEFAULT_GLOBAL_SETTINGS))
        ui._user_settings.clear()

    def tearDown(self):
        ui.SETTINGS_FILE = self.previous_file
        ui.bot_settings.clear()
        ui.bot_settings.update(self.previous_settings)
        ui._user_settings.clear()
        ui._user_settings.update(self.previous_users)
        self.tmp.cleanup()

    def write(self, value):
        Path(ui.SETTINGS_FILE).write_text(json.dumps(value), encoding="utf-8")

    def test_old_flat_current_and_partial_settings(self):
        self.write({"authorized_user_ids": [2], "notifications_enabled": False,
                    "notify_order": False, "unknown_future_key": {"v": 1}})
        with patch.dict("os.environ", {"ADMIN_ID": "1"}):
            ui.load_settings()
        self.assertFalse(ui.get_user_settings(1)["notifications_enabled"])
        self.assertFalse(ui.get_user_settings(1)["notify_order"])
        self.assertEqual(ui.bot_settings["authorized_user_ids"], [2])
        self.assertFalse(ui.is_safe_mode_enabled())

        ui.bot_settings["safe_mode"] = True
        ui.bot_settings["stats_currency"] = "USD"
        ui.get_user_settings(2)["notify_system"] = False
        ui.save_settings(required=True)
        ui.bot_settings["safe_mode"] = False
        ui._user_settings.clear()
        ui.load_settings()
        self.assertTrue(ui.is_safe_mode_enabled())
        self.assertFalse(ui.get_user_settings(2)["notify_system"])

        self.write({"authorized_user_ids": [2], "safe_mode": True,
                    "user_settings": {"2": {"notify_review": False}}})
        ui.load_settings()
        self.assertTrue(ui.is_safe_mode_enabled())
        self.assertFalse(ui.get_user_settings(2)["notify_review"])
        self.assertTrue(ui.get_user_settings(2)["notify_system"])

    def test_malformed_optional_does_not_reset_auth_safety_or_user_preferences(self):
        self.write({
            "authorized_user_ids": [2], "safe_mode": True,
            "night_mode_reply": 42, "reply_templates": [{"text": "broken"}],
            "autoresponder_rules": [{"trigger": "broken"}],
            "review_request_enabled": True, "review_request_text": [],
            "review_request_delay": "soon", "stats_currency": "??",
            "user_settings": {"2": {"notify_order": False}},
            "unknown_new_key": "ignored",
        })
        ui.load_settings()
        self.assertEqual(ui.bot_settings["authorized_user_ids"], [2])
        self.assertTrue(ui.is_safe_mode_enabled())
        self.assertFalse(ui.get_user_settings(2)["notify_order"])
        self.assertIsNone(ui.bot_settings["night_mode_reply"])
        self.assertFalse(ui.bot_settings["night_mode"])
        self.assertNotIn("reply_templates", ui.bot_settings)
        self.assertNotIn("autoresponder_rules", ui.bot_settings)
        self.assertFalse(ui.bot_settings["review_request_enabled"])
        self.assertEqual(ui.bot_settings["review_request_delay_seconds"], 300)
        self.assertIsNone(ui.bot_settings["stats_currency"])

    def test_invalid_safety_or_auth_state_fails_closed_without_partial_mutation(self):
        ui.bot_settings["safe_mode"] = True
        ui._user_settings[2] = {**ui._DEFAULT_USER_SETTINGS, "notify_order": False}
        self.write({"safe_mode": "false", "authorized_user_ids": [2]})
        with self.assertRaises(RuntimeError):
            ui.load_settings()
        self.assertTrue(ui.is_safe_mode_enabled())
        self.assertFalse(ui.get_user_settings(2)["notify_order"])
        self.write({"safe_mode": True, "authorized_user_ids": ["invalid"]})
        with self.assertRaises(RuntimeError):
            ui.load_settings()
        self.assertTrue(ui.is_safe_mode_enabled())

    def test_atomic_save_failure_keeps_original_file(self):
        self.write({"safe_mode": False})
        before = Path(ui.SETTINGS_FILE).read_bytes()
        with patch.object(ui.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(RuntimeError):
                ui.toggle_safe_mode_saved()
        self.assertEqual(Path(ui.SETTINGS_FILE).read_bytes(), before)
        self.assertFalse(ui.is_safe_mode_enabled())


class MigrationMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def initialize(self, name):
        path = self.base / f"{name}.sqlite3"
        store = ReviewReceiptStore(path, self.base / "missing.json")
        store.initialize()
        store.initialize()
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"review_receipts", "orders", "audit_events",
                             "critical_event_backlog"} <= tables)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name='critical_event_backlog'").fetchone()[0], 1)
        return store

    def test_new_empty_and_legacy_schemas(self):
        self.initialize("new")
        for name in ("pre_receipts", "old_orders", "statistics", "order_history", "current"):
            path = self.base / f"{name}.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                if name == "pre_receipts":
                    db.execute("CREATE TABLE review_receipts ("
                               "order_id TEXT PRIMARY KEY, delivered_at TEXT NOT NULL "
                               "DEFAULT CURRENT_TIMESTAMP)")
                    db.execute("INSERT INTO review_receipts(order_id) VALUES ('ABC12345')")
                else:
                    db.execute("CREATE TABLE orders (order_id TEXT PRIMARY KEY, "
                               "first_seen_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL, "
                               "current_status TEXT NOT NULL, amount TEXT, currency TEXT)")
                    db.execute("INSERT INTO orders VALUES "
                               "('ABC12345', 1, 2, 'CLOSED', NULL, NULL)")
                    if name in ("statistics", "order_history", "current"):
                        db.execute("CREATE TABLE legacy_stats (kind TEXT NOT NULL, "
                                   "record_key TEXT NOT NULL, recorded_at REAL NOT NULL, "
                                   "amount TEXT, currency TEXT, PRIMARY KEY(kind, record_key))")
                        db.execute("INSERT INTO legacy_stats VALUES "
                                   "('order', 'ABC12345', 2, NULL, NULL)")
                    if name in ("order_history", "current"):
                        db.execute("ALTER TABLE orders ADD COLUMN buyer_username TEXT")
                    if name == "current":
                        db.execute("CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                                   "ts INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, "
                                   "target TEXT NOT NULL, result TEXT NOT NULL, details_safe TEXT)")
                        db.execute("INSERT INTO audit_events "
                                   "(ts, actor, action, target, result) VALUES "
                                   "(1, 'system', 'SETTINGS', 'global', 'UPDATED')")
                db.commit()
            self.initialize(name)
            with closing(sqlite3.connect(path)) as db:
                if name == "pre_receipts":
                    self.assertEqual(db.execute(
                        "SELECT COUNT(*) FROM review_receipts").fetchone()[0], 1)
                else:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
                    self.assertIsNone(db.execute(
                        "SELECT amount FROM orders WHERE order_id='ABC12345'").fetchone()[0])
                if name == "current":
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], 1)


class MessageLimitsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewReceiptStore(Path(self.tmp.name) / "state.sqlite3",
                                        Path(self.tmp.name) / "missing.json")
        self.store.initialize()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def review_runtime(self):
        source = Path("main.py").read_text(encoding="utf-8")
        wanted = {"_review_state_operation", "_review_fingerprint",
                  "_safe_review_event_part", "_fetch_and_send_review"}
        nodes = [n for n in ast.parse(source).body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted]
        runtime = {"asyncio": asyncio, "hashlib": hashlib, "json": json, "re": re,
                   "Bot": object, "FunPayClient": object, "StateError": StateError,
                   "html_preview": html_preview,
                   "logger": SimpleNamespace(warning=lambda *args: None,
                                             notify=lambda *args: None),
                   "get_all_recipients": lambda: [1],
                   "get_user_settings": lambda _: {"notifications_enabled": True,
                                                    "notify_review": True}}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), runtime)
        return runtime

    async def test_extreme_order_and_review_texts_fit_telegram(self):
        order = SimpleNamespace(buyer_username="<&>" * 1000, price=12,
                                description="&" * 10000, chat_id=None)
        event = SimpleNamespace(type=FunPayAPI.enums.EventTypes.NEW_ORDER, order=order)
        client = object.__new__(FunPayClient)
        notification = client.describe_event(event)[0].text
        self.assertLessEqual(len(notification), 4096)
        self.assertIn("&amp;", notification)

        runtime = self.review_runtime()
        review = SimpleNamespace(text="&" * 10000, order_id="ABC12345", author_id=3, stars=5)
        full_order = SimpleNamespace(id="ABC12345", seller_id=2, buyer_id=3,
                                     buyer_username="<&>" * 1000, review=review)
        fake = SimpleNamespace(get_order_snapshot=lambda _: (full_order, 2),
                               _review_notification_lock=asyncio.Lock(),
                               _review_state_failed=False, review_state=self.store,
                               _notified_reviews={})
        sent = []

        async def send_message(recipient, text, **kwargs):
            sent.append(text)

        await runtime["_fetch_and_send_review"](
            SimpleNamespace(send_message=send_message), fake, "ABC12345")
        self.assertEqual(len(sent), 1)
        self.assertLessEqual(len(sent[0]), 4096)
        self.assertIn("&amp;", sent[0])
        self.assertTrue(self.store.get_review_receipt("ABC12345")[0])

    async def test_repeated_and_changed_review_share_one_observation(self):
        runtime = self.review_runtime()
        review = SimpleNamespace(text="First", order_id="ABC12345", author_id=3,
                                 stars=5, reply=None)
        order = SimpleNamespace(id="ABC12345", seller_id=2, buyer_id=3,
                                buyer_username="Buyer", review=review)
        client = SimpleNamespace(get_order_snapshot=lambda _: (order, 2),
                                 _review_notification_lock=asyncio.Lock(),
                                 _review_state_failed=False, review_state=self.store,
                                 _notified_reviews={})
        sent = []

        async def send_message(recipient, text, **kwargs):
            sent.append(text)

        bot = SimpleNamespace(send_message=send_message)
        for _ in range(2):
            await runtime["_fetch_and_send_review"](bot, client, "ABC12345")
        review.reply = "Seller reply"
        await runtime["_fetch_and_send_review"](bot, client, "ABC12345")
        self.assertEqual(len(sent), 1)
        review.text = "Changed"
        review.stars = 3
        await runtime["_fetch_and_send_review"](bot, client, "ABC12345")
        self.assertEqual(len(sent), 2)
        self.assertIn("Отзыв изменён", sent[1])
        self.assertTrue(self.store.has_review_observation("ABC12345"))
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM review_observations WHERE order_id = 'ABC12345'"
            ).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT rating FROM review_observations WHERE order_id = 'ABC12345'"
            ).fetchone()[0], 3)
        self.assertEqual(self.store.get_review_receipt("ABC12345")[1],
                         runtime["_review_fingerprint"](3, "Changed"))


class PrivacyTests(unittest.TestCase):
    def test_offline_logger_does_not_write_runtime_daily_log(self):
        name = f"bot_{datetime.now():%Y-%m-%d}.log"
        runtime_log = Path(logger.__file__).resolve().parent / "logs" / name
        previous_size = runtime_log.stat().st_size if runtime_log.exists() else None
        logger.warning("synthetic-offline-warning")
        self.assertEqual(runtime_log.stat().st_size if runtime_log.exists() else None,
                         previous_size)
        self.assertTrue((Path(logger.LOGS_DIR) / name).is_file())

    def test_console_and_file_logger_scrub_credential_markers(self):
        marker = "synthetic-only-value"
        with patch.object(logger, "_write_to_file") as file_write, patch("builtins.print") as output:
            logger.notify(f"Example BOT_TOKEN={marker} trailing")
            logger.warning("Example 12345678:abcdefghijklmnopqrstuvwxyz")
        visible = " ".join(str(call.args[0]) for call in output.call_args_list)
        persisted = " ".join(str(call.args[1]) for call in file_write.call_args_list)
        self.assertNotIn(marker, visible + persisted)
        self.assertNotIn("12345678:abcdefghijklmnopqrstuvwxyz", visible + persisted)
        self.assertIn("[секрет скрыт]", visible)


class CallbackBoundsTests(unittest.TestCase):
    def test_menu_callbacks_use_bounded_ids_and_pages(self):
        menus = [
            ui.get_main_keyboard(1), ui.get_stats_keyboard(),
            ui._review_request_screen()[1], ui._review_delay_screen()[1],
            ui._order_list_screen("all", 100000, 1000001,
                                  [{"order_id": "ABC12345", "product_description": "&" * 1000,
                                    "current_status": "PAID"}])[1],
            ui._analytics_keyboard("30d"),
            ui._audit_screen(100000, 2000020,
                             [{"ts": 1, "action": "SETTINGS", "result": "UPDATED",
                               "id": 10**18 - 1}])[1],
        ]
        callbacks = [button.callback_data for menu in menus
                     for row in menu.inline_keyboard for button in row]
        self.assertTrue(callbacks)
        self.assertTrue(all(len(value.encode("utf-8")) <= 64 for value in callbacks))
        self.assertTrue(all("BUYER_TEXT" not in value for value in callbacks))


class ShutdownGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_send_does_not_start_after_runner_stop(self):
        sent = []
        client = SimpleNamespace(
            _account_lock=threading.RLock(), _outgoing_echo_lock=threading.Lock(),
            _runner_publish_lock=threading.Lock(), _runner_stop=threading.Event(),
            _recent_outgoing_text={},
            account=SimpleNamespace(send_message=lambda *args, **kwargs: sent.append(args)),
        )
        entered = threading.Event()

        def send_queued():
            entered.set()
            return FunPayClient.send_review_request_once(client, 2, "reply")

        with client._account_lock:
            task = asyncio.create_task(asyncio.to_thread(send_queued))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            FunPayClient.stop_runner(client)
        self.assertIs(await asyncio.wait_for(task, 2), False)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
