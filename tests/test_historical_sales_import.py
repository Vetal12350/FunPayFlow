"""Offline official-sales import checks with small synthetic ZIP files."""
import log_isolation

import csv
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import telegram as ui
import import_funpay_sales as sales_import
from import_funpay_sales import REQUIRED, SalesImportError, import_zip, parse_row
from state import ReviewReceiptStore

REPORT_AT = int(datetime(2026, 9, 25, 1, tzinfo=timezone.utc).timestamp())


def make_row(order_id, status="closed", *, currency="USD", amount="10.25",
             summary="A lot", section="1", buyer="7", rating="5", hidden=""):
    row = {key: "" for key in REQUIRED}
    row["review_hidden"] = hidden
    row.update(order_uid=order_id, game_id="10", game_name="Game",
               section_type_id="digital", section_local_id=section,
               section_name="Section", buyer_user_id=buyer, buyer_name="Buyer",
               currency=currency, amount=amount,
               created_at="2026-09-24T20:50:00Z", paid_at="2026-09-24T21:30:00Z",
               closed_at="2026-09-24T21:35:00Z" if status == "closed" else "",
               refunded_at="2026-09-24T21:40:00Z" if status == "refunded" else "",
               partially_refunded_at="2026-09-24T21:42:00Z"
               if status == "partially_refunded" else "",
               status=status, role="seller", review_text="line one\nline two" if rating else "",
               review_rating=rating, review_reply="", type_data=json.dumps({
                   "fields": {"summary": {"value": {"ru": summary}},
                              "desc": {"value": {"ru": "long description\nignored"}}},
                   "amount": "2", "auto_delivery": False,
               }))
    return row


def make_zip(path, rows):
    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=sorted(REQUIRED | {"review_hidden"}))
    writer.writeheader()
    writer.writerows(rows)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("sales.csv", text.getvalue().encode("utf-8-sig"))


class HistoricalImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "state.sqlite3"
        self.zip = self.root / "sales.zip"
        self.store = ReviewReceiptStore(self.db, self.root / "missing.json")

    def tearDown(self):
        self.tmp.cleanup()

    def db_rows(self, table):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_stream_bom_multiline_duplicate_malformed_and_dry_run(self):
        valid = make_row("AA000001")
        broken = make_row("AA000002", amount="0")
        make_zip(self.zip, [valid, valid, broken])
        self.store.initialize()
        before = self.db.read_bytes()
        report = import_zip(self.zip, self.db, dry_run=True)
        self.assertEqual((report["rows"], report["unique_order_ids"],
                          report["duplicates"], report["parse_errors"]), (3, 1, 1, 1))
        self.assertEqual(report["predicted_inserts"], 1)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.db_rows("orders"), 0)
        self.assertEqual(parse_row(valid)["lot_summary"], "A lot")
        self.assertNotIn("long description", str(parse_row(valid)))

    def test_import_idempotency_money_status_reviews_and_backup(self):
        rows = [make_row("AA000001"), make_row("AA000002", currency="RUB"),
                make_row("AA000003", status="refunded", rating=""),
                make_row("AA000004", status="partially_refunded", rating=""),
                make_row("AA000005", status="paid", rating="")]
        rows[2]["closed_at"] = "2026-09-24T21:35:00Z"
        rows[3]["review_reply"] = "seller reply only"
        make_zip(self.zip, rows)
        first = import_zip(self.zip, self.db)
        second = import_zip(self.zip, self.db)
        self.assertEqual((first["predicted_inserts"], second["predicted_updates"],
                          second["unchanged"]), (5, 0, 5))
        self.assertTrue((self.root / second["backup"]).is_file())
        self.assertEqual(self.db_rows("orders"), 5)
        self.assertEqual(self.db_rows("review_observations"), 2)
        with closing(sqlite3.connect(self.db)) as connection:
            status = connection.execute(
                "SELECT current_status, official_status, paid_at_utc, closed_at_utc, "
                "partially_refunded_at_utc, amount, confirmed_currency, lot_summary, "
                "lot_amount, section_type_id FROM orders WHERE order_id='AA000004'",
            ).fetchone()
            self.assertEqual(status[0:2], ("PAID", "partially_refunded"))
            self.assertIsNotNone(status[4])
            self.assertEqual(status[5:10], ("10.25", "USD", "A lot", "2", "digital"))
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM order_status_observations WHERE order_id='AA000003'"
            ).fetchone()[0], 3)
            self.assertEqual(connection.execute(
                "SELECT rating, time_known FROM review_observations WHERE order_id='AA000001'"
            ).fetchone(), (5, 0))
        all_sales = self.store.get_sales_overview("all")
        self.assertEqual((all_sales["orders"], all_sales["usd_turnover"],
                          all_sales["refunds"], all_sales["partial_refunds"]),
                         (2, "10.25", 1, 1))
        self.assertEqual(self.store.get_legacy_statistics("week", REPORT_AT)["usd_turnover"],
                         10.25)
        self.assertIn("Частичный возврат", ui._order_card_screen(
            self.store.get_order_history("AA000004"), "all", 0)[0])

    def test_45_inserts_7_updates_then_identical_export_is_noop(self):
        self.store.initialize()
        rows = [make_row(f"C{i:07d}") for i in range(52)]
        for row in rows[:7]:
            self.store.record_order_observation(
                row["order_uid"], "CLOSED", REPORT_AT,
                product_description="Known live product")
        make_zip(self.zip, rows)
        preview = import_zip(self.zip, self.db, dry_run=True)
        self.assertEqual((preview["predicted_inserts"], preview["predicted_updates"],
                          preview["unchanged"]), (45, 7, 0))
        first = import_zip(self.zip, self.db)
        self.assertEqual((first["predicted_inserts"], first["predicted_updates"],
                          first["unchanged"]), (45, 7, 0))

        def business_snapshot():
            with closing(sqlite3.connect(self.db)) as connection:
                return tuple(tuple(connection.execute(
                    f"SELECT * FROM {table} ORDER BY {sort}").fetchall())
                    for table, sort in (("orders", "order_id"),
                                        ("order_status_observations", "order_id, status"),
                                        ("review_observations", "order_id")))

        before = business_snapshot()
        turnover = self.store.get_sales_overview("all")["usd_turnover"]
        with closing(sqlite3.connect(self.db)) as connection:
            with connection:
                connection.execute("CREATE TABLE write_probe (table_name TEXT)")
                for table in ("orders", "order_status_observations", "review_observations"):
                    connection.execute(
                        f"CREATE TRIGGER probe_{table} AFTER UPDATE ON {table} "
                        f"BEGIN INSERT INTO write_probe VALUES ('{table}'); END")
        dry_again = import_zip(self.zip, self.db, dry_run=True)
        self.assertEqual((dry_again["predicted_inserts"], dry_again["predicted_updates"],
                          dry_again["unchanged"]), (0, 0, 52))
        second = import_zip(self.zip, self.db)
        self.assertEqual((second["predicted_inserts"], second["predicted_updates"],
                          second["unchanged"]), (0, 0, 52))
        self.assertEqual(business_snapshot(), before)
        self.assertEqual(self.store.get_sales_overview("all")["usd_turnover"], turnover)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM write_probe").fetchone()[0], 0)

    def test_authoritative_change_counts_once_and_null_keeps_known_value(self):
        make_zip(self.zip, [make_row("AA000001")])
        import_zip(self.zip, self.db)
        changed = make_row("AA000001", amount="20.25", summary="")
        make_zip(self.zip, [changed])
        preview = import_zip(self.zip, self.db, dry_run=True)
        self.assertEqual((preview["predicted_updates"], preview["unchanged"]), (1, 0))
        result = import_zip(self.zip, self.db)
        self.assertEqual((result["predicted_updates"], result["unchanged"]), (1, 0))
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute(
                "SELECT amount, lot_summary FROM orders WHERE order_id='AA000001'").fetchone(),
                ("20.25", "A lot"))
        again = import_zip(self.zip, self.db, dry_run=True)
        self.assertEqual((again["predicted_updates"], again["unchanged"]), (0, 1))

    def test_missing_observation_is_updated_without_order_row_update(self):
        make_zip(self.zip, [make_row("AA000001")])
        import_zip(self.zip, self.db)
        with closing(sqlite3.connect(self.db)) as connection:
            with connection:
                connection.execute("DELETE FROM order_status_observations "
                                   "WHERE order_id='AA000001' AND status='CLOSED'")
                connection.execute("CREATE TABLE write_probe (n INTEGER)")
                connection.execute("CREATE TRIGGER probe_orders AFTER UPDATE ON orders "
                                   "BEGIN INSERT INTO write_probe VALUES (1); END")
        self.assertEqual(import_zip(self.zip, self.db, dry_run=True)["predicted_updates"], 1)
        self.assertEqual(import_zip(self.zip, self.db)["predicted_updates"], 1)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM write_probe").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM order_status_observations "
                "WHERE order_id='AA000001' AND status='CLOSED'").fetchone()[0], 1)

    def test_live_merge_and_archive_overlap(self):
        self.store.initialize()
        observed = int(datetime(2026, 9, 25, 0, tzinfo=timezone.utc).timestamp())
        self.store.record_order_observation("AA000001", "CLOSED", observed,
                                            product_description="Live product", chat_id=42,
                                            buyer_username="Live name")
        with closing(sqlite3.connect(self.db)) as connection:
            with connection:
                connection.execute("INSERT INTO legacy_stats VALUES "
                                   "('order', 'AA000001', ?, '999', 'USD')", (observed,))
        row = make_row("AA000001", amount="7.50")
        make_zip(self.zip, [row])
        report = import_zip(self.zip, self.db)
        self.assertEqual((report["predicted_updates"], report["conflicts"]), (1, 0))
        card = self.store.get_order_history("AA000001")
        self.assertEqual((card["amount"], card["buyer_id"], card["buyer_username"]),
                         ("7.50", 7, "Buyer"))
        self.assertEqual((card["product_description"], card["chat_id"]),
                         ("Live product", 42))
        self.assertEqual(self.store.get_sales_overview("all")["usd_turnover"], "7.50")
        self.assertEqual(self.store.get_legacy_statistics("week", REPORT_AT)["usd_turnover"],
                         7.50)

    def test_newer_live_status_conflict_does_not_restore_refunded_turnover(self):
        self.store.initialize()
        observed = int(datetime(2026, 9, 25, 0, tzinfo=timezone.utc).timestamp())
        self.store.record_order_observation("AA000001", "CLOSED", observed,
                                            product_description="Known live product")
        row = make_row("AA000001", status="refunded", amount="8.25")
        row["closed_at"] = "2026-09-24T21:35:00Z"
        make_zip(self.zip, [row])
        report = import_zip(self.zip, self.db)
        self.assertEqual(report["conflicts"], 1)
        card = self.store.get_order_history("AA000001")
        self.assertEqual((card["current_status"], card["official_status"]),
                         ("CLOSED", "refunded"))
        self.assertEqual(self.store.get_sales_overview("all")["usd_turnover"], "0")
        self.assertEqual(self.store.get_sales_overview("all")["refunds"], 1)
        self.assertEqual(self.store.get_legacy_statistics("week", REPORT_AT)["usd_turnover"], 0)

    def test_product_identity_buyer_id_rating_and_paid_time(self):
        one = make_row("AA000001", summary="Same", buyer="9", rating="1")
        two = make_row("AA000002", summary="Same", buyer="9", rating="5")
        three = make_row("AA000003", summary="Same", section="2", buyer="10",
                         rating="", currency="RUB")
        four = make_row("AA000004", summary="", buyer="11", rating="")
        make_zip(self.zip, [one, two, three, four])
        import_zip(self.zip, self.db)
        products = self.store.get_sales_top_products("all")
        self.assertEqual(sorted(row["orders"] for row in products), [1, 2])
        buyers = self.store.get_sales_buyers("all")
        self.assertEqual((buyers["unique"], buyers["repeat"]), (3, 1))
        reviews = self.store.get_sales_reviews("all")
        self.assertEqual((reviews["reviews"], reviews["average_rating"]), (2, 3.0))
        self.assertEqual(reviews["rating_distribution"], {1: 1, 5: 1})
        paid = int(datetime(2026, 9, 24, 21, 30, tzinfo=timezone.utc).timestamp())
        window = self.store.get_sales_overview("today", paid + 120)
        self.assertEqual(window["orders"], 4)
        by_time = self.store.get_sales_by_time("all")
        self.assertEqual(by_time["best_day"]["day"],
                         datetime.fromtimestamp(paid).date().isoformat())
        self.assertEqual(by_time["hour"]["hour"], datetime.fromtimestamp(paid).hour)
        self.assertEqual(by_time["weekday"]["weekday"],
                         (datetime.fromtimestamp(paid).weekday() + 1) % 7)
        self.assertIn("Средняя оценка", ui._analytics_reviews_text("all", reviews))

    def test_live_rating_change_keeps_one_observation(self):
        self.store.initialize()
        self.store.record_review_observation("AA000001", 100, 2)
        self.store.record_review_observation("AA000001", 200, 5)
        self.assertEqual(self.db_rows("review_observations"), 1)
        self.assertEqual(self.store.get_sales_reviews("all")["rating_distribution"], {5: 1})

    def test_hidden_reviews_persist_but_visible_analytics_exclude_them(self):
        rows = [make_row(f"D{i:07d}", rating="1", hidden="1" if i < 50 else "0")
                for i in range(52)]
        rows.append(make_row("E0000001", rating="5", hidden="1"))
        rows.append(make_row("E0000002", rating="", hidden=""))
        make_zip(self.zip, rows)
        import_zip(self.zip, self.db)
        self.assertEqual(self.db_rows("review_observations"), 53)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM review_observations WHERE review_hidden = 1"
            ).fetchone()[0], 51)
        reviews = self.store.get_sales_reviews("all")
        self.assertEqual((reviews["reviews"], reviews["closed_orders"],
                          reviews["reviewed_orders"]), (2, 54, 2))
        self.assertEqual(reviews["rating_distribution"], {1: 2})
        self.assertEqual(reviews["average_rating"], 1.0)
        text = ui._analytics_reviews_text("all", reviews)
        self.assertIn("Получено отзывов: 2", text)
        self.assertIn("1⭐ 2", text)
        self.assertIn("5⭐ 0", text)
        self.assertIn("3.7%", text)  # 2 visible reviews / 54 closed orders.
        self.assertEqual(self.store.get_sales_overview("all")["reviews"], 2)

    def test_null_legacy_visibility_keeps_review_and_hidden_archive_overlap_excluded(self):
        self.store.initialize()
        self.store.record_order_observation("AA000001", "CLOSED", REPORT_AT - 100)
        self.store.record_review_observation("AA000001", REPORT_AT - 90, 3)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertIsNone(connection.execute(
                "SELECT review_hidden FROM review_observations WHERE order_id='AA000001'"
            ).fetchone()[0])
        reviews = self.store.get_sales_reviews("all")
        self.assertEqual((reviews["reviews"], reviews["reviewed_orders"],
                          reviews["rating_distribution"]), (1, 1, {3: 1}))
        self.assertEqual(self.store.get_legacy_statistics("week", REPORT_AT)["reviews_count"], 1)

        make_zip(self.zip, [make_row("AA000002", rating="1", hidden="1")])
        import_zip(self.zip, self.db)
        with closing(sqlite3.connect(self.db)) as connection:
            with connection:
                connection.execute("INSERT INTO legacy_stats "
                                   "(kind, record_key, recorded_at) VALUES ('review', 'AA000002', ?)",
                                   (REPORT_AT - 80,))
        self.assertEqual(self.store.get_sales_reviews("all")["reviews"], 1)
        self.assertEqual(self.store.get_legacy_statistics("week", REPORT_AT)["reviews_count"], 1)

    def test_review_visibility_column_migrates_old_observation_as_unknown(self):
        with closing(sqlite3.connect(self.db)) as connection:
            with connection:
                connection.execute("CREATE TABLE review_observations ("
                                   "order_id TEXT PRIMARY KEY, observed_at INTEGER NOT NULL, "
                                   "rating INTEGER, time_known INTEGER NOT NULL DEFAULT 1)")
                connection.execute("INSERT INTO review_observations "
                                   "(order_id, observed_at, rating) VALUES ('AA000001', ?, 4)",
                                   (REPORT_AT - 90,))
        self.store.initialize()
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute(
                "SELECT rating, review_hidden FROM review_observations "
                "WHERE order_id='AA000001'").fetchone(), (4, None))
        self.assertEqual(self.store.get_sales_reviews("all")["rating_distribution"], {4: 1})

    def test_hidden_flag_preserved_without_review_body_or_rating(self):
        make_zip(self.zip, [make_row("AA000001", rating="", hidden="1")])
        import_zip(self.zip, self.db)
        self.assertEqual(self.db_rows("review_observations"), 1)
        self.assertEqual(self.store.get_sales_reviews("all")["reviews"], 0)
        make_zip(self.zip, [make_row("AA000001", rating="", hidden="0")])
        self.assertEqual(import_zip(self.zip, self.db)["predicted_updates"], 1)
        self.assertEqual(self.store.get_sales_reviews("all")["reviews"], 1)

    def test_new_official_visibility_updates_without_duplicate_review(self):
        make_zip(self.zip, [make_row("AA000001", rating="1", hidden="0")])
        import_zip(self.zip, self.db)
        self.assertEqual(self.store.get_sales_reviews("all")["reviews"], 1)
        make_zip(self.zip, [make_row("AA000001", rating="1", hidden="1")])
        preview = import_zip(self.zip, self.db, dry_run=True)
        self.assertEqual((preview["predicted_updates"], preview["unchanged"]), (1, 0))
        result = import_zip(self.zip, self.db)
        self.assertEqual((result["predicted_updates"], result["unchanged"]), (1, 0))
        self.assertEqual(self.db_rows("review_observations"), 1)
        self.assertEqual(self.store.get_sales_reviews("all")["reviews"], 0)
        self.assertEqual(self.store.get_sales_reviews("all")["rating_distribution"], {})
        self.assertEqual(import_zip(self.zip, self.db, dry_run=True)["unchanged"], 1)

        make_zip(self.zip, [make_row("AA000001", rating="1", hidden="")])
        self.assertEqual(import_zip(self.zip, self.db)["unchanged"], 1)
        self.assertEqual(self.store.get_sales_reviews("all")["reviews"], 0)
        make_zip(self.zip, [make_row("AA000001", rating="1", hidden="0")])
        self.assertEqual(import_zip(self.zip, self.db)["predicted_updates"], 1)
        self.assertEqual(self.store.get_sales_reviews("all")["reviews"], 1)
        self.assertEqual(self.db_rows("review_observations"), 1)
        make_zip(self.zip, [make_row("AA000001", rating="", hidden="1")])
        self.assertEqual(import_zip(self.zip, self.db)["predicted_updates"], 1)
        self.assertEqual(self.store.get_sales_reviews("all")["reviews"], 0)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute(
                "SELECT rating, review_hidden FROM review_observations "
                "WHERE order_id='AA000001'").fetchone(), (1, 1))

    def test_import_crosses_batch_boundary(self):
        make_zip(self.zip, [make_row(f"B{i:07d}", rating="") for i in range(1001)])
        report = import_zip(self.zip, self.db)
        self.assertEqual(report["predicted_inserts"], 1001)
        self.assertEqual(self.db_rows("orders"), 1001)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone(), ("ok",))

    def test_failed_second_batch_keeps_usable_db_and_backup(self):
        self.store.initialize()
        make_zip(self.zip, [make_row(f"B{i:07d}", rating="") for i in range(1001)])
        original = sales_import._apply_batch
        calls = 0

        def fail_second(connection, rows):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic failure")
            return original(connection, rows)

        with patch.object(sales_import, "_apply_batch", side_effect=fail_second):
            with self.assertRaises(SalesImportError) as caught:
                import_zip(self.zip, self.db)
        self.assertTrue((self.root / caught.exception.backup_name).is_file())
        self.assertEqual(self.db_rows("orders"), 1000)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone(), ("ok",))
        with closing(sqlite3.connect(self.root / caught.exception.backup_name)) as backup:
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
