"""Локальные receipts успешно доставленных уведомлений об отзывах."""

import re
import sqlite3
from contextlib import closing
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
