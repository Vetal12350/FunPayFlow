"""Thank-you message for a good (4-5 star) review: once per order, offline only."""
import ast
import asyncio
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from funpayflow.state import ReviewReceiptStore
from funpayflow.telegram import review_thanks_text


def _utc(hour):
    return datetime(2026, 11, 17, hour, 0, tzinfo=timezone.utc)


class TextTests(unittest.TestCase):
    def test_wish_follows_moscow_time_of_day(self):
        # Moscow = UTC+3
        cases = {2: "утра", 5: "утра", 10: "дня", 16: "вечера", 21: "ночи", 1: "ночи"}
        for utc_hour, word in cases.items():
            with self.subTest(utc_hour=utc_hour):
                text = review_thanks_text("Olga", _utc(utc_hour))
                self.assertTrue(text.startswith("Olga, большое спасибо за отзыв!"))
                self.assertIn(word, text)


class SendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewReceiptStore(Path(self.tmp.name) / "state.sqlite3",
                                        Path(self.tmp.name) / "missing.json")
        self.store.initialize()
        self.sent, self.enabled, self.fail = [], True, None
        source = Path("src/funpayflow/main.py").read_text(encoding="utf-8")
        nodes = [n for n in ast.parse(source).body
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "_maybe_send_review_thanks"]
        self.ns = {
            "asyncio": asyncio, "time": time, "FunPayClient": object,
            "logger": SimpleNamespace(warning=lambda *a: None),
            "module_enabled": lambda _id: True,
            "is_review_thanks_enabled": lambda: self.enabled,
            "is_safe_mode_enabled": lambda: False,
            "restart_requested": lambda: False,
            "review_thanks_text": lambda buyer: f"thanks {buyer}",
            "_review_chat_context": lambda client, buyer: (77, "Seller"),
            "_audit_action": lambda *a: None,
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), self.ns)

        def send(chat_id, text, *, enabled_check=None):
            if self.fail:
                raise self.fail
            self.sent.append((chat_id, text))
            return True

        self.client = SimpleNamespace(review_state=self.store, send_review_request_once=send)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def thank(self, stars, order_id="ABC12345", buyer="Olga"):
        await self.ns["_maybe_send_review_thanks"](self.client, order_id, buyer, stars)

    async def test_good_review_thanked_exactly_once(self):
        await self.thank(5)
        await self.thank(5)          # replayed / edited review
        await self.thank(4)
        self.assertEqual(self.sent, [(77, "thanks Olga")])
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("SELECT state FROM review_thanks").fetchall(), [("sent",)])

    async def test_four_stars_is_good(self):
        await self.thank(4)
        self.assertEqual(len(self.sent), 1)

    async def test_low_or_unknown_rating_never_thanked(self):
        for stars in (1, 2, 3, None):
            await self.thank(stars)
        self.assertEqual(self.sent, [])

    async def test_switched_off_sends_nothing(self):
        self.enabled = False
        await self.thank(5)
        self.assertEqual(self.sent, [])

    async def test_unclear_send_is_never_retried(self):
        self.fail = TimeoutError()
        await self.thank(5)
        self.fail = None
        await self.thank(5)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
