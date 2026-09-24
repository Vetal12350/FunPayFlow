"""Локальные receipts отзывов и наблюдения заказов одного FunPay-аккаунта."""

import json
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


DEFAULT_DB_PATH = Path(__file__).resolve().with_name("state.sqlite3")


class StateError(RuntimeError):
    """Persistent state недоступен или не может быть проверен."""


class ReviewReceiptStore:
    def __init__(self, path: Path = DEFAULT_DB_PATH, legacy_stats_path: Path | None = None):
        self.path = Path(path)
        self.legacy_stats_path = (Path(legacy_stats_path) if legacy_stats_path is not None
                                  else self.path.with_name("stats_log.json"))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    @staticmethod
    def _require_order_id(order_id: str) -> None:
        if not isinstance(order_id, str) or not re.fullmatch(r"[A-Z0-9]{8}", order_id):
            raise StateError("Invalid review receipt ID.")

    @staticmethod
    def _require_fingerprint(fingerprint: str) -> None:
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise StateError("Invalid review fingerprint.")

    def initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                with connection:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS review_receipts ("
                        "order_id TEXT PRIMARY KEY, "
                        "delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                    )
                    columns = {
                        row[1]: row for row in connection.execute("PRAGMA table_info(review_receipts)")
                    }
                    if (columns.get("order_id", (None,) * 6)[5] != 1
                            or columns.get("delivered_at", (None,) * 6)[3] != 1):
                        raise StateError("Persistent state schema is incompatible.")
                    if "fingerprint" not in columns:
                        connection.execute("ALTER TABLE review_receipts ADD COLUMN fingerprint TEXT")
                        columns = {
                            row[1]: row for row in connection.execute("PRAGMA table_info(review_receipts)")
                        }
                    if columns["fingerprint"][2].upper() != "TEXT":
                        raise StateError("Persistent review schema is incompatible.")
                    connection.execute(
                        "SELECT order_id, delivered_at, fingerprint FROM review_receipts LIMIT 1"
                    ).fetchone()
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS orders ("
                        "order_id TEXT PRIMARY KEY, "
                        "first_seen_at INTEGER NOT NULL, "
                        "last_seen_at INTEGER NOT NULL, "
                        "current_status TEXT NOT NULL "
                        "CHECK (current_status IN ('PAID', 'CLOSED', 'REFUNDED')), "
                        "closed_at_utc INTEGER, amount TEXT, currency TEXT)"
                    )
                    order_columns = {
                        row[1]: row for row in connection.execute("PRAGMA table_info(orders)")
                    }
                    if (order_columns.get("order_id", (None,) * 6)[5] != 1
                            or any(order_columns.get(name, (None,) * 6)[3] != 1
                                   for name in ("first_seen_at", "last_seen_at", "current_status"))):
                        raise StateError("Persistent order schema is incompatible.")
                    if "closed_at_utc" not in order_columns:
                        connection.execute("ALTER TABLE orders ADD COLUMN closed_at_utc INTEGER")
                    elif order_columns["closed_at_utc"][2].upper() != "INTEGER":
                        raise StateError("Persistent order schema is incompatible.")
                    for name in ("amount", "currency"):
                        if name not in order_columns:
                            connection.execute(f"ALTER TABLE orders ADD COLUMN {name} TEXT")
                        elif order_columns[name][2].upper() != "TEXT":
                            raise StateError("Persistent order schema is incompatible.")
                    # Для уже закрытых заказов последняя запись — лучшее
                    # доступное время закрытия; историю возвратов не выдумываем.
                    connection.execute(
                        "UPDATE orders SET closed_at_utc = last_seen_at "
                        "WHERE current_status = 'CLOSED' AND closed_at_utc IS NULL"
                    )
                    connection.execute(
                        "SELECT order_id, first_seen_at, last_seen_at, current_status, closed_at_utc "
                        "FROM orders LIMIT 1"
                    ).fetchone()
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS legacy_stats ("
                        "kind TEXT NOT NULL CHECK(kind IN ('order', 'review', 'withdrawal')), "
                        "record_key TEXT NOT NULL, recorded_at REAL NOT NULL, "
                        "amount TEXT, currency TEXT, PRIMARY KEY(kind, record_key))"
                    )
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS review_observations ("
                        "order_id TEXT PRIMARY KEY, observed_at INTEGER NOT NULL)"
                    )
                    # Уже доставленные отзывы — подтверждённый минимум истории.
                    connection.execute(
                        "INSERT OR IGNORE INTO review_observations (order_id, observed_at) "
                        "SELECT order_id, CAST(strftime('%s', delivered_at) AS INTEGER) "
                        "FROM review_receipts WHERE strftime('%s', delivered_at) IS NOT NULL"
                    )
                    self._import_legacy_stats(connection)
                    if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        raise StateError("Persistent state integrity check failed.")
        except (sqlite3.Error, OSError):
            raise StateError("Persistent state initialization failed.") from None

    def _import_legacy_stats(self, connection: sqlite3.Connection) -> None:
        """Идемпотентно переносит старый локальный JSON, если он лежит рядом с БД."""
        if not self.legacy_stats_path.is_file():
            return
        try:
            records = json.loads(self.legacy_stats_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            raise StateError("Legacy statistics import failed.") from None
        if not isinstance(records, list):
            raise StateError("Legacy statistics import failed.")
        for record in records:
            if not isinstance(record, dict) or record.get("type") not in (
                "order", "review", "withdrawal"
            ):
                raise StateError("Legacy statistics import failed.")
            kind = record["type"]
            key = record.get("transaction_id" if kind == "withdrawal" else "order_id")
            if not isinstance(key, str) or not key:
                raise StateError("Legacy statistics import failed.")
            if kind != "withdrawal":
                self._require_order_id(key)
            try:
                raw_ts = record["ts"]
                if isinstance(raw_ts, bool):
                    raise ValueError
                ts = Decimal(str(raw_ts))
                if not ts.is_finite() or ts <= 0:
                    raise ValueError
            except (KeyError, InvalidOperation, ValueError):
                raise StateError("Legacy statistics import failed.") from None
            amount = None
            currency = None
            if kind != "review":
                try:
                    raw_amount = record.get("amount")
                    if not isinstance(raw_amount, bool) and raw_amount is not None:
                        value = Decimal(str(raw_amount))
                        if value.is_finite() and value > 0:
                            amount = str(value)
                except InvalidOperation:
                    pass
                raw_currency = record.get("currency")
                if isinstance(raw_currency, str):
                    cleaned = raw_currency.strip()
                    if cleaned.upper() in ("$", "USD"):
                        currency = "USD"
                    elif cleaned in ("₽", "€"):
                        currency = cleaned
            connection.execute(
                "INSERT INTO legacy_stats (kind, record_key, recorded_at, amount, currency) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(kind, record_key) DO NOTHING",
                (kind, key, float(ts), amount, currency),
            )

    def has_review_receipt(self, order_id: str) -> bool:
        self._require_order_id(order_id)
        try:
            with closing(self._connect()) as connection:
                return connection.execute(
                    "SELECT 1 FROM review_receipts WHERE order_id = ? LIMIT 1",
                    (order_id,),
                ).fetchone() is not None
        except (sqlite3.Error, OSError):
            raise StateError("Persistent state read failed.") from None

    def get_review_receipt(self, order_id: str) -> tuple[bool, str | None]:
        self._require_order_id(order_id)
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT fingerprint FROM review_receipts WHERE order_id = ? LIMIT 1",
                    (order_id,),
                ).fetchone()
                if row is None:
                    return False, None
                if row[0] is not None:
                    self._require_fingerprint(row[0])
                return True, row[0]
        except (sqlite3.Error, OSError):
            raise StateError("Persistent state read failed.") from None

    def baseline_review_fingerprint(self, order_id: str, fingerprint: str) -> None:
        self._require_order_id(order_id)
        self._require_fingerprint(fingerprint)
        try:
            with closing(self._connect()) as connection:
                with connection:
                    result = connection.execute(
                        "UPDATE review_receipts SET fingerprint = ? "
                        "WHERE order_id = ? AND fingerprint IS NULL",
                        (fingerprint, order_id),
                    )
                    if result.rowcount != 1:
                        raise StateError("Legacy review receipt unavailable.")
        except (sqlite3.Error, OSError):
            raise StateError("Persistent state write failed.") from None

    def record_review_receipt(self, order_id: str, fingerprint: str | None = None) -> None:
        self._require_order_id(order_id)
        if fingerprint is not None:
            self._require_fingerprint(fingerprint)
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        "INSERT INTO review_receipts (order_id, fingerprint) VALUES (?, ?) "
                        "ON CONFLICT(order_id) DO UPDATE SET "
                        "fingerprint = COALESCE(excluded.fingerprint, review_receipts.fingerprint)",
                        (order_id, fingerprint),
                    )
        except (sqlite3.Error, OSError):
            raise StateError("Persistent state write failed.") from None

    def record_review_observation(
        self, order_id: str, observed_at_utc: int | None = None,
    ) -> None:
        """Факт проверенного buyer review, независимо от Telegram delivery."""
        self._require_order_id(order_id)
        if observed_at_utc is None:
            observed_at_utc = int(time.time())
        if type(observed_at_utc) is not int or observed_at_utc < 0:
            raise StateError("Invalid review observation time.")
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        "INSERT OR IGNORE INTO review_observations (order_id, observed_at) "
                        "VALUES (?, ?)", (order_id, observed_at_utc),
                    )
        except (sqlite3.Error, OSError):
            raise StateError("Persistent review observation write failed.") from None

    def record_order_observation(
        self, order_id: str, status: str, observed_at_utc: int | None = None,
        *, amount=None, currency=None,
    ) -> None:
        """Записывает текущий статус; время — наблюдение ботом, не дата заказа."""
        self._require_order_id(order_id)
        if status not in ("PAID", "CLOSED", "REFUNDED"):
            raise StateError("Invalid order status.")
        if observed_at_utc is None:
            observed_at_utc = int(time.time())
        if type(observed_at_utc) is not int or observed_at_utc < 0:
            raise StateError("Invalid observation time.")
        recorded_amount = None
        recorded_currency = None
        if status == "CLOSED":
            try:
                if amount is not None and not isinstance(amount, bool):
                    value = Decimal(str(amount))
                    if value.is_finite() and value > 0:
                        recorded_amount = str(value)
            except InvalidOperation:
                pass
            if isinstance(currency, str) and 0 < len(currency.strip()) <= 16:
                cleaned = currency.strip()
                recorded_currency = "USD" if cleaned.upper() in ("$", "USD") else cleaned
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        "INSERT INTO orders (order_id, first_seen_at, last_seen_at, "
                        "current_status, closed_at_utc, amount, currency) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(order_id) DO UPDATE SET "
                        "last_seen_at = MAX(orders.last_seen_at, excluded.last_seen_at), "
                        "closed_at_utc = COALESCE(orders.closed_at_utc, excluded.closed_at_utc), "
                        "amount = COALESCE(orders.amount, excluded.amount), "
                        "currency = COALESCE(orders.currency, excluded.currency), "
                        "current_status = CASE WHEN excluded.last_seen_at >= orders.last_seen_at "
                        "THEN excluded.current_status ELSE orders.current_status END",
                        (order_id, observed_at_utc, observed_at_utc, status,
                         observed_at_utc if status == "CLOSED" else None,
                         recorded_amount, recorded_currency),
                    )
        except (sqlite3.Error, OSError):
            raise StateError("Persistent order write failed.") from None

    def get_order_statistics(self, now_utc: int | None = None) -> dict[str, dict[str, int]]:
        """Считает только впервые увиденные ботом заказы, по UTC."""
        if now_utc is None:
            now_utc = int(time.time())
        if type(now_utc) is not int or now_utc < 0:
            raise StateError("Invalid statistics time.")
        today_utc = int(datetime.fromtimestamp(now_utc, timezone.utc)
                        .replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        periods = {
            "today": today_utc,
            "7_days": now_utc - 7 * 86400,
            "30_days": now_utc - 30 * 86400,
            "all_time": 0,
        }
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                result = {}
                for period, start in periods.items():
                    sql = (
                        "SELECT COUNT(*), "
                        "COALESCE(SUM(CASE WHEN current_status = 'CLOSED' THEN 1 ELSE 0 END), 0), "
                        "COALESCE(SUM(CASE WHEN current_status = 'REFUNDED' THEN 1 ELSE 0 END), 0) "
                        "FROM orders"
                    )
                    if period == "all_time":
                        count, closed, refunded = connection.execute(sql).fetchone()
                    else:
                        count, closed, refunded = connection.execute(
                            sql + " WHERE first_seen_at >= ? AND first_seen_at <= ?",
                            (start, now_utc),
                        ).fetchone()
                    result[period] = {"orders": count, "closed": closed, "refunded": refunded}
                return result
        except (sqlite3.Error, OSError):
            raise StateError("Persistent order read failed.") from None

    def get_legacy_statistics(
        self, period: str, now_utc: int | None = None,
    ) -> dict:
        """Архив старого бота + новые закрытия и проверенные buyer reviews."""
        if period not in ("today", "week", "month"):
            raise StateError("Invalid statistics period.")
        if now_utc is None:
            now_utc = int(time.time())
        if type(now_utc) is not int or now_utc < 0:
            raise StateError("Invalid statistics time.")
        today_local = int(datetime.fromtimestamp(now_utc)
                          .replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        start = {"today": today_local, "week": now_utc - 7 * 86400,
                 "month": now_utc - 30 * 86400}[period]
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                archived_orders = connection.execute(
                    "SELECT amount, currency FROM legacy_stats "
                    "WHERE kind = 'order' AND recorded_at BETWEEN ? AND ?",
                    (start, now_utc),
                ).fetchall()
                current_orders = connection.execute(
                    "SELECT o.amount, o.currency FROM orders AS o "
                    "WHERE o.closed_at_utc BETWEEN ? AND ? AND NOT EXISTS ("
                    "SELECT 1 FROM legacy_stats AS l WHERE l.kind = 'order' "
                    "AND l.record_key = o.order_id)",
                    (start, now_utc),
                ).fetchall()
                archived_reviews = connection.execute(
                    "SELECT COUNT(*) FROM legacy_stats "
                    "WHERE kind = 'review' AND recorded_at BETWEEN ? AND ?",
                    (start, now_utc),
                ).fetchone()[0]
                current_reviews = connection.execute(
                    "SELECT COUNT(*) FROM review_observations AS r "
                    "WHERE r.observed_at BETWEEN ? AND ? AND NOT EXISTS ("
                    "SELECT 1 FROM legacy_stats AS l WHERE l.kind = 'review' "
                    "AND l.record_key = r.order_id)",
                    (start, now_utc),
                ).fetchone()[0]
                withdrawals_count = connection.execute(
                    "SELECT COUNT(*) FROM legacy_stats "
                    "WHERE kind = 'withdrawal' AND recorded_at BETWEEN ? AND ?",
                    (start, now_utc),
                ).fetchone()[0]
                # Только подтверждённый USD: не переводим другие/неизвестные валюты.
                usd_turnover = sum(
                    (Decimal(amount) for amount, currency in archived_orders + current_orders
                     if amount is not None and currency in ("USD", "$")),
                    Decimal(0),
                )
                return {
                    "orders_count": len(archived_orders) + len(current_orders),
                    "reviews_count": archived_reviews + current_reviews,
                    "usd_turnover": usd_turnover,
                    "withdrawals_count": withdrawals_count,
                }
        except (sqlite3.Error, OSError):
            raise StateError("Persistent statistics read failed.") from None
