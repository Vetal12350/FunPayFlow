"""Offline checks for persistent order history and its Telegram screens."""
import log_isolation
import ast
import asyncio
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import FunPayAPI
from funpayflow import telegram as ui
from funpayflow.state import ReviewReceiptStore, StateError


def load_event_adapters():
    source = Path("src/funpayflow/main.py").read_text(encoding="utf-8")
    names = {"_order_observation", "_order_history_fields"}
    nodes = [node for node in ast.parse(source).body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    scope = {"FunPayAPI": FunPayAPI, "StateError": StateError, "re": re,
             "datetime": datetime}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), scope)
    return scope


class Callback:
    def __init__(self, action):
        self.data = action
        self.from_user = SimpleNamespace(id=1)
        self.answers = []
        self.edits = []
        self.message = SimpleNamespace(edit_text=self.edit_text)

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class OrderHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.store = ReviewReceiptStore(self.path, Path(self.tmp.name) / "missing.json")
        self.store.initialize()
        self.old_client = ui._runtime_client
        ui._runtime_client = SimpleNamespace(review_state=self.store)
        self.old_auth = ui.is_authorized
        ui.is_authorized = lambda user_id: True

    async def asyncTearDown(self):
        ui._runtime_client = self.old_client
        ui.is_authorized = self.old_auth
        self.tmp.cleanup()

    def test_event_adapter_and_persistent_enrichment(self):
        adapters = load_event_adapters()
        order = SimpleNamespace(
            id="ABC12345", status=FunPayAPI.types.OrderStatuses.PAID,
            description="Widget <a> 2 шт.", buyer_username="Alice", buyer_id=7,
            amount=2, price=12.5, subcategory_name="Goods", date=datetime(2025, 1, 2, 3, 4),
        )
        event = SimpleNamespace(type=FunPayAPI.enums.EventTypes.NEW_ORDER, order=order)
        identity = adapters["_order_observation"](event)
        fields = adapters["_order_history_fields"](order)
        self.assertEqual(identity, ("ABC12345", "PAID"))
        self.assertEqual(fields["quantity"], 2)
        self.assertNotIn("chat_id", fields)
        self.assertNotIn("currency", fields)
        self.assertFalse(self.store.record_order_observation(*identity, 100, **fields))
        self.assertFalse(self.store.record_order_observation(*identity, 101))
        order.status = FunPayAPI.types.OrderStatuses.CLOSED
        event.type = FunPayAPI.enums.EventTypes.ORDER_STATUS_CHANGED
        self.assertEqual(adapters["_order_observation"](event)[1], "CLOSED")
        self.assertTrue(self.store.record_order_observation("ABC12345", "CLOSED", 200))
        self.assertFalse(self.store.record_order_observation("ABC12345", "CLOSED", 201))
        self.store.record_order_observation("ABC12345", "REFUNDED", 300)
        self.store.record_order_observation("ABC12345", "REFUNDED", 301)
        reopened = ReviewReceiptStore(self.path, self.store.legacy_stats_path)
        reopened.initialize()
        card = reopened.get_order_history("ABC12345")
        self.assertEqual(card["current_status"], "REFUNDED")
        self.assertEqual(card["buyer_username"], "Alice")
        self.assertEqual(card["buyer_id"], 7)
        self.assertEqual(card["product_description"], "Widget <a> 2 шт.")
        self.assertEqual(card["quantity"], 2)
        self.assertEqual(card["subcategory_name"], "Goods")
        self.assertEqual(card["listed_price"], "12.5")
        self.assertEqual(card["funpay_order_date_local"], "2025-01-02T03:04:00")
        self.assertIsNone(card["confirmed_currency"])
        self.assertIsNone(card["chat_id"])
        self.assertEqual(card["observed_statuses"], {"PAID": 100, "CLOSED": 200, "REFUNDED": 300})
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM order_status_observations").fetchone()[0], 3)

    def test_sparse_old_schema_migrates_without_invented_history(self):
        old_path = Path(self.tmp.name) / "old.sqlite3"
        with closing(sqlite3.connect(old_path)) as db:
            with db:
                db.execute("CREATE TABLE orders (order_id TEXT PRIMARY KEY, first_seen_at INTEGER NOT NULL, "
                           "last_seen_at INTEGER NOT NULL, current_status TEXT NOT NULL)")
                db.execute("INSERT INTO orders VALUES ('ABC12345', 100, 200, 'CLOSED')")
        old = ReviewReceiptStore(old_path, Path(self.tmp.name) / "no-archive.json")
        old.initialize()
        card = old.get_order_history("ABC12345")
        self.assertIsNone(card["buyer_username"])
        self.assertIsNone(card["listed_price"])
        self.assertEqual(card["observed_statuses"], {})
        self.assertEqual(old.get_order_statistics(300)["all_time"]["orders"], 1)

    def test_quantity_is_not_inferred_from_library_default(self):
        adapter = load_event_adapters()["_order_history_fields"]
        order = SimpleNamespace(description="Widget", amount=1, price=0)
        self.assertIsNone(adapter(order)["quantity"])
        self.assertEqual(adapter(order)["listed_price"], 0)

    def test_existing_statistics_keep_closed_turnover_semantics(self):
        from time import time
        observed = int(time())
        self.store.record_order_observation("ABC12345", "PAID", observed,
                                            listed_price=12.5)
        before = self.store.get_legacy_statistics("today", observed + 1)
        self.assertEqual(before["orders_count"], 0)
        self.store.record_order_observation("ABC12345", "CLOSED", observed,
                                            amount=12.5, currency="USD")
        after = self.store.get_legacy_statistics("today", observed + 1)
        self.assertEqual(after["orders_count"], 1)
        self.assertEqual(str(after["usd_turnover"]), "12.5")

    def test_pagination_and_filters(self):
        for i in range(23):
            self.store.record_order_observation(
                f"AB{i:06d}", "REFUNDED" if i % 3 == 0 else "CLOSED" if i % 3 == 1 else "PAID",
                100 + i,
            )
        count, first = self.store.list_order_history(page=0)
        self.assertEqual((count, len(first)), (23, 10))
        self.assertEqual(first[0]["order_id"], "AB000022")
        self.assertEqual(len(self.store.list_order_history(page=2)[1]), 3)
        self.assertEqual(self.store.list_order_history("CLOSED")[0], 8)
        self.assertEqual(self.store.list_order_history("REFUNDED")[0], 8)
        text, markup = ui._order_list_screen("all", 0, count, first)
        self.assertIn("1/3", text)
        data = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn("ord_list:all:1", data)
        self.assertTrue(all(len(item) <= 64 for item in data))

    def test_card_escapes_unknowns_without_chat_button(self):
        self.store.record_order_observation("ABC12345", "PAID", 100,
                                            buyer_username="<buyer>",
                                            product_description="<item>&", listed_price=12)
        card = self.store.get_order_history("ABC12345")
        text, markup = ui._order_card_screen(card, "all", 0)
        self.assertIn("&lt;buyer&gt;", text)
        self.assertIn("&lt;item&gt;&amp;", text)
        self.assertNotIn("Количество:", text)
        self.assertNotIn("Чат", str(markup))
        self.assertNotIn("Завершён замечен", text)
        self.assertIn("Цена: 12", text)
        self.assertNotIn("12 $", text)
        self.store.record_order_observation("ABC12345", "PAID", 101, chat_id=7)
        card = self.store.get_order_history("ABC12345")
        _, markup = ui._order_card_screen(card, "all", 0)
        self.assertNotIn("ord_chat:", str(markup))


    async def test_menu_and_card_callbacks(self):
        self.assertIn("📦 Заказы", str(ui.get_main_keyboard(1)))
        callback = Callback("orders")
        await ui._orders_callback(callback, callback.data, 1)
        self.assertEqual(len(callback.answers), 1)
        self.assertIn("Заказы", callback.edits[0][0])
        self.store.record_order_observation("ABC12345", "CLOSED", 100)
        callback = Callback("ord_open:ABC12345:closed:0")
        await ui._orders_callback(callback, callback.data, 1)
        self.assertEqual(len(callback.answers), 1)
        self.assertIn("Заказ #ABC12345", callback.edits[0][0])


if __name__ == "__main__":
    unittest.main()
