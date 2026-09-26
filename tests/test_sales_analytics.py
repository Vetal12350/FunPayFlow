"""Synthetic SQLite sales analytics checks; no bot startup or network."""
import log_isolation
import copy
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from funpayflow import telegram as ui
from funpayflow.state import ReviewReceiptStore, StateError, normalize_reporting_currency


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
        self.previous_currency = ui.bot_settings["stats_currency"]
        self.previous_primary_currency = ui.bot_settings["primary_currency"]
        self.previous_filter = dict(ui._analytics_currency_filters)
        self.previous_settings = copy.deepcopy(ui.bot_settings)
        self.previous_effective = ui._effective_modules
        ui._runtime_client = SimpleNamespace(review_state=self.store)
        ui.bot_settings["modules"] = ui.profile_modules("all")
        ui.bot_settings["setup_completed"] = True
        ui.configure_module_runtime(fresh_install=False)
        ui.bot_settings["stats_currency"] = "USD"
        ui.bot_settings["primary_currency"] = "USD"
        ui._analytics_currency_filters.clear()
        self.midnight = int(datetime(2026, 9, 24).timestamp())
        self.now = self.midnight + 18 * 3600

    async def asyncTearDown(self):
        ui._runtime_client = self.previous_client
        ui.bot_settings["stats_currency"] = self.previous_currency
        ui.bot_settings["primary_currency"] = self.previous_primary_currency
        ui._analytics_currency_filters.clear()
        ui._analytics_currency_filters.update(self.previous_filter)
        ui.bot_settings.clear()
        ui.bot_settings.update(self.previous_settings)
        ui._effective_modules = self.previous_effective
        self.tmp.cleanup()

    def test_configured_currency_selects_money_without_changing_counts_or_rows(self):
        for order_id, amount, currency in (
            ("AA000001", 10, "USD"), ("AA000002", 20, " rub "),
            ("AA000003", 30, "eur"), ("AA000004", 40, "GBP"),
            ("AA000005", 50, "XYZ"), ("AA000006", 60, None),
        ):
            self.sale(order_id, -100, price=amount, currency=currency,
                      product=currency or "unknown")
        for index, currency in enumerate(("USD", "$", "RUB", "EUR"), 1):
            self.store.record_withdrawal_observation(
                f"withdraw{index}", index, currency, self.now - 50)
        with closing(sqlite3.connect(self.path)) as db:
            before = db.execute(
                "SELECT order_id, confirmed_currency, listed_price FROM orders ORDER BY order_id"
            ).fetchall()
        expected = {"USD": ("10", 2, "3"), "RUB": ("20", 1, "3"),
                    "EUR": ("30", 1, "4"), "GBP": ("40", 0, "0"),
                    "XYZ": ("50", 0, "0")}
        for currency, (turnover, withdrawals, withdrawal_amount) in expected.items():
            overview = self.store.get_sales_overview("today", self.now, currency=currency)
            legacy = self.store.get_legacy_statistics("today", self.now, currency=currency)
            self.assertEqual((overview["orders"], overview["usd_orders"],
                              overview["usd_turnover"]), (6, 1, turnover))
            self.assertEqual((legacy["orders_count"], legacy["withdrawals_count"],
                              str(legacy["usd_turnover"]),
                              str(legacy["usd_withdrawals"])),
                             (6, 4, turnover, withdrawal_amount))
            self.assertEqual(self.store.get_sales_records(currency)["most_expensive"]
                             ["usd_amount"], turnover)
            self.assertEqual(self.store.get_sales_top_products(
                "today", "turnover", self.now, currency)[0]["usd_turnover"], turnover)
        self.assertEqual(self.store.get_sales_reviews("today", self.now)["closed_orders"], 6)
        with closing(sqlite3.connect(self.path)) as db:
            after = db.execute(
                "SELECT order_id, confirmed_currency, listed_price FROM orders ORDER BY order_id"
            ).fetchall()
        self.assertEqual(before, after)

    def test_display_normalization_and_rejection(self):
        self.assertEqual(normalize_reporting_currency(" eur "), "EUR")
        for invalid in (None, "$", "EU1", "РУБ", "ßs", "USD'", "US D"):
            with self.assertRaises(StateError):
                normalize_reporting_currency(invalid)
        for currency, display in (("USD", "$"), ("EUR", "€"),
                                  ("GBP", "£"), ("UAH", "₴"),
                                  ("RUB", "RUB"), ("XYZ", "XYZ")):
            ui.bot_settings["stats_currency"] = currency
            ui.bot_settings["primary_currency"] = currency
            self.assertEqual(ui._analytics_money("12.5"), f"12.50 {display}")
            text = ui.format_stats_text("today", {"orders_count": 1, "reviews_count": 0,
                                                  "usd_turnover": 12.5,
                                                  "withdrawals_count": 0,
                                                  "usd_withdrawals": 0})
            self.assertIn(f"12.50 {display}", text)

    def test_money_period_comparison_uses_the_same_selected_currency(self):
        self.sale("AA000001", -100, price=20, currency="RUB")
        self.sale("AA000002", -100, price=900, currency="USD")
        self.sale("AA000003", -86400, price=10, currency="RUB")
        self.sale("AA000004", -86400, price=800, currency="USD")
        rub = self.store.get_sales_overview("today", self.now, "RUB")
        usd = self.store.get_sales_overview("today", self.now, "USD")
        self.assertEqual((rub["orders"], rub["previous_orders"]), (2, 2))
        self.assertEqual((rub["usd_turnover"], rub["previous_usd_turnover"]),
                         ("20", "10"))
        self.assertEqual((usd["usd_turnover"], usd["previous_usd_turnover"]),
                         ("900", "800"))
        self.assertEqual(self.store.get_sales_by_time("today", self.now, "RUB")
                         ["best_turnover_day"]["usd_turnover"], "20")

    async def test_telegram_uses_selected_currency_for_sales_query(self):
        self.sale("AA000001", -100, price=10, currency="USD")
        self.sale("AA000002", -100, price=20, currency="RUB")
        ui.bot_settings["stats_currency"] = "RUB"
        ui.bot_settings["primary_currency"] = "RUB"
        callback = Callback("ana_over:all")
        self.assertTrue(await ui._analytics_callback(callback, callback.data))
        self.assertEqual(len(callback.edits), 1)
        self.assertIn("20.00 RUB", callback.edits[0][0])
        self.assertNotIn("30.00 RUB", callback.edits[0][0])

    def test_historical_currencies_statistics_and_no_cross_currency_sum(self):
        self.sale("AA000001", -100, price=10, currency="USD")
        self.sale("AA000002", -100, price=20, currency=" rub ")
        self.sale("AA000003", -100, price=999, currency="UNKNOWN")
        self.store.record_withdrawal_observation("one", 3, "USD", self.now - 20)
        self.store.record_withdrawal_observation("two", 4, "RUB", self.now - 20)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT confirmed_currency FROM orders "
                                        "WHERE order_id = 'AA000002'").fetchone()[0], "rub")
            before = db.execute("SELECT order_id, confirmed_currency FROM orders "
                                "ORDER BY order_id").fetchall()
        self.assertEqual(self.store.get_sales_currencies(), ["RUB", "USD"])
        self.assertEqual(self.store.get_sales_turnover_by_currency("today", self.now),
                         {"RUB": "20", "USD": "10"})
        data = self.store.get_legacy_statistics("today", self.now)
        self.assertEqual(data["orders_count"], 3)
        self.assertEqual(data["turnover_by_currency"], {"USD": 10, "RUB": 20})
        self.assertEqual(data["withdrawals_by_currency"], {"USD": 3, "RUB": 4})
        text = ui.format_stats_text("today", data)
        for expected in ("10.00 $", "20.00 RUB", "3.00 $", "4.00 RUB"):
            self.assertIn(expected, text)
        self.assertNotIn("30.00 $", text)
        self.assertNotIn("7.00 $", text)
        with closing(sqlite3.connect(self.path)) as db:
            after = db.execute("SELECT order_id, confirmed_currency FROM orders "
                               "ORDER BY order_id").fetchall()
        self.assertEqual(before, after)

    def test_one_currency_statistics_remain_compact(self):
        self.sale("AA000001", -100, price=10, currency="USD")
        data = self.store.get_legacy_statistics("today", self.now)
        text = ui.format_stats_text("today", data)
        self.assertIn("💰 Оборот: <b>10.00 $</b>", text)
        self.assertNotIn("💰 Оборот:\n•", text)

    async def test_currency_filter_all_is_data_driven_and_never_ranks_mixed_money(self):
        self.sale("AA000001", -100, price=10, currency="USD", product="A", buyer="Buyer")
        self.sale("AA000002", -100, price=20, currency="RUB", product="B", buyer="Buyer")
        ui.bot_settings["primary_currency"] = "USD"
        opening = Callback("analytics")
        await ui._analytics_callback(opening, opening.data)
        buttons = {button.text: button.callback_data
                   for row in opening.edits[0][1]["reply_markup"].inline_keyboard
                   for button in row}
        self.assertEqual(buttons["✅ USD"], "ana_currency:USD:30d")
        self.assertEqual(buttons["RUB"], "ana_currency:RUB:30d")
        self.assertEqual(buttons["Все"], "ana_currency:all:30d")
        self.assertEqual(ui._action_feature("ana_currency:RUB:30d"), "sales_analytics")

        selected = Callback("ana_currency:RUB:all")
        await ui._analytics_callback(selected, selected.data)
        self.assertIn("20.00 RUB", selected.edits[0][0])
        self.assertNotIn("10.00 $", selected.edits[0][0])
        self.assertEqual(ui.bot_settings["primary_currency"], "USD")
        selected = Callback("ana_currency:all:all")
        await ui._analytics_callback(selected, selected.data)
        all_text = selected.edits[0][0]
        self.assertIn("Заказов: 2", all_text)
        self.assertIn("USD: 10.00 $", all_text)
        self.assertIn("RUB: 20.00 RUB", all_text)
        self.assertNotIn("Средний чек", all_text)
        self.assertNotIn("Самый дорогой", all_text)
        self.assertNotIn("30.00", all_text)
        self.assertEqual(ui.bot_settings["primary_currency"], "USD")
        today = Callback("ana_currency:all:today")
        await ui._analytics_callback(today, today.data)
        self.assertNotIn("Лучший день продаж", today.edits[0][0])
        top = Callback("ana_top:all:count")
        await ui._analytics_callback(top, top.data)
        self.assertNotIn("💰", top.edits[0][0])
        self.assertNotIn("ana_top:all:turnover", {
            button.callback_data for row in top.edits[0][1]["reply_markup"].inline_keyboard
            for button in row})
        records = Callback("ana_records")
        await ui._analytics_callback(records, records.data)
        self.assertNotIn("Самый дорогой", records.edits[0][0])
        self.assertNotIn("максимальным оборотом", records.edits[0][0])

    def test_best_day_only_for_multi_day_periods(self):
        self.sale("AA000001", -100, price=10, currency="USD")
        for period in ("today", "7d", "30d", "all"):
            data = self.store.get_sales_overview(period, self.now)
            text = ui._analytics_overview_text(period, data)
            self.assertEqual("Лучший день продаж" in text, period != "today")
            time_data = self.store.get_sales_by_time(period, self.now)
            time_text = ui._analytics_time_text(period, time_data)
            self.assertEqual("Лучший день продаж" in time_text, period != "today")

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
