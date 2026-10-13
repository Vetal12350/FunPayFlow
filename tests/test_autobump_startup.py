"""Startup autobump policy checks without importing or running main.py."""
import log_isolation

import ast
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from funpayflow import telegram as ui
from funpayflow.state import ReviewReceiptStore


class AutobumpStartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = ui.SETTINGS_FILE
        self.old_settings = copy.deepcopy(ui.bot_settings)
        self.old_effective = ui._effective_modules
        self.old_client = ui._runtime_client
        store = ReviewReceiptStore(Path(self.tmp.name) / 'state.sqlite3')
        store.initialize()
        ui._runtime_client = SimpleNamespace(review_state=store)
        ui.SETTINGS_FILE = str(Path(self.tmp.name) / "settings.json")
        ui.bot_settings.clear()
        ui.bot_settings.update(copy.deepcopy(ui._DEFAULT_GLOBAL_SETTINGS))
        ui.configure_module_runtime(fresh_install=False)

    def tearDown(self):
        ui.SETTINGS_FILE = self.old_path
        ui.bot_settings.clear()
        ui.bot_settings.update(self.old_settings)
        ui._effective_modules = self.old_effective
        ui._runtime_client = self.old_client
        self.tmp.cleanup()

    def test_on_persisted_ui_and_restart(self):
        ui.save_settings(required=True)
        self.assertTrue(ui.bot_settings["auto_bump"])
        self.assertTrue(json.loads(Path(ui.SETTINGS_FILE).read_text(encoding="utf-8"))["auto_bump"])
        self.assertIn("Автоподнятие лотов: ✅", str(ui.get_main_keyboard(1)))
        ui.bot_settings["auto_bump"] = False
        ui.load_settings()
        self.assertTrue(ui.bot_settings["auto_bump"])

    def test_manual_off_survives_restart(self):
        ui.toggle_autobump_saved()
        self.assertFalse(ui.bot_settings["auto_bump"])
        ui.bot_settings["auto_bump"] = True
        ui.load_settings()
        self.assertFalse(ui.bot_settings["auto_bump"])
        self.assertIn("Автоподнятие лотов: ⛔", str(ui.get_main_keyboard(1)))

    def test_save_failure_restores_both_toggle_directions(self):
        for previous in (True, False):
            with self.subTest(previous=previous):
                ui.bot_settings['auto_bump'] = previous
                ui.save_settings(required=True)
                with patch.object(ui, "save_settings", side_effect=RuntimeError("offline")):
                    with self.assertRaises(RuntimeError):
                        ui.toggle_autobump_saved()
                self.assertEqual(ui.bot_settings['auto_bump'], previous)
                self.assertEqual(json.loads(Path(ui.SETTINGS_FILE).read_text(
                    encoding='utf-8'))['auto_bump'], previous)

    def test_critical_notice_dedup_survives_restart_without_changing_autobump(self):
        ui.bot_settings['auto_bump'] = False
        with patch('funpayflow.telegram.time.time', return_value=10000):
            self.assertTrue(ui.claim_critical_notice('AUTH'))
            self.assertFalse(ui.claim_critical_notice('AUTH'))
            ui.bot_settings['critical_notice'] = None
            ui.load_settings()
            self.assertFalse(ui.claim_critical_notice('AUTH'))
            self.assertTrue(ui.claim_critical_notice('STATE'))
        self.assertFalse(ui.bot_settings['auto_bump'])
        with patch('funpayflow.telegram.time.time', return_value=11800):
            self.assertTrue(ui.claim_critical_notice('STATE'))
        self.assertEqual(set(ui.bot_settings['critical_notice']), {'code', 'at'})

    def test_critical_notice_save_failure_remains_best_effort(self):
        with patch.object(ui, 'save_settings', side_effect=RuntimeError('synthetic')):
            self.assertTrue(ui.claim_critical_notice('STATE'))
        self.assertIsNone(ui.bot_settings['critical_notice'])
        self.assertTrue(ui.bot_settings['auto_bump'])

    def test_readiness_order_and_single_worker(self):
        source = Path("src/funpayflow/main.py").read_text(encoding="utf-8")
        functions = {node.name: node for node in ast.parse(source).body
                     if isinstance(node, ast.AsyncFunctionDef)}
        startup = ast.unparse(functions["main"])
        self.assertLess(startup.index("client.initialize_account"),
                        startup.index("client.review_state.initialize"))
        self.assertLess(startup.index("client.review_state.initialize"),
                        startup.index("client.review_state.recover_autobump_progress"))
        self.assertLess(startup.index("client.review_state.recover_autobump_progress"),
                        startup.index("_supervise_tasks(bot, client)"))
        self.assertNotIn("enable_autobump_on_startup", startup)
        self.assertEqual(startup.count("disable_autobump()"), 0)
        supervisor = ast.unparse(functions["_supervise_tasks"])
        self.assertEqual(supervisor.count("asyncio.create_task(auto_bump_loop(bot, client)"), 1)
        loop = ast.unparse(functions["auto_bump_loop"])
        self.assertIn("get_autobump_state()", loop)


if __name__ == "__main__":
    unittest.main()
