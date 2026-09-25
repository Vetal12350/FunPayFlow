"""Startup autobump policy checks without importing or running main.py."""
import log_isolation

import ast
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import telegram as ui


class AutobumpStartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = ui.SETTINGS_FILE
        self.old_settings = copy.deepcopy(ui.bot_settings)
        ui.SETTINGS_FILE = str(Path(self.tmp.name) / "settings.json")
        ui.bot_settings.clear()
        ui.bot_settings.update(copy.deepcopy(ui._DEFAULT_GLOBAL_SETTINGS))

    def tearDown(self):
        ui.SETTINGS_FILE = self.old_path
        ui.bot_settings.clear()
        ui.bot_settings.update(self.old_settings)
        self.tmp.cleanup()

    def test_on_persisted_ui_and_restart(self):
        ui.enable_autobump_on_startup()
        self.assertTrue(ui.bot_settings["auto_bump"])
        self.assertTrue(json.loads(Path(ui.SETTINGS_FILE).read_text(encoding="utf-8"))["auto_bump"])
        self.assertIn("Автоподнятие лотов: ✅", str(ui.get_main_keyboard(1)))
        ui.bot_settings["auto_bump"] = False
        ui.load_settings()
        ui.enable_autobump_on_startup()
        self.assertTrue(ui.bot_settings["auto_bump"])

    def test_save_failure_restores_ram_and_no_false_persistence(self):
        with patch.object(ui, "save_settings", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                ui.enable_autobump_on_startup()
        self.assertFalse(ui.bot_settings["auto_bump"])
        self.assertFalse(Path(ui.SETTINGS_FILE).exists())

    def test_readiness_order_and_single_worker(self):
        source = Path("main.py").read_text(encoding="utf-8")
        functions = {node.name: node for node in ast.parse(source).body
                     if isinstance(node, ast.AsyncFunctionDef)}
        startup = ast.unparse(functions["main"])
        self.assertLess(startup.index("client.initialize_account"),
                        startup.index("client.review_state.initialize"))
        self.assertLess(startup.index("client.review_state.initialize"),
                        startup.index("enable_autobump_on_startup()"))
        self.assertLess(startup.index("enable_autobump_on_startup()"),
                        startup.index("_supervise_tasks(bot, client)"))
        self.assertEqual(startup.count("disable_autobump()"), 0)
        supervisor = ast.unparse(functions["_supervise_tasks"])
        self.assertEqual(supervisor.count("asyncio.create_task(auto_bump_loop(bot, client)"), 1)
        loop = ast.unparse(functions["auto_bump_loop"])
        self.assertIn("last_state = bot_settings.get('auto_bump', False)", loop)


if __name__ == "__main__":
    unittest.main()
