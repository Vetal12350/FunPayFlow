"""Offline release ZIP security, reproducibility and extracted-copy smoke."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

from scripts import build_release as release


ROOT = Path(__file__).resolve().parents[1]


def _copy_allowlist(destination: Path) -> None:
    for name in release.REQUIRED_FILES:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)


class ReleaseBuilderTests(unittest.TestCase):
    def test_bilingual_readmes_describe_same_core_features(self):
        english = (ROOT / "README.md").read_text(encoding="utf-8")
        russian = (ROOT / "README.ru.md").read_text(encoding="utf-8")
        self.assertTrue(english.startswith("[Русский](README.ru.md) | English"))
        self.assertTrue(russian.startswith("Русский | [English](README.md)"))
        for feature in ("autobump", "notifications", "night_mode", "review_request",
                        "order_history", "statistics", "sales_analytics", "sales_import",
                        "withdrawals", "logs_ui", "SAFE_MODE", "FUNPAY_BOT_DATA_DIR"):
            with self.subTest(feature=feature):
                self.assertIn(feature, english)
                self.assertIn(feature, russian)
        self.assertIn("Russian-only in v1.0", english)
        self.assertIn("только на русском языке", russian)
        self.assertIn("# FunPayFlow", english)
        self.assertIn("# FunPayFlow", russian)
        self.assertIn("independent open-source project", english)
        self.assertIn("not affiliated with\nor endorsed by FunPay", english)
        self.assertIn("независимый open-source проект", russian)
        self.assertIn("не являющийся официальным", russian)
        self.assertIn("FunPayFlow is released under the MIT License. See LICENSE.", english)
        self.assertIn("FunPayFlow распространяется по лицензии MIT. См. LICENSE.", russian)
        for concept in ("Telegram", "statistics", "sales analytics"):
            self.assertIn(concept, english)
        for concept in ("Telegram", "автоподнятие", "уведомления о заказах",
                        "история продаж", "статистика", "аналитика"):
            self.assertIn(concept, russian.lower() if concept != "Telegram" else russian)

    def test_release_workflow_is_draft_only_after_verify(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8")
        self.assertIn("needs: verify", workflow)
        self.assertIn("contents: read", workflow)
        self.assertEqual(workflow.count("contents: write"), 1)
        self.assertIn("--require-license", workflow)
        self.assertIn("test -f LICENSE", workflow)
        self.assertIn("--verify-tag --draft", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn("FunPayFlow-${GITHUB_REF_NAME}.zip", workflow)
        self.assertIn("FunPayFlow-${GITHUB_REF_NAME}.zip.sha256", workflow)
        self.assertIn('FunPayFlow ${GITHUB_REF_NAME}', workflow)
        self.assertNotIn("python main.py", workflow)

    def test_public_package_and_service_names(self):
        import tomllib

        project = tomllib.loads((ROOT / "pyproject.toml").read_text(
            encoding="utf-8"))["project"]
        self.assertEqual(project["name"], "funpayflow")
        self.assertEqual(project["version"], "1.0.0")
        self.assertEqual(release.project_version(), "1.0.0")
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("service_name=funpayflow.service", installer)
        self.assertTrue((ROOT / "systemd" / "funpayflow.service.in").is_file())
        self.assertIn("Description=FunPayFlow", (ROOT / "systemd" / "funpayflow.service.in")
                      .read_text(encoding="utf-8"))

    def test_mit_license_and_private_reporting_policy(self):
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertTrue(license_text.startswith("MIT License\n"))
        self.assertIn("Copyright (c) 2026 Vetal12350", license_text)
        self.assertIn("Permission is hereby granted, free of charge", license_text)
        self.assertIn("THE SOFTWARE IS PROVIDED \"AS IS\"", license_text)
        self.assertIn("Private\nVulnerability Reporting", security)
        self.assertIn("когда владелец включит", security)
        self.assertIn("GitHub Issues", security)

    def test_allowlist_top_folder_checksum_and_determinism(self):
        with tempfile.TemporaryDirectory(prefix="FunPay release test ") as folder:
            root = Path(folder) / "source with spaces"
            root.mkdir()
            _copy_allowlist(root)
            (root / ".env").write_text("synthetic private file", encoding="utf-8")
            (root / "state.sqlite3").write_bytes(b"synthetic database")
            (root / "logs").mkdir()
            (root / "logs" / "bot.log").write_text("synthetic", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "fixture.py").write_text("synthetic", encoding="utf-8")
            first, checksum = release.build_release(root, root / "out one")
            second, _ = release.build_release(root, root / "out two")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first.name, "FunPayFlow-v1.0.0.zip")
            self.assertEqual(checksum.name, first.name + ".sha256")
            release.verify_checksum(first, checksum)
            with zipfile.ZipFile(first) as archive:
                expected = {f"FunPayFlow-v1.0.0/{name}"
                            for name in release.archive_files(release.REQUIRED_FILES)}
                self.assertEqual(set(archive.namelist()), expected)
                self.assertIn("FunPayFlow-v1.0.0/README.md", expected)
                self.assertIn("FunPayFlow-v1.0.0/README.ru.md", expected)
                self.assertIn("FunPayFlow-v1.0.0/LICENSE", expected)
                self.assertIn("FunPayFlow-v1.0.0/app/main.py", expected)
                self.assertIn("FunPayFlow-v1.0.0/linux/install.sh", expected)
                self.assertNotIn("FunPayFlow-v1.0.0/main.py", expected)
                self.assertTrue(all(info.date_time == (2020, 1, 1, 0, 0, 0)
                                    for info in archive.infolist()))
            self.assertEqual(checksum.read_text(encoding="ascii").split()[0],
                             hashlib.sha256(first.read_bytes()).hexdigest())
            self.assertNotIn(b"synthetic private file", first.read_bytes())

    def test_missing_required_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _copy_allowlist(root)
            (root / "Start.bat").unlink()
            with self.assertRaises(release.ReleaseError):
                release.build_release(root, root / "dist")
            self.assertFalse(list((root / "dist").glob("*.zip")))

    def test_missing_license_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _copy_allowlist(root)
            (root / "LICENSE").unlink()
            with self.assertRaises(release.ReleaseError):
                release.build_release(root, root / "dist")
            self.assertFalse(list((root / "dist").glob("*.zip")))

    def test_owner_marker_is_detected_without_echo(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _copy_allowlist(root)
            marker = "SYNTHETIC_PRIVATE_OWNER_MARKER"
            with (root / "README.md").open("a", encoding="utf-8") as stream:
                stream.write("\n" + marker + "\n")
            with self.assertRaises(release.ReleaseError) as caught:
                release.build_release(root, root / "dist", private_markers=(marker,))
            self.assertNotIn(marker, str(caught.exception))

    def test_audit_rejects_private_extra_member_and_bad_checksum(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _copy_allowlist(root)
            archive, checksum = release.build_release(root, root / "dist")
            with zipfile.ZipFile(archive, "a") as bundle:
                bundle.writestr("FunPayFlow-v1.0.0/.env", "SYNTHETIC_PRIVATE_VALUE")
            with self.assertRaises(release.ReleaseError) as caught:
                release.audit_archive(archive, version="1.0.0")
            self.assertNotIn("SYNTHETIC_PRIVATE_VALUE", str(caught.exception))
            with self.assertRaises(release.ReleaseError):
                release.verify_checksum(archive, checksum)

    def test_tag_version_match(self):
        release.verify_tag("v1.0.0")
        with self.assertRaises(release.ReleaseError):
            release.verify_tag("v0.1.0")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _copy_allowlist(root)
            source = (root / "pyproject.toml").read_text(encoding="utf-8")
            (root / "pyproject.toml").write_text(
                source.replace('version = "1.0.0"', 'version = "1.0.1"', 1), encoding="utf-8")
            with self.assertRaises(release.ReleaseError):
                release.project_version(root)


class ExtractedReleaseSmoke(unittest.TestCase):
    def test_extracted_runtime_windows_and_linux_install_files(self):
        with tempfile.TemporaryDirectory(prefix="FunPay archive smoke ") as folder:
            temporary = Path(folder)
            archive, _ = release.build_release(ROOT, temporary / "dist")
            extraction = temporary / "extracted release with spaces"
            extraction.mkdir()
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(extraction)
            release_root = extraction / "FunPayFlow-v1.0.0"
            code = release_root / "app"
            self.assertTrue((code / ".env.example").is_file())
            self.assertIn("[Русский](README.ru.md) | English",
                          (release_root / "README.md").read_text(encoding="utf-8"))
            self.assertIn("Русский | [English](README.md)",
                          (release_root / "README.ru.md").read_text(encoding="utf-8"))
            for name in ("Setup.bat", "Start.bat", "app/ResolveDataDir.bat",
                         "linux/install.sh", "linux/systemd/funpayflow.service.in",
                         "app/render_service.py", "app/README.md"):
                self.assertTrue((release_root / name).is_file(), name)
            self.assertFalse((release_root / "ResolveDataDir.bat").exists())
            private = temporary / "fresh private data with spaces"
            environment = dict(os.environ)
            for key in ("FUNPAY_GOLDEN_KEY", "BOT_TOKEN", "ADMIN_ID", "FUNPAY_USER_ID",
                        "BOT_PASSWORD", "DEBUG", "FUNPAY_BOT_DATA_DIR"):
                environment.pop(key, None)
            environment["FUNPAY_BOT_DATA_DIR"] = str(private)
            environment.pop("PYTHONPATH", None)
            environment["UV_OFFLINE"] = "1"
            environment["PYTHONIOENCODING"] = "utf-8"
            isolated = temporary / "isolated dependencies with spaces"
            venv = subprocess.run(["uv", "venv", "--offline", "--python", "3.13",
                str(isolated)], cwd=code, env=environment, text=True,
                capture_output=True, check=False)
            self.assertEqual(venv.returncode, 0, venv.stderr[-1200:])
            sync_env = dict(environment, UV_PROJECT_ENVIRONMENT=str(isolated))
            sync = subprocess.run(["uv", "sync", "--offline", "--frozen",
                "--no-install-project", "--no-dev"], cwd=code, env=sync_env,
                text=True, capture_output=True, check=False)
            self.assertEqual(sync.returncode, 0, sync.stderr[-1200:])
            python = (isolated / "Scripts" / "python.exe" if os.name == "nt"
                      else isolated / "bin" / "python")
            script = r'''
import json, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
private = pathlib.Path(sys.argv[2]).resolve()
checkout = pathlib.Path(sys.argv[3]).resolve()
assert root != checkout and checkout not in [pathlib.Path(p).resolve() for p in sys.path if p]
sys.path.insert(0, str(root))
import main, telegram, state, runtime_paths, setup_config, console_ui, render_service
for module in (main, telegram, state, runtime_paths, setup_config, console_ui, render_service):
    assert pathlib.Path(module.__file__).resolve().is_relative_to(root)
assert runtime_paths.data_dir() == private
assert state.DEFAULT_DB_PATH == private / 'state.sqlite3'
assert pathlib.Path(telegram.SETTINGS_FILE) == private / 'bot_settings.json'
assert pathlib.Path(main.LOCK_FILE) == private / 'bot.lock'
assert pathlib.Path(telegram.LOGS_DIR) == private / 'logs'
inputs = iter(('111', '222', '0'))
secrets = iter(('synthetic-key', 'synthetic-token', ''))
setup_config.configure(private, input_fn=lambda _: next(inputs),
    secret_fn=lambda _: next(secrets), output_fn=lambda _: None)
assert (private / '.env').is_file()
state.ReviewReceiptStore().initialize()
assert (private / 'state.sqlite3').is_file()
print('extracted-import-and-config:PASS')
'''
            result = subprocess.run([str(python), "-I", "-B", "-c", script,
                str(code), str(private), str(ROOT)], cwd=code, env=environment,
                text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr[-1200:])
            self.assertIn("extracted-import-and-config:PASS", result.stdout)
            self.assertNotIn("synthetic-key", result.stdout + result.stderr)
            self.assertFalse((code / ".env").exists())
            self.assertFalse((code / "state.sqlite3").exists())

            legacy = subprocess.run([str(python), "-I", "-B", "-c",
                "import sys,pathlib; root=pathlib.Path(sys.argv[1]).resolve(); "
                "sys.path.insert(0,str(root)); import runtime_paths; "
                "assert runtime_paths.data_dir()==root; print('legacy:PASS')",
                str(code)], cwd=code,
                env={key: value for key, value in environment.items()
                     if key != "FUNPAY_BOT_DATA_DIR"},
                text=True, capture_output=True, check=False)
            self.assertEqual(legacy.returncode, 0, legacy.stderr[-1000:])
            self.assertIn("legacy:PASS", legacy.stdout)

            metadata = subprocess.run([str(python), "-I", "-B", "-c",
                "import pathlib,sys,tomllib; root=pathlib.Path(sys.argv[1]); "
                "doc=tomllib.loads((root/'pyproject.toml').read_text(encoding='utf-8')); "
                "assert doc['project']['version']=='1.0.0'; print('metadata:PASS')",
                str(code)], cwd=code, text=True, capture_output=True, check=False)
            self.assertEqual(metadata.returncode, 0, metadata.stderr[-1000:])

            # Exercise the process lock from the extracted code without main().
            ready = private / "lock-test-ready"
            holder_code = (
                "import pathlib,sys; root=pathlib.Path(sys.argv[1]); "
                "sys.path.insert(0,str(root)); import main; "
                "main.acquire_lock(); pathlib.Path(sys.argv[2]).write_text('ready'); "
                "sys.stdin.readline(); main.release_lock()"
            )
            holder = subprocess.Popen([str(python), "-X", "utf8", "-I", "-B", "-c", holder_code,
                str(code), str(ready)], cwd=code, env=environment, text=True, encoding="utf-8",
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready.exists(), "Extracted lock holder did not start")
                contender = subprocess.run([str(python), "-X", "utf8", "-I", "-B", "-c",
                    "import pathlib,sys; root=pathlib.Path(sys.argv[1]); "
                    "sys.path.insert(0,str(root)); import main; main.acquire_lock()",
                    str(code)], cwd=code, env=environment,
                    text=True, encoding="utf-8", capture_output=True, timeout=10, check=False)
                self.assertEqual(contender.returncode, 3, contender.stderr[-1000:])
                self.assertNotIn("synthetic-key", contender.stdout + contender.stderr)
            finally:
                if holder.stdin is not None:
                    try:
                        holder.stdin.write("\n")
                        holder.stdin.flush()
                    except OSError:
                        pass
                try:
                    holder.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    holder.kill()
                    holder.communicate()

            if os.name == "nt":
                self.assertIn('call "%RESOLVER%"',
                              (release_root / "Setup.bat").read_text(encoding="utf-8"))
                self.assertIn('call "%RESOLVER%"',
                              (release_root / "Start.bat").read_text(encoding="utf-8"))
                self.assertIn('if "%BOT_EXIT%"=="3"',
                              (release_root / "Start.bat").read_text(encoding="utf-8"))
                win_env = dict(environment, LOCALAPPDATA=str(temporary / "Synthetic App Data"))
                win_env.pop("FUNPAY_BOT_DATA_DIR", None)
                resolver = subprocess.run(["cmd.exe", "/d", "/c",
                    str(code / "ResolveDataDir.bat"), "--print"], cwd=temporary,
                    env=win_env, text=True, capture_output=True, check=False)
                self.assertEqual(resolver.returncode, 0, resolver.stderr)
                self.assertIn("FunPayFlow", resolver.stdout)
                missing = subprocess.run(["cmd.exe", "/d", "/c",
                    str(release_root / "Start.bat")], cwd=temporary, env=win_env, input="\n",
                    text=True, encoding="utf-8", errors="replace", capture_output=True,
                    timeout=10, check=False)
                self.assertNotEqual(missing.returncode, 0)
                self.assertIn("Конфигурация не найдена", missing.stdout)
                self.assertNotIn("synthetic-key", missing.stdout + missing.stderr)

                unsupported = subprocess.run(["cmd.exe", "/d", "/c",
                    str(release_root / "Setup.bat")], cwd=temporary,
                    env=dict(win_env, OS="SYNTHETIC_UNSUPPORTED_OS"), input="2\n",
                    text=True, encoding="utf-8", errors="replace", capture_output=True,
                    timeout=10, check=False)
                self.assertEqual(unsupported.returncode, 1, unsupported.stderr)
                self.assertIn("FUNPAYFLOW", unsupported.stdout)
                self.assertIn("v1.0.0", unsupported.stdout)
                self.assertIn("Windows 10 or newer is required.", unsupported.stdout)

                venv = subprocess.run(["uv", "venv", "--offline", "--python", "3.13",
                    str(code / ".venv")], cwd=code, env=environment,
                    text=True, capture_output=True, check=False)
                self.assertEqual(venv.returncode, 0, venv.stderr[-1000:])
                (code / "main.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
                synthetic_start = subprocess.run(["cmd.exe", "/d", "/c",
                    str(release_root / "Start.bat")], cwd=temporary,
                    env=environment, input="\n", text=True, encoding="utf-8",
                    errors="replace", capture_output=True, timeout=15, check=False)
                self.assertEqual(synthetic_start.returncode, 3, synthetic_start.stderr[-1000:])
                self.assertIn("FunPayFlow v1.0.0", synthetic_start.stdout)
                self.assertIn("Другой экземпляр FunPayFlow", synthetic_start.stdout)
                self.assertNotIn("synthetic-key", synthetic_start.stdout + synthetic_start.stderr)
            else:
                if shutil.which("bash"):
                    syntax = subprocess.run(["bash", "-n", str(release_root / "linux" / "install.sh")],
                        cwd=code, text=True, capture_output=True, check=False)
                    self.assertEqual(syntax.returncode, 0, syntax.stderr)
                unit = subprocess.run([str(python), "-B",
                    str(code / "render_service.py"), "--code-dir", str(code),
                    "--data-dir", str(private), "--output", str(temporary / "test.service")],
                    cwd=code, text=True, capture_output=True, check=False)
                self.assertEqual(unit.returncode, 0, unit.stderr[-1000:])
                unit_text = (temporary / "test.service").read_text(encoding="utf-8")
                self.assertIn(f'WorkingDirectory="{code}"', unit_text)
                self.assertIn(f'EnvironmentFile="{private / ".env"}"', unit_text)
                self.assertNotIn("synthetic-key", unit_text)
                if shutil.which("systemd-analyze"):
                    executable = code / ".venv" / "bin" / "python"
                    executable.parent.mkdir(parents=True)
                    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    executable.chmod(0o755)
                    verified = subprocess.run(["systemd-analyze", "verify",
                        str(temporary / "test.service")], cwd=code,
                        text=True, capture_output=True, check=False)
                    self.assertEqual(verified.returncode, 0, verified.stderr[-1200:])


if __name__ == "__main__":
    unittest.main()
