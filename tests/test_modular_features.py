"""Offline coverage for static modules, setup and runtime task gates."""

import ast
import asyncio
import copy
import json
import os
import queue
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from funpayflow import telegram as ui
from funpayflow.feature_registry import (_index, all_features, get_feature, is_feature_enabled,
                              is_fresh_install, profile_modules, resolve_modules,
                              validate_modules_config)
from funpayflow.runtime_events import ActionEvent, ActionKind
from funpayflow.state import ReviewReceiptStore, StateError


class Message:
    def __init__(self, text="", user_id=100):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))


class Callback:
    def __init__(self, action, user_id=100):
        self.data = action
        self.from_user = SimpleNamespace(id=user_id)
        self.edits = []
        self.answers = []
        self.message = SimpleNamespace(edit_text=self.edit_text)

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


def callback_data(markup):
    return {button.callback_data for row in markup.inline_keyboard for button in row}


def main_function(name, namespace):
    source = Path("src/funpayflow/main.py").read_text(encoding="utf-8")
    node = next(node for node in ast.parse(source).body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), namespace)
    return namespace[name]


class RegistryTests(unittest.TestCase):
    def test_known_ids_duplicates_unknown_and_missing_defaults(self):
        expected = {"autobump", "notifications", "night_mode", "review_request",
                    "order_history", "statistics", "sales_analytics", "sales_import",
                    "withdrawals", "logs_ui"}
        self.assertEqual({feature.id for feature in all_features()}, expected)
        self.assertIsNone(get_feature("external_plugin"))
        with self.assertRaises(ValueError):
            _index((all_features()[0], all_features()[0]))
        resolved = resolve_modules({"unknown_future_module": "future-format", "autobump": False})
        self.assertFalse(is_feature_enabled(resolved, "unknown_future_module"))
        self.assertFalse(resolved["autobump"])
        self.assertTrue(resolved["statistics"])  # Legacy missing-ID default.
        self.assertFalse(resolve_modules({}, fresh_default=True)["statistics"])
        with self.assertRaises(TypeError):
            resolved["autobump"] = True
        for malformed in (None, [], {"autobump": "yes"}, {"autobump": 1}):
            with self.assertRaises(ValueError):
                validate_modules_config(malformed)

    def test_profiles_exact_and_install_classification(self):
        self.assertEqual({key for key, value in profile_modules("minimal").items() if value},
                         {"notifications"})
        self.assertEqual({key for key, value in profile_modules("seller").items() if value},
                         {"notifications", "order_history", "statistics", "sales_analytics",
                          "sales_import", "withdrawals", "logs_ui"})
        self.assertTrue(all(profile_modules("all").values()))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings, db, legacy = (root / name for name in
                                    ("bot_settings.json", "state.sqlite3", "stats_log.json"))
            self.assertTrue(is_fresh_install(settings, db, legacy))
            db.touch()
            self.assertFalse(is_fresh_install(settings, db, legacy))
            db.unlink()
            legacy.touch()
            self.assertFalse(is_fresh_install(settings, db, legacy))

    def test_fresh_marker_precedes_account_and_sqlite_startup(self):
        source = ast.parse(Path("src/funpayflow/main.py").read_text(encoding="utf-8"))
        main_node = next(node for node in source.body
                         if isinstance(node, ast.AsyncFunctionDef) and node.name == "main")
        calls = {}
        for node in ast.walk(main_node):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                calls.setdefault(node.func.id, node.lineno)
        self.assertLess(calls["is_fresh_install"], calls["configure_module_runtime"])
        self.assertLess(calls["configure_module_runtime"], calls["save_settings"])
        self.assertLess(calls["save_settings"], calls["FunPayClient"])


class ModuleStateMixin:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_file = ui.SETTINGS_FILE
        self.old_settings = copy.deepcopy(ui.bot_settings)
        self.old_user_settings = copy.deepcopy(ui._user_settings)
        self.old_effective = ui.effective_modules()
        self.old_runtime = ui._runtime_client
        self.env = patch.dict(os.environ, {"ADMIN_ID": "100"})
        self.env.start()
        ui.SETTINGS_FILE = str(Path(self.temp.name) / "bot_settings.json")
        ui.bot_settings.clear()
        ui.bot_settings.update(copy.deepcopy(ui._DEFAULT_GLOBAL_SETTINGS))
        ui._user_settings.clear()
        ui._module_drafts.clear()
        ui._interaction_state.clear()
        ui._runtime_client = None
        ui.configure_module_runtime(fresh_install=False)

    def tearDown(self):
        ui.SETTINGS_FILE = self.old_file
        ui.bot_settings.clear()
        ui.bot_settings.update(self.old_settings)
        ui._user_settings.clear()
        ui._user_settings.update(self.old_user_settings)
        ui._effective_modules = self.old_effective
        ui._runtime_client = self.old_runtime
        ui._module_drafts.clear()
        ui._interaction_state.clear()
        self.env.stop()
        self.temp.cleanup()


class SettingsTests(ModuleStateMixin, unittest.TestCase):
    def test_safe_mode_fresh_legacy_and_malformed_settings(self):
        ui.configure_module_runtime(fresh_install=True)
        self.assertFalse(ui.is_safe_mode_enabled())
        path = Path(ui.SETTINGS_FILE)
        for value in (True, False):
            path.write_text(json.dumps({"safe_mode": value}), encoding="utf-8")
            ui.load_settings()
            self.assertIs(ui.is_safe_mode_enabled(), value)
            ui.configure_module_runtime(fresh_install=False)
            self.assertIs(ui.is_safe_mode_enabled(), value)
        ui.bot_settings["safe_mode"] = True
        path.write_text(json.dumps({"safe_mode": "invalid"}), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            ui.load_settings()
        self.assertTrue(ui.is_safe_mode_enabled())

    def test_legacy_usd_currency_is_preserved_and_codes_are_normalized(self):
        path = Path(ui.SETTINGS_FILE)
        path.write_text(json.dumps({"stats_currency": " USD ",
                                    "authorized_user_ids": [100]}), encoding="utf-8")
        ui.load_settings()
        self.assertEqual(ui.bot_settings["stats_currency"], "USD")
        self.assertEqual(ui.bot_settings["primary_currency"], "USD")
        path.write_text(json.dumps({"stats_currency": " eur ",
                                    "authorized_user_ids": [100]}), encoding="utf-8")
        ui.load_settings()
        self.assertEqual(ui.bot_settings["stats_currency"], "EUR")
        self.assertEqual(ui.bot_settings["primary_currency"], "EUR")
        ui.save_settings(required=True)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))
                         ["primary_currency"], "EUR")

    def test_legacy_values_and_modules_survive_migration(self):
        old = {"auto_bump": False, "night_mode": True, "safe_mode": True,
               "review_request_enabled": True, "authorized_user_ids": [100],
               "stats_currency": "EUR", "user_settings": {"100": {"notify_review": False}}}
        Path(ui.SETTINGS_FILE).write_text(json.dumps(old), encoding="utf-8")
        ui.load_settings()
        ui.configure_module_runtime(fresh_install=False)
        self.assertTrue(ui.bot_settings["setup_completed"])
        self.assertTrue(all(ui.effective_modules().values()))
        self.assertFalse(ui.bot_settings["auto_bump"])
        self.assertTrue(ui.bot_settings["night_mode"])
        self.assertTrue(ui.bot_settings["review_request_enabled"])
        self.assertFalse(ui.get_user_settings(100)["notify_review"])
        self.assertEqual(ui.bot_settings["stats_currency"], "EUR")
        ui.save_settings(required=True)
        saved = json.loads(Path(ui.SETTINGS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(saved["modules"], profile_modules("all"))
        self.assertEqual(saved["authorized_user_ids"], [100])

    def test_malformed_config_and_atomic_failure_do_not_replace_settings(self):
        original = {"authorized_user_ids": [100], "modules": {"autobump": "wrong"}}
        path = Path(ui.SETTINGS_FILE)
        path.write_text(json.dumps(original), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(RuntimeError):
            ui.load_settings()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(ui.bot_settings["authorized_user_ids"], [])
        ui.bot_settings["authorized_user_ids"] = [100]
        ui.bot_settings["modules"] = profile_modules("seller")
        with patch.object(ui.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(RuntimeError):
                ui.save_settings(required=True)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(ui.bot_settings["authorized_user_ids"], [100])

    def test_unknown_future_id_is_ignored_when_loading(self):
        settings = {"authorized_user_ids": [100],
                    "modules": {"autobump": False, "future_feature": {"new": "shape"}}}
        Path(ui.SETTINGS_FILE).write_text(json.dumps(settings), encoding="utf-8")
        ui.load_settings()
        ui.configure_module_runtime(fresh_install=False)
        self.assertEqual(ui.bot_settings["authorized_user_ids"], [100])
        self.assertFalse(ui.module_enabled("autobump"))
        self.assertTrue(ui.module_enabled("statistics"))
        self.assertNotIn("future_feature", ui.bot_settings["modules"])

    def test_fresh_setup_interruption_and_currency_unknown(self):
        ui.configure_module_runtime(fresh_install=True)
        self.assertFalse(ui.bot_settings["setup_completed"])
        self.assertIsNone(ui.bot_settings["stats_currency"])
        self.assertIsNone(ui.bot_settings["primary_currency"])
        self.assertEqual(ui.bot_settings["modules"], profile_modules("all"))
        self.assertFalse(any(ui.effective_modules().values()))
        ui.save_settings(required=True)
        db_path = Path(self.temp.name) / "state.sqlite3"
        db_path.touch()  # First boot may stop after creating SQLite.
        self.assertFalse(is_fresh_install(Path(ui.SETTINGS_FILE), db_path,
                                          Path(self.temp.name) / "stats_log.json"))
        ui.load_settings()
        ui.configure_module_runtime(fresh_install=False)
        self.assertFalse(ui.bot_settings["setup_completed"])
        self.assertFalse(any(ui.effective_modules().values()))
        ui.bot_settings["modules"] = profile_modules("all")
        ui.configure_module_runtime(fresh_install=False)
        self.assertFalse(any(ui.effective_modules().values()))


class TelegramModuleTests(ModuleStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_modules_save_without_currency_and_statistics_configures_it(self):
        ui.configure_module_runtime(fresh_install=True)
        await ui._modules_callback(Callback("modules_open"), "modules_open", 100)
        await ui._modules_callback(Callback("modules_save"), "modules_save", 100)
        self.assertTrue(ui.bot_settings["setup_completed"])
        self.assertIsNone(ui.bot_settings["stats_currency"])
        self.assertTrue(all(ui.bot_settings["modules"].values()))
        ui.configure_module_runtime(fresh_install=False)
        stats = Callback("menu_stats")
        await ui.callback_handler(stats)
        self.assertIn("currency_open:stats", callback_data(stats.edits[-1][1]["reply_markup"]))
        currency = Callback("currency_open:stats")
        await ui.callback_handler(currency)
        await ui.text_handler(Message(" rub "))
        self.assertEqual(ui.bot_settings["stats_currency"], "RUB")
        self.assertEqual(ui.bot_settings["primary_currency"], "RUB")
        self.assertTrue(ui.bot_settings["modules"]["statistics"])
        self.assertEqual(json.loads(Path(ui.SETTINGS_FILE).read_text(encoding="utf-8"))
                         ["primary_currency"], "RUB")

    async def test_analytics_currency_setup_is_separate_and_atomic(self):
        ui.configure_module_runtime(fresh_install=True)
        await ui._modules_callback(Callback("modules_open"), "modules_open", 100)
        await ui._modules_callback(Callback("modules_save"), "modules_save", 100)
        ui.configure_module_runtime(fresh_install=False)
        analytics = Callback("analytics")
        await ui.callback_handler(analytics)
        self.assertIn("currency_open:analytics",
                      callback_data(analytics.edits[-1][1]["reply_markup"]))
        denied = Callback("currency_open:analytics", user_id=101)
        with patch.object(ui, "is_authorized", return_value=True):
            await ui.callback_handler(denied)
        self.assertIn("владельцу", str(denied.answers[-1]))
        await ui.callback_handler(Callback("currency_open:analytics"))
        with patch.object(ui.os, "replace", side_effect=OSError("disk")):
            failed = Message("EUR")
            await ui.text_handler(failed)
        self.assertIsNone(ui.bot_settings["primary_currency"])
        self.assertIsNone(ui.bot_settings["stats_currency"])
        self.assertIn("Не удалось сохранить", failed.sent[-1][0])
        await ui.text_handler(Message("EUR"))
        self.assertEqual(ui.bot_settings["primary_currency"], "EUR")
        self.assertEqual(ui.bot_settings["stats_currency"], "EUR")
        self.assertEqual(json.loads(Path(ui.SETTINGS_FILE).read_text(encoding="utf-8"))
                         ["primary_currency"], "EUR")

    async def test_module_save_preserves_legacy_primary_currency(self):
        ui.bot_settings["primary_currency"] = "USD"
        ui.bot_settings["stats_currency"] = "USD"
        await ui._modules_callback(Callback("modules_open"), "modules_open", 100)
        await ui._modules_callback(Callback("modules_toggle:night_mode"),
                                   "modules_toggle:night_mode", 100)
        await ui._modules_callback(Callback("modules_save"), "modules_save", 100)
        self.assertEqual(ui.bot_settings["primary_currency"], "USD")
        saved = json.loads(Path(ui.SETTINGS_FILE).read_text(encoding="utf-8"))
        self.assertEqual((saved["primary_currency"], saved["stats_currency"]),
                         ("USD", "USD"))

    async def test_wizard_profile_save_without_currency_and_restart_snapshot(self):
        ui.configure_module_runtime(fresh_install=True)
        start = Message("/start")
        await ui.cmd_start(start)
        self.assertTrue(any("Первоначальная настройка" in text for text, _ in start.sent))
        await ui._modules_callback(Callback("modules_profile:seller"), "modules_profile:seller", 100)
        save = Callback("modules_save")
        await ui._modules_callback(save, save.data, 100)
        self.assertTrue(ui.bot_settings["setup_completed"])
        self.assertIn("restart_open", callback_data(save.edits[-1][1]["reply_markup"]))
        self.assertIsNone(ui.bot_settings["stats_currency"])
        self.assertIsNone(ui.bot_settings["primary_currency"])
        self.assertFalse(any(ui.effective_modules().values()))
        self.assertEqual(ui.bot_settings["modules"], profile_modules("seller"))
        self.assertEqual(json.loads(Path(ui.SETTINGS_FILE).read_text(encoding="utf-8"))
                         ["primary_currency"], None)
        ui.configure_module_runtime(fresh_install=False)
        self.assertEqual(dict(ui.effective_modules()), profile_modules("seller"))

    async def test_manager_owner_draft_cancel_recommended_and_save_failure(self):
        denied = Callback("modules_open", user_id=101)
        await ui._modules_callback(denied, denied.data, 101)
        self.assertFalse(ui._module_drafts)
        self.assertIn("modules_open", callback_data(ui.get_main_keyboard(100)))
        self.assertNotIn("modules_open", callback_data(ui._system_screen(100)[1]))
        await ui._modules_callback(Callback("modules_open"), "modules_open", 100)
        await ui._modules_callback(Callback("modules_toggle:night_mode"),
                                   "modules_toggle:night_mode", 100)
        await ui._modules_callback(Callback("modules_cancel"), "modules_cancel", 100)
        self.assertTrue(ui.bot_settings["modules"]["night_mode"])
        await ui._modules_callback(Callback("modules_open"), "modules_open", 100)
        await ui._modules_callback(Callback("modules_recommended"), "modules_recommended", 100)
        self.assertEqual(ui._module_drafts[100]["modules"], profile_modules("seller"))
        with patch.object(ui.os, "replace", side_effect=OSError("disk")):
            await ui._modules_callback(Callback("modules_save"), "modules_save", 100)
        self.assertEqual(ui.bot_settings["modules"], profile_modules("all"))
        await ui._modules_callback(Callback("modules_save"), "modules_save", 100)
        self.assertEqual(ui.bot_settings["modules"], profile_modules("seller"))
        self.assertTrue(ui.module_enabled("night_mode"))  # Current process unchanged.
        self.assertFalse(json.loads(Path(ui.SETTINGS_FILE).read_text(encoding="utf-8"))
                         ["modules"]["night_mode"])

    async def test_disabled_menu_callbacks_log_and_pending_input(self):
        chosen = profile_modules("minimal")
        chosen["sales_import"] = True
        ui.bot_settings["modules"] = chosen
        ui.configure_module_runtime(fresh_install=False)
        main_actions = callback_data(ui.get_main_keyboard(100))
        self.assertIn("menu_notifications", main_actions)
        self.assertIn("modules_open", main_actions)
        self.assertIn("restart_open", main_actions)
        for hidden in ("toggle_bump", "menu_stats", "analytics", "orders",
                       "menu_night_mode", "review_menu", "menu_logs"):
            self.assertNotIn(hidden, main_actions)
        self.assertIn("sales_import_open", callback_data(ui._system_screen(100)[1]))
        for old in ("toggle_bump", "ord_list:all:0", "menu_stats", "analytics",
                    "review_toggle", "toggle_night_mode", "sales_import_confirm:old",
                    "menu_logs"):
            if old.startswith("sales_import_"):
                continue  # Import is enabled in this configuration.
            callback = Callback(old)
            await ui.callback_handler(callback)
            self.assertTrue(callback.answers)
            self.assertIn("отключён", str(callback.answers[-1]))
        log = Message("/log 2026-01-01")
        await ui.cmd_log(log)
        self.assertIn("отключён", log.sent[-1][0])
        ui._interaction_state[100] = {"action": "night_edit"}
        await ui.text_handler(Message("new auto reply"))
        self.assertFalse(ui.bot_settings["night_mode"])
        self.assertNotIn(100, ui._interaction_state)
        ui.bot_settings["modules"]["sales_import"] = False
        ui.configure_module_runtime(fresh_install=False)
        stale_import = Callback("sales_import_open")
        await ui.callback_handler(stale_import)
        self.assertIn("отключён", str(stale_import.answers[-1]))

    async def test_all_modules_show_existing_main_menu_actions(self):
        self.assertEqual(callback_data(ui.get_main_keyboard(100)), {
            "toggle_bump", "menu_stats", "analytics", "orders", "menu_status",
            "system", "menu_notifications", "menu_night_mode", "review_menu", "menu_logs",
            "modules_open", "restart_open",
        })
        self.assertNotIn("modules_open", callback_data(ui._system_screen(100)[1]))
        self.assertEqual(ui.get_main_keyboard(100).inline_keyboard[-1][0].callback_data,
                         "restart_open")
        self.assertEqual(ui.get_main_keyboard(100).inline_keyboard[-2][0].callback_data,
                         "modules_open")
        self.assertNotIn("restart_open", callback_data(ui.get_main_keyboard(101)))

    async def test_unverified_account_currency_is_absent_from_modules(self):
        ui.configure_module_runtime(fresh_install=True)
        ui._runtime_client = SimpleNamespace(account=SimpleNamespace(
            balance=SimpleNamespace(total_rub=10, total_usd=0, total_eur=0)))
        draft = ui._new_module_draft("wizard")
        draft["modules"] = profile_modules("seller")
        text, markup = ui._module_draft_screen(draft)
        self.assertNotIn("currency", draft)
        self.assertNotIn("Валюта", text)
        self.assertNotIn("modules_currency", callback_data(markup))
        self.assertNotIn("Использовать USD", text)

    async def test_unchanged_modules_save_does_not_prompt_restart(self):
        await ui._modules_callback(Callback("modules_open"), "modules_open", 100)
        saved = Callback("modules_save")
        await ui._modules_callback(saved, saved.data, 100)
        self.assertNotIn("restart_open", callback_data(saved.edits[-1][1]["reply_markup"]))

    async def test_all_profile_warning_and_manual_selection(self):
        ui.configure_module_runtime(fresh_install=True)
        ui._module_drafts[100] = ui._new_module_draft("wizard")
        all_callback = Callback("modules_profile:all")
        await ui._modules_callback(all_callback, all_callback.data, 100)
        self.assertTrue(all(ui._module_drafts[100]["modules"].values()))
        self.assertIn("автоматически включится", all_callback.edits[-1][0])
        self.assertIn("Ночной режим и запрос отзыва", all_callback.edits[-1][0])
        self.assertIn("SAFE_MODE", all_callback.edits[-1][0])
        await ui._modules_callback(Callback("modules_manual"), "modules_manual", 100)
        await ui._modules_callback(Callback("modules_toggle:autobump"),
                                   "modules_toggle:autobump", 100)
        self.assertFalse(ui._module_drafts[100]["modules"]["autobump"])

    async def test_manual_selection_saves_without_hot_start(self):
        ui.configure_module_runtime(fresh_install=True)
        ui._module_drafts[100] = ui._new_module_draft("wizard")
        await ui._modules_callback(Callback("modules_manual"), "modules_manual", 100)
        await ui._modules_callback(Callback("modules_save"), "modules_save", 100)
        self.assertTrue(ui.bot_settings["setup_completed"])
        self.assertTrue(ui.bot_settings["modules"]["autobump"])
        self.assertFalse(ui.module_enabled("autobump"))
        self.assertFalse(ui.bot_settings["auto_bump"])
        ui.configure_module_runtime(fresh_install=False)
        ui.enable_autobump_on_startup()
        self.assertTrue(ui.bot_settings["auto_bump"])

    async def test_reenable_preserves_ordinary_settings_and_sqlite(self):
        db_path = Path(self.temp.name) / "state.sqlite3"
        store = ReviewReceiptStore(db_path, Path(self.temp.name) / "missing-stats.json")
        store.initialize()
        store.record_order_observation("ABC12345", "CLOSED")
        store.schedule_review_request("ABC12345", "Synthetic buyer", 1)
        ui.bot_settings["night_mode"] = True
        ui.bot_settings["review_request_enabled"] = True
        off = profile_modules("all")
        off["night_mode"] = False
        off["review_request"] = False
        off["withdrawals"] = False
        ui.bot_settings["modules"] = off
        ui.save_settings(required=True)
        ui.configure_module_runtime(fresh_install=False)
        self.assertFalse(ui.module_enabled("night_mode"))
        self.assertTrue(ui.bot_settings["night_mode"])
        self.assertTrue(ui.bot_settings["review_request_enabled"])
        self.assertEqual(store.count_pending_review_requests(), 1)
        with closing(sqlite3.connect(db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
        ui.bot_settings["modules"] = profile_modules("all")
        ui.save_settings(required=True)
        ui.configure_module_runtime(fresh_install=False)
        self.assertTrue(ui.module_enabled("night_mode"))
        self.assertTrue(ui.bot_settings["night_mode"])
        self.assertEqual(store.count_pending_review_requests(), 1)

    async def test_disabled_withdrawal_status_and_problem_are_not_errors(self):
        ui._set_problem("WITHDRAWAL_REPEATED")
        ui.bot_settings["modules"] = profile_modules("minimal")
        ui.configure_module_runtime(fresh_install=False)
        self.assertNotIn("WITHDRAWAL_REPEATED",
                         {problem["code"] for problem in ui.get_active_problems()})
        old_started = ui._runtime_started_at
        ui._runtime_started_at = time.monotonic()
        ui._runtime_client = SimpleNamespace(
            get_runner_health=lambda: {"state": "healthy", "consecutive_errors": 0,
                                       "last_success_monotonic": time.monotonic()},
            event_queue=queue.Queue(maxsize=10),
            review_state=SimpleNamespace(count_pending_review_requests=lambda: 0),
            _review_state_failed=False,
        )
        try:
            status = ui.get_runtime_status_text()
            self.assertIn("Withdrawal polling: модуль отключён", status)
            self.assertIn("🧩 Модули: 1/10", status)
        finally:
            ui._runtime_started_at = old_started

    async def test_notifications_off_still_persists_review_observation(self):
        ui.bot_settings["modules"] = profile_modules("minimal")
        ui.bot_settings["modules"]["notifications"] = False
        ui.configure_module_runtime(fresh_install=False)
        store = ReviewReceiptStore(Path(self.temp.name) / "state.sqlite3",
                                   Path(self.temp.name) / "missing-stats.json")
        store.initialize()
        review = SimpleNamespace(text="Synthetic review", order_id="ABC12345",
                                 author_id=3, stars=5)
        order = SimpleNamespace(id="ABC12345", seller_id=2, buyer_id=3,
                                buyer_username="Synthetic buyer", review=review)
        client = SimpleNamespace(get_order_snapshot=lambda _: (order, 2),
                                 _review_notification_lock=asyncio.Lock(),
                                 _review_state_failed=False, review_state=store,
                                 _notified_reviews={})
        source = Path("src/funpayflow/main.py").read_text(encoding="utf-8")
        names = {"_review_state_operation", "_review_fingerprint",
                 "_safe_review_event_part", "_fetch_and_send_review"}
        nodes = [node for node in ast.parse(source).body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in names]
        namespace = {"asyncio": asyncio, "hashlib": __import__("hashlib"),
                     "json": json, "re": __import__("re"), "Bot": object,
                     "FunPayClient": object, "StateError": StateError,
                     "module_enabled": ui.module_enabled,
                     "logger": SimpleNamespace(warning=lambda *args: None),
                     "html_preview": lambda value, limit: str(value)[:limit]}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), namespace)
        async def send_message(*args, **kwargs):
            self.fail("Event notification was sent")
        await namespace["_fetch_and_send_review"](
            SimpleNamespace(send_message=send_message), client, "ABC12345")
        with closing(sqlite3.connect(store.path)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM review_observations WHERE order_id = 'ABC12345'"
            ).fetchone()[0], 1)


class WorkerGateTests(ModuleStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_supervisor_profiles_create_only_enabled_workers_and_shutdown(self):
        for profile, expected in (("minimal", {"notifications", "session_refresh"}),
                                  ("seller", {"notifications", "session_refresh", "withdrawals"}),
                                  ("all", {"notifications", "session_refresh", "withdrawals", "auto_bump"})):
            with self.subTest(profile=profile):
                ui.bot_settings["modules"] = profile_modules(profile)
                ui.configure_module_runtime(fresh_install=False)
                started = []
                async def worker(name, *args):
                    started.append((name, args))
                    await asyncio.Event().wait()
                async def polling(*args, **kwargs):
                    await asyncio.sleep(0.01)
                async def stop_polling():
                    pass
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
                    "notifications_loop": lambda *args: worker("notifications", *args),
                    "session_refresh_loop": lambda *args: worker("session_refresh", *args),
                    "auto_bump_loop": lambda *args: worker("auto_bump", *args),
                    "withdrawals_poll_loop": lambda *args: worker("withdrawals", *args),
                    "module_enabled": ui.module_enabled,
                    "wait_for_restart": lambda: asyncio.Event().wait(),
                    "restart_requested": lambda: False,
                    "RuntimeAction": SimpleNamespace(NONE="none", RESTART="restart"),
                    "set_telegram_polling_state": lambda *args: None,
                    "_set_problem": lambda *args: None,
                    "logger": SimpleNamespace(error=lambda *args: None),
                }
                supervise = main_function("_supervise_tasks", namespace)
                await supervise(object(), client)
                self.assertEqual({name for name, _ in started}, expected)
                self.assertEqual(len(started), len(expected))
                self.assertTrue(all(client in args for _, args in started))
                self.assertEqual(client.stop_count, 1)
                self.assertEqual(client.join_count, 1)

    async def test_disabled_review_request_keeps_pending_and_night_send_is_blocked(self):
        ui.bot_settings["modules"] = profile_modules("minimal")
        ui.configure_module_runtime(fresh_install=False)
        request_namespace = {"FunPayClient": object, "asyncio": asyncio,
                             "module_enabled": ui.module_enabled}
        request = main_function("_send_scheduled_review_request", request_namespace)
        store = SimpleNamespace(discard_pending_review_request=lambda *args: self.fail("deleted"))
        client = SimpleNamespace(review_state=store,
                                 send_review_request_once=lambda *args: self.fail("sent"))
        await request(client, "ABC12345", "Buyer", 0)
        night_namespace = {"FunPayClient": object, "ActionEvent": ActionEvent,
                           "ActionKind": ActionKind, "asyncio": asyncio,
                           "module_enabled": ui.module_enabled,
                           "is_night_mode_enabled": lambda: True,
                           "is_safe_mode_enabled": lambda: False,
                           "restart_requested": lambda: False}
        night = main_function("_send_night_mode_reply", night_namespace)
        await night(SimpleNamespace(account=SimpleNamespace(
            send_message=lambda *args: self.fail("sent"))),
            ActionEvent(ActionKind.NIGHT_MESSAGE, chat_id=1))

    async def test_notifications_off_keeps_core_runner_consumer(self):
        ui.bot_settings["modules"] = profile_modules("minimal")
        ui.bot_settings["modules"]["notifications"] = False
        ui.configure_module_runtime(fresh_install=False)
        queue_ = queue.Queue()
        queue_.put(object())
        calls, sent = [], []
        state = SimpleNamespace(recover_critical_events=lambda: None,
                                pending_review_requests=lambda: [])
        client = SimpleNamespace(event_queue=queue_, review_state=state,
                                 enqueue_pending_critical_events=lambda: 0,
                                 start_runner=lambda: calls.append("runner"),
                                 runner_failed=lambda: False,
                                 describe_event=lambda event: [ActionEvent(
                                     ActionKind.NOTIFY_MESSAGE, text="synthetic")])
        checks = 0
        def stop_requested():
            nonlocal checks
            checks += 1
            return checks > 2
        client.runner_stop_requested = stop_requested
        async def send_message(*args, **kwargs):
            sent.append(args)
        namespace = {"asyncio": asyncio, "queue": queue, "Bot": object,
                     "FunPayClient": object, "QueuedCriticalEvent": type("QueuedCriticalEvent", (), {}),
                     "ReviewCheckEvent": type("ReviewCheckEvent", (), {}),
                     "ActionKind": ActionKind, "module_enabled": ui.module_enabled,
                     "_order_observation": lambda event: None,
                     "get_all_recipients": lambda: [100],
                     "get_user_settings": lambda uid: {"notifications_enabled": True,
                                                       "notify_message": True},
                     "_strip_html": lambda value: value,
                     "_set_problem": lambda *args: None,
                     "_clear_problem": lambda *args: None,
                     "logger": SimpleNamespace(error=lambda *args: None,
                                               notify=lambda *args: None),
                     "StateError": RuntimeError}
        consumer = main_function("notifications_loop", namespace)
        await asyncio.wait_for(consumer(SimpleNamespace(send_message=send_message), client), 3)
        self.assertEqual(calls, ["runner"])
        self.assertEqual(sent, [])
