"""Offline plan-only contract tests; never collect real production observations."""
import ast
import copy
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/vps_deploy_executor.py"
SOURCE = SCRIPT.read_text(encoding="utf-8")
SPEC = importlib.util.spec_from_file_location("vps_deploy_executor", SCRIPT)
c = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(c)
SHA = "a" * 40
OLD = "b" * 40
STAGE = "/root/FunPayFlow-stage-" + OLD[:8]
SECRET = "SYNTHETIC_SECRET_DO_NOT_PRINT"


def environment():
    return {"SSH_ORIGINAL_COMMAND": "deploy " + SHA,
            "SSH_CONNECTION": "synthetic connection", "SSH_CLIENT": "synthetic client",
            "USER": "root", "LOGNAME": "root", "HOME": "/root",
            "SHELL": "/bin/bash", "PATH": "/usr/bin:/bin"}


def observations():
    return {
        **dict.fromkeys(c.REQUIRED_TRUE, True),
        **dict.fromkeys(c.REQUIRED_FALSE, False),
        "origin": c.ORIGIN, "upstream_main": SHA, "previous_sha": OLD,
        "previous_stage": STAGE, "new_stage": "/root/FunPayFlow-stage-" + SHA,
        "unit": c.expected_unit(STAGE), "active": True, "main_runner_count": 1,
        "main_pid": 123, "pid_cwd": STAGE, "working_directory": STAGE,
        "restart_count_before": 3, "restart_count_after": 3, "health_seconds": 20,
    }


class PlanTests(unittest.TestCase):
    def setUp(self):
        # Tripwires apply to all tested entrypoints, including successful plans.
        # They prevent accidentally growing these tests into live deploy tests.
        for target in ("subprocess.run", "subprocess.Popen", "socket.socket",
                       "builtins.open", "pathlib.Path.open", "os.system",
                       "os.mkdir", "os.remove", "os.unlink", "os.replace",
                       "os.rename", "shutil.rmtree"):
            guard = patch(target, side_effect=AssertionError("Unexpected side effect"))
            guard.start()
            self.addCleanup(guard.stop)

    def test_valid_plan_only_and_observations_unchanged(self):
        facts = observations()
        before = copy.deepcopy(facts)
        self.assertEqual(c.validate_plan("deploy " + SHA, facts),
                         "PLAN_VALIDATED_DEPLOY_DISABLED")
        self.assertEqual(facts, before)

    def test_command_exact_grammar(self):
        self.assertEqual(c.parse_command("deploy " + SHA), SHA)
        for value in (None, b"deploy " + SHA.encode(), "", "deploy", SHA,
                      "deploy " + SHA.upper(), "deploy " + SHA[:-1],
                      "deploy " + SHA + "0", "deploy  " + SHA,
                      " deploy " + SHA, "deploy\t" + SHA,
                      "deploy " + SHA + "\n", "deploy " + SHA + "\x00",
                      "deploy " + SHA + " deploy " + SHA,
                      "deploy " + SHA + "; id", "deploy $(id)",
                      "X=1 deploy " + SHA, "deploy `id`", "deploy /tmp/stage",
                      "deploy " + "а" * 40):  # Cyrillic lookalike
            with self.subTest(value=value), self.assertRaises(c.Rejected) as error:
                c.parse_command(value)
            self.assertEqual(str(error.exception), "PLAN_REJECTED")

    def test_each_required_observation_fails_closed(self):
        for key in c.REQUIRED_TRUE | c.REQUIRED_FALSE:
            expected = observations()[key]
            for value in (not expected, None, "unknown", int(expected), SECRET):
                facts = observations()
                facts[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(c.Rejected):
                    c.validate_plan("deploy " + SHA, facts)

    def test_schema_rejects_missing_extra_and_wrong_shapes(self):
        for key in c.FACT_KEYS:
            facts = observations()
            del facts[key]
            with self.subTest(key=key), self.assertRaises(c.Rejected):
                c.validate_plan("deploy " + SHA, facts)
        for facts in (None, [], {}, {**observations(), "command": SECRET}):
            with self.subTest(facts=type(facts)), self.assertRaises(c.Rejected):
                c.validate_plan("deploy " + SHA, facts)

    def test_wrong_main_fork_paths_health_and_runtime(self):
        failures = {
            "upstream_main": (OLD, SHA.upper(), None),
            "origin": ("https://example.invalid/fork.git", c.ORIGIN + "/", None),
            "previous_sha": ("unknown", SHA, None),
            "previous_stage": ("/tmp/stage", STAGE + "/../escape", STAGE + "/", None),
            "new_stage": (STAGE, "/root/FunPayFlow-stage-" + SHA[:12], None),
            "pid_cwd": ("/tmp", None), "working_directory": ("/tmp", None),
            "active": (False, "active", 1),
            "main_runner_count": (0, 2, True, "1", None),
            "main_pid": (0, 1, True, "123", None),
            "restart_count_before": (-1, True, None),
            "restart_count_after": (4, 2, True, None),
            "health_seconds": (0, 19, True, "20", None),
        }
        for key, values in failures.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(c.Rejected):
                    c.validate_plan("deploy " + SHA, {**observations(), key: value})

    def test_unit_must_match_exactly_and_have_two_paths(self):
        unit = c.expected_unit(STAGE)
        self.assertEqual(unit.count(STAGE), 2)
        for value in (None, "", unit.replace(STAGE, "/tmp", 1),
                      unit + f"# {STAGE}\n", unit + "ExecStartPost=/bin/false\n",
                      unit.replace("User=root", "User=other"),
                      unit.replace("funpayflow.main", "other.main"),
                      unit.replace(c.DATA, "/tmp/private"), unit + SECRET):
            with self.subTest(value=value), self.assertRaises(c.Rejected):
                c.validate_plan("deploy " + SHA, {**observations(), "unit": value})

    def test_valid_transport_still_refuses_deployment(self):
        for _ in range(2):  # Repeated requests cannot deploy either.
            out, err = self.invoke(environment())
            self.assertEqual(out, "DEPLOY_NOT_IMPLEMENTED\n")
            self.assertEqual(err, "")

    def invoke(self, env, argv=None, tty=False):
        out, err = io.StringIO(), io.StringIO()
        out.isatty = Mock(return_value=tty)
        err.isatty = Mock(return_value=False)
        with patch.dict(c.os.environ, env, clear=True), patch.object(c.sys, "stdout", out), \
                patch.object(c.sys, "stderr", err), \
                patch.object(c.sys, "stdin", Mock(isatty=Mock(return_value=False))):
            self.assertEqual(c.main([] if argv is None else argv), 2)
        return out.getvalue(), err.getvalue()

    def test_environment_and_cli_fail_without_echoing_input(self):
        for key in ("LD_PRELOAD", "PYTHONPATH", "GIT_CONFIG_COUNT", "SSH_AUTH_SOCK",
                    "SSH_TTY", "LANG", "TERM", "BASH_ENV", "FUNPAY_BOT_DATA_DIR"):
            out, err = self.invoke({**environment(), key: SECRET})
            self.assertEqual((out, err), ("REQUEST_REJECTED\n", ""))
        for key in environment():
            env = environment()
            del env[key]
            self.assertEqual(self.invoke(env), ("REQUEST_REJECTED\n", ""))
        for key in ("USER", "LOGNAME", "HOME", "SHELL", "PATH"):
            self.assertEqual(self.invoke({**environment(), key: SECRET}),
                             ("REQUEST_REJECTED\n", ""))
        for argv in (["deploy", SHA], ["--selfcheck"], [SECRET], ["--deploy", SHA]):
            self.assertEqual(self.invoke(environment(), argv), ("REQUEST_REJECTED\n", ""))
        self.assertEqual(self.invoke(environment(), tty=True), ("REQUEST_REJECTED\n", ""))
        self.assertEqual(self.invoke({**environment(), "SSH_ORIGINAL_COMMAND": SECRET}),
                         ("REQUEST_REJECTED\n", ""))

    def test_failure_and_interrupt_output_is_fixed(self):
        for error in (RuntimeError(SECRET), OSError(SECRET), KeyboardInterrupt(SECRET)):
            with patch.object(c, "validate_transport", side_effect=error):
                self.assertEqual(self.invoke(environment()), ("REQUEST_REJECTED\n", ""))

    def test_source_has_no_execution_or_io_implementation(self):
        imports = {node.names[0].name for node in ast.walk(ast.parse(SOURCE))
                   if isinstance(node, ast.Import)}
        self.assertEqual(imports, {"os", "re", "sys"})
        from_imports = {node.module for node in ast.walk(ast.parse(SOURCE))
                        if isinstance(node, ast.ImportFrom)}
        self.assertEqual(from_imports, {"__future__", "pathlib"})
        for node in ast.walk(ast.parse(SOURCE)):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else (
                    node.func.attr if isinstance(node.func, ast.Attribute) else "")
                self.assertNotIn(name, {"open", "exec", "eval", "run", "Popen",
                                        "unlink", "remove", "replace", "mkdir",
                                        "connect", "system", "fork", "write_bytes"})


if __name__ == "__main__":
    unittest.main()
