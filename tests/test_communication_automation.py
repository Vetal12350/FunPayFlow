"""Offline automation checks; main.py is parsed, never imported or run."""
import log_isolation
import asyncio
import ast
import copy
import json
from contextlib import closing
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import FunPayAPI
import telegram as ui
from funpay import FunPayClient
from runtime_events import ActionKind
from state import ReviewReceiptStore, StateError


MAIN_FUNCTIONS = {
    "_review_chat_context", "_send_scheduled_review_request",
    "_schedule_closed_review_request",
}
source = Path("main.py").read_text(encoding="utf-8")
nodes = [node for node in ast.parse(source).body
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in MAIN_FUNCTIONS]
runtime = {
    "asyncio": asyncio, "time": time, "FunPayAPI": FunPayAPI, "FunPayClient": object,
    "bot_settings": ui.bot_settings,
    "expand_review_request_text": ui.expand_review_request_text,
    "is_review_request_enabled": ui.is_review_request_enabled,
    "is_safe_mode_enabled": ui.is_safe_mode_enabled,
    "restart_requested": lambda: False,
    "module_enabled": ui.module_enabled,
    "_audit_action": ui._audit_action,
    "logger": SimpleNamespace(warning=lambda *args: None, error=lambda *args: None),
}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), runtime)


class Account:
    id = 10
    username = "Seller"

    def get_chat_by_name(self, name, make_request=False):
        return SimpleNamespace(id=2) if name == "Buyer" else None


class Client:
    def __init__(self, store=None):
        self.account = Account()
        self._account_lock = threading.RLock()
        self.review_state = store
        self.sent = []
        self.send_error = None
        self.stop_requested = False
        self.order = SimpleNamespace(status=FunPayAPI.types.OrderStatuses.CLOSED,
                                     buyer_username="Buyer", seller_id=10, review=None)

    def send_review_request_once(self, chat_id, value, *, enabled_check=None):
        if self.stop_requested:
            return False
        if enabled_check is not None and not enabled_check():
            return False
        self.sent.append((chat_id, value))
        if self.send_error:
            raise self.send_error

    def get_order_snapshot(self, order_id):
        return self.order, self.account.id

    def runner_stop_requested(self):
        return self.stop_requested


class Message:
    def __init__(self, text, user_id=1):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.sent = []

    async def answer(self, value, **kwargs):
        self.sent.append((value, kwargs))


class Callback:
    def __init__(self, action, user_id=1):
        self.data = action
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(edit_text=self.edit_text)
        self.edits = []
        self.answers = []

    async def edit_text(self, value, **kwargs):
        self.edits.append((value, kwargs))

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


class AutomationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_settings = copy.deepcopy(ui.bot_settings)
        self.original_effective = ui._effective_modules
        self.original_file = ui.SETTINGS_FILE
        ui.SETTINGS_FILE = str(Path(self.tmp.name) / "settings.json")
        ui.bot_settings.clear()
        ui.bot_settings.update(copy.deepcopy(ui._DEFAULT_GLOBAL_SETTINGS))
        ui.configure_module_runtime(fresh_install=False)
        ui._interaction_state.clear()
        self.store = ReviewReceiptStore(Path(self.tmp.name) / "state.sqlite3",
                                        Path(self.tmp.name) / "no-legacy.json")
        self.store.initialize()
        self.client = Client(self.store)

    async def asyncTearDown(self):
        ui.bot_settings.clear()
        ui.bot_settings.update(self.original_settings)
        ui._effective_modules = self.original_effective
        ui.SETTINGS_FILE = self.original_file
        ui._interaction_state.clear()
        self.tmp.cleanup()

    def status(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            return db.execute("SELECT state, sent_at FROM review_requests WHERE order_id = 'ABC12345'").fetchone()

    async def test_review_delay_seconds_presets_schedule_exactly(self):
        ui.bot_settings["review_request_enabled"] = True
        schedule = runtime["_schedule_closed_review_request"]
        event = SimpleNamespace(type=FunPayAPI.enums.EventTypes.ORDER_STATUS_CHANGED,
                                order=SimpleNamespace(buyer_username="Buyer"))
        for index, seconds in enumerate((0, 5, 10, 30)):
            order_id = f"DLY{index:05d}"
            self.store.record_order_observation(order_id, "CLOSED")
            ui.bot_settings["review_request_delay_seconds"] = seconds
            started = []
            with patch("time.time", return_value=1000):
                await schedule(self.client, event, (order_id, "CLOSED"), True,
                               lambda *args: started.append(args))
            self.assertEqual(started, [(order_id, "Buyer", 1000 + seconds)])

    async def test_review_ui_presets_and_order_url(self):
        menu = str(ui.get_main_keyboard(1))
        stats = str(ui.get_stats_keyboard())
        system = str(ui._system_screen()[1])
        self.assertIn("📈 Аналитика", menu)
        self.assertIn("⭐ Запрос отзыва", menu)
        self.assertIn("🔔 Мои уведомления", menu)
        self.assertNotIn("Чаты", menu)
        self.assertNotIn("Автоответчик", menu)
        self.assertNotIn("Аналитика", stats)
        self.assertEqual([button.callback_data for row in ui.get_stats_keyboard().inline_keyboard
                          for button in row],
                         ["stats_today", "stats_week", "stats_month", "menu_main"])
        orders = str(ui._orders_screen()[1])
        for label in ("🕘 Последние заказы", "✅ Завершённые", "↩️ Возвраты"):
            self.assertIn(label, orders)
        self.assertNotIn("Уведомления", system)
        self.assertEqual([[button.text for button in row]
                          for row in ui.get_reply_keyboard().keyboard], [["🛠 Главное меню"]])
        delay_buttons = str(ui._review_delay_screen()[1])
        for seconds in (0, 5, 10, 30, 60, 300):
            self.assertIn(f"review_delay:{seconds}", delay_buttons)
        for seconds in (0, 5, 10, 30):
            callback = Callback(f"review_delay:{seconds}")
            self.assertTrue(await ui._review_callback(callback, callback.data, 1))
            self.assertEqual(ui.bot_settings["review_request_delay_seconds"], seconds)
            self.assertEqual(len(callback.answers), 1)
        self.assertEqual(
            ui.expand_review_request_text("{order_url}", "Seller", "Buyer", "ABC12345"),
            "https://funpay.com/orders/ABC12345/",
        )
        with self.assertRaises(ValueError):
            ui.expand_review_request_text("{order_url}", "Seller", "Buyer", "../secret")

    async def test_removed_callbacks_are_inert(self):
        for action in ("chat_menu", "chat_history:1", "tpl_add", "auto_menu", "auto_rule:1"):
            callback = Callback(action)
            with patch.object(ui, "is_authorized", return_value=True):
                await ui.callback_handler(callback)
            self.assertEqual(callback.edits, [])
            self.assertEqual(len(callback.answers), 1)

    async def test_legacy_automation_keys_and_review_settings_migrate(self):
        legacy = {"auto_bump": False, "night_mode": False,
                  "authorized_user_ids": [], "reply_templates": [{"text": "legacy"}],
                  "autoresponder_enabled": True,
                  "autoresponder_rules": [{"trigger": "legacy"}],
                  "review_request_enabled": True,
                  "review_request_delay": 1,
                  "review_request_text": ui.OLD_DEFAULT_REVIEW_REQUEST_TEXT}
        Path(ui.SETTINGS_FILE).write_text(json.dumps(legacy), encoding="utf-8")
        ui.load_settings()
        self.assertEqual(ui.bot_settings["review_request_delay_seconds"], 60)
        self.assertEqual(ui.bot_settings["review_request_text"], ui.DEFAULT_REVIEW_REQUEST_TEXT)
        self.assertNotIn("autoresponder_rules", ui.bot_settings)
        self.assertNotIn("reply_templates", ui.bot_settings)
        legacy["review_request_text"] = "Custom review text"
        Path(ui.SETTINGS_FILE).write_text(json.dumps(legacy), encoding="utf-8")
        ui.load_settings()
        self.assertEqual(ui.bot_settings["review_request_text"], "Custom review text")


    async def test_closed_schedule_duplicate_and_non_closed(self):
        first_closed = self.store.record_order_observation("ABC12345", "CLOSED")
        self.assertTrue(first_closed)
        ui.bot_settings["review_request_enabled"] = True
        ui.bot_settings["review_request_delay_seconds"] = 0
        started = []
        event = SimpleNamespace(type=FunPayAPI.enums.EventTypes.ORDER_STATUS_CHANGED,
                                order=SimpleNamespace(buyer_username="Buyer"))
        schedule = runtime["_schedule_closed_review_request"]
        await schedule(self.client, event, ("ABC12345", "CLOSED"), first_closed,
                       lambda *args: started.append(args))
        replay = self.store.record_order_observation("ABC12345", "CLOSED")
        self.assertFalse(replay)
        await schedule(self.client, event, ("ABC12345", "CLOSED"), replay,
                       lambda *args: started.append(args))
        await schedule(self.client, event, ("ABC12345", "REFUNDED"), False,
                       lambda *args: started.append(args))
        event.type = FunPayAPI.enums.EventTypes.NEW_ORDER
        await schedule(self.client, event, ("ABC12345", "CLOSED"), True,
                       lambda *args: started.append(args))
        self.assertEqual(len(started), 1)
        self.assertEqual(self.status()[0], "pending")

    async def test_success_restart_and_ambiguous_no_retry(self):
        self.store.record_order_observation("ABC12345", "CLOSED")
        self.store.schedule_review_request("ABC12345", "Buyer", int(time.time()))
        restarted = ReviewReceiptStore(self.store.path, Path(self.tmp.name) / "no-legacy.json")
        restarted.initialize()
        self.assertEqual(len(restarted.pending_review_requests()), 1)
        ui.bot_settings["review_request_enabled"] = True
        await runtime["_send_scheduled_review_request"](self.client, "ABC12345", "Buyer", int(time.time()))
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.status()[0], "sent")
        await runtime["_send_scheduled_review_request"](self.client, "ABC12345", "Buyer", int(time.time()))
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(restarted.pending_review_requests(), [])


    async def test_safe_mode_pauses_review_request_without_retry_after_transport(self):
        self.store.record_order_observation("ABC12345", "CLOSED")
        self.store.schedule_review_request("ABC12345", "Buyer", int(time.time()))
        ui.bot_settings["review_request_enabled"] = True
        ui.bot_settings["safe_mode"] = True
        task = asyncio.create_task(runtime["_send_scheduled_review_request"](
            self.client, "ABC12345", "Buyer", int(time.time())))
        await asyncio.sleep(0.05)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.status()[0], "pending")
        ui.bot_settings["safe_mode"] = False
        await asyncio.wait_for(task, 2)
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.status()[0], "sent")

        self.store.record_order_observation("DEF12345", "CLOSED")
        self.store.schedule_review_request("DEF12345", "Buyer", int(time.time()))
        self.client.send_error = TimeoutError()
        await runtime["_send_scheduled_review_request"](
            self.client, "DEF12345", "Buyer", int(time.time()))
        self.assertEqual(len(self.client.sent), 2)
        self.assertEqual(self.store.pending_review_requests(), [])

    async def test_shutdown_before_review_request_transport_keeps_pending(self):
        self.store.record_order_observation("ABC12345", "CLOSED")
        self.store.schedule_review_request("ABC12345", "Buyer", int(time.time()))
        ui.bot_settings["review_request_enabled"] = True

        def stop_before_send(chat_id, value, *, enabled_check=None):
            self.client.stop_requested = True
            return False

        self.client.send_review_request_once = stop_before_send
        await runtime["_send_scheduled_review_request"](
            self.client, "ABC12345", "Buyer", int(time.time()))
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.status()[0], "pending")

    async def test_crash_after_claim_cannot_resend_after_restart(self):
        self.store.record_order_observation("ABC12345", "CLOSED")
        self.store.schedule_review_request("ABC12345", "Buyer", int(time.time()))
        self.assertTrue(self.store.claim_review_request("ABC12345", int(time.time())))
        restarted = ReviewReceiptStore(self.store.path, Path(self.tmp.name) / "no-legacy.json")
        restarted.initialize()
        self.assertEqual(restarted.pending_review_requests(), [])
        ui.bot_settings["review_request_enabled"] = True
        await runtime["_send_scheduled_review_request"](self.client, "ABC12345", "Buyer", int(time.time()))
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.status()[0], "ambiguous")

    async def test_fresh_order_review_prevents_request(self):
        self.store.record_order_observation("ABC12345", "CLOSED")
        self.store.schedule_review_request("ABC12345", "Buyer", int(time.time()))
        ui.bot_settings["review_request_enabled"] = True
        self.client.order.review = object()
        await runtime["_send_scheduled_review_request"](self.client, "ABC12345", "Buyer", int(time.time()))
        self.assertEqual(self.client.sent, [])
        self.assertTrue(self.store.has_review_observation("ABC12345"))

    async def test_off_at_final_send_gate_starts_no_request(self):
        self.store.record_order_observation("ABC12345", "CLOSED")
        self.store.schedule_review_request("ABC12345", "Buyer", int(time.time()))
        ui.bot_settings["review_request_enabled"] = True
        def off_before_send(chat_id, value, *, enabled_check=None):
            ui.bot_settings["review_request_enabled"] = False
            return False if not enabled_check() else None
        self.client.send_review_request_once = off_before_send
        await runtime["_send_scheduled_review_request"](self.client, "ABC12345", "Buyer", int(time.time()))
        self.assertEqual(self.client.sent, [])
        self.assertIsNone(self.status())

    async def test_review_arrives_while_delayed_task_waits(self):
        self.store.record_order_observation("ABC12345", "CLOSED")
        scheduled = int(time.time()) + 2
        self.store.schedule_review_request("ABC12345", "Buyer", scheduled)
        ui.bot_settings["review_request_enabled"] = True
        task = asyncio.create_task(runtime["_send_scheduled_review_request"](
            self.client, "ABC12345", "Buyer", scheduled))
        await asyncio.sleep(0.05)
        self.store.record_review_observation("ABC12345")
        await task
        self.assertEqual(self.client.sent, [])

    async def test_disable_while_delayed_task_waits(self):
        self.store.record_order_observation("ABC12345", "CLOSED")
        scheduled = int(time.time()) + 2
        self.store.schedule_review_request("ABC12345", "Buyer", scheduled)
        ui.bot_settings["review_request_enabled"] = True
        task = asyncio.create_task(runtime["_send_scheduled_review_request"](
            self.client, "ABC12345", "Buyer", scheduled))
        await asyncio.sleep(0.05)
        ui.bot_settings["review_request_enabled"] = False
        await task
        self.assertEqual(self.client.sent, [])

    async def test_review_arrival_disable_refund_and_ambiguous(self):
        self.store.record_order_observation("ABC12345", "CLOSED")
        self.store.schedule_review_request("ABC12345", "Buyer", int(time.time()))
        ui.bot_settings["review_request_enabled"] = True
        self.store.record_review_observation("ABC12345")
        await runtime["_send_scheduled_review_request"](self.client, "ABC12345", "Buyer", int(time.time()))
        self.assertEqual(self.client.sent, [])
        self.assertFalse(self.store.claim_review_request("ABC12345", int(time.time())))
        self.assertIsNone(self.status())
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("DELETE FROM review_observations WHERE order_id = 'ABC12345'")
            db.commit()
        self.store.schedule_review_request("ABC12345", "Buyer", int(time.time()))
        ui.bot_settings["review_request_enabled"] = False
        await runtime["_send_scheduled_review_request"](self.client, "ABC12345", "Buyer", int(time.time()))
        self.assertEqual(self.client.sent, [])
        self.assertIsNone(self.status())
        ui.bot_settings["review_request_enabled"] = True
        self.store.schedule_review_request("ABC12345", "Buyer", int(time.time()))
        self.client.order.status = FunPayAPI.types.OrderStatuses.REFUNDED
        self.store.record_order_observation("ABC12345", "REFUNDED")
        await runtime["_send_scheduled_review_request"](self.client, "ABC12345", "Buyer", int(time.time()))
        self.assertEqual(self.client.sent, [])
        self.assertIsNone(self.status())
        self.store.record_order_observation("ABC12345", "CLOSED")
        self.client.order.status = FunPayAPI.types.OrderStatuses.CLOSED
        self.store.schedule_review_request("ABC12345", "Buyer", int(time.time()))
        self.client.send_error = TimeoutError()
        await runtime["_send_scheduled_review_request"](self.client, "ABC12345", "Buyer", int(time.time()))
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.status()[0], "ambiguous")
        await runtime["_send_scheduled_review_request"](self.client, "ABC12345", "Buyer", int(time.time()))
        self.assertEqual(len(self.client.sent), 1)


class ReviewTransportTests(unittest.TestCase):
    def test_unread_notification_retains_funpay_link(self):
        client = SimpleNamespace(
            account=SimpleNamespace(id=10, username="Seller"),
            _notified_unread_chats={}, _NOTIFY_COOLDOWN_SECONDS=60,
            _is_recent_outgoing_echo=lambda *args: False,
        )
        chat = SimpleNamespace(id=2, name="Buyer", unread=True,
                               last_message_type=FunPayAPI.types.MessageTypes.NON_SYSTEM,
                               last_message_text="Hello", last_message_author_id=20)
        event = SimpleNamespace(type=FunPayAPI.enums.EventTypes.LAST_CHAT_MESSAGE_CHANGED,
                                chat=chat)
        actions = FunPayClient.describe_event(client, event)
        notices = [action for action in actions if action.kind is ActionKind.NOTIFY_MESSAGE]
        self.assertEqual(len(notices), 1)
        self.assertIn("https://funpay.com/chat/?node=2", notices[0].text)
        self.assertIn("есть непрочитанные сообщения", notices[0].text)

    def test_installed_parser_attribute_error_and_single_ack_send(self):
        calls = []
        response = SimpleNamespace(content=b"", json=lambda: {
            "response": {"ok": True},
            "objects": [{"data": {"messages": [{
                "html": "<span>accepted without message-text</span>", "id": "1",
            }]}}],
        })

        def offline_method(method, endpoint, headers, payload, **kwargs):
            calls.append((method, endpoint, json.loads(payload["request"])))
            return response

        account = SimpleNamespace(is_initiated=True, csrf_token="offline-placeholder",
                                  bot_character="", runner=None, method=offline_method)
        account._Account__bot_character = ""
        with self.assertRaisesRegex(AttributeError, "'NoneType' object has no attribute 'text'"):
            FunPayAPI.Account.send_message(account, 2, "Review request")
        self.assertEqual(len(calls), 1)
        calls.clear()

        client = SimpleNamespace(account=account, _account_lock=threading.RLock(),
                                 _runner_stop=threading.Event(),
                                 _outgoing_echo_lock=threading.Lock(),
                                 _recent_outgoing_text={})
        self.assertTrue(FunPayClient.send_review_request_once(client, 2, "Review request"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0:2], ("post", "runner/"))
        self.assertEqual(calls[0][2]["data"]["content"], "Review request")


if __name__ == "__main__":
    unittest.main()
