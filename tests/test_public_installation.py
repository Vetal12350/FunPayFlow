"""Offline installation and first-run checks with synthetic credentials only."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dotenv import dotenv_values

import render_service
import runtime_paths
import setup_config


ROOT = Path(__file__).resolve().parents[1]


class PrivatePathTests(unittest.TestCase):
    def test_legacy_and_explicit_directory_with_spaces(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(runtime_paths.data_dir(), ROOT)
            self.assertEqual(runtime_paths.runtime_file("state.sqlite3"), ROOT / "state.sqlite3")
        with tempfile.TemporaryDirectory(prefix="FunPay data path ") as folder:
            with patch.dict(os.environ, {"FUNPAY_BOT_DATA_DIR": folder}):
                base = Path(folder).resolve()
                self.assertEqual(runtime_paths.runtime_file(".env"), base / ".env")
                self.assertEqual(runtime_paths.runtime_file("bot_settings.json"),
                                 base / "bot_settings.json")
                self.assertEqual(runtime_paths.runtime_file("bot.lock"), base / "bot.lock")
                self.assertEqual(runtime_paths.logs_dir(), base / "logs")
                self.assertEqual(runtime_paths.imports_dir(), base / "imports")
            with patch.dict(os.environ, {"FUNPAY_BOT_DATA_DIR": "relative"}):
                with self.assertRaises(ValueError):
                    runtime_paths.data_dir()

    def test_explicit_directory_never_moves_legacy_files(self):
        with tempfile.TemporaryDirectory(prefix="FunPay migration test ") as folder:
            code, private = Path(folder) / "old code", Path(folder) / "new private"
            code.mkdir()
            private.mkdir()
            old_settings = code / "bot_settings.json"
            old_settings.write_bytes(b"synthetic legacy settings")
            with patch.object(runtime_paths, "CODE_DIR", code):
                with patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(runtime_paths.runtime_file("bot_settings.json"), old_settings)
                with patch.dict(os.environ, {"FUNPAY_BOT_DATA_DIR": str(private)}):
                    self.assertEqual(runtime_paths.runtime_file("bot_settings.json"),
                                     private / "bot_settings.json")
                    self.assertFalse((private / "bot_settings.json").exists())
            self.assertEqual(old_settings.read_bytes(), b"synthetic legacy settings")


class ConfigurationTests(unittest.TestCase):
    def test_atomic_config_and_rerun_preservation(self):
        with tempfile.TemporaryDirectory(prefix="FunPay config test ") as folder:
            prompts = iter(["123", "456", "1"])
            secrets = iter(["fake-golden", "123456789:FAKE", ""])
            output = []
            created = setup_config.configure(Path(folder), input_fn=lambda _: next(prompts),
                secret_fn=lambda _: next(secrets), output_fn=output.append)
            self.assertTrue(created)
            path = Path(folder) / ".env"
            first = path.read_bytes()
            self.assertEqual(set(dotenv_values(path)), set(setup_config.FIELDS))
            self.assertEqual(dotenv_values(path)["ADMIN_ID"], "123")
            self.assertEqual(dotenv_values(path)["FUNPAY_USER_ID"], "456")
            self.assertNotIn("fake-golden", "".join(output))
            self.assertNotIn("123456789:FAKE", "".join(output))
            self.assertFalse(setup_config.configure(Path(folder), input_fn=lambda _: "",
                secret_fn=lambda _: self.fail("Secrets should not be requested on rerun"),
                output_fn=output.append))
            self.assertEqual(path.read_bytes(), first)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_input_never_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("synthetic-original\n", encoding="utf-8")
            prompts = iter(["y", "not-an-id"])
            with self.assertRaises(ValueError):
                setup_config.configure(Path(folder),
                    input_fn=lambda _: next(prompts),
                    secret_fn=lambda _: "synthetic", output_fn=lambda _: None)
            self.assertEqual(path.read_text(encoding="utf-8"), "synthetic-original\n")
        for value in ("0", "-1", "1.2", "abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                setup_config._validate("ADMIN_ID", value)

    def test_atomic_write_failure_preserves_previous_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_bytes(b"synthetic-existing")
            with patch.object(setup_config.os, "replace", side_effect=OSError("synthetic")):
                with self.assertRaises(OSError):
                    setup_config._atomic_text(path, "replacement")
            self.assertEqual(path.read_bytes(), b"synthetic-existing")


class IsolatedFirstRunTests(unittest.TestCase):
    def _run_script(self, source: str, folder: Path) -> dict:
        environment = dict(os.environ)
        for key in setup_config.FIELDS:
            environment.pop(key, None)
        environment["FUNPAY_BOT_DATA_DIR"] = str(folder)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, "-I", "-B", "-c", source],
            cwd=folder, env=environment, capture_output=True, text=True, check=False)
        if result.returncode:
            self.fail(f"Isolated first run failed: {result.stderr[-1000:]}")
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_fresh_install_restart_and_private_file_locations(self):
        with tempfile.TemporaryDirectory(prefix="FunPay fresh install ") as folder:
            data = Path(folder).resolve()
            prompts = iter(["111", "222", "0"])
            secret = iter(["fake-golden", "123456789:FAKE", ""])
            setup_config.configure(data, input_fn=lambda _: next(prompts),
                secret_fn=lambda _: next(secret), output_fn=lambda _: None)
            prefix = f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
            first = self._run_script(prefix + """
import json
from pathlib import Path
import main, telegram as ui, logger, state
from feature_registry import all_features, is_fresh_install
from runtime_paths import imports_dir, logs_dir, runtime_file
fresh = is_fresh_install(Path(ui.SETTINGS_FILE), state.DEFAULT_DB_PATH,
                         runtime_file('stats_log.json'))
ui.configure_module_runtime(fresh_install=fresh)
all_on = len(ui.bot_settings['modules']) == 10 and all(ui.bot_settings['modules'].values())
inactive = not any(ui.effective_modules().values())
ui.save_settings(required=True)
state.ReviewReceiptStore().initialize()
main.acquire_lock()
lock = Path(main.LOCK_FILE).exists()
main.release_lock()
logger.info('synthetic first-run location check')
imports_dir().mkdir(parents=True, exist_ok=True)
print(json.dumps({'fresh': fresh, 'all_on': all_on, 'inactive': inactive,
 'complete': ui.bot_settings['setup_completed'], 'currency': ui.bot_settings['primary_currency'],
 'lock': lock, 'settings': str(ui.SETTINGS_FILE), 'db': str(state.DEFAULT_DB_PATH),
 'logs': str(logs_dir()), 'imports': str(imports_dir())}))
""", data)
            self.assertTrue(first["fresh"])
            self.assertTrue(first["all_on"])
            self.assertTrue(first["inactive"])
            self.assertFalse(first["complete"])
            self.assertIsNone(first["currency"])
            self.assertTrue(first["lock"])
            self.assertEqual(first["settings"], str(data / "bot_settings.json"))
            self.assertEqual(first["db"], str(data / "state.sqlite3"))
            self.assertEqual(first["logs"], str(data / "logs"))
            self.assertEqual(first["imports"], str(data / "imports"))
            self.assertTrue(list((data / "logs").glob("bot_*.log")))
            second = self._run_script(prefix + """
import json
from pathlib import Path
import telegram as ui, state
from feature_registry import is_fresh_install
from runtime_paths import runtime_file
fresh = is_fresh_install(Path(ui.SETTINGS_FILE), state.DEFAULT_DB_PATH,
                         runtime_file('stats_log.json'))
ui.configure_module_runtime(fresh_install=fresh)
pending = not ui.bot_settings['setup_completed']
ui.bot_settings['setup_completed'] = True
ui.save_settings(required=True)
print(json.dumps({'fresh': fresh, 'pending': pending}))
""", data)
            self.assertEqual(second, {"fresh": False, "pending": True})
            third = self._run_script(prefix + """
import json
from pathlib import Path
import telegram as ui, state
from feature_registry import is_fresh_install
from runtime_paths import runtime_file
fresh = is_fresh_install(Path(ui.SETTINGS_FILE), state.DEFAULT_DB_PATH,
                         runtime_file('stats_log.json'))
ui.configure_module_runtime(fresh_install=fresh)
print(json.dumps({'fresh': fresh, 'complete': ui.bot_settings['setup_completed'],
 'all_on': all(ui.effective_modules().values()),
 'currency': ui.bot_settings['primary_currency']}))
""", data)
            self.assertEqual(third, {"fresh": False, "complete": True,
                                     "all_on": True, "currency": None})


class InstallerStaticTests(unittest.TestCase):
    def test_windows_scripts_and_service_rendering(self):
        setup = (ROOT / "Setup.bat").read_text(encoding="utf-8")
        start = (ROOT / "Start.bat").read_text(encoding="utf-8")
        resolver = (ROOT / "ResolveDataDir.bat").read_text(encoding="utf-8")
        self.assertIn('pushd "%~dp0"', setup)
        self.assertIn('pushd "%~dp0"', start)
        self.assertIn('call "%~dp0ResolveDataDir.bat"', setup)
        self.assertIn('call "%~dp0ResolveDataDir.bat"', start)
        self.assertIn('%LOCALAPPDATA%\\FunPaySellerBot', resolver)
        self.assertIn('%USERPROFILE%\\AppData\\Local\\FunPaySellerBot', resolver)
        self.assertIn('if defined FUNPAY_BOT_DATA_DIR goto :resolved', resolver)
        self.assertIn('Version.Major -ge 10', setup)
        self.assertIn("--locked --no-dev --python 3.13", setup)
        self.assertIn('--data-dir "%FUNPAY_BOT_DATA_DIR%"', setup)
        self.assertIn('path.parent.mkdir(parents=True, exist_ok=True)',
                      (ROOT / "setup_config.py").read_text(encoding="utf-8"))
        self.assertIn('if not exist "%FUNPAY_BOT_DATA_DIR%\\.env"', start)
        self.assertIn('".venv\\Scripts\\python.exe" main.py', start)
        self.assertNotIn(".install-data-dir", setup + start)
        self.assertNotIn("FUNPAY_GOLDEN_KEY", setup + start + resolver)
        with tempfile.TemporaryDirectory(prefix="FunPay service test ") as folder:
            code = Path(folder) / "code with spaces"
            data = Path(folder) / "data with spaces"
            rendered = render_service.render(code, data, "seller")
            self.assertIn('WorkingDirectory=' + render_service._unit_word(str(code)), rendered)
            self.assertIn('EnvironmentFile=' + render_service._unit_word(str(data / ".env")),
                          rendered)
            self.assertIn('Environment=' + render_service._unit_word(
                "FUNPAY_BOT_DATA_DIR=" + str(data)), rendered)
            self.assertIn("Restart=on-failure", rendered)
            self.assertNotIn("@CODE_DIR@", rendered)
            self.assertNotIn("fake-golden", rendered)
            dollar_code = Path(folder) / "code$literal"
            dollar_unit = render_service.render(dollar_code, data, "seller")
            self.assertIn("ExecStart=" + render_service._exec_word(
                str(dollar_code / ".venv" / "bin" / "python")), dollar_unit)

    @unittest.skipUnless(os.name == "nt", "Windows cmd.exe is required")
    def test_windows_release_folders_share_private_data(self):
        with tempfile.TemporaryDirectory(prefix="FunPay windows releases ") as folder:
            root = Path(folder)
            first_release = root / "release one with spaces"
            next_release = root / "release two with spaces"
            first_release.mkdir()
            next_release.mkdir()
            for release in (first_release, next_release):
                shutil.copyfile(ROOT / "ResolveDataDir.bat", release / "ResolveDataDir.bat")
            environment = dict(os.environ)
            for key in (*setup_config.FIELDS, "FUNPAY_BOT_DATA_DIR"):
                environment.pop(key, None)
            environment["LOCALAPPDATA"] = str(root / "Synthetic User" / "App Data")
            environment["USERPROFILE"] = str(root / "Synthetic User")
            environment["FUNPAY_GOLDEN_KEY"] = "SYNTHETIC_SECRET_NEVER_PRINT"

            def resolve(release: Path, env: dict[str, str]) -> Path:
                result = subprocess.run(
                    ["cmd.exe", "/d", "/c", str(release / "ResolveDataDir.bat"), "--print"],
                    cwd=release, env=env, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("SYNTHETIC_SECRET_NEVER_PRINT", result.stdout + result.stderr)
                self.assertTrue(result.stdout.startswith("FUNPAY_BOT_DATA_DIR="))
                return Path(result.stdout.strip().split("=", 1)[1])

            data = resolve(first_release, environment)
            self.assertEqual(data, Path(environment["LOCALAPPDATA"]) / "FunPaySellerBot")
            self.assertNotIn(first_release, data.parents)
            self.assertEqual(resolve(next_release, environment), data)

            prompts = iter(["111", "222", "0"])
            secrets = iter(["synthetic-key", "synthetic-token", ""])
            self.assertTrue(setup_config.configure(data, input_fn=lambda _: next(prompts),
                secret_fn=lambda _: next(secrets), output_fn=lambda _: None))
            config = (data / ".env").read_bytes()
            database = data / "state.sqlite3"
            database.write_bytes(b"synthetic database fixture")
            self.assertFalse(setup_config.configure(data, input_fn=lambda _: "",
                secret_fn=lambda _: self.fail("Existing secrets must not be requested"),
                output_fn=lambda _: None))
            self.assertEqual((data / ".env").read_bytes(), config)
            self.assertEqual(database.read_bytes(), b"synthetic database fixture")

            overridden = dict(environment, FUNPAY_BOT_DATA_DIR=str(root / "custom private"))
            self.assertEqual(resolve(next_release, overridden), root / "custom private")
            fallback = dict(environment)
            fallback.pop("LOCALAPPDATA")
            self.assertEqual(resolve(next_release, fallback),
                Path(environment["USERPROFILE"]) / "AppData" / "Local" / "FunPaySellerBot")

    def test_linux_syntax_and_permission_strategy(self):
        script = ROOT / "install.sh"
        source = script.read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", source)
        self.assertIn("umask 077", source)
        self.assertIn('chmod 700 -- "$data_dir"', source)
        self.assertIn('chmod 600 -- "$data_dir/.env"', source)
        self.assertIn('"$code_dir/setup_config.py" --data-dir "$data_dir"', source)
        self.assertNotIn("eval ", source)
        if shutil.which("bash"):
            result = subprocess.run(["bash", "-n", str(script)], capture_output=True,
                                    text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
