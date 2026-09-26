"""Offline installation and first-run checks with synthetic credentials only."""

import json
import io
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from dotenv import dotenv_values

import render_service
import runtime_paths
import setup_config
import console_ui


ROOT = Path(__file__).resolve().parents[1]


def _without_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


class PrivatePathTests(unittest.TestCase):
    def test_legacy_and_explicit_directory_with_spaces(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(os.path.samefile(runtime_paths.data_dir(), ROOT))
            self.assertTrue(os.path.samefile(
                runtime_paths.runtime_file("state.sqlite3").parent, ROOT))
        with tempfile.TemporaryDirectory(prefix="FunPay data path ") as folder:
            with patch.dict(os.environ, {"FUNPAY_BOT_DATA_DIR": folder}):
                base = Path(folder).resolve()
                for target in (runtime_paths.runtime_file(".env"),
                               runtime_paths.runtime_file("bot_settings.json"),
                               runtime_paths.runtime_file("bot.lock"),
                               runtime_paths.logs_dir(), runtime_paths.imports_dir()):
                    self.assertTrue(os.path.samefile(target.parent, base))
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
                    self.assertTrue(os.path.samefile(
                        runtime_paths.runtime_file("bot_settings.json"), old_settings))
                with patch.dict(os.environ, {"FUNPAY_BOT_DATA_DIR": str(private)}):
                    self.assertTrue(os.path.samefile(
                        runtime_paths.runtime_file("bot_settings.json").parent, private))
                    self.assertFalse((private / "bot_settings.json").exists())
            self.assertEqual(old_settings.read_bytes(), b"synthetic legacy settings")


class ConfigurationTests(unittest.TestCase):
    def test_ansi_normalization_preserves_visible_unicode(self):
        styled = "\x1b[96mCtrl+V\x1b[0m поддерживается · \x1b[1;32mзначение скрыто\x1b[0m ✓ ●"
        self.assertEqual(_without_ansi(styled),
                         "Ctrl+V поддерживается · значение скрыто ✓ ●")

    def test_rich_presentation_and_narrow_plain_fallback(self):
        for language, heading, error_text in (
            ("ru", "ШАГ 1 ИЗ 4", "Значение не может быть пустым."),
            ("en", "STEP 1 OF 4", "The value is empty."),
        ):
            with self.subTest(language=language):
                with patch.dict(os.environ, {"TERM": "xterm-256color"}):
                    os.environ.pop("NO_COLOR", None)
                    captured = io.StringIO()
                    ui = console_ui.InstallerConsole(language, stream=captured, width=72,
                                                     force_rich=True)
                    ui.banner("1.0.0")
                    ui.step(1, "Authorization")
                    ui.write(f"[ERROR] {error_text}")
                    ui.read_secret("Golden Key (hidden): ", lambda _, changed: "SYNTHETIC_SECRET")
                    result = ui.run_saving(lambda: "saved")
                self.assertEqual(result, "saved")
                raw_screen = captured.getvalue()
                screen = _without_ansi(raw_screen)
                self.assertIn("FUNPAYFLOW", screen)
                self.assertIn("v1.0.0", screen)
                self.assertIn(console_ui._COPY[language]["tagline"], screen)
                self.assertIn(console_ui._COPY[language]["seller_line"], screen)
                self.assertIn(heading, screen)
                self.assertIn("25%", screen)
                self.assertIn(error_text, screen)
                self.assertNotIn("SYNTHETIC_SECRET", raw_screen)
                self.assertTrue(ui.rich)
                self.assertEqual(console_ui._secret_mask(True), "●" * 12)
                self.assertEqual(console_ui._secret_mask(False), "")
                narrow = io.StringIO()
                fallback = console_ui.InstallerConsole(language, stream=narrow,
                                                       width=28, force_rich=True)
                fallback.banner("1.0.0")
                fallback.step(1, "Authorization")
                fallback.write(f"[ERROR] {error_text}")
                self.assertIn("FunPayFlow v1.0.0", narrow.getvalue())
                self.assertIn(error_text, narrow.getvalue())
                self.assertNotIn("\x1b[", narrow.getvalue())
                seen = []
                self.assertEqual(fallback.read_secret("Optional: ",
                    lambda prompt, changed: (seen.append((prompt, changed)) or "")), "")
                self.assertEqual(seen, [("Optional: ", None)])
                with patch.dict(os.environ, {"NO_COLOR": "1"}):
                    no_color = console_ui.InstallerConsole(language, stream=io.StringIO(),
                                                           width=72, force_rich=True)
                    self.assertFalse(no_color.rich)
                    self.assertEqual(no_color.read_secret("Optional: ",
                        lambda prompt, changed: ""), "")

    def test_secret_field_renderables_show_help_and_fixed_mask(self):
        for language, paste_hint, hidden_hint in (
            ("ru", "Ctrl+V поддерживается", "значение скрыто"),
            ("en", "Ctrl+V supported", "value hidden"),
        ):
            with self.subTest(language=language), patch.dict(
                    os.environ, {"TERM": "xterm-256color"}):
                os.environ.pop("NO_COLOR", None)
                output = io.StringIO()
                ui = console_ui.InstallerConsole(language, stream=output, width=72,
                                                 force_rich=True)
                self.assertTrue(ui.rich)
                ui.console.print(ui._secret_title_renderable("Golden Key: "))
                ui.console.print(ui._secret_field_renderable(True))
                ui.console.print(ui._secret_help_renderable())
                raw = output.getvalue()
                visible = _without_ansi(raw)
                for expected in ("Golden Key", "›", paste_hint, hidden_hint,
                                 console_ui._COPY[language]["hidden"], "●" * 12):
                    self.assertIn(expected, visible)
                self.assertEqual(visible.count("●"), 12)
                self.assertNotIn("SYNTHETIC_SECRET", raw)

                empty_output = io.StringIO()
                empty_ui = console_ui.InstallerConsole(language, stream=empty_output,
                                                       width=72, force_rich=True)
                empty_ui.console.print(empty_ui._secret_field_renderable(False))
                empty_visible = _without_ansi(empty_output.getvalue())
                self.assertIn("›", empty_visible)
                self.assertNotIn("●", empty_visible)

    def test_rich_landing_has_bilingual_selector_and_welcome(self):
        for language, welcome, subtitle, selected in (
            ("ru", "ДОБРО ПОЖАЛОВАТЬ", "Автоматизация • Аналитика • Управление",
             "[1] Русский  ✓"),
            ("en", "WELCOME", "Automation • Analytics • Control",
             "[2] English  ✓"),
        ):
            with self.subTest(language=language), patch.dict(
                    os.environ, {"TERM": "xterm-256color"}):
                os.environ.pop("NO_COLOR", None)
                screen = io.StringIO()
                ui = console_ui.InstallerConsole(language, stream=screen,
                                                 width=72, force_rich=True)
                ui.landing()
                visible = _without_ansi(screen.getvalue())
                for expected in ("FUNPAYFLOW", "v1.0.0", welcome,
                                 subtitle, "[1] Русский", "[2] English", selected):
                    self.assertIn(expected, visible)
                self.assertIn(console_ui._COPY[language]["seller_line"], visible)
                self.assertTrue(ui.rich)

    def test_live_secret_field_tracks_typing_paste_backspace_and_blank(self):
        for language in ("ru", "en"):
            with self.subTest(language=language):
                with patch.dict(os.environ, {"TERM": "xterm-256color"}):
                    os.environ.pop("NO_COLOR", None)
                    screen = io.StringIO()
                    ui = console_ui.InstallerConsole(language, stream=screen,
                                                     width=72, force_rich=True)
                    keys = iter(("a", "\b", "\x16", "\r"))
                    changes = []

                    def read_secret(prompt, changed):
                        def record_change(has_value):
                            changes.append(has_value)
                            if changed is not None:
                                changed(has_value)

                        return setup_config._windows_secret_input(
                            prompt, language=language, getch=lambda: next(keys),
                            clipboard=lambda: "SYNTHETIC_PASTED_SECRET",
                            stream=io.StringIO(), on_change=record_change)

                    value = ui.read_secret("Golden Key: ", read_secret)
                    self.assertEqual(value, "SYNTHETIC_PASTED_SECRET")
                    self.assertEqual(changes, [True, False, True])
                    self.assertNotIn(value, screen.getvalue())
                    self.assertEqual(console_ui._secret_mask(True), "●" * 12)
                    self.assertEqual(console_ui._secret_mask(False), "")

                    blank_keys = iter(("\r",))
                    blank = ui.read_secret("Optional: ", lambda prompt, changed:
                        setup_config._windows_secret_input(
                            prompt, language=language, getch=lambda: next(blank_keys),
                            stream=io.StringIO(), on_change=changed))
                    self.assertEqual(blank, "")
                    self.assertNotIn("SYNTHETIC_PASTED_SECRET", screen.getvalue())

                    cancel_keys = iter(("\x03",))
                    with self.assertRaises(KeyboardInterrupt):
                        ui.read_secret("Cancel: ", lambda prompt, changed:
                            setup_config._windows_secret_input(
                                prompt, language=language,
                                getch=lambda: next(cancel_keys),
                                stream=io.StringIO(), on_change=changed))

    def test_rich_success_and_start_states_are_local_only(self):
        with patch.dict(os.environ, {"TERM": "xterm-256color"}):
            os.environ.pop("NO_COLOR", None)
            for language, completed, locked in (
                ("ru", "Настройка завершена", "Другой экземпляр FunPayFlow"),
                ("en", "Setup completed", "Another FunPayFlow"),
            ):
                with self.subTest(language=language):
                    output = io.StringIO()
                    ui = console_ui.InstallerConsole(language, stream=output,
                                                     width=70, force_rich=True)
                    synthetic = Path("synthetic data")
                    ui.finish(synthetic, written=True, dependencies_ready=True)
                    ui.start_ready(synthetic)
                    ui.start_error("lock", show_banner=False)
                    rendered = output.getvalue()
                    self.assertIn(completed, rendered)
                    self.assertIn(locked, rendered)
                    self.assertIn("synthetic data", rendered)
                    self.assertNotIn("connected to FunPay", rendered)
                    self.assertNotIn("Telegram connected", rendered)
                    self.assertTrue(ui.rich)

    def test_russian_default_and_english_field_labels(self):
        for language, heading, help_text, accepted in (
            ("ru", "[1/4] Авторизация FunPay",
             "Ключ авторизации FunPay. Хранится только на этом компьютере.",
             "[OK] Значение принято."),
            ("en", "[1/4] FunPay authorization",
             "FunPay authorization key. It stays only on this computer.",
             "[OK] Value accepted."),
        ):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as folder:
                ordinary = iter(("111", "222", "0"))
                hidden = iter(("SYNTHETIC_PRIVATE_KEY", "SYNTHETIC_PRIVATE_TOKEN", ""))
                output = []
                setup_config.configure(Path(folder), language=language,
                    input_fn=lambda _: next(ordinary), secret_fn=lambda _: next(hidden),
                    output_fn=output.append)
                self.assertIn(heading, output)
                self.assertIn(help_text, output)
                self.assertIn(accepted, output)
                self.assertNotIn("SYNTHETIC_PRIVATE_KEY", "\n".join(output))
                self.assertNotIn("SYNTHETIC_PRIVATE_TOKEN", "\n".join(output))
                self.assertNotIn("ValueError", "\n".join(output))

    def test_localized_validation_and_safe_default_preference(self):
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            screen = io.StringIO()
            ui = console_ui.InstallerConsole("ru", stream=screen, force_rich=True)
            ui.write("[OK] Готово")
            self.assertEqual(screen.getvalue().strip(), "[OK] Готово")
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(setup_config.load_language(Path(folder)), "ru")
            (Path(folder) / setup_config.LANGUAGE_FILE).write_text("invalid\n", encoding="utf-8")
            self.assertEqual(setup_config.load_language(Path(folder)), "ru")
        for language, empty, bad_id, control in (
            ("ru", "Значение не может быть пустым.",
             "Telegram ID должен содержать только положительные цифры.",
             "Вставленное значение содержит недопустимые символы."),
            ("en", "The value is empty.",
             "Telegram ID must contain a positive number of digits only.",
             "Pasted value contains unsupported control characters."),
        ):
            with self.subTest(language=language):
                for field, value, expected in (
                    ("FUNPAY_GOLDEN_KEY", "", empty),
                    ("ADMIN_ID", "not-an-id", bad_id),
                    ("BOT_TOKEN", "SYNTHETIC_PRIVATE_TOKEN\n", control),
                ):
                    with self.assertRaises(ValueError) as caught:
                        setup_config._validate(field, value, language=language)
                    self.assertEqual(str(caught.exception), expected)
                    self.assertNotIn("SYNTHETIC_PRIVATE_TOKEN", str(caught.exception))

    def test_saved_language_reused_without_affecting_existing_env(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_bytes(b"synthetic original config")
            script = [sys.executable, "-B", str(ROOT / "setup_config.py"), "--data-dir", folder]
            first = subprocess.run([*script, "--language", "en"], input="\n",
                capture_output=True, text=True, encoding="utf-8", check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(setup_config.load_language(Path(folder)), "en")
            second = subprocess.run(script, input="\n", capture_output=True,
                text=True, encoding="utf-8", check=False)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("Existing configuration found.", second.stdout)
            self.assertIn("[OK] Setup completed", second.stdout)
            russian_env = dict(os.environ, PYTHONIOENCODING="utf-8")
            russian = subprocess.run([*script, "--language", "ru"], input="\n",
                capture_output=True, text=True, encoding="utf-8", env=russian_env,
                check=False)
            self.assertEqual(russian.returncode, 0, russian.stderr)
            self.assertEqual(setup_config.load_language(Path(folder)), "ru")
            self.assertIn("Найдена существующая конфигурация.", russian.stdout)
            self.assertIn("[1] Оставить текущую", russian.stdout)
            self.assertIn("[2] Изменить", russian.stdout)
            self.assertIn("[3] Отмена", russian.stdout)
            self.assertIn("[OK] Настройка завершена", russian.stdout)
            self.assertEqual(path.read_bytes(), b"synthetic original config")

    def test_atomic_config_and_rerun_preservation(self):
        with tempfile.TemporaryDirectory(prefix="FunPay config test ") as folder:
            prompts = iter(["123", "456", "1"])
            secrets = iter(["fake-golden", "123456789:FAKE", ""])
            output = []
            created = setup_config.configure(Path(folder), language="en", input_fn=lambda _: next(prompts),
                secret_fn=lambda _: next(secrets), output_fn=output.append)
            self.assertTrue(created)
            path = Path(folder) / ".env"
            first = path.read_bytes()
            self.assertEqual(set(dotenv_values(path)), set(setup_config.FIELDS))
            self.assertEqual(dotenv_values(path)["ADMIN_ID"], "123")
            self.assertEqual(dotenv_values(path)["FUNPAY_USER_ID"], "456")
            self.assertNotIn("fake-golden", "".join(output))
            self.assertNotIn("123456789:FAKE", "".join(output))
            self.assertIn("[1/4] FunPay authorization", output)
            self.assertIn("[2/4] Telegram bot", output)
            self.assertIn("[3/4] Owner account", output)
            self.assertIn("[4/4] Save configuration", output)
            self.assertIn("FunPay authorization key. It stays only on this computer.", output)
            self.assertFalse(setup_config.configure(Path(folder), language="en", input_fn=lambda _: "",
                secret_fn=lambda _: self.fail("Secrets should not be requested on rerun"),
                output_fn=output.append))
            self.assertIn("[1] Keep current configuration", output)
            self.assertIn("[2] Edit configuration", output)
            self.assertIn("[3] Cancel", output)
            self.assertEqual(path.read_bytes(), first)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_input_never_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("synthetic-original\n", encoding="utf-8")
            prompts = iter(["2", "not-an-id"])
            output = []
            with self.assertRaises(StopIteration):
                setup_config.configure(Path(folder), language="en",
                    input_fn=lambda _: next(prompts),
                    secret_fn=lambda _: "synthetic", output_fn=output.append)
            self.assertEqual(path.read_text(encoding="utf-8"), "synthetic-original\n")
            self.assertIn("[ERROR] Telegram ID must contain a positive number of digits only.", output)
            self.assertNotIn("synthetic", " ".join(output))
            self.assertNotIn("not-an-id", " ".join(output))
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

    def test_ctrl_v_secret_paste_is_hidden_and_validated(self):
        typed = iter(("\x16", "\r"))
        screen = io.StringIO()
        pasted = setup_config._windows_secret_input("Golden Key (hidden): ",
            getch=lambda: next(typed), clipboard=lambda: "SYNTHETIC_PASTED_SECRET",
            stream=screen)
        self.assertEqual(pasted, "SYNTHETIC_PASTED_SECRET")
        self.assertNotIn(pasted, screen.getvalue())
        self.assertEqual(screen.getvalue(), "Golden Key (hidden): \n")
        with self.assertRaises(ValueError) as error:
            setup_config._validate("FUNPAY_GOLDEN_KEY", pasted + "\r\n")
        self.assertNotIn(pasted, str(error.exception))

    def test_rerun_edit_cancel_and_success_summary(self):
        with tempfile.TemporaryDirectory(prefix="FunPay setup UX ") as folder:
            path = Path(folder) / ".env"
            path.write_bytes(b"synthetic original config")
            with self.assertRaises(setup_config.SetupCancelled):
                setup_config.configure(Path(folder), language="en", input_fn=lambda _: "3",
                    secret_fn=lambda _: self.fail("Secrets must not be requested on cancel"),
                    output_fn=lambda _: None)
            self.assertEqual(path.read_bytes(), b"synthetic original config")
            result = subprocess.run([sys.executable, "-B", str(ROOT / "setup_config.py"),
                "--data-dir", folder, "--language", "en"], input="\n", capture_output=True, text=True,
                encoding="utf-8", check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("FunPayFlow v1.0.0", result.stdout)
            self.assertIn("[OK] Setup completed", result.stdout)
            self.assertIn("Data directory:", result.stdout)
            self.assertIn("Next step: Run Start.bat", result.stdout)
            self.assertEqual((Path(folder) / setup_config.LANGUAGE_FILE).read_text(
                encoding="utf-8"), "en\n")
            self.assertEqual(path.read_bytes(), b"synthetic original config")


class IsolatedFirstRunTests(unittest.TestCase):
    def _run_script(self, source: str, folder: Path) -> dict:
        environment = dict(os.environ)
        for key in setup_config.FIELDS:
            environment.pop(key, None)
        environment["FUNPAY_BOT_DATA_DIR"] = str(folder)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, "-X", "utf8", "-I", "-B", "-c", source],
            cwd=folder, env=environment, capture_output=True, text=True,
            encoding="utf-8", check=False)
        if result.returncode:
            self.fail(f"Isolated first run failed: {result.stderr[-1000:]}")
        return json.loads(result.stdout.strip().splitlines()[-1])

    @unittest.skipUnless(os.name == "nt", "Windows console encoding regression")
    def test_persisted_settings_load_with_legacy_stdout_encoding(self):
        with tempfile.TemporaryDirectory(prefix="FunPay encoding test ") as folder:
            data = Path(folder)
            (data / "bot_settings.json").write_text(
                '{"setup_completed": true}\n', encoding="utf-8")
            environment = dict(os.environ, FUNPAY_BOT_DATA_DIR=folder)
            for key in setup_config.FIELDS:
                environment.pop(key, None)
            environment.pop("PYTHONPATH", None)
            source = ("import sys; sys.stdout.reconfigure(encoding='cp1252'); "
                      f"sys.path.insert(0, {str(ROOT)!r}); import telegram; "
                      "print('settings-import:PASS')")
            result = subprocess.run([sys.executable, "-I", "-B", "-c", source],
                cwd=folder, env=environment, text=True, encoding="utf-8",
                capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr[-1000:])
            self.assertIn("Настройки загружены", result.stdout)
            self.assertIn("settings-import:PASS", result.stdout)

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
            for key, expected in (("settings", data / "bot_settings.json"),
                                  ("db", data / "state.sqlite3"),
                                  ("logs", data / "logs"),
                                  ("imports", data / "imports")):
                self.assertTrue(os.path.samefile(first[key], expected), key)
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
        self.assertIn('call "%RESOLVER%"', setup)
        self.assertIn('call "%RESOLVER%"', start)
        self.assertIn('%~dp0app\\ResolveDataDir.bat', setup)
        self.assertIn('%~dp0app\\ResolveDataDir.bat', start)
        self.assertIn('%LOCALAPPDATA%\\FunPayFlow', resolver)
        self.assertIn('%USERPROFILE%\\AppData\\Local\\FunPayFlow', resolver)
        self.assertIn('if defined FUNPAY_BOT_DATA_DIR goto :resolved', resolver)
        self.assertIn('Version.Major -ge 10', setup)
        self.assertIn("--locked --no-dev --python 3.13", setup)
        self.assertIn('--data-dir "%FUNPAY_BOT_DATA_DIR%"', setup)
        self.assertIn('path.parent.mkdir(parents=True, exist_ok=True)',
                      (ROOT / "setup_config.py").read_text(encoding="utf-8"))
        self.assertIn('if not exist "%FUNPAY_BOT_DATA_DIR%\\.env"', start)
        self.assertIn('".venv\\Scripts\\python.exe" main.py', start)
        self.assertIn('installer_language.txt', start)
        self.assertIn('--language "%INSTALLER_LANGUAGE%"', setup)
        self.assertIn('Выберите язык / Choose language', setup)
        self.assertIn('[1] Русский', setup)
        self.assertIn('[2] English', setup)
        self.assertIn("console_ui.py start-ready", start)
        self.assertIn("console_ui.py start-error", start)
        self.assertIn("[1/3] Preparing Python", setup)
        self.assertIn("[2/3] Installing dependencies", setup)
        self.assertIn("[3/3] Opening configuration", setup)
        self.assertIn('>>"%SETUP_LOG%" 2>&1', setup)
        self.assertIn('echo %MSG_DETAILS% "%SETUP_LOG%"', setup)
        self.assertIn('Подробный вывод установщика:', setup)
        self.assertIn('Другой экземпляр FunPayFlow уже запущен.',
                      (ROOT / "console_ui.py").read_text(encoding="utf-8"))
        self.assertNotIn(".install-data-dir", setup + start)
        self.assertNotIn("FUNPAY_GOLDEN_KEY", setup + start + resolver)
        self.assertNotIn("installer_language", (ROOT / "telegram.py").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="FunPay service test ") as folder:
            code = Path(folder) / "code with spaces"
            data = Path(folder) / "data with spaces"
            code.mkdir()
            data.mkdir()
            rendered = render_service.render(code, data, "seller")
            self.assertIn('WorkingDirectory=' + render_service._scalar_path(
                str(code.resolve())), rendered)
            workdir = next(line.partition("=")[2] for line in rendered.splitlines()
                           if line.startswith("WorkingDirectory="))
            self.assertTrue(os.path.samefile(workdir.replace("\\\\", "\\"), code))
            self.assertIn('EnvironmentFile=' + render_service._unit_word(str(data.resolve() / ".env")),
                          rendered)
            self.assertIn('Environment=' + render_service._unit_word(
                "FUNPAY_BOT_DATA_DIR=" + str(data.resolve())), rendered)
            self.assertIn("Restart=on-failure", rendered)
            self.assertNotIn("@CODE_DIR@", rendered)
            self.assertNotIn('WorkingDirectory="', rendered)
            self.assertNotIn("fake-golden", rendered)
            with self.assertRaises(ValueError):
                render_service.render(Path("relative code"), data, "seller")
            dollar_code = Path(folder) / "code$literal"
            dollar_unit = render_service.render(dollar_code, data, "seller")
            self.assertIn("ExecStart=" + render_service._exec_word(
                str(dollar_code.resolve() / ".venv" / "bin" / "python")), dollar_unit)

    @unittest.skipUnless(os.name == "nt", "Windows cmd.exe is required")
    def test_setup_first_language_screen_without_installing(self):
        environment = dict(os.environ, OS="SYNTHETIC_UNSUPPORTED_OS")
        for selection, selected_text, other_text in (
            ("\n", "Требуется Windows 10 или новее.", "Windows 10 or newer is required."),
            ("2\n", "Windows 10 or newer is required.", "Требуется Windows 10 или новее."),
        ):
            with self.subTest(selection=selection):
                result = subprocess.run(["cmd.exe", "/d", "/c", str(ROOT / "Setup.bat")],
                    cwd=ROOT, env=environment, input=selection, capture_output=True,
                    text=True, encoding="utf-8", timeout=10,
                    check=False)
                self.assertEqual(result.returncode, 1, result.stderr[-1000:])
                self.assertIn("FUNPAYFLOW", result.stdout)
                self.assertIn("v1.0.0", result.stdout)
                self.assertIn("Выберите язык / Choose language", result.stdout)
                self.assertIn(selected_text, result.stdout)
                self.assertNotIn(other_text, result.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows cmd.exe is required")
    def test_setup_success_waits_for_enter_and_returns_to_parent_shell(self):
        with tempfile.TemporaryDirectory(prefix="FunPay setup wait with spaces ") as folder:
            root = Path(folder)
            release = root / "release with spaces"
            app = release / "app"
            tools_dir = root / "fake offline tools"
            data = root / "private data"
            app.mkdir(parents=True)
            tools_dir.mkdir()
            data.mkdir()
            shutil.copyfile(ROOT / "Setup.bat", release / "Setup.bat")
            shutil.copyfile(ROOT / "ResolveDataDir.bat", app / "ResolveDataDir.bat")
            shutil.copyfile(ROOT / "pyproject.toml", app / "pyproject.toml")
            (tools_dir / "uv.cmd").write_text(
                '@echo off\r\nif "%~1"=="run" (\r\n'
                '  echo [OK] synthetic setup complete\r\n'
                '  type nul > "%WAIT_MARKER%"\r\n)\r\nexit /b 0\r\n',
                encoding="utf-8")
            (tools_dir / "powershell.cmd").write_text('@echo off\r\nexit /b 0\r\n',
                                                       encoding="utf-8")
            wrapper = root / "parent shell.cmd"
            wrapper.write_text('@echo off\r\ncall "' + str(release / "Setup.bat")
                               + '"\r\necho SHELL_ALIVE\r\n', encoding="utf-8")
            marker = root / "success marker"
            environment = dict(os.environ, OS="Windows_NT", LOCALAPPDATA=str(data),
                               WAIT_MARKER=str(marker),
                               PATH=str(tools_dir) + os.pathsep + os.environ["PATH"])
            process = subprocess.Popen(["cmd.exe", "/d", "/c", str(wrapper)],
                cwd=root, env=environment, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8")
            try:
                process.stdin.write("1\n")
                process.stdin.flush()
                deadline = time.monotonic() + 10
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                if process.poll() is not None and not marker.exists():
                    stdout, stderr = process.communicate()
                    self.fail(f"Offline setup exited early: {stdout[-1000:]} {stderr[-500:]}")
                self.assertTrue(marker.exists(), "Offline setup did not reach success")
                self.assertIsNone(process.poll(), "Setup exited before Enter")
                process.stdin.write("\n")
                process.stdin.flush()
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr[-1000:])
                self.assertIn("Нажмите Enter, чтобы закрыть окно", stdout)
                self.assertIn("SHELL_ALIVE", stdout)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

            failed = subprocess.Popen(["cmd.exe", "/d", "/c", str(wrapper)],
                cwd=root, env=dict(environment, OS="SYNTHETIC_UNSUPPORTED_OS"),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8")
            try:
                failed.stdin.write("1\n")
                failed.stdin.flush()
                time.sleep(0.1)
                self.assertIsNone(failed.poll(), "Failure screen exited before Enter")
                failed.stdin.write("\n")
                failed.stdin.flush()
                failure_output, failure_error = failed.communicate(timeout=10)
                self.assertIn("Нажмите Enter, чтобы закрыть окно", failure_output)
                self.assertIn("SHELL_ALIVE", failure_output)
                self.assertNotIn("Traceback", failure_error)
            finally:
                if failed.poll() is None:
                    failed.kill()
                    failed.communicate()

            # With redirected input and the real redirection check, CI exits without a prompt.
            (tools_dir / "powershell.cmd").unlink()
            redirected = subprocess.run(["cmd.exe", "/d", "/c", str(wrapper)],
                cwd=root, env=environment, input="1\n", capture_output=True,
                text=True, encoding="utf-8", timeout=10,
                check=False)
            self.assertEqual(redirected.returncode, 0, redirected.stderr[-1000:])
            self.assertIn("SHELL_ALIVE", redirected.stdout)
            self.assertNotIn("Нажмите Enter, чтобы закрыть окно", redirected.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows cmd.exe is required")
    def test_start_reports_second_instance_without_running_production_bot(self):
        with tempfile.TemporaryDirectory(prefix="FunPay Start UX ") as folder:
            root = Path(folder)
            code = root / "release with spaces"
            data = root / "private data with spaces"
            code.mkdir()
            data.mkdir()
            for name in ("Start.bat", "ResolveDataDir.bat"):
                shutil.copyfile(ROOT / name, code / name)
            shutil.copyfile(ROOT / "console_ui.py", code / "console_ui.py")
            shutil.copyfile(ROOT / "pyproject.toml", code / "pyproject.toml")
            (data / ".env").write_text("synthetic-only\n", encoding="utf-8")
            (code / "main.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
            created = subprocess.run(["uv", "venv", "--offline", "--python", "3.13",
                str(code / ".venv")], cwd=code, capture_output=True, text=True,
                encoding="utf-8",
                check=False)
            self.assertEqual(created.returncode, 0, created.stderr[-1000:])
            environment = dict(os.environ, FUNPAY_BOT_DATA_DIR=str(data))
            for language, lock_message, config_message in (
                ("ru", "Другой экземпляр FunPayFlow уже запущен.",
                 "[OK] Конфигурация найдена."),
                ("en", "Another FunPayFlow instance is already running.",
                 "[OK] Configuration found."),
            ):
                with self.subTest(language=language):
                    (data / setup_config.LANGUAGE_FILE).write_text(language + "\n", encoding="utf-8")
                    result = subprocess.run(["cmd.exe", "/d", "/c", str(code / "Start.bat")],
                        cwd=code, env=environment, input="\n", capture_output=True, text=True,
                        encoding="utf-8", timeout=15, check=False)
                    self.assertEqual(result.returncode, 3, result.stderr[-1000:])
                    self.assertIn("FunPayFlow v1.0.0", result.stdout)
                    self.assertIn(lock_message, result.stdout)
                    self.assertIn(config_message, result.stdout)
                    self.assertNotIn("Traceback", result.stdout + result.stderr)
                    self.assertNotIn("synthetic-only", result.stdout + result.stderr)

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
                    cwd=release, env=env, capture_output=True, text=True,
                    encoding="utf-8", check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("SYNTHETIC_SECRET_NEVER_PRINT", result.stdout + result.stderr)
                self.assertTrue(result.stdout.startswith("FUNPAY_BOT_DATA_DIR="))
                return Path(result.stdout.strip().split("=", 1)[1])

            def assert_same_directory(actual: Path, expected: Path) -> None:
                expected.mkdir(parents=True, exist_ok=True)
                self.assertTrue(os.path.samefile(actual, expected))

            data = resolve(first_release, environment)
            assert_same_directory(data, Path(environment["LOCALAPPDATA"]) / "FunPayFlow")
            self.assertNotIn(first_release, data.parents)
            assert_same_directory(resolve(next_release, environment), data)

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
            assert_same_directory(resolve(next_release, overridden), root / "custom private")
            fallback = dict(environment)
            fallback.pop("LOCALAPPDATA")
            assert_same_directory(resolve(next_release, fallback),
                Path(environment["USERPROFILE"]) / "AppData" / "Local" / "FunPayFlow")

    def test_linux_syntax_and_permission_strategy(self):
        script = ROOT / "install.sh"
        source = script.read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", source)
        self.assertIn("umask 077", source)
        self.assertIn('chmod 700 -- "$data_dir"', source)
        self.assertIn('chmod 600 -- "$data_dir/.env"', source)
        self.assertIn('"$code_dir/setup_config.py" --data-dir "$data_dir"', source)
        self.assertNotIn("eval ", source)
        self.assertNotIn(b"\r", script.read_bytes())
        bash = shutil.which("bash")
        if os.name == "nt":
            git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
            if git_bash.is_file():
                bash = str(git_bash)
        if bash:
            result = subprocess.run([bash, "-n", str(script)], capture_output=True,
                                    text=True, encoding="utf-8", check=False)
            self.assertEqual(result.returncode, 0, f"{bash}: {result.stderr!r}")


if __name__ == "__main__":
    unittest.main()
