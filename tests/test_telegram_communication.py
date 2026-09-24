"""Offline checks for Telegram communication; no production bot is started."""
import asyncio
import ast
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import telegram as ui


class FakeMessage:
    def __init__(self, user_id=1, text=None):
        self.from_user = SimpleNamespace(id=user_id)
        self.text = text
        self.sent = []
        self.edits = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeCallback:
    def __init__(self, data, user_id=1):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeMessage(user_id)
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


class FakeAccount:
    def __init__(self):
        self.id = 10
        self.username = "Seller"
        self.chats = [SimpleNamespace(id=i, name=f"Buyer{i}", unread=i == 2,
                                      last_message_text="preview") for i in range(1, 13)]
        self.sent = []

    def request_chats(self):
        return self.chats

    def add_chats(self, chats):
        pass

    def get_chat_by_id(self, chat_id):
        return next((c for c in self.chats if c.id == chat_id), None)

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


class CommunicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_settings = copy.deepcopy(ui.bot_settings)
        self.previous_client = ui._runtime_client
        self.previous_file = ui.SETTINGS_FILE
        ui.SETTINGS_FILE = str(Path(self.tmp.name) / "settings.json")
        ui.bot_settings.clear()
        ui.bot_settings.update(copy.deepcopy(ui._DEFAULT_GLOBAL_SETTINGS))
        ui._interaction_state.clear()
        ui._chat_pages.clear()
        self.account = FakeAccount()
        ui._runtime_client = SimpleNamespace(
            account=self.account, _account_lock=threading.RLock(),
            send_message_once=lambda chat_id, text: self.account.send_message(
                chat_id, text, update_last_saved_message=True),
            _manual_get_chat_history=lambda chat_id, **kwargs: [
                SimpleNamespace(author_id=10, author="Seller", text="<mine>"),
                SimpleNamespace(author_id=20, author=f"Buyer{chat_id}", text="<&>"),
            ],
        )

    async def asyncTearDown(self):
        ui.bot_settings.clear()
        ui.bot_settings.update(self.previous_settings)
        ui.SETTINGS_FILE = self.previous_file
        ui._runtime_client = self.previous_client
        ui._interaction_state.clear()
        ui._chat_pages.clear()
        self.tmp.cleanup()

    async def test_night_mode_save_reset_reload_and_echo(self):
        self.assertEqual(ui.get_night_mode_reply_text(), ui.NIGHT_MODE_MESSAGE_TEXT)
        ui._save_global_setting("night_mode_reply", "Привет <buyer> 🌙")
        self.assertEqual(ui.get_night_mode_reply_text(), "Привет <buyer> 🌙")
        ui.load_settings()
        self.assertEqual(ui.get_night_mode_reply_text(), "Привет <buyer> 🌙")
        self.assertIn("&lt;buyer&gt;", ui._night_mode_screen()[0])
        self.assertTrue(ui.is_night_mode_reply_text("Привет <buyer> 🌙"))
        ui._save_global_setting("night_mode_reply", None)
        self.assertEqual(ui.get_night_mode_reply_text(), ui.NIGHT_MODE_MESSAGE_TEXT)
        self.assertTrue(ui.is_night_mode_reply_text("Привет <buyer> 🌙"))

    async def test_night_toggle_and_save_failure(self):
        self.assertTrue(ui.toggle_night_mode_saved())
        self.assertFalse(ui.toggle_night_mode_saved())
        with patch.object(ui, "save_settings", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                ui._save_global_setting("night_mode_reply", "new")
        self.assertIsNone(ui.bot_settings["night_mode_reply"])
        self.assertFalse(ui.is_night_mode_enabled())

    async def test_night_mode_worker_uses_custom_text(self):
        source = Path("main.py").read_text(encoding="utf-8")
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "_send_night_mode_reply")
        namespace = {"FunPayClient": object, "asyncio": asyncio,
                     "is_night_mode_enabled": ui.is_night_mode_enabled,
                     "is_safe_mode_enabled": ui.is_safe_mode_enabled,
                     "get_night_mode_reply_text": ui.get_night_mode_reply_text,
                     "logger": SimpleNamespace(notify=lambda *args: None,
                                               warning=lambda *args: None)}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), namespace)
        ui._save_global_setting("night_mode_reply", "custom")
        ui.bot_settings["night_mode"] = True
        await namespace["_send_night_mode_reply"](ui._runtime_client, "2", "message")
        self.assertEqual(self.account.sent[0][1], "custom")
        self.assertTrue(self.account.sent[0][2]["update_last_saved_message"])
        ui.bot_settings["night_mode"] = False
        await namespace["_send_night_mode_reply"](ui._runtime_client, "2", "message")
        self.assertEqual(len(self.account.sent), 1)

    async def test_safe_mode_blocks_night_mode_auto_send(self):
        source = Path("main.py").read_text(encoding="utf-8")
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "_send_night_mode_reply")
        namespace = {"FunPayClient": object, "asyncio": asyncio,
                     "is_night_mode_enabled": ui.is_night_mode_enabled,
                     "is_safe_mode_enabled": ui.is_safe_mode_enabled,
                     "get_night_mode_reply_text": ui.get_night_mode_reply_text,
                     "logger": SimpleNamespace(notify=lambda *args: None,
                                               warning=lambda *args: None)}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), namespace)
        ui.bot_settings["night_mode"] = True
        ui.bot_settings["safe_mode"] = True
        await namespace["_send_night_mode_reply"](ui._runtime_client, "2", "message")
        self.assertEqual(self.account.sent, [])

        entered, release = threading.Event(), threading.Event()

        def prepare_chat(name):
            entered.set()
            release.wait(2)
            return SimpleNamespace(id=2)

        self.account.get_chat_by_name = prepare_chat
        ui.bot_settings["safe_mode"] = False
        task = asyncio.create_task(namespace["_send_night_mode_reply"](
            ui._runtime_client, ("Buyer2", 2), "order"))
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        ui.bot_settings["safe_mode"] = True
        release.set()
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.account.sent, [])

    async def test_chats_pagination_and_safe_preview(self):
        self.assertEqual([[b.text for b in row] for row in ui.get_reply_keyboard().keyboard],
                         [["🛠 Главное меню"]])
        self.assertIn("chats:0", [b.callback_data for row in ui.get_main_keyboard(1).inline_keyboard
                                   for b in row])
        chats = ui._fetch_chats()
        text, markup = ui._chats_screen(chats, 0)
        self.assertIn("1/2", text)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "chat:1")
        self.assertIn("🟠", markup.inline_keyboard[1][0].text)
        self.assertIn("chats:1", [b.callback_data for row in markup.inline_keyboard for b in row])
        name, messages = ui._fetch_chat(2)
        view, markup = ui._chat_screen(2, name, messages)
        self.assertIn("&lt;mine&gt;", view)
        self.assertIn("&lt;&amp;&gt;", view)
        self.assertIn("🧑‍💻 я", view)
        self.assertIn("👤 собеседник", view)
        self.assertLess(len(ui._chat_screen(2, "x", [("x", "&" * 10000)] * 10)[0]), 4096)
        ui._runtime_client._manual_get_chat_history = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("private response"))
        self.assertEqual(ui._fetch_chat(2)[1], [("Последний фрагмент · история недоступна", "preview")])

    async def test_manual_reply_binding_single_send_and_cancel(self):
        ui._chat_pages[1] = ui._fetch_chats()
        await ui._communication_callback(FakeCallback("reply:2"), "reply:2", 1)
        self.assertEqual(ui._interaction_state[1]["chat_id"], 2)
        with patch.object(ui, "is_authorized", return_value=True):
            await ui.text_handler(FakeMessage(text="Hello"))
            await ui.text_handler(FakeMessage(text="ignored"))
        self.assertEqual(len(self.account.sent), 1)
        self.assertEqual(self.account.sent[0][0:2], (2, "Hello"))
        self.assertEqual(ui._interaction_state, {})
        await ui._communication_callback(FakeCallback("reply:3"), "reply:3", 1)
        with patch.object(ui, "is_authorized", return_value=True):
            await ui.text_handler(FakeMessage(text="❌ Отмена"))
        self.assertEqual(len(self.account.sent), 1)

    async def test_authorization_and_command_safety(self):
        cb = FakeCallback("reply:2", user_id=900)
        with patch.object(ui, "is_authorized", return_value=False):
            await ui.callback_handler(cb)
            await ui.text_handler(FakeMessage(900, "send"))
        self.assertNotIn(900, ui._interaction_state)
        self.assertEqual(self.account.sent, [])
        ui._interaction_state[1] = {"action": "night_edit"}
        with patch.object(ui, "is_authorized", return_value=True):
            await ui.text_handler(FakeMessage(text="/status"))
        self.assertNotIn(1, ui._interaction_state)
        self.assertIsNone(ui.bot_settings["night_mode_reply"])

    async def test_transport_ambiguity_has_no_retry(self):
        ui._interaction_state[1] = {"action": "reply", "chat_id": 2}
        calls = []
        def timeout(*args, **kwargs):
            calls.append(1)
            raise ui.requests.exceptions.Timeout()
        with patch.object(ui, "is_authorized", return_value=True), patch.object(ui, "_send_to_chat", timeout):
            msg = FakeMessage(text="Hello")
            await ui.text_handler(msg)
        self.assertEqual(calls, [1])
        self.assertIn("не удалось подтвердить", msg.sent[0][0].lower())
        self.assertNotIn(1, ui._interaction_state)

    async def test_definite_funpay_rejection_is_safe(self):
        error = ui.FunPayAPI.exceptions.MessageNotDeliveredError.__new__(
            ui.FunPayAPI.exceptions.MessageNotDeliveredError)
        error.error_message = "private server response"
        def reject(*args, **kwargs):
            raise error
        with patch.object(ui, "_send_to_chat", reject):
            msg = FakeMessage()
            await ui._send_one_reply(msg, 2, "text")
        self.assertIn("отклонил", msg.sent[0][0])
        self.assertNotIn("private", msg.sent[0][0])

    async def test_templates_save_reload_rollback_and_variables(self):
        template = {"id": "abcdef12", "title": "Hi", "text": "Hi {chat_name} from {account}"}
        ui._save_global_setting("reply_templates", [template])
        ui.load_settings()
        self.assertEqual(ui._template("abcdef12"), template)
        self.assertEqual(ui._expand_template(template["text"], "Buyer", "Seller"), "Hi Buyer from Seller")
        with self.assertRaises(ValueError):
            ui._expand_template("Order {order_id}", "Buyer", "Seller")
        with self.assertRaises(ValueError):
            ui._validate_template("{chat_name!r}")
        self.assertIn("&lt;Buyer&gt;", ui._escaped_preview(
            ui._expand_template("{chat_name}", "<Buyer>", "Seller"), 100))
        with patch.object(ui, "save_settings", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                ui._save_global_setting("reply_templates", [])
        self.assertEqual(ui._templates(), [template])

    async def test_quick_send_requires_confirmation_and_consumes_state(self):
        ui._chat_pages[1] = ui._fetch_chats()
        ui._save_global_setting("reply_templates", [{"id": "abcdef12", "title": "Hi",
                                                      "text": "Hi {chat_name}"}])
        cb = FakeCallback("quick_preview:abcdef12:2")
        await ui._communication_callback(cb, cb.data, 1)
        self.assertEqual(self.account.sent, [])
        self.assertIn("Hi Buyer2", cb.message.edits[0][0])
        send = FakeCallback("quick_send:abcdef12:2")
        await ui._communication_callback(send, send.data, 1)
        await ui._communication_callback(FakeCallback(send.data), send.data, 1)
        self.assertEqual(len(self.account.sent), 1)

    async def test_template_ui_crud_and_pending_isolation(self):
        ui._chat_pages[1] = ui._fetch_chats()
        with patch.object(ui, "is_authorized", return_value=True):
            await ui._communication_callback(FakeCallback("tpl_add:0"), "tpl_add:0", 1)
            await ui.text_handler(FakeMessage(text="Title"))
            await ui.text_handler(FakeMessage(text="Text {account}"))
            self.assertEqual(len(ui._templates()), 1)
            item_id = ui._templates()[0]["id"]
            await ui._communication_callback(FakeCallback(f"tpl_title:{item_id}:0"), f"tpl_title:{item_id}:0", 1)
            await ui.text_handler(FakeMessage(text="Renamed"))
            self.assertEqual(ui._template(item_id)["title"], "Renamed")
            await ui._communication_callback(FakeCallback(f"tpl_delete:{item_id}:0"), f"tpl_delete:{item_id}:0", 1)
            self.assertEqual(ui._templates(), [])
            ui._interaction_state[1] = {"action": "reply", "chat_id": 2}
            await ui.text_handler(FakeMessage(text="🛠 Главное меню"))
            self.assertNotIn(1, ui._interaction_state)
            ui._interaction_state[1] = {"action": "reply", "chat_id": 2}
            await ui.callback_handler(FakeCallback("menu_stats"))
            self.assertNotIn(1, ui._interaction_state)


if __name__ == "__main__":
    unittest.main()
