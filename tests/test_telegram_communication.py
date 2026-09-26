"""Offline checks for Telegram communication; no production bot is started."""
import log_isolation
import asyncio
import ast
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from funpayflow import telegram as ui
from funpayflow.runtime_events import ActionEvent, ActionKind
from funpayflow.runtime_control import automatic_action_gate, restart_requested


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
        self.previous_effective = ui._effective_modules
        self.previous_client = ui._runtime_client
        self.previous_file = ui.SETTINGS_FILE
        ui.SETTINGS_FILE = str(Path(self.tmp.name) / "settings.json")
        ui.bot_settings.clear()
        ui.bot_settings.update(copy.deepcopy(ui._DEFAULT_GLOBAL_SETTINGS))
        ui.configure_module_runtime(fresh_install=False)
        ui._interaction_state.clear()
        self.account = FakeAccount()
        ui._runtime_client = SimpleNamespace(
            account=self.account, _account_lock=threading.RLock(),
            runner_stop_requested=lambda: False,
        )

    async def asyncTearDown(self):
        ui.bot_settings.clear()
        ui.bot_settings.update(self.previous_settings)
        ui._effective_modules = self.previous_effective
        ui.SETTINGS_FILE = self.previous_file
        ui._runtime_client = self.previous_client
        ui._interaction_state.clear()
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
        source = Path("src/funpayflow/main.py").read_text(encoding="utf-8")
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "_send_night_mode_reply")
        namespace = {"FunPayClient": object, "asyncio": asyncio,
                     "ActionEvent": ActionEvent, "ActionKind": ActionKind,
                     "is_night_mode_enabled": ui.is_night_mode_enabled,
                     "is_safe_mode_enabled": ui.is_safe_mode_enabled,
                     "restart_requested": restart_requested,
                     "automatic_action_gate": automatic_action_gate,
                     "module_enabled": ui.module_enabled,
                     "get_night_mode_reply_text": ui.get_night_mode_reply_text,
                     "logger": SimpleNamespace(notify=lambda *args: None,
                                               warning=lambda *args: None)}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), namespace)
        ui._save_global_setting("night_mode_reply", "custom")
        ui.bot_settings["night_mode"] = True
        await namespace["_send_night_mode_reply"](
            ui._runtime_client, ActionEvent(ActionKind.NIGHT_MESSAGE, chat_id=2))
        self.assertEqual(self.account.sent[0][1], "custom")
        self.assertTrue(self.account.sent[0][2]["update_last_saved_message"])
        ui.bot_settings["night_mode"] = False
        await namespace["_send_night_mode_reply"](
            ui._runtime_client, ActionEvent(ActionKind.NIGHT_MESSAGE, chat_id=2))
        self.assertEqual(len(self.account.sent), 1)

    async def test_safe_mode_blocks_night_mode_auto_send(self):
        source = Path("src/funpayflow/main.py").read_text(encoding="utf-8")
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "_send_night_mode_reply")
        namespace = {"FunPayClient": object, "asyncio": asyncio,
                     "ActionEvent": ActionEvent, "ActionKind": ActionKind,
                     "is_night_mode_enabled": ui.is_night_mode_enabled,
                     "is_safe_mode_enabled": ui.is_safe_mode_enabled,
                     "restart_requested": restart_requested,
                     "automatic_action_gate": automatic_action_gate,
                     "module_enabled": ui.module_enabled,
                     "get_night_mode_reply_text": ui.get_night_mode_reply_text,
                     "logger": SimpleNamespace(notify=lambda *args: None,
                                               warning=lambda *args: None)}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), namespace)
        ui.bot_settings["night_mode"] = True
        ui.bot_settings["safe_mode"] = True
        await namespace["_send_night_mode_reply"](
            ui._runtime_client, ActionEvent(ActionKind.NIGHT_MESSAGE, chat_id=2))
        self.assertEqual(self.account.sent, [])

        entered, release = threading.Event(), threading.Event()

        def prepare_chat(name):
            entered.set()
            release.wait(2)
            return SimpleNamespace(id=2)

        self.account.get_chat_by_name = prepare_chat
        ui.bot_settings["safe_mode"] = False
        task = asyncio.create_task(namespace["_send_night_mode_reply"](
            ui._runtime_client, ActionEvent(ActionKind.NIGHT_ORDER,
                                            buyer="Buyer2", chat_id=2)))
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        ui.bot_settings["safe_mode"] = True
        release.set()
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.account.sent, [])




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

    async def test_authorization_save_failure_rolls_back_access(self):
        user_id = 987654321
        previous_ids = list(ui.bot_settings["authorized_user_ids"])
        ui.authorized_users.discard(user_id)
        ui._user_settings.pop(user_id, None)
        message = FakeMessage(user_id, "synthetic-password")
        with patch.dict(ui.os.environ, {"BOT_PASSWORD": "synthetic-password", "ADMIN_ID": "1"}), patch.object(
            ui, "save_settings", side_effect=RuntimeError("disk")
        ):
            await ui.text_handler(message)
        self.assertNotIn(user_id, ui.authorized_users)
        self.assertNotIn(user_id, ui._user_settings)
        self.assertEqual(ui.bot_settings["authorized_user_ids"], previous_ids)
        self.assertIn("Не удалось сохранить", message.sent[0][0])







if __name__ == "__main__":
    unittest.main()
