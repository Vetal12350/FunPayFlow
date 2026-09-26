"""Explicit, offline import of an official FunPay sales ZIP into state.sqlite3.

Rollback: stop the bot, preserve the failed DB and its -wal/-shm files, then
restore the reported pre-sales-import .bak over the DB before restarting.
"""

import argparse
import csv
import io
import json
import re
import sqlite3
import sys
import unicodedata
import uuid
import zipfile
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .state import DEFAULT_DB_PATH, ReviewReceiptStore


REQUIRED = {"order_uid", "game_id", "game_name", "section_type_id",
            "section_local_id", "section_name", "buyer_user_id", "buyer_name",
            "currency", "amount", "created_at", "paid_at", "closed_at",
            "refunded_at", "partially_refunded_at", "status", "role",
            "review_text", "review_rating", "review_reply", "type_data"}
STATUSES = {"paid": "PAID", "closed": "CLOSED", "refunded": "REFUNDED",
            "partially_refunded": "PAID"}
BATCH_SIZE = 1000


class SalesImportError(RuntimeError):
    def __init__(self, error_type, backup_name):
        self.error_type = error_type
        self.backup_name = backup_name
        super().__init__("Import failed after backup creation.")


def _timestamp(value):
    if not value:
        return None
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None or date.utcoffset().total_seconds() != 0:
        raise ValueError("Timestamp is not UTC.")
    return int(date.timestamp())


def _optional_int(value):
    if not value:
        return None
    if not value.isdecimal():
        raise ValueError("Invalid identifier.")
    return int(value)


def _optional_code(value):
    if not value:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("Invalid section identifier.")
    return value


def _clean(value, limit=255):
    if not value:
        return None
    value = unicodedata.normalize("NFC", value).strip()
    if len(value) > limit:
        raise ValueError("Field too long.")
    return value or None


def parse_row(raw):
    """Return only bounded, analytics-useful fields; discard review and lot bodies."""
    if None in raw or not re.fullmatch(r"[A-Z0-9]{8}", raw["order_uid"] or ""):
        raise ValueError("Invalid row or order ID.")
    if raw["role"] != "seller" or raw["status"] not in STATUSES:
        raise ValueError("Unsupported role or status.")
    try:
        amount = Decimal(raw["amount"])
    except (InvalidOperation, TypeError):
        raise ValueError("Invalid amount.") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Invalid amount.")
    currency = (raw["currency"] or "").upper().strip()
    if not re.fullmatch(r"[A-Z]{2,8}", currency):
        raise ValueError("Invalid currency.")
    timestamps = {key: _timestamp(raw[key]) for key in
                  ("created_at", "paid_at", "closed_at", "refunded_at",
                   "partially_refunded_at")}
    if not any(value is not None for value in timestamps.values()):
        raise ValueError("No official timestamp.")
    lot = json.loads(raw["type_data"]) if raw["type_data"] else {}
    if not isinstance(lot, dict):
        raise ValueError("Invalid lot snapshot.")
    fields = lot.get("fields") or {}
    if not isinstance(fields, dict):
        raise ValueError("Invalid lot fields.")
    summary = fields.get("summary")
    if isinstance(summary, dict):
        value = summary.get("value")
        summary = (value.get("ru") or value.get("en")) if isinstance(value, dict) else None
    if summary is not None and not isinstance(summary, str):
        summary = None
    summary = _clean(" ".join(summary.split()), 4000) if summary else None
    lot_amount = lot.get("amount")
    if lot_amount is not None:
        try:
            lot_amount = Decimal(str(lot_amount))
            lot_amount = str(lot_amount) if lot_amount.is_finite() and lot_amount >= 0 else None
        except InvalidOperation:
            lot_amount = None
    rating_text = (raw["review_rating"] or "").strip()
    rating = int(rating_text) if rating_text in {"1", "2", "3", "4", "5"} else None
    hidden_text = (raw.get("review_hidden") or "").strip()
    if hidden_text not in {"", "0", "1"}:
        raise ValueError("Invalid review visibility flag.")
    review_hidden = int(hidden_text) if hidden_text else None
    has_review = bool((raw["review_text"] or "").strip() or rating is not None
                      or review_hidden == 1)
    return {
        "order_id": raw["order_uid"], "status": raw["status"],
        "canonical": STATUSES[raw["status"]], "amount": str(amount),
        "currency": currency, "buyer_id": _optional_int(raw["buyer_user_id"]),
        "buyer_username": _clean(raw["buyer_name"]),
        "game_id": _optional_int(raw["game_id"]), "game_name": _clean(raw["game_name"]),
        "section_type_id": _optional_code(raw["section_type_id"]),
        "section_local_id": _optional_int(raw["section_local_id"]),
        "section_name": _clean(raw["section_name"]),
        "lot_summary": summary, "lot_amount": lot_amount,
        "rating": rating, "review_hidden": review_hidden,
        "has_review": has_review, **timestamps,
    }


def _backup_database(path):
    if not path.exists():
        return None
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.pre-sales-import-{suffix}-{uuid.uuid4().hex[:8]}.bak")
    if backup.exists():
        raise FileExistsError("Backup target already exists.")
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as destination:
        source.backup(destination)
    return backup


_ORDER_COLUMNS = (
    "order_id", "first_seen_at", "last_seen_at", "current_status", "closed_at_utc",
    "amount", "currency", "buyer_username", "buyer_id", "product_description",
    "subcategory_name", "listed_price", "confirmed_currency", "official_status",
    "created_at_utc", "paid_at_utc", "refunded_at_utc", "partially_refunded_at_utc",
    "game_id", "game_name", "section_type_id", "section_local_id", "section_name",
    "lot_summary", "lot_amount",
)
_ORDER_UPDATES = (
    ("first_seen_at", "MIN(orders.first_seen_at, excluded.first_seen_at)"),
    ("last_seen_at", "MAX(orders.last_seen_at, excluded.last_seen_at)"),
    ("current_status", "CASE WHEN excluded.last_seen_at > orders.last_seen_at "
                       "THEN excluded.current_status ELSE orders.current_status END"),
    ("closed_at_utc", "COALESCE(excluded.closed_at_utc, orders.closed_at_utc)"),
    ("amount", "excluded.amount"),
    ("currency", "excluded.currency"),
    ("confirmed_currency", "excluded.confirmed_currency"),
    ("buyer_username", "COALESCE(excluded.buyer_username, orders.buyer_username)"),
    ("buyer_id", "COALESCE(excluded.buyer_id, orders.buyer_id)"),
    ("product_description", "COALESCE(orders.product_description, excluded.product_description)"),
    ("subcategory_name", "COALESCE(orders.subcategory_name, excluded.subcategory_name)"),
    ("listed_price", "COALESCE(orders.listed_price, excluded.listed_price)"),
    ("official_status", "excluded.official_status"),
    ("created_at_utc", "COALESCE(excluded.created_at_utc, orders.created_at_utc)"),
    ("paid_at_utc", "COALESCE(excluded.paid_at_utc, orders.paid_at_utc)"),
    ("refunded_at_utc", "COALESCE(excluded.refunded_at_utc, orders.refunded_at_utc)"),
    ("partially_refunded_at_utc", "COALESCE(excluded.partially_refunded_at_utc, "
                                  "orders.partially_refunded_at_utc)"),
    ("game_id", "COALESCE(excluded.game_id, orders.game_id)"),
    ("game_name", "COALESCE(excluded.game_name, orders.game_name)"),
    ("section_type_id", "COALESCE(excluded.section_type_id, orders.section_type_id)"),
    ("section_local_id", "COALESCE(excluded.section_local_id, orders.section_local_id)"),
    ("section_name", "COALESCE(excluded.section_name, orders.section_name)"),
    ("lot_summary", "COALESCE(excluded.lot_summary, orders.lot_summary)"),
    ("lot_amount", "COALESCE(excluded.lot_amount, orders.lot_amount)"),
)
_ORDER_INSERT_SQL = (
    "INSERT INTO orders (" + ", ".join(_ORDER_COLUMNS) + ") VALUES (" +
    ", ".join("?" for _ in _ORDER_COLUMNS) + ") ON CONFLICT(order_id) DO UPDATE SET " +
    ", ".join(f"{column} = {expression}" for column, expression in _ORDER_UPDATES)
)
_ORDER_COMPARE_SQL = (
    "WITH incoming (" + ", ".join(_ORDER_COLUMNS) + ") AS (VALUES (" +
    ", ".join("?" for _ in _ORDER_COLUMNS) + ")) " +
    "SELECT orders.current_status, (" + " OR ".join(
        f"orders.{column} IS NOT {expression.replace('excluded.', 'incoming.')}"
        for column, expression in _ORDER_UPDATES
    ) + ") FROM orders, incoming WHERE orders.order_id = incoming.order_id"
)


def _order_values(row):
    times = [row[key] for key in ("created_at", "paid_at", "closed_at",
                                     "refunded_at", "partially_refunded_at") if row[key] is not None]
    first_seen, last_seen = min(times), max(times)
    return (row["order_id"], first_seen, last_seen, row["canonical"], row["closed_at"],
            row["amount"], row["currency"], row["buyer_username"], row["buyer_id"],
            row["lot_summary"], row["section_name"], row["amount"], row["currency"],
            row["status"], row["created_at"], row["paid_at"], row["refunded_at"],
            row["partially_refunded_at"], row["game_id"], row["game_name"],
            row["section_type_id"], row["section_local_id"], row["section_name"],
            row["lot_summary"], row["lot_amount"])


def _classify_row(connection, row):
    """Use the same SQL merge expressions as the writer for a null-safe diff."""
    existing = connection.execute(_ORDER_COMPARE_SQL, _order_values(row)).fetchone()
    if existing is None:
        conflict, order_changed, observations = False, True, {}
    else:
        conflict, order_changed = existing[0] != row["canonical"], bool(existing[1])
        observations = dict(connection.execute(
            "SELECT status, first_observed_at FROM order_status_observations WHERE order_id = ?",
            (row["order_id"],)))
    status_changes = [(status, timestamp) for status, timestamp in
                      (("PAID", row["paid_at"]), ("CLOSED", row["closed_at"]),
                       ("REFUNDED", row["refunded_at"]))
                      if timestamp is not None and observations.get(status) != timestamp]
    review_changed = False
    if row["has_review"] or row["review_hidden"] is not None:
        review = (connection.execute(
            "SELECT rating, time_known, review_hidden FROM review_observations "
            "WHERE order_id = ?",
            (row["order_id"],)).fetchone() if existing is not None else None)
        if review is None:
            review_changed = row["has_review"]
        elif row["has_review"] and not (review[1] == 1 and review[0] is not None):
            rating = row["rating"] if row["rating"] is not None else review[0]
            if rating != review[0]:
                review_changed = True
        if (review is not None and row["review_hidden"] is not None
                and row["review_hidden"] != review[2]):
            review_changed = True
    kind = ("insert" if existing is None else
            "update" if order_changed or status_changes or review_changed else "unchanged")
    return kind, conflict, order_changed, status_changes, review_changed


def _count_classification(result, kind, conflict):
    if kind == "insert":
        result["predicted_inserts"] += 1
    else:
        result["potential_existing_matches"] += 1
        result["conflicts"] += conflict
        result["predicted_updates" if kind == "update" else "unchanged"] += 1


def _apply_batch(connection, rows):
    """One transaction per batch; preserve newer live status and non-null live context."""
    counts = {"predicted_inserts": 0, "predicted_updates": 0, "unchanged": 0,
              "potential_existing_matches": 0, "conflicts": 0}
    with connection:
        connection.execute("BEGIN IMMEDIATE")  # Classify and merge against one write snapshot.
        for row in rows:
            kind, conflict, order_changed, status_changes, review_changed = _classify_row(
                connection, row)
            _count_classification(counts, kind, conflict)
            if kind == "unchanged":
                continue
            if order_changed:
                connection.execute(_ORDER_INSERT_SQL, _order_values(row))
            for status, timestamp in status_changes:
                connection.execute(
                    "INSERT INTO order_status_observations "
                    "(order_id, status, first_observed_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(order_id, status) DO UPDATE SET "
                    "first_observed_at = excluded.first_observed_at",
                    (row["order_id"], status, timestamp),
                )
            if review_changed:
                connection.execute(
                    "INSERT INTO review_observations "
                    "(order_id, observed_at, rating, time_known, review_hidden) "
                    "VALUES (?, 0, ?, 0, ?) ON CONFLICT(order_id) DO UPDATE SET "
                    "rating = CASE WHEN review_observations.time_known = 1 "
                    "AND review_observations.rating IS NOT NULL "
                    "THEN review_observations.rating ELSE "
                    "COALESCE(excluded.rating, review_observations.rating) END, "
                    "review_hidden = COALESCE(excluded.review_hidden, "
                    "review_observations.review_hidden)",
                    (row["order_id"], row["rating"], row["review_hidden"]),
                )
    return counts


def import_zip(zip_path: Path, db_path: Path = DEFAULT_DB_PATH, *, dry_run=False,
               progress=None):
    """Stream an official ZIP; return aggregate-only results, never source records."""
    zip_path, db_path = Path(zip_path), Path(db_path)
    result = {"rows": 0, "unique_order_ids": 0, "parse_errors": 0, "duplicates": 0,
              "potential_existing_matches": 0, "conflicts": 0,
              "predicted_inserts": 0, "predicted_updates": 0, "unchanged": 0,
              "reviews": 0, "ratings": 0, "lot_summaries": 0,
              "statuses": Counter(), "currencies": Counter(),
              "first_date": None, "last_date": None, "backup": None}
    seen = set()
    batch = []
    backup = None
    if dry_run:
        connection = (sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
                      if db_path.exists() else None)
        if connection is not None:
            connection.execute("BEGIN")  # One read-only snapshot for all preview counters.
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        backup = _backup_database(db_path)
        result["backup"] = backup.name if backup else None
        try:
            ReviewReceiptStore(db_path, db_path.with_name("stats_log.json")).initialize()
        except Exception as error:
            if backup:
                raise SalesImportError(type(error).__name__, backup.name) from None
            raise
        connection = sqlite3.connect(db_path, timeout=30)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = [info for info in archive.infolist()
                       if not info.is_dir() and info.filename.lower().endswith(".csv")]
            if len(members) != 1:
                raise ValueError("Expected exactly one CSV in the ZIP.")
            with archive.open(members[0]) as binary:
                with io.TextIOWrapper(binary, encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    if not reader.fieldnames or not REQUIRED.issubset(reader.fieldnames):
                        raise ValueError("Official sales columns are missing.")
                    for raw in reader:
                        result["rows"] += 1
                        try:
                            row = parse_row(raw)
                        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                            result["parse_errors"] += 1
                            continue
                        order_id = row["order_id"]
                        if order_id in seen:
                            result["duplicates"] += 1
                            continue
                        seen.add(order_id)
                        result["statuses"][row["status"]] += 1
                        result["currencies"][row["currency"]] += 1
                        result["reviews"] += row["has_review"]
                        result["ratings"] += row["rating"] is not None
                        result["lot_summaries"] += row["lot_summary"] is not None
                        dates = [row[key] for key in ("created_at", "paid_at", "closed_at",
                                                       "refunded_at", "partially_refunded_at")
                                 if row[key] is not None]
                        first_date, last_date = min(dates), max(dates)
                        result["first_date"] = (first_date if result["first_date"] is None else
                                                min(result["first_date"], first_date))
                        result["last_date"] = (last_date if result["last_date"] is None else
                                               max(result["last_date"], last_date))
                        if dry_run:
                            try:
                                kind, conflict = (_classify_row(connection, row)[:2]
                                                  if connection is not None else ("insert", False))
                            except sqlite3.OperationalError as error:
                                if str(error) == "no such table: orders":
                                    kind, conflict = "insert", False
                                else:
                                    raise
                            _count_classification(result, kind, conflict)
                        else:
                            batch.append(row)
                            if len(batch) >= BATCH_SIZE:
                                counts = _apply_batch(connection, batch)
                                for key, value in counts.items():
                                    result[key] += value
                                batch.clear()
                        if progress and result["rows"] % 10000 == 0:
                            progress(result["rows"])
        if batch:
            counts = _apply_batch(connection, batch)
            for key, value in counts.items():
                result[key] += value
        result["unique_order_ids"] = len(seen)
        for key in ("first_date", "last_date"):
            if result[key] is not None:
                result[key] = datetime.fromtimestamp(result[key], timezone.utc).date().isoformat()
        result["statuses"] = dict(result["statuses"])
        result["currencies"] = dict(result["currencies"])
        return result
    except Exception as error:
        if backup:
            raise SalesImportError(type(error).__name__, backup.name) from None
        raise
    finally:
        if connection is not None:
            connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline FunPay sales history import",
        epilog="On failure, stop the bot and restore the named .bak beside the database "
               "after preserving the failed DB and its -wal/-shm files.",
    )
    parser.add_argument("zip_path", type=Path)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = import_zip(args.zip_path, args.db, dry_run=args.dry_run,
                            progress=lambda count: print(f"Processed {count} rows"))
    except Exception as error:
        if isinstance(error, SalesImportError):
            print(f"Import unavailable: {error.error_type}. "
                  f"Backup retained: {error.backup_name}", file=sys.stderr)
        else:
            print(f"Import unavailable: {type(error).__name__}.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
