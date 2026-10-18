"""Offline controller regression tests: subprocess and controller IO are fakes."""
from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vps_status_controller.py"
SPEC = importlib.util.spec_from_file_location("vps_status_controller", SCRIPT)
c = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(c)

CONFIG = {"host": "8.8.8.8", "identity": r"C:\Synthetic User\id_ed25519"}
SOURCE = b"# synthetic trusted source\n"
SECRET = "SYNTHETIC_SECRET_SENTINEL"


def issue():
    return {"number": 123, "title": "[VPS] status", "body": "status", "state": "OPEN",
            "author": {"login": "Vetal12350"}, "labels": [{"name": "vps-local"}]}


def report():
    return {"report": "funpayflow_vps_preflight_v1", "read_only": True,
            "deployment_performed": False, "service": "active", "legacy_service": "inactive",
            "main_runtime_processes": "one", "code_commit": "a" * 40,
            "tracked_worktree": "clean", "data_directory": "present", "env_file": "present",
            "settings_file": "present", "sqlite": "ok", "disk_free": "at_least_512_mb"}


def result(value, returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=json.dumps(value).encode(),
                           stderr=SECRET.encode())


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("synthetic-local")
        self.files = {str(c.PREFLIGHT): SOURCE,
                      str(self.root / "config.json"): json.dumps(CONFIG).encode()}
        self.opened = []
        self.stdout = io.StringIO()
        self.run = Mock(side_effect=AssertionError("Unexpected subprocess"))
        for target, replacement in (("subprocess.run", self.run),
                                     ("os.fsync", Mock()), ("pathlib.Path.mkdir", Mock()),
                                     ("pathlib.Path.open", lambda path, *a, **kw: self.fake_open(path, *a, **kw)),
                                     ("sys.stdout", self.stdout)):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(c, "PREFLIGHT_SHA256", hashlib.sha256(SOURCE).hexdigest())
        patcher.start()
        self.addCleanup(patcher.stop)

    @contextmanager
    def fake_open(self, path, mode="r", **kwargs):
        key = str(path)
        self.opened.append((key, mode))
        if "x" in mode and key in self.files:
            raise FileExistsError
        if "r" in mode and key not in self.files:
            raise FileNotFoundError
        content = self.files.get(key, b"") if "r" in mode else b""
        stream = io.BytesIO(content) if "b" in mode else io.StringIO(content.decode())
        stream.fileno = lambda: 123
        # Exclusive creation exists even if writing/fsync later fails.
        if "x" in mode:
            self.files[key] = b""
        try:
            yield stream
        finally:
            if any(ch in mode for ch in "xwa"):
                value = stream.getvalue()
                self.files[key] = value if isinstance(value, bytes) else value.encode()
            stream.close()

    def queue(self, issues=None, status=None):
        self.run.side_effect = [result([issue()] if issues is None else issues),
                                result(report() if status is None else status), result({})]

    def test_disabled_by_default_does_no_io(self):
        self.assertEqual(c.main([]), 0)
        self.run.assert_not_called()
        self.assertEqual(self.opened, [])
        self.assertIn("Disabled", self.stdout.getvalue())

    def test_valid_issue_uses_only_fixed_readonly_command_and_source(self):
        self.queue()
        c.poll(self.root, CONFIG, True)
        self.assertEqual(self.run.call_count, 3)
        poll, ssh, comment = self.run.call_args_list
        self.assertEqual(poll.args[0][:5], ["gh.exe", "issue", "list", "-R", c.REPO])
        cmd = ssh.args[0]
        self.assertEqual(cmd[0:4], ["ssh.exe", "-F", "NUL", "-T"])
        self.assertEqual(cmd[-5:], [CONFIG["identity"], "-l", "root", CONFIG["host"], "python3 -"])
        for option in ("StrictHostKeyChecking=yes", "BatchMode=yes", "ForwardAgent=no",
                       "ClearAllForwardings=yes", "ProxyCommand=none", "ProxyJump=none",
                       "IdentitiesOnly=yes", "PasswordAuthentication=no",
                       "KbdInteractiveAuthentication=no", "ConnectTimeout=10"):
            self.assertIn(option, cmd)
        self.assertEqual(ssh.kwargs["input"], SOURCE)
        for call in self.run.call_args_list:
            self.assertIs(call.kwargs["shell"], False)
            self.assertEqual(call.kwargs["timeout"], 45)
        body = comment.args[0][-1]
        self.assertEqual(comment.args[0][1:3], ["issue", "comment"])
        for private in (SECRET, CONFIG["host"], CONFIG["identity"], "a" * 40):
            self.assertNotIn(private, body + self.stdout.getvalue())
        self.assertTrue(any(name.endswith("123.done") for name in self.files))
        self.assertEqual(self.opened[-1], (str(c.PREFLIGHT), "rb"))

    def test_all_malformed_and_nonowner_issues_are_rejected(self):
        candidates = [None, [], "status", {}, {**issue(), "author": None}]
        for field, values in {
            "number": [True, 0, -1, c.MAX_NUMBER + 1, "123"],
            "title": ["[VPS] deploy", "[VPS] status\n", "x" * 10000],
            "body": ["status\n", "exec('" + SECRET + "')", "status; restart", "x" * 10000],
            "state": ["CLOSED"], "author": [{"login": "attacker"}, {"login": "vetal12350"}],
            "labels": [None, ["vps-local"], [{"name": "other"}],
                       [{"name": "vps-local"}] * 101],
        }.items():
            candidates.extend({**issue(), field: value} for value in values)
        # Batches are bounded by the controller's 50-issue ceiling.
        self.run.side_effect = [result(candidates)]
        c.poll(self.root, CONFIG, True)
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.opened, [])

    def test_no_supported_mutating_commands(self):
        for command in ("deploy", "backup", "restart", "stop", "start", "stage", "approve"):
            with self.subTest(command=command):
                self.assertFalse(c.eligible({**issue(), "title": f"[VPS] {command}",
                                             "body": command}))
                with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                    c.main(["--" + command])
        self.run.assert_not_called()

    def test_malicious_or_private_hosts_fail_before_ssh(self):
        for host in ("-oProxyCommand=evil", "8.8.8.8;evil", "user@8.8.8.8", "host.example",
                     "127.0.0.1", "10.0.0.1", "169.254.169.254", "224.0.0.1", "::1",
                     "8.8.8.8\n", 123, None, True):
            with self.subTest(host=host), self.assertRaises(c.Failure):
                c.status({**CONFIG, "host": host})
        self.run.assert_not_called()
        self.assertEqual(self.opened, [])

    def test_identity_location_only_never_reads_key_material(self):
        self.run.return_value = result(report())
        self.run.side_effect = None
        c.status(CONFIG)
        self.assertEqual(self.opened, [(str(c.PREFLIGHT), "rb")])
        for identity in ("relative.key", r"\\server\key", r"\\?\C:\key", "-i key",
                         "C:\\key\n", r"C:\key:stream", r"C:\..\key", r"C:\%SECRET%"):
            with self.subTest(identity=identity), self.assertRaises(c.Failure):
                c.validate_config({**CONFIG, "identity": identity})

    def test_config_allowlist(self):
        for value in (None, [], {**CONFIG, "command": "evil"}, {"host": CONFIG["host"]}):
            with self.assertRaises(c.Failure):
                c.validate_config(value)

    def test_timeout_and_nonzero_exit_only_fixed_error_categories(self):
        for error, category in ((subprocess.TimeoutExpired("private", 45, output=SECRET), "timeout"),
                                (OSError(SECRET), "ssh"), (result({}, 1), "ssh")):
            with self.subTest(category=category):
                self.files.pop(str(self.root / "processed" / "123.done"), None)
                self.run.side_effect = [result([issue()]), error, result({})]
                c.poll(self.root, CONFIG, True)
                self.assertEqual(self.run.call_args.args[0][-1],
                                 f"VPS status unavailable ({category}).")
        self.assertNotIn(SECRET, self.stdout.getvalue())

    def test_schema_allowlist_rejects_secret_in_every_field(self):
        for key in report():
            value = report()
            value[key] = SECRET
            with self.subTest(key=key), self.assertRaises(c.Failure):
                c.sanitized_report(json.dumps(value).encode())
        for value in (None, [], {**report(), "private": SECRET}, {"service": "active"},
                      {**report(), "read_only": 1}, {**report(), "deployment_performed": 0}):
            with self.assertRaises(c.Failure):
                c.sanitized_report(json.dumps(value).encode())
        for raw in (SECRET.encode(), b"{" * 5000, b"\xff", b'{"service":1,"service":2}'):
            with self.assertRaises(c.Failure):
                c.sanitized_report(raw)

    def test_malformed_secret_output_never_reaches_github_or_terminal(self):
        self.queue(status={**report(), "service": SECRET})
        c.poll(self.root, CONFIG, True)
        self.assertEqual(self.run.call_args.args[0][-1], "VPS status unavailable (schema).")
        self.assertNotIn(SECRET, self.stdout.getvalue())

    def test_duplicate_issue_after_restart_does_not_execute(self):
        self.queue()
        c.poll(self.root, CONFIG, False)
        # New poll has no shared in-memory processed set: persistent file is authoritative.
        self.run.reset_mock()
        self.run.side_effect = [result([issue(), copy.deepcopy(issue())])]
        c.poll(Path(str(self.root)), copy.deepcopy(CONFIG), True)
        self.assertEqual(self.run.call_count, 1)

    def test_claim_is_durable_before_execution_and_failure_stays_claimed(self):
        def fake_run(command, **kwargs):
            if command[0] == "gh.exe":
                return result([issue()])
            self.assertIn(str(self.root / "processed" / "123.done"), self.files)
            self.assertTrue(c.os.fsync.called)
            raise subprocess.TimeoutExpired("private", 45)
        self.run.side_effect = fake_run
        c.poll(self.root, CONFIG, False)
        c.poll(self.root, CONFIG, False)
        self.assertEqual(sum(call.args[0][0] == "ssh.exe" for call in self.run.call_args_list), 1)

    def test_persistence_failure_prevents_ssh(self):
        self.run.side_effect = [result([issue()])]
        with patch.object(c.os, "fsync", side_effect=OSError(SECRET)), self.assertRaises(OSError):
            c.poll(self.root, CONFIG, True)
        self.assertEqual(self.run.call_count, 1)

    def test_modified_preflight_is_not_sent(self):
        self.files[str(c.PREFLIGHT)] = b"print('" + SECRET.encode() + b"')"
        with self.assertRaises(c.Failure) as caught:
            c.status(CONFIG)
        self.assertEqual(caught.exception.category, "source")
        self.run.assert_not_called()

    def test_crlf_checkout_preserves_original_source_bytes(self):
        self.files[str(c.PREFLIGHT)] = SOURCE.replace(b"\n", b"\r\n")
        self.run.side_effect = [result(report())]
        c.status(CONFIG)
        self.assertEqual(self.run.call_args.kwargs["input"], self.files[str(c.PREFLIGHT)])

    def test_no_automatic_comment_or_issue_close(self):
        self.queue()
        c.poll(self.root, CONFIG, False)
        self.assertEqual(self.run.call_count, 2)
        self.assertFalse(any("close" in call.args[0] for call in self.run.call_args_list))

    def test_failed_comment_delivery_is_not_retried(self):
        self.run.side_effect = [result([issue()]), result(report()),
                                subprocess.TimeoutExpired("private", 45)]
        with self.assertRaises(c.Failure):
            c.poll(self.root, CONFIG, True)
        self.run.side_effect = [result([issue()])]
        c.poll(self.root, CONFIG, True)
        self.assertEqual(self.run.call_count, 4)

    def test_bad_github_output_is_fail_closed(self):
        for value in ({}, None, [issue()] * 51):
            self.run.side_effect = [result(value)]
            with self.assertRaises(c.Failure):
                c.poll(self.root, CONFIG, True)
        self.assertEqual(self.opened, [])

    def test_configuration_is_local_exclusive_and_never_reads_identity(self):
        self.files.pop(str(self.root / "config.json"))
        with patch("builtins.input", side_effect=[CONFIG["host"], CONFIG["identity"]]):
            c.configure(self.root)
        self.assertEqual(json.loads(self.files[str(self.root / "config.json")]), CONFIG)
        with patch("builtins.input", side_effect=[CONFIG["host"], CONFIG["identity"]]), \
                self.assertRaises(FileExistsError):
            c.configure(self.root)
        self.assertTrue(all(name.endswith("config.json") for name, mode in self.opened))
        self.run.assert_not_called()

    def test_main_once_watch_and_fixed_error_output(self):
        # Replace the module's os reference rather than changing the process platform.
        fake_os = SimpleNamespace(name="nt")
        with patch.object(c, "os", fake_os), patch.object(c, "local_directory", return_value=self.root), \
                patch.object(c, "poll") as poll:
            self.assertEqual(c.main(["--once"]), 0)
            poll.assert_called_once_with(self.root, CONFIG, False)
            poll.reset_mock()
            with patch.object(c.time, "sleep", side_effect=KeyboardInterrupt) as sleep:
                self.assertEqual(c.main(["--watch", "--post-results"]), 0)
            poll.assert_called_once_with(self.root, CONFIG, True)
            sleep.assert_called_once_with(60)
            poll.side_effect = OSError(SECRET)
            self.assertEqual(c.main(["--once"]), 1)
        self.assertNotIn(SECRET, self.stdout.getvalue())

    def test_non_windows_run_is_disabled(self):
        with patch.object(c, "os", SimpleNamespace(name="posix")):
            self.assertEqual(c.main(["--once"]), 1)
        self.run.assert_not_called()
        self.assertEqual(self.opened, [])

    def test_localappdata_required_and_must_be_outside_checkout(self):
        with patch.dict(c.os.environ, {}, clear=True), self.assertRaises(c.Failure):
            c.local_directory()
        with patch.dict(c.os.environ, {"LOCALAPPDATA": r"C:\SyntheticLocal"}), \
                patch.object(Path, "resolve", return_value=Path("same-checkout")), \
                self.assertRaises(c.Failure):
            c.local_directory()


if __name__ == "__main__":
    unittest.main()
