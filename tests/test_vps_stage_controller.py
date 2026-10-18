"""Offline plan-only regression tests. No SSH, network, keys or disk writes."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vps_stage_controller.py"
SPEC = importlib.util.spec_from_file_location("vps_stage_controller", SCRIPT)
c = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(c)
SHA = "a" * 40
# Synthetic public address for validation only; never contacted.
CONFIG = {"host": "8.8.8.8", "identity": r"C:\Synthetic User\id_ed25519"}
SECRET = "SYNTHETIC_PRIVATE_SENTINEL"


def ci():
    return {"runs": [{"id": 123, "path": ".github/workflows/ci.yml", "name": "CI",
                      "head_sha": SHA, "head_branch": "main", "event": "push",
                      "status": "completed", "conclusion": "success",
                      "repo": c.REPO, "head_repo": c.REPO}]}


def result(value, code=0):
    return SimpleNamespace(returncode=code, stdout=json.dumps(value).encode(),
                           stderr=SECRET.encode())


class StagePlanTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.stderr = io.StringIO()
        self.run = Mock(side_effect=AssertionError("Unexpected subprocess"))
        self.read = Mock(return_value=json.dumps(CONFIG).encode())
        self.answer = Mock(return_value=f"PLAN {SHA} {CONFIG['host']}")
        self.tty = SimpleNamespace(isatty=Mock(return_value=True))
        for target, replacement in (
            ("subprocess.run", self.run), ("sys.stdout", self.output),
            ("sys.stderr", self.stderr), ("sys.stdin", self.tty),
            ("builtins.input", self.answer),
            ("pathlib.Path.open", Mock(side_effect=AssertionError("Unexpected file IO"))),
            ("pathlib.Path.mkdir", Mock(side_effect=AssertionError("Unexpected mkdir"))),
        ):
            p = patch(target, replacement)
            p.start()
            self.addCleanup(p.stop)
        for name, replacement in (("supported", Mock(return_value=True)),):
            p = patch.object(c, name, replacement)
            p.start()
            self.addCleanup(p.stop)
        for name, replacement in (
            ("read_bounded", self.read),
            ("local_directory", Mock(return_value=Path("synthetic-local"))),
        ):
            p = patch.object(c._status, name, replacement)
            p.start()
            self.addCleanup(p.stop)

    def queue(self):
        self.run.side_effect = [result({"sha": SHA, "type": "commit"}), result(ci()),
                                result({"sha": SHA, "type": "commit"}), result(ci())]

    def assert_no_ssh(self):
        for call in self.run.call_args_list:
            command = call.args[0]
            self.assertEqual(command[0], "gh.exe")
            self.assertEqual(command[1:6],
                             ["api", "--hostname", "github.com", "--method", "GET"])
            self.assertIn(command[6], (c.MAIN,
                          c.CI + f"?branch=main&event=push&head_sha={SHA}&per_page=1"))
            self.assertEqual(command[7], "--jq")
            self.assertEqual(len(command), 9)
            self.assertIs(call.kwargs["shell"], False)
            self.assertEqual(call.kwargs["timeout"], 45)
            self.assertIsNone(call.kwargs["input"])

    def test_disabled_without_explicit_plan(self):
        self.assertEqual(c.main([]), 0)
        self.run.assert_not_called()
        self.read.assert_not_called()
        self.answer.assert_not_called()

    def test_acknowledgment_is_successful_preview_only(self):
        self.queue()
        self.assertEqual(c.main(["--plan", SHA]), 0)
        self.assertEqual(self.run.call_count, 4)
        self.assert_no_ssh()
        self.read.assert_called_once_with(Path("synthetic-local") / "config.json", 4096)
        output = self.output.getvalue()
        self.assertIn("PLAN ONLY", output)
        self.assertIn(f"/root/FunPayFlow-stage-{SHA[:12]}", output)
        self.assertIn(c.ORIGIN, output)
        self.assertIn("Nothing staged; no SSH attempted", output)
        self.assertNotIn(CONFIG["identity"], output)
        self.assertNotIn(SECRET, output)
        self.assertIn(f"PLAN {SHA} {CONFIG['host']}", self.answer.call_args.args[0])

    def test_unsafe_sha_injection_fails_before_io(self):
        for sha in ("a" * 39, "a" * 41, "A" * 40, SHA + "\n", "main", "--help",
                    "a;whoami", "$(id)", "../../main", "", SECRET):
            with self.subTest(sha=sha):
                self.assertEqual(c.main(["--plan=" + sha]), 1)
        for sha in (None, True, 123, [], {}):
            with self.assertRaises(c.Failure):
                c.validate_sha(sha)
        self.run.assert_not_called()
        self.read.assert_not_called()
        self.assertNotIn(SECRET, self.output.getvalue())

    def test_no_execution_or_issue_interface_and_sanitized_parser(self):
        for args in (["--stage", SHA], ["--deploy"], ["--watch"], ["--configure"],
                     ["--host", SECRET], ["--command", SECRET], ["--issue", "61"],
                     ["--pl", SHA], [SECRET]):
            with self.subTest(args=args), self.assertRaises(SystemExit) as caught:
                c.main(args)
            self.assertEqual(caught.exception.code, 2)
        self.assertNotIn(SECRET, self.stderr.getvalue())
        self.run.assert_not_called()
        self.read.assert_not_called()

    def test_noninteractive_declines_before_config_github_or_prompt(self):
        self.tty.isatty.return_value = False
        self.assertEqual(c.main(["--plan", SHA]), 1)
        self.assertFalse(c.preview(SHA, CONFIG))
        self.run.assert_not_called()
        self.read.assert_not_called()
        self.answer.assert_not_called()

    def test_windows_python_requirement(self):
        with patch.object(c, "supported", return_value=False):
            self.assertEqual(c.main(["--plan", SHA]), 1)
        self.read.assert_not_called()
        self.run.assert_not_called()
        for platform, version, expected in (("nt", (3, 13), True), ("nt", (3, 12), False),
                                            ("posix", (3, 14), False)):
            # Exercise the real guard without changing pathlib's platform.
            with patch.object(c, "os", SimpleNamespace(name=platform)), \
                    patch.object(c.sys, "version_info", version):
                self.assertEqual(supported_guard(), expected)

    def test_untrusted_host_and_identity_never_reach_github(self):
        for field, values in {
            "host": ["-oProxyCommand=evil", "8.8.8.8;id", "root@8.8.8.8", "example.org",
                     "127.0.0.1", "10.0.0.1", "169.254.169.254", "224.0.0.1", "::1",
                     "8.8.8.8\n", True, None, 123],
            "identity": ["relative", r"\\server\key", r"\\?\C:\key", "-i key",
                         "C:\\key\n", r"C:\key:stream", r"C:\..\key", r"C:\%SECRET%"],
        }.items():
            for value in values:
                self.read.return_value = json.dumps({**CONFIG, field: value}).encode()
                with self.subTest(field=field, value=value):
                    self.assertEqual(c.main(["--plan", SHA]), 1)
        self.run.assert_not_called()

    def test_bad_config_schema_and_filesystem_errors_are_sanitized(self):
        for raw in (b"{}", b"[]", b"null", b"\xff", b"{" * 5000,
                    b'{"host":1,"host":2}', json.dumps({**CONFIG, "command": SECRET}).encode()):
            self.read.return_value = raw
            self.assertEqual(c.main(["--plan", SHA]), 1)
        self.read.side_effect = OSError(SECRET)
        self.assertEqual(c.main(["--plan", SHA]), 1)
        self.assertNotIn(SECRET, self.output.getvalue())
        self.run.assert_not_called()

    def test_noncurrent_or_malformed_main_fails_closed(self):
        for value in ({"sha": "b" * 40, "type": "commit"}, {"sha": SHA, "type": "tag"},
                      {"sha": SHA}, {"sha": SHA, "type": "commit", "extra": SECRET}, [], None):
            self.run.side_effect = [result(value)]
            self.assertEqual(c.main(["--plan", SHA]), 1)
        self.answer.assert_not_called()
        self.assert_no_ssh()

    def test_missing_ci_or_any_unvetted_ci_field_fails_closed(self):
        values = [None, [], {}, {"runs": []}, {"runs": [ci()["runs"][0]] * 2}]
        for key in ci()["runs"][0]:
            value = ci()
            value["runs"][0][key] = SECRET
            values.append(value)
        for key, val in (("status", "in_progress"), ("conclusion", "failure"),
                         ("conclusion", "skipped"), ("conclusion", "cancelled"),
                         ("conclusion", None), ("id", True), ("id", 0),
                         ("repo", "attacker/FunPayFlow"), ("event", "pull_request")):
            value = ci()
            value["runs"][0][key] = val
            values.append(value)
        values.extend([{"runs": [None]}, {"runs": [dict(ci()["runs"][0], extra=SECRET)]}])
        for value in values:
            with self.subTest(value=value):
                self.run.side_effect = [result({"sha": SHA, "type": "commit"}), result(value)]
                self.assertEqual(c.main(["--plan", SHA]), 1)
        self.answer.assert_not_called()
        self.assertNotIn(SECRET, self.output.getvalue())
        self.assert_no_ssh()

    def test_decline_wrong_sha_host_whitespace_or_issue_authorization(self):
        for answer in ("", "yes", "approve", "status", f"PLAN {'b' * 40} {CONFIG['host']}",
                       f"PLAN {SHA} 1.1.1.1", f"PLAN {SHA} {CONFIG['host']} ", SECRET):
            self.run.reset_mock()
            self.queue()
            self.answer.return_value = answer
            self.assertEqual(c.main(["--plan", SHA]), 1)
            self.assertEqual(self.run.call_count, 2)
        self.assertNotIn(SECRET, self.output.getvalue())
        self.assert_no_ssh()

    def test_head_or_ci_changes_during_approval_fail_closed(self):
        for tail in ([result({"sha": "b" * 40, "type": "commit"})],
                     [result({"sha": SHA, "type": "commit"}), result({"runs": []})]):
            self.run.side_effect = [result({"sha": SHA, "type": "commit"}), result(ci()), *tail]
            self.assertEqual(c.main(["--plan", SHA]), 1)
        self.assertNotIn("Plan acknowledged", self.output.getvalue())
        self.assert_no_ssh()

    def test_github_errors_timeouts_and_raw_output_do_not_leak_or_retry(self):
        for failure in (OSError(SECRET), subprocess.TimeoutExpired(SECRET, 45, output=SECRET),
                        result({"private": SECRET}, 1),
                        SimpleNamespace(returncode=0, stdout=SECRET.encode(), stderr=b""),
                        SimpleNamespace(returncode=0, stdout=b"x" * 16385, stderr=b"")):
            self.run.reset_mock()
            self.run.side_effect = [failure]
            self.assertEqual(c.main(["--plan", SHA]), 1)
            self.assertEqual(self.run.call_count, 1)
        self.assertNotIn(SECRET, self.output.getvalue() + self.stderr.getvalue())
        self.assert_no_ssh()

    def test_eof_or_interrupt_declines_without_execution(self):
        for error in (EOFError(SECRET), KeyboardInterrupt):
            self.queue()
            self.answer.side_effect = error
            self.assertEqual(c.main(["--plan", SHA]), 1)
        self.assert_no_ssh()
        self.assertNotIn(SECRET, self.output.getvalue())

    def test_no_remote_status_or_mutation_interface_even_after_approval(self):
        self.queue()
        self.assertEqual(c.main(["--plan", SHA]), 0)
        self.assert_no_ssh()
        self.assertNotIn("Staging succeeded", self.output.getvalue())
        # In particular, this module never reuses phase 1's SSH/preflight path.
        self.assertFalse(hasattr(c, "stage"))
        self.assertFalse(hasattr(c, "status"))
        self.assertFalse(hasattr(c, "deploy"))


supported_guard = c.supported


if __name__ == "__main__":
    unittest.main()
