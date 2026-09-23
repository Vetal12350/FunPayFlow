"""Локальные receipts отзывов и наблюдения заказов одного FunPay-аккаунта."""

import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_DB_PATH = Path(__file__).resolve().with_name("state.sqlite3")


class StateError(RuntimeError):
    """Persistent state недоступен или не может быть проверен."""


class ReviewReceiptStore:
    def __init__(self, path: Path = DEFAULT_DB_PATH):
        self.path = Path(path)

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
                    connection.execute(
                        "SELECT order_id, delivered_at FROM review_receipts LIMIT 1"
                    ).fetchone()
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS orders ("
                        "order_id TEXT PRIMARY KEY, "
                        "first_seen_at INTEGER NOT NULL, "
                        "last_seen_at INTEGER NOT NULL, "
                        "current_status TEXT NOT NULL "
                        "CHECK (current_status IN ('PAID', 'CLOSED', 'REFUNDED')))"
                    )
                    order_columns = {
                        row[1]: row for row in connection.execute("PRAGMA table_info(orders)")
                    }
                    if (order_columns.get("order_id", (None,) * 6)[5] != 1
                            or any(order_columns.get(name, (None,) * 6)[3] != 1
                                   for name in ("first_seen_at", "last_seen_at", "current_status"))):
                        raise StateError("Persistent order schema is incompatible.")
                    connection.execute(
                        "SELECT order_id, first_seen_at, last_seen_at, current_status "
                        "FROM orders LIMIT 1"
                    ).fetchone()
                    if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        raise StateError("Persistent state integrity check failed.")
        except (sqlite3.Error, OSError):
            raise StateError("Persistent state initialization failed.") from None

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

    def record_review_receipt(self, order_id: str) -> None:
        self._require_order_id(order_id)
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        "INSERT OR IGNORE INTO review_receipts (order_id) VALUES (?)",
                        (order_id,),
                    )
        except (sqlite3.Error, OSError):
            raise StateError("Persistent state write failed.") from None

    def record_order_observation(
        self, order_id: str, status: str, observed_at_utc: int | None = None
    ) -> None:
        """Записывает текущий статус; время — наблюдение ботом, не дата заказа."""
        self._require_order_id(order_id)
        if status not in ("PAID", "CLOSED", "REFUNDED"):
            raise StateError("Invalid order status.")
        if observed_at_utc is None:
            observed_at_utc = int(time.time())
        if type(observed_at_utc) is not int or observed_at_utc < 0:
            raise StateError("Invalid observation time.")
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        "INSERT INTO orders (order_id, first_seen_at, last_seen_at, current_status) "
                        "VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(order_id) DO UPDATE SET "
                        "last_seen_at = MAX(orders.last_seen_at, excluded.last_seen_at), "
                        "current_status = CASE WHEN excluded.last_seen_at >= orders.last_seen_at "
                        "THEN excluded.current_status ELSE orders.current_status END",
                        (order_id, observed_at_utc, observed_at_utc, status),
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
