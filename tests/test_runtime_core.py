"""Offline durability and typed-event checks for the runtime core."""
import ast
import asyncio
from contextlib import closing
import json
from pathlib import Path
import queue
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime
import re
from types import SimpleNamespace
from unittest.mock import patch

import FunPayAPI

from funpay import FunPayClient, _EventQueueOverflow
from runtime_events import (ActionEvent, ActionKind, QueuedCriticalEvent,
                            ReviewCheckEvent, critical_snapshot, hydrate_critical)
from state import ReviewReceiptStore, StateError


def order_event(status="PAID", kind="NEW_ORDER"):
    order = SimpleNamespace(
        id="ABC12345", status=getattr(FunPayAPI.types.OrderStatuses, status),
        buyer_username="Example", buyer_id=7, chat_id=3,
        description="Sample product", subcategory_name="Sample",
        price=12.5, sum=None, currency="USD", amount=1, date=None,
        html="PRIVATE HTML MUST NOT PERSIST", message_body="PRIVATE BODY",
    )
    return SimpleNamespace(type=getattr(FunPayAPI.enums.EventTypes, kind), order=order)


class RuntimeCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.store = ReviewReceiptStore(self.path, Path(self.tmp.name) / "missing.json")
        self.store.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, capacity=2):
        client = object.__new__(FunPayClient)
        client.review_state = self.store
        client.event_queue = queue.Queue(maxsize=capacity)
        client._runner_publish_lock = threading.Lock()
        client._queued_backlog_ids = set()
        return client

    def test_one_active_class_and_key_method_parity(self):
        current = ast.parse(Path("funpay.py").read_text(encoding="utf-8"))
        classes = [n for n in current.body if isinstance(n, ast.ClassDef)
                   and n.name == "FunPayClient"]
        self.assertEqual(len(classes), 1)
        original = subprocess.check_output(
            ["git", "show", "HEAD:funpay.py"], text=True, encoding="utf-8")
        old_classes = [n for n in ast.parse(original).body if isinstance(n, ast.ClassDef)
                       and n.name == "FunPayClient"]
        self.assertEqual(len(old_classes), 2)
        old = {n.name: n for n in old_classes[-1].body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        new = {n.name: n for n in classes[0].body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in ("bump_lots", "send_message_once", "get_completed_withdrawals",
                     "get_runner_health", "start_runner", "stop_runner", "refresh_session"):
            self.assertEqual(ast.dump(old[name], include_attributes=False),
                             ast.dump(new[name], include_attributes=False), name)

    def test_persist_before_queue_and_recover_interrupted(self):
        snapshot = critical_snapshot(order_event())
        self.assertEqual(snapshot.event_id, "order:NEW_ORDER:ABC12345:PAID")
        self.assertTrue(self.store.insert_critical_event(
            snapshot.event_id, snapshot.event_type, snapshot.entity_id, snapshot.payload))
        self.assertFalse(self.store.insert_critical_event(
            snapshot.event_id, snapshot.event_type, snapshot.entity_id, snapshot.payload))
        restarted = ReviewReceiptStore(self.path, Path(self.tmp.name) / "missing.json")
        restarted.initialize()
        client = self.client()
        client.review_state = restarted
        self.assertEqual(client.enqueue_pending_critical_events(), 0)
        item = client.event_queue.get_nowait()
        self.assertIsInstance(item, QueuedCriticalEvent)
        self.assertEqual(item.event.order.price, 12.5)
        client.dequeued_critical_event(item.event_id)
        self.assertTrue(restarted.mark_critical_processing(item.event_id))
        self.assertTrue(restarted.claim_critical_modifying_action(item.event_id))
        restarted.recover_critical_events()  # simulated death after dequeue
        self.assertEqual(len(restarted.pending_critical_events(2)), 1)
        self.assertTrue(restarted.mark_critical_processing(item.event_id))
        self.assertFalse(restarted.claim_critical_modifying_action(item.event_id))
        self.assertTrue(restarted.mark_critical_done(item.event_id))
        self.assertEqual(restarted.pending_critical_events(2), [])
        self.assertFalse(restarted.insert_critical_event(
            snapshot.event_id, snapshot.event_type, snapshot.entity_id, snapshot.payload))

    def test_feedback_trigger_uses_no_body_and_no_false_identity(self):
        types = FunPayAPI.types.MessageTypes
        feedback = SimpleNamespace(
            type=FunPayAPI.enums.EventTypes.LAST_CHAT_MESSAGE_CHANGED,
            chat=SimpleNamespace(id=3, last_message_type=types.NEW_FEEDBACK,
                                 last_message_text="Review #ABC12345 private text"),
        )
        first, second = critical_snapshot(feedback), critical_snapshot(feedback)
        self.assertEqual(first.entity_id, "ABC12345")
        self.assertNotEqual(first.event_id, second.event_id)
        self.assertEqual(first.payload, {"order_id": "ABC12345"})
        self.assertIsInstance(hydrate_critical(first.event_type, first.entity_id, first.payload),
                              ReviewCheckEvent)
        self.store.insert_critical_event(first.event_id, first.event_type,
                                         first.entity_id, first.payload)
        with closing(sqlite3.connect(self.path)) as db:
            raw = db.execute("SELECT safe_payload FROM critical_event_backlog").fetchone()[0]
        self.assertNotIn("private text", raw)
        self.assertEqual(json.loads(raw), {"order_id": "ABC12345"})

    def test_order_snapshot_excludes_raw_html_and_message(self):
        snapshot = critical_snapshot(order_event())
        encoded = json.dumps(snapshot.payload)
        self.assertNotIn("PRIVATE HTML", encoded)
        self.assertNotIn("PRIVATE BODY", encoded)
        self.assertNotIn("html", snapshot.payload)
        self.assertNotIn("message_body", snapshot.payload)
        self.assertEqual(hydrate_critical(snapshot.event_type, snapshot.entity_id,
                                          snapshot.payload).order.price, 12.5)

    def test_malformed_row_isolated_and_following_valid_row_loaded(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("INSERT INTO critical_event_backlog "
                       "(event_id, event_type, entity_id, safe_payload, "
                       "created_at, updated_at, state) VALUES "
                       "('bad', 'NEW_ORDER', 'ABC12345', '{malformed', 1, 1, 'pending')")
            db.commit()
        snapshot = critical_snapshot(order_event())
        self.store.insert_critical_event(snapshot.event_id, snapshot.event_type,
                                         snapshot.entity_id, snapshot.payload)
        client = self.client()
        self.assertEqual(client.enqueue_pending_critical_events(), 1)
        self.assertEqual(client.event_queue.qsize(), 1)
        with closing(sqlite3.connect(self.path)) as db:
            state = db.execute("SELECT state FROM critical_event_backlog WHERE event_id='bad'").fetchone()[0]
        self.assertEqual(state, "ambiguous")

    def test_queue_full_retains_durable_event_and_existing_overflow(self):
        client = self.client(capacity=1)
        client.event_queue.put_nowait(object())
        snapshot = critical_snapshot(order_event())
        self.store.insert_critical_event(snapshot.event_id, snapshot.event_type,
                                         snapshot.entity_id, snapshot.payload)
        self.assertEqual(client.enqueue_pending_critical_events(), 0)
        self.assertEqual(len(self.store.pending_critical_events(2)), 1)
        client.runner = SimpleNamespace(
            get_updates=lambda: object(), parse_updates=lambda _: [order_event(status="CLOSED")])
        client._account_lock = threading.RLock()
        client._runner_stop = threading.Event()
        client._runner_health_lock = threading.Lock()
        client._runner_consecutive_errors = 0
        client._runner_last_failure_category = None
        client._runner_last_failure_type = None
        client._runner_health = "starting"
        with patch("funpay.logger.error", lambda *args: None):
            with self.assertRaises(_EventQueueOverflow):
                client.listen_events(is_cancelled=lambda: False)
        self.assertEqual(client.event_queue.qsize(), 1)
        self.assertEqual(len(self.store.pending_critical_events(3)), 2)

    def test_typed_actions_replace_control_markers(self):
        client = self.client()
        actions = client.describe_event(order_event())
        self.assertTrue(actions)
        self.assertTrue(all(isinstance(action, ActionEvent) for action in actions))
        self.assertIn(ActionKind.NOTIFY_ORDER, [action.kind for action in actions])
        self.assertNotIn("|||", repr(actions))
        feedback = SimpleNamespace(
            type=FunPayAPI.enums.EventTypes.LAST_CHAT_MESSAGE_CHANGED,
            chat=SimpleNamespace(id=3, last_message_type=FunPayAPI.types.MessageTypes.NEW_FEEDBACK,
                                 last_message_text="Review #ABC12345"),
        )
        review_actions = client.describe_event(feedback)
        self.assertEqual(review_actions,
                         [ActionEvent(ActionKind.REVIEW_CHECK_NOW, order_id="ABC12345")])

    def test_invalid_payload_rejected_and_backlog_capacity_guard(self):
        with self.assertRaises(StateError):
            self.store.insert_critical_event("x", "NEW_ORDER", "ABC12345",
                                             {"raw_response": "secret"})
        snapshot = critical_snapshot(order_event())
        with closing(sqlite3.connect(self.path)) as db:
            db.executemany(
                "INSERT INTO critical_event_backlog "
                "(event_id, event_type, entity_id, safe_payload, "
                "created_at, updated_at, state) VALUES "
                "(?, 'NEW_ORDER', 'ABC12345', '{}', 1, 1, 'pending')",
                [(f"synthetic:{i}",) for i in range(4096)],
            )
            db.commit()
        with self.assertRaises(StateError):
            self.store.insert_critical_event(snapshot.event_id, snapshot.event_type,
                                             snapshot.entity_id, snapshot.payload)

    def test_sqlite_unavailable_fails_before_ram_publish(self):
        snapshot = critical_snapshot(order_event())
        with patch.object(self.store, "_connect", side_effect=sqlite3.OperationalError("offline")):
            with self.assertRaises(StateError):
                self.store.insert_critical_event(snapshot.event_id, snapshot.event_type,
                                                 snapshot.entity_id, snapshot.payload)
        self.assertEqual(self.client().event_queue.qsize(), 0)

    def test_replayed_order_flows_through_consumer_once(self):
        source = Path("main.py").read_text(encoding="utf-8")
        wanted = {"_order_observation", "_order_history_fields",
                  "_schedule_closed_review_request", "notifications_loop"}
        nodes = [node for node in ast.parse(source).body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in wanted]
        async def no_autoresponse(*args):
            return False
        async def no_night_reply(*args):
            return None
        async def no_review_check(*args, **kwargs):
            return None
        from runtime_events import ActionKind
        runtime = {
            "asyncio": asyncio, "queue": queue, "time": __import__("time"), "re": re,
            "datetime": datetime, "FunPayAPI": FunPayAPI, "FunPayClient": FunPayClient,
            "Bot": object, "StateError": StateError, "ActionKind": ActionKind,
            "QueuedCriticalEvent": QueuedCriticalEvent, "ReviewCheckEvent": ReviewCheckEvent,
            "bot_settings": {"stats_currency": "USD", "review_request_enabled": False},
            "is_review_request_enabled": lambda: False,
            "_maybe_autorespond": no_autoresponse,
            "_send_night_mode_reply": no_night_reply,
            "_fetch_and_send_review": no_review_check,
            "_send_scheduled_review_request": no_review_check,
            "get_all_recipients": lambda: [],
            "_set_problem": lambda *args: None,
            "_clear_problem": lambda *args: None,
            "logger": SimpleNamespace(error=lambda *args: None, notify=lambda *args: None),
            "_strip_html": lambda value: value,
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), runtime)
        snapshot = critical_snapshot(order_event())
        self.store.insert_critical_event(snapshot.event_id, snapshot.event_type,
                                         snapshot.entity_id, snapshot.payload)
        client = self.client()
        client.account = SimpleNamespace(username="Example")
        client.start_runner = lambda: None
        client.runner_failed = lambda: False
        client.runner_stop_requested = lambda: not self.store.pending_critical_events(1) and (
            self._critical_state(snapshot.event_id) == "done")
        client.enqueue_pending_critical_events()
        asyncio.run(asyncio.wait_for(runtime["notifications_loop"](object(), client), 3))
        self.assertEqual(self._critical_state(snapshot.event_id), "done")
        self.assertEqual(self.store.pending_critical_events(1), [])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM order_status_observations "
                "WHERE order_id = 'ABC12345' AND status = 'PAID'").fetchone()[0], 1)

    def _critical_state(self, event_id):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT state FROM critical_event_backlog WHERE event_id = ?",
                              (event_id,)).fetchone()[0]


if __name__ == "__main__":
    unittest.main()
