"""Synthetic SQLite sales analytics checks; no bot startup or network."""
import log_isolation
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import telegram as ui
from state import ReviewReceiptStore


class Callback:
    def __init__(self, action):
        self.data = action
        self.answers = []
        self.edits = []
        self.message = SimpleNamespace(edit_text=self.edit_text)

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class SalesAnalyticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.store = ReviewReceiptStore(self.path, Path(self.tmp.name) / "missing.json")
        self.store.initialize()
        self.previous_client = ui._runtime_client
        ui._runtime_client = SimpleNamespace(review_state=self.store)
        self.midnight = int(datetime(2026, 9, 24).timestamp())
        self.now = self.midnight + 18 * 3600

    async def asyncTearDown(self):
        ui._runtime_client = self.previous_client
        self.tmp.cleanup()

    def sale(self, order_id, offset, *, price=None, currency=None, product=None,
             subcategory=None, buyer_id=None, buyer=None):
        self.store.record_order_observation(
            order_id, "CLOSED", self.now + offset, listed_price=price,
            confirmed_currency=currency, product_description=product,
            subcategory_name=subcategory, buyer_id=buyer_id,
            buyer_username=buyer,
        )

    def seed(self):
        self.sale("AA000001", -100, price=100, currency="USD", product="Widget <A>",
                  subcategory="One", buyer_id=1, buyer="Alice")
        self.sale("AA000002", -200, price=999, product="Widget <A>",
                  subcategory="One", buyer_id=1, buyer="Alice")
        self.store.record_order_observation("AA000002", "REFUNDED", self.now - 150)
        self.sale("AA000003", -2 * 86400, price=25, currency="USD",
                  product="Gadget", subcategory="One", buyer_id=2, buyer="Bob")
        self.sale("AA000004", -6 * 86400, price=5, currency="USD",
                  product="Gadget", subcategory="Two", buyer="Alias")
        self.sale("AA000005", -8 * 86400, price=200, currency="USD",
                  product="Older", buyer_id=3, buyer="Carol")
        self.sale("AA000006", -31 * 86400, price=300, currency="USD",
                  product="Oldest", buyer_id=4, buyer="Dave")
        self.sale("AA000007", -86400, price=40, currency="USD",
                  product="Yesterday", buyer_id=5, buyer="Eve")
        self.store.record_review_observation("AA000001", self.now - 50)
        self.store.record_review_observation("AA000003", self.now - 2 * 86400 + 50)
        self.store.record_review_observation("AA000001", self.now - 10)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                # This old bot recorded '$' without verifying the API currency.
                db.execute("INSERT INTO legacy_stats VALUES ('order', 'AA000008', ?, '500', 'USD')",
                           (self.now - 2 * 86400,))
                db.execute("INSERT INTO orders (order_id, first_seen_at, last_seen_at, "
                           "current_status, closed_at_utc) VALUES ('AA000009', 1, 1, 'CLOSED', 1)")

    def test_periods_counts_money_and_sparse_history(self):
        self.seed()
        today = self.store.get_sales_overview("today", self.now)
        self.assertEqual(today["orders"], 1)  # A fully refunded order is not a sale.
        self.assertEqual(today["usd_orders"], 1)
        self.assertEqual(today["usd_turnover"], "100")
        self.assertEqual(today["previous_orders"], 1)
        self.assertEqual(today["previous_usd_turnover"], "40")
        self.assertEqual((today["refunds"], today["terminal_orders"]), (1, 2))
        self.assertEqual((today["buyers"], today["repeat_buyers"]), (1, 0))
        self.assertEqual(today["most_expensive"]["order_id"], "AA000001")
        self.assertEqual(today["reviews"], 1)
        self.assertEqual(today["reviewed_sales"], 1)
        week = self.store.get_sales_overview("7d", self.now)
        self.assertEqual((week["orders"], week["previous_orders"]), (5, 1))
        self.assertEqual(week["usd_turnover"], "170")
        self.assertEqual(week["previous_usd_turnover"], "200")
        month = self.store.get_sales_overview("30d", self.now)
        self.assertEqual((month["orders"], month["previous_orders"]), (6, 1))
        self.assertEqual(month["usd_turnover"], "370")
        self.assertEqual(month["previous_usd_turnover"], "300")
        all_time = self.store.get_sales_overview("all", self.now)
        self.assertEqual(all_time["orders"], 8)
        self.assertEqual(all_time["usd_turnover"], "670")
        self.assertIsNone(all_time["previous_orders"])

    def test_top_products_exact_identity_and_decimal_ranking(self):
        self.seed()
        by_count = self.store.get_sales_top_products("all", now_utc=self.now)
        widget = next(row for row in by_count if row["product"] == "Widget <A>")
        self.assertEqual((widget["orders"], widget["usd_turnover"]), (1, "100"))
        gadgets = [row for row in by_count if row["product"] == "Gadget"]
        self.assertEqual(len(gadgets), 2)  # Different subcategory stays separate.
        by_money = self.store.get_sales_top_products("all", "turnover", self.now)
        self.assertEqual(by_money[0]["product"], "Oldest")
        self.assertTrue(all(row["usd_turnover"] is not None for row in by_money))

    def test_buyers_reviews_days_records(self):
        self.seed()
        buyers = self.store.get_sales_buyers("today", self.now)
        self.assertEqual((buyers["unique"], buyers["repeat"]), (1, 0))
        self.assertEqual(buyers["top"][0], {"username": "Alice", "orders": 1})
        reviews = self.store.get_sales_reviews("today", self.now)
        self.assertEqual(reviews, {"reviews": 1, "closed_orders": 1,
                                   "reviewed_orders": 1, "average_rating": None,
                                   "rating_distribution": {}})
        by_time = self.store.get_sales_by_time("today", self.now)
        self.assertEqual(by_time["best_day"]["orders"], 1)
        self.assertEqual(by_time["best_day"]["day"], "2026-09-24")
        self.assertIn("hour", by_time)
        self.assertIn("weekday", by_time)
        records = self.store.get_sales_records()
        self.assertEqual(records["most_expensive"]["order_id"], "AA000006")
        self.assertEqual(records["top_product"]["product"], "Gadget")
        self.assertEqual(records["top_turnover_product"]["product"], "Oldest")
        self.assertEqual(records["top_buyer"]["orders"], 1)
        self.assertEqual(records["best_day"]["orders"], 2)
        self.assertEqual(records["best_turnover_day"]["usd_turnover"], "300")

    def test_zero_previous_and_unknown_currency_are_distinct(self):
        self.sale("AA000001", -100, price=7, currency="USD")
        current = self.store.get_sales_overview("today", self.now)
        self.assertEqual(current["previous_orders"], 0)
        self.assertEqual(current["previous_usd_turnover"], "0")
        text = ui._analytics_overview_text("today", current)
        self.assertIn("новые данные", text)
        self.assertNotIn("Infinity", text)
        self.store.record_order_observation("AA000002", "CLOSED", self.now - 200,
                                            listed_price=999, currency="USD")
        self.assertEqual(self.store.get_sales_overview("today", self.now)["usd_turnover"], "7")

    def test_confirmed_usd_decimal_average_and_unknown_previous(self):
        self.sale("AA000001", -100, price=0.10, currency="USD")
        self.sale("AA000002", -200, price=0.20, currency="USD")
        self.sale("AA000003", -300, price=999, currency="EUR")
        self.sale("AA000004", -86400, price=1000)  # Previous period, unknown currency.
        data = self.store.get_sales_overview("today", self.now)
        self.assertEqual((data["orders"], data["usd_orders"], data["usd_turnover"]),
                         (3, 2, "0.3"))
        self.assertIsNone(data["previous_usd_turnover"])
        self.assertIn("0.15 $", ui._analytics_overview_text("today", data))

    def test_review_archive_precedence_and_sparse_refund(self):
        self.sale("AA000001", -100, product="A")
        self.store.record_review_observation("AA000001", self.now - 10)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("INSERT INTO legacy_stats VALUES ('review', 'AA000001', ?, NULL, NULL)",
                           (self.now - 2 * 86400,))
                db.execute("INSERT INTO orders (order_id, first_seen_at, last_seen_at, "
                           "current_status) VALUES ('AA000002', 1, 1, 'REFUNDED')")
        self.assertEqual(self.store.get_sales_reviews("today", self.now)["reviews"], 0)
        self.assertEqual(self.store.get_sales_reviews("7d", self.now)["reviews"], 1)
        self.assertEqual(self.store.get_sales_overview("all", self.now)["refunds"], 1)
        self.assertEqual(self.store.get_sales_overview("today", self.now)["refunds"], 0)

    def test_html_escape_and_analytics_menu(self):
        self.seed()
        data = self.store.get_sales_overview("today", self.now)
        rendered = ui._analytics_overview_text("today", data)
        self.assertIn("Widget &lt;A&gt;", rendered)
        self.assertNotIn("Widget <A>", rendered)
        self.assertIn("50.0%", rendered)
        self.assertIn("📈 Аналитика", str(ui.get_main_keyboard(1)))
        self.assertNotIn("📈 Аналитика", str(ui.get_stats_keyboard()))
        top = ui._analytics_top_text("today", "count",
                                     self.store.get_sales_top_products("today", now_utc=self.now))
        self.assertIn("&lt;A&gt;", top)
        buyers = ui._analytics_buyers_text("today", self.store.get_sales_buyers("today", self.now))
        self.assertIn("Alice", buyers)

    async def test_telegram_analytics_callbacks_are_offline(self):
        self.seed()
        for action in ("analytics", "ana_over:today", "ana_top:today:count",
                       "ana_buy:today", "ana_reviews:today", "ana_time:today", "ana_records"):
            callback = Callback(action)
            self.assertTrue(await ui._analytics_callback(callback, action))
            self.assertEqual(len(callback.answers), 1, action)
            self.assertEqual(len(callback.edits), 1, action)
            self.assertEqual(callback.edits[0][1]["parse_mode"], "HTML")

    def test_large_sqlite_dataset_uses_bounded_top(self):
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                orders = [(f"Z{i:07d}", self.now - i, self.now - i, "CLOSED",
                           f"Product {i % 50}", "USD", "1.01") for i in range(10000)]
                db.executemany(
                    "INSERT INTO orders (order_id, first_seen_at, last_seen_at, "
                    "current_status, product_description, confirmed_currency, listed_price) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", orders)
                db.executemany(
                    "INSERT INTO order_status_observations VALUES (?, 'CLOSED', ?)",
                    [(row[0], row[1]) for row in orders])
        started = time.monotonic()
        top = self.store.get_sales_top_products("all", now_utc=self.now)
        overview = self.store.get_sales_overview("all", self.now)
        self.assertEqual(len(top), 10)
        self.assertEqual(overview["orders"], 10000)
        self.assertEqual(overview["usd_turnover"], "10100.00")
        self.assertLess(time.monotonic() - started, 10)


if __name__ == "__main__":
    unittest.main()
