"""Offline regression checks for a single long-lived FunPay polling producer."""
import log_isolation
import ast
import asyncio
from pathlib import Path
import queue
import ssl
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import FunPayAPI
import requests
import urllib3.exceptions

from funpayflow.funpay import FunPayClient, _AmbiguousRaiseOutcome


class RecoveryTests(unittest.TestCase):
    def client(self, outcomes):
        client = object.__new__(FunPayClient)
        client._account_lock = threading.RLock()
        client._runner_publish_lock = threading.Lock()
        client._runner_health_lock = threading.Lock()
        client._runner_stop = SimpleNamespace(wait=lambda _: False)
        client._runner_health = "starting"
        client._runner_incident_id = 0
        client._runner_retry_delay = None
        client._runner_consecutive_errors = 0
        client._runner_last_failure_category = None
        client._runner_last_failure_type = None
        client._runner_last_success_monotonic = None
        client._queued_backlog_ids = set()
        client.event_queue = queue.Queue(maxsize=4)
        calls = []
        pending = iter(outcomes)

        def get_updates():
            calls.append("poll")
            result = next(pending)
            if isinstance(result, BaseException):
                raise result
            return result

        client.runner = SimpleNamespace(get_updates=get_updates,
                                        parse_updates=lambda update: update)
        waits = []

        def wait(seconds):
            waits.append(seconds)
            return seconds == 15.0  # stop immediately after the first success

        client._runner_stop.wait = wait
        return client, calls, waits

    def test_transport_classifier_and_certificate_safety(self):
        errors = [requests.exceptions.ReadTimeout("PRIVATE"),
                  requests.exceptions.ConnectTimeout("PRIVATE"),
                  requests.exceptions.ConnectionError("PRIVATE"),
                  requests.exceptions.SSLError("PRIVATE"),
                  urllib3.exceptions.SSLError("PRIVATE"),
                  ConnectionResetError("PRIVATE")]
        for error in errors:
            with self.subTest(kind=type(error).__name__):
                self.assertEqual(FunPayClient._classify_runner_error(error), "RECOVERABLE")
        cert = requests.exceptions.SSLError(ssl.SSLCertVerificationError("PRIVATE"))
        self.assertEqual(FunPayClient._classify_runner_error(cert), "FATAL")
        wrapped_cert = requests.exceptions.ConnectionError(cert)
        self.assertEqual(FunPayClient._classify_runner_error(wrapped_cert), "FATAL")
        wrapped_tls = urllib3.exceptions.MaxRetryError(
            None, "runner/", urllib3.exceptions.SSLError("PRIVATE"))
        self.assertEqual(FunPayClient._classify_runner_error(wrapped_tls), "RECOVERABLE")
        diagnostic = requests.exceptions.SSLError(
            "[SSL: TLSV1_ALERT_INTERNAL_ERROR] PRIVATE_COOKIE=do-not-log")
        self.assertEqual(FunPayClient._safe_runner_error_detail(diagnostic),
                         "TLS_ALERT_INTERNAL_ERROR")
        self.assertEqual(FunPayClient._classify_runner_error(ValueError("PRIVATE")), "FATAL")
        self.assertEqual(FunPayClient._classify_runner_error(RuntimeError("PRIVATE")), "UNKNOWN")
        response = SimpleNamespace(status_code=403, request=SimpleNamespace(
            url="https://example.invalid/", headers={}, body=None))
        self.assertEqual(FunPayClient._classify_runner_error(
            FunPayAPI.exceptions.UnauthorizedError(response)), "AUTH")
        for status, category in ((401, "AUTH"), (408, "RECOVERABLE"),
                                 (429, "RECOVERABLE"), (500, "RECOVERABLE"),
                                 (502, "RECOVERABLE"), (503, "RECOVERABLE"),
                                 (504, "RECOVERABLE"), (501, "UNKNOWN")):
            response.status_code = status
            with self.subTest(status=status):
                self.assertEqual(FunPayClient._classify_runner_error(
                    FunPayAPI.exceptions.RequestFailedError(response)), category)

    def test_real_incident_pattern_recovers_without_new_runner(self):
        event = SimpleNamespace(type=object())
        client, calls, waits = self.client([
            requests.exceptions.ReadTimeout("PRIVATE"),
            requests.exceptions.ReadTimeout("PRIVATE"),
            requests.exceptions.ReadTimeout("PRIVATE"),
            requests.exceptions.SSLError("PRIVATE"), [event],
        ])
        already_queued = object()
        client.event_queue.put_nowait(already_queued)
        client.listen_events()
        self.assertEqual(len(calls), 5)
        self.assertEqual(waits, [5.0, 10.0, 20.0, 40.0, 15.0])
        self.assertIs(client.event_queue.get_nowait(), already_queued)
        self.assertIs(client.event_queue.get_nowait(), event)
        self.assertEqual(client.event_queue.qsize(), 0)
        self.assertEqual(client.get_runner_health()["state"], "healthy")
        self.assertEqual(client.get_runner_health()["consecutive_errors"], 0)
        self.assertEqual(client.get_runner_health()["incident_id"], 1)

    def test_long_outage_is_capped_and_does_not_exhaust_retry_budget(self):
        client, calls, waits = self.client(
            [requests.exceptions.SSLError("PRIVATE")] * 12 + [[]])
        client.listen_events()
        self.assertEqual(len(calls), 13)
        self.assertEqual(waits, [5.0, 10.0, 20.0, 40.0] + [60.0] * 8 + [15.0])
        self.assertEqual(client.get_runner_health()["state"], "healthy")
        self.assertEqual(client.get_runner_health()["incident_id"], 1)

    def test_repeated_reconnect_reuses_runner_and_resets_counter(self):
        outcomes = [requests.exceptions.SSLError("PRIVATE"), [],
                    requests.exceptions.ReadTimeout("PRIVATE"), [],
                    requests.exceptions.SSLError("PRIVATE"), []]
        client, calls, waits = self.client(outcomes)
        runner = client.runner
        successful_polls = 0

        def wait(seconds):
            nonlocal successful_polls
            waits.append(seconds)
            if seconds == 15.0:
                successful_polls += 1
            return successful_polls == 3

        client._runner_stop.wait = wait
        client.listen_events()
        self.assertEqual(len(calls), 6)
        self.assertIs(client.runner, runner)
        self.assertEqual(waits, [5.0, 15.0, 5.0, 15.0, 5.0, 15.0])
        self.assertEqual(client.get_runner_health()["incident_id"], 3)
        self.assertEqual(client.get_runner_health()["consecutive_errors"], 0)

    def test_outage_health_and_logs_contain_no_exception_text(self):
        client, _, waits = self.client([requests.exceptions.SSLError(
            "PRIVATE_COOKIE=do-not-log")])
        captured = []

        def wait(seconds):
            waits.append(seconds)
            health = client.get_runner_health()
            self.assertEqual(health["state"], "backoff")
            self.assertEqual(health["retry_delay"], 5.0)
            self.assertEqual(health["incident_id"], 1)
            return True

        client._runner_stop.wait = wait
        with patch("funpayflow.funpay.logger.warning", captured.append):
            client.listen_events()
        self.assertEqual(waits, [5.0])
        self.assertEqual(len(captured), 1)
        self.assertNotIn("PRIVATE_COOKIE", captured[0])

    def test_auth_does_not_retry(self):
        response = SimpleNamespace(status_code=403, request=SimpleNamespace(
            url="https://example.invalid/", headers={}, body=None))
        client, _, waits = self.client([FunPayAPI.exceptions.UnauthorizedError(response)])
        with self.assertRaises(FunPayAPI.exceptions.UnauthorizedError):
            client.listen_events()
        self.assertEqual(waits, [])
        self.assertEqual(client.get_runner_health()["last_failure_category"], "AUTH")

    def test_lots_raise_transport_failure_remains_ambiguous_without_retry(self):
        class Subcategory:
            type = object()
            category = SimpleNamespace(id=1, name="Synthetic", position=1)
            id = 2

        async def no_sleep(_seconds):
            return None

        for error_type in (requests.exceptions.ReadTimeout, requests.exceptions.SSLError):
            with self.subTest(error=error_type.__name__):
                client, _, _ = self.client([])
                client._raise_action_gate = threading.local()
                client.raise_time = {}
                calls = []

                def raise_lots(_game_id):
                    calls.append("raise")
                    self.assertTrue(client._raise_action_gate.is_allowed())
                    raise error_type("PRIVATE")

                client.account = SimpleNamespace(
                    get=lambda: None,
                    get_user=lambda _uid: SimpleNamespace(
                        get_sorted_lots=lambda _kind: {Subcategory(): object()}),
                    raise_lots=raise_lots)
                with patch("funpayflow.funpay.asyncio.sleep", no_sleep):
                    with self.assertRaises(_AmbiguousRaiseOutcome):
                        asyncio.run(client.bump_lots(1))
                self.assertEqual(calls, ["raise"])
                self.assertEqual(client.raise_time, {})

    def test_ambiguous_raise_does_not_prevent_independent_poll_recovery(self):
        class Subcategory:
            type = object()
            category = SimpleNamespace(id=1, name="Synthetic", position=1)
            id = 2

        client, polls, waits = self.client([
            requests.exceptions.ReadTimeout("PRIVATE"),
            requests.exceptions.ConnectTimeout("PRIVATE"),
            requests.exceptions.SSLError("PRIVATE"), []])
        client._raise_action_gate = threading.local()
        client.raise_time = {}
        raises = []

        def raise_lots(_game_id):
            raises.append("raise")
            self.assertTrue(client._raise_action_gate.is_allowed())
            raise requests.exceptions.ReadTimeout("PRIVATE")

        client.account = SimpleNamespace(
            get=lambda: None,
            get_user=lambda _uid: SimpleNamespace(
                get_sorted_lots=lambda _kind: {Subcategory(): object()}),
            raise_lots=raise_lots)

        async def no_sleep(_seconds):
            return None

        with patch("funpayflow.funpay.asyncio.sleep", no_sleep):
            with self.assertRaises(_AmbiguousRaiseOutcome):
                asyncio.run(client.bump_lots(1))
        client.listen_events()
        self.assertEqual(raises, ["raise"])
        self.assertEqual(len(polls), 4)
        self.assertEqual(waits, [5.0, 10.0, 20.0, 15.0])
        self.assertEqual(client.get_runner_health()["state"], "healthy")

    def test_successful_bump_does_not_affect_later_poll_tls_recovery(self):
        class Subcategory:
            type = object()
            category = SimpleNamespace(id=1, name="Synthetic", position=1)
            id = 2

        client, polls, waits = self.client([
            requests.exceptions.ReadTimeout("PRIVATE"), [],
            requests.exceptions.SSLError("PRIVATE"), []])
        client._raise_action_gate = threading.local()
        client.raise_time = {}
        raises = []
        client.account = SimpleNamespace(
            get=lambda: None,
            get_user=lambda _uid: SimpleNamespace(
                get_sorted_lots=lambda _kind: {Subcategory(): object()}),
            raise_lots=lambda game_id: raises.append(game_id))
        successful_polls = 0

        def wait(seconds):
            nonlocal successful_polls
            waits.append(seconds)
            if seconds == 15.0:
                successful_polls += 1
            return successful_polls == 2

        client._runner_stop.wait = wait

        async def no_sleep(_seconds):
            return None

        with patch("funpayflow.funpay.asyncio.sleep", no_sleep):
            succeeded, _, _ = asyncio.run(client.bump_lots(1))
        client.listen_events()
        self.assertTrue(succeeded)
        self.assertEqual(raises, [1])
        self.assertEqual(len(polls), 4)
        self.assertEqual(waits, [5.0, 15.0, 5.0, 15.0])
        self.assertEqual(client.get_runner_health()["state"], "healthy")

    def test_genuine_fatal_still_exits(self):
        client, _, waits = self.client([ValueError("PRIVATE")])
        with self.assertRaises(ValueError):
            client.listen_events()
        self.assertEqual(waits, [])
        self.assertEqual(client.get_runner_health()["state"], "failed")

    def test_stop_interrupts_backoff(self):
        client, calls, waits = self.client([requests.exceptions.SSLError("PRIVATE")])
        client._runner_stop.wait = lambda seconds: waits.append(seconds) or True
        client.listen_events()
        self.assertEqual(len(calls), 1)
        self.assertEqual(waits, [5.0])

    def test_start_runner_never_creates_second_producer(self):
        client, _, _ = self.client([])
        client._runner_start_lock = threading.Lock()
        client._runner_stop = threading.Event()
        client._runner_thread = None
        entered = threading.Event()

        def producer():
            entered.set()
            client._runner_stop.wait(1)

        client._run_runner = producer
        client.start_runner()
        self.assertTrue(entered.wait(1))
        original = client._runner_thread
        client.start_runner()
        self.assertIs(client._runner_thread, original)
        client._runner_stop.set()
        original.join(1)
        self.assertFalse(original.is_alive())

    def test_best_effort_notice_and_coalesced_transitions(self):
        source = Path("src/funpayflow/main.py").read_text(encoding="utf-8")
        names = {"_send_runtime_notice", "_notify_funpay_connection"}
        nodes = [node for node in ast.parse(source).body
                 if isinstance(node, ast.AsyncFunctionDef) and node.name in names]
        warnings = []
        namespace = {
            "asyncio": asyncio, "Bot": object,
            "get_all_recipients": lambda: [1],
            "get_reply_keyboard": lambda: None,
            "logger": SimpleNamespace(warning=warnings.append),
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), namespace)

        class FailingBot:
            calls = 0

            async def send_message(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("PRIVATE")

        async def run():
            bot = FailingBot()
            warned = recovered = 0
            for state, incident in (("healthy", 0), ("backoff", 1),
                                    ("backoff", 1), ("healthy", 1),
                                    ("healthy", 1), ("backoff", 2),
                                    ("healthy", 2)):
                warned, recovered = await namespace["_notify_funpay_connection"](
                    bot, {"state": state, "incident_id": incident}, warned, recovered)
            return bot.calls, warned, recovered

        self.assertEqual(asyncio.run(run()), (4, 2, 2))
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("PRIVATE", warnings[0])


if __name__ == "__main__":
    unittest.main()
