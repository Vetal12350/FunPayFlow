"""Offline automation checks; main.py is parsed, never imported or run."""
import asyncio
import ast
import copy
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
from state import ReviewReceiptStore, StateError


MAIN_FUNCTIONS = {
    "_verified_incoming_message", "_maybe_autorespond", "_review_chat_context",
    "_send_scheduled_review_request", "_schedule_closed_review_request",
}
source = Path("main.py").read_text(encoding="utf-8")
nodes = [node for node in ast.parse(source).body
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in MAIN_FUNCTIONS]
runtime = {
    "asyncio": asyncio, "time": time, "FunPayAPI": FunPayAPI, "FunPayClient": object,
    "bot_settings": ui.bot_settings, "match_autoresponder": ui.match_autoresponder,
    "_expand_template": ui._expand_template,
    "expand_review_request_text": ui.expand_review_request_text,
    "is_review_request_enabled": ui.is_review_request_enabled,
    "is_safe_mode_enabled": ui.is_safe_mode_enabled,
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
        self.message = SimpleNamespace(id=100, text="HELLO", author_id=20,
                                       author="Buyer", by_bot=False)
        self.order = SimpleNamespace(status=FunPayAPI.types.OrderStatuses.CLOSED,
                                     buyer_username="Buyer", seller_id=10, review=None)
        self._manual_get_chat_history = lambda *args, **kwargs: [self.message]

    def send_message_once(self, chat_id, value, *, enabled_check=None):
        if enabled_check is not None and not enabled_check():
            return False
        self.sent.append((chat_id, value))
        if self.send_error:
            raise self.send_error

    def get_order_snapshot(self, order_id):
        return self.order, self.account.id


def incoming(text="HELLO", *, author=20, kind=None):
    chat = SimpleNamespace(id=2, name="Buyer", last_message_text=text,
                           last_message_type=kind or FunPayAPI.types.MessageTypes.NON_SYSTEM,
                           unread=True)
    return SimpleNamespace(type=FunPayAPI.enums.EventTypes.LAST_CHAT_MESSAGE_CHANGED, chat=chat)


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
        self.original_file = ui.SETTINGS_FILE
        ui.SETTINGS_FILE = str(Path(self.tmp.name) / "settings.json")
        ui.bot_settings.clear()
        ui.bot_settings.update(copy.deepcopy(ui._DEFAULT_GLOBAL_SETTINGS))
        ui._interaction_state.clear()
        self.store = ReviewReceiptStore(Path(self.tmp.name) / "state.sqlite3",
                                        Path(self.tmp.name) / "no-legacy.json")
        self.store.initialize()
        self.client = Client(self.store)
        self.rule_exact = {"id": "11111111", "trigger": " hello ", "template_id": "aaaaaaaa",
                           "match_mode": "EXACT", "enabled": True}
        self.rule_contains = {"id": "22222222", "trigger": "hell", "template_id": "bbbbbbbb",
                              "match_mode": "CONTAINS", "enabled": True}
        ui.bot_settings["reply_templates"] = [
            {"id": "aaaaaaaa", "title": "Exact", "text": "Exact {chat_name}"},
            {"id": "bbbbbbbb", "title": "Contains", "text": "Contains {account}"},
        ]
        ui.bot_settings["autoresponder_rules"] = [self.rule_contains, self.rule_exact]
        ui.bot_settings["autoresponder_enabled"] = True

    async def asyncTearDown(self):
        ui.bot_settings.clear()
        ui.bot_settings.update(self.original_settings)
        ui.SETTINGS_FILE = self.original_file
        ui._interaction_state.clear()
        self.tmp.cleanup()

    def status(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            return db.execute("SELECT state, sent_at FROM review_requests WHERE order_id = 'ABC12345'").fetchone()

    async def test_matching_normalization_exact_priority_and_disabled(self):
        self.assertEqual(ui.match_autoresponder("  HeLLo\r\n")[0]["id"], "11111111")
        self.assertEqual(ui.match_autoresponder("say HELLO please")[0]["id"], "22222222")
        same_mode = {**self.rule_contains, "id": "33333333", "trigger": "hello"}
        ui.bot_settings["autoresponder_rules"] = [self.rule_contains, same_mode]
        self.assertEqual(ui.match_autoresponder("hello there")[0]["id"], "22222222")
        ui.bot_settings["autoresponder_enabled"] = False
        self.assertIsNone(ui.match_autoresponder("HELLO"))

    async def test_one_send_duplicate_cooldown_and_precedence(self):
        seen, cooldowns = {}, {}
        handled = await runtime["_maybe_autorespond"](self.client, incoming(), seen, cooldowns)
        self.assertTrue(handled)
        self.assertEqual(self.client.sent, [(2, "Exact Buyer")])
        self.assertTrue(await runtime["_maybe_autorespond"](self.client, incoming(), seen, cooldowns))
        self.assertEqual(len(self.client.sent), 1)
        self.client.message.id = 101
        self.assertTrue(await runtime["_maybe_autorespond"](self.client, incoming(), seen, cooldowns))
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("if not handled_by_rule:", source)

    async def test_autoresponse_receipt_survives_restart_and_new_message_id(self):
        self.assertTrue(await runtime["_maybe_autorespond"](self.client, incoming(), {}, {}))
        self.assertEqual(len(self.client.sent), 1)
        reopened = ReviewReceiptStore(self.store.path, self.store.legacy_stats_path)
        reopened.initialize()
        self.client.review_state = reopened
        self.assertTrue(await runtime["_maybe_autorespond"](self.client, incoming(), {}, {}))
        self.assertEqual(len(self.client.sent), 1)
        self.client.message.id += 1
        self.assertTrue(await runtime["_maybe_autorespond"](self.client, incoming(), {}, {}))
        self.assertEqual(len(self.client.sent), 2)

    async def test_own_system_unknown_and_timeout(self):
        self.client.message.author_id = 10
        self.client.message.author = "Seller"
        self.assertFalse(await runtime["_maybe_autorespond"](self.client, incoming(), {}, {}))
        self.client.message.author_id = 20
        self.client.message.author = "Buyer"
        self.assertFalse(await runtime["_maybe_autorespond"](
            self.client, incoming(kind=FunPayAPI.types.MessageTypes.NEW_FEEDBACK), {}, {}))
        self.client.message.author = None
        self.assertFalse(await runtime["_maybe_autorespond"](self.client, incoming(), {}, {}))
        self.client.message.author = "Buyer"
        self.client.send_error = TimeoutError()
        seen, cooldowns = {}, {}
        self.assertTrue(await runtime["_maybe_autorespond"](self.client, incoming(), seen, cooldowns))
        self.assertEqual(len(self.client.sent), 1)
        self.assertTrue(await runtime["_maybe_autorespond"](self.client, incoming(), seen, cooldowns))
        self.assertEqual(len(self.client.sent), 1)

    async def test_rule_crud_reload_broken_reference_and_pending_isolation(self):
        ui._save_global_setting("autoresponder_rules", [self.rule_exact])
        ui.load_settings()
        self.assertEqual(ui.bot_settings["autoresponder_rules"], [self.rule_exact])
        ui.bot_settings["reply_templates"] = []
        self.assertIsNone(ui.match_autoresponder("hello"))
        ui.bot_settings["reply_templates"] = [{"id": "aaaaaaaa", "title": "Exact", "text": "hi"}]
        with patch.object(ui, "is_authorized", return_value=True):
            await ui.callback_handler(Callback("auto_add"))
            self.assertEqual(ui._interaction_state[1]["action"], "auto_add_trigger")
            await ui.text_handler(Message("New trigger"))
            self.assertEqual(ui._interaction_state[1]["action"], "auto_add_mode")
            await ui.callback_handler(Callback("auto_mode:CONTAINS"))
            self.assertEqual(ui._interaction_state[1]["action"], "auto_add_template")
            await ui.callback_handler(Callback("auto_template:aaaaaaaa"))
            self.assertEqual(len(ui.bot_settings["autoresponder_rules"]), 2)
            self.assertNotIn(1, ui._interaction_state)
            ui._interaction_state[1] = {"action": "review_edit"}
            await ui.callback_handler(Callback("menu_main"))
            self.assertNotIn(1, ui._interaction_state)
        ui.load_settings()
        self.assertEqual(len(ui.bot_settings["autoresponder_rules"]), 2)

    async def test_template_dependency_and_review_text_validation(self):
        with self.assertRaises(ValueError):
            ui.expand_review_request_text("{price}", "Seller", "Buyer", "ABC12345")
        self.assertEqual(ui.expand_review_request_text("{account} {buyer} {order_id}",
                                                      "Seller", "Buyer", "ABC12345"),
                         "Seller Buyer ABC12345")
        ui._interaction_state[1] = {"action": "review_edit"}
        with patch.object(ui, "is_authorized", return_value=True):
            await ui.text_handler(Message("{price}"))
        self.assertEqual(ui.bot_settings["review_request_text"], ui.DEFAULT_REVIEW_REQUEST_TEXT)
        self.assertEqual(ui._interaction_state[1]["action"], "review_edit")
        with patch.object(ui, "is_authorized", return_value=True):
            cb = Callback("tpl_delete:aaaaaaaa:0")
            await ui.callback_handler(cb)
        self.assertIsNotNone(ui._template("aaaaaaaa"))

    async def test_closed_schedule_duplicate_and_non_closed(self):
        first_closed = self.store.record_order_observation("ABC12345", "CLOSED")
        self.assertTrue(first_closed)
        ui.bot_settings["review_request_enabled"] = True
        ui.bot_settings["review_request_delay"] = 0
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

    async def test_safe_mode_blocks_autoresponse_and_final_send_gate(self):
        ui.bot_settings["safe_mode"] = True
        self.assertTrue(await runtime["_maybe_autorespond"](self.client, incoming(), {}, {}))
        self.assertEqual(self.client.sent, [])
        ui.bot_settings["safe_mode"] = False

        def safe_before_send(chat_id, value, *, enabled_check=None):
            ui.bot_settings["safe_mode"] = True
            return False if not enabled_check() else None

        self.client.send_message_once = safe_before_send
        self.assertTrue(await runtime["_maybe_autorespond"](self.client, incoming(), {}, {}))
        self.assertEqual(self.client.sent, [])

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
        self.client.send_message_once = off_before_send
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

    async def test_save_failure_rolls_back(self):
        previous = copy.deepcopy(ui.bot_settings["autoresponder_rules"])
        with patch.object(ui, "save_settings", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                ui._save_global_setting("autoresponder_rules", [])
        self.assertEqual(ui.bot_settings["autoresponder_rules"], previous)

    async def test_shared_send_path_marks_self_echo_once(self):
        calls = []
        fake = SimpleNamespace(
            _account_lock=threading.RLock(), _outgoing_echo_lock=threading.Lock(),
            _recent_outgoing_text={},
            account=SimpleNamespace(send_message=lambda *args, **kwargs: calls.append((args, kwargs))),
        )
        FunPayClient.send_message_once(fake, 2, "reply")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1]["update_last_saved_message"])
        self.assertTrue(FunPayClient._is_recent_outgoing_echo(fake, 2, "reply"))
        self.assertFalse(FunPayClient._is_recent_outgoing_echo(fake, 3, "reply"))
        self.assertIs(FunPayClient.send_message_once(fake, 2, "reply", enabled_check=lambda: False), False)
        self.assertEqual(len(calls), 1)

    async def test_rule_edits_and_review_settings_ui(self):
        with patch.object(ui, "is_authorized", return_value=True):
            await ui.callback_handler(Callback("auto_rule_mode:11111111"))
            self.assertEqual(ui._rule("11111111")["match_mode"], "CONTAINS")
            await ui.callback_handler(Callback("auto_rule_toggle:11111111"))
            self.assertFalse(ui._rule("11111111")["enabled"])
            await ui.callback_handler(Callback("auto_rule_trigger:11111111"))
            await ui.text_handler(Message("Changed"))
            self.assertEqual(ui._rule("11111111")["trigger"], "Changed")
            await ui.callback_handler(Callback("auto_select:11111111:bbbbbbbb"))
            self.assertEqual(ui._rule("11111111")["template_id"], "bbbbbbbb")
            await ui.callback_handler(Callback("review_toggle"))
            self.assertTrue(ui.bot_settings["review_request_enabled"])
            await ui.callback_handler(Callback("review_delay"))
            await ui.text_handler(Message("1440"))
            self.assertEqual(ui.bot_settings["review_request_delay"], 1440)
            await ui.callback_handler(Callback("review_edit"))
            await ui.text_handler(Message("Спасибо, {buyer}!"))
            self.assertEqual(ui.bot_settings["review_request_text"], "Спасибо, {buyer}!")
            await ui.callback_handler(Callback("review_reset"))
            self.assertEqual(ui.bot_settings["review_request_text"], ui.DEFAULT_REVIEW_REQUEST_TEXT)
            await ui.callback_handler(Callback("auto_rule_delete:11111111"))
            self.assertIsNone(ui._rule("11111111"))


if __name__ == "__main__":
    unittest.main()
