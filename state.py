"""Локальные receipts отзывов и наблюдения заказов одного FunPay-аккаунта."""

import json
import math
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path


DEFAULT_DB_PATH = Path(__file__).resolve().with_name("state.sqlite3")


class _DecimalSum:
    """SQLite aggregate that keeps monetary arithmetic in Decimal, not binary float."""
    def __init__(self):
        self.total = Decimal(0)
        self.found = False

    def step(self, raw):
        if raw is None:
            return
        try:
            value = Decimal(str(raw))
            if value.is_finite() and value >= 0:
                with localcontext() as context:
                    context.prec = max(
                        context.prec, len(self.total.as_tuple().digits) +
                        len(value.as_tuple().digits) +
                        abs(self.total.as_tuple().exponent) +
                        abs(value.as_tuple().exponent) + 4,
                    )
                    self.total += value
                self.found = True
        except InvalidOperation:
            pass

    def finalize(self):
        return str(self.total) if self.found else None


def _decimal_compare(left: str, right: str) -> int:
    first, second = Decimal(left), Decimal(right)
    return (first > second) - (first < second)


# A sale is a first observed CLOSED transition, an archived old-bot closure,
# or (all-time only) a sparse old CLOSED row with no trustworthy date.
# Only confirmed_currency may qualify an order for sales money. The SQL token
# is replaced with a validated reporting currency, never with source data.
_REPORTING_AMOUNT = ("CASE WHEN UPPER(TRIM(o.confirmed_currency)) = '__CURRENCY__' "
               "AND o.official_status IS NOT 'partially_refunded' "
               "THEN COALESCE(o.amount, o.listed_price) END")
_SALE_COLUMNS = ("o.order_id, COALESCE(o.lot_summary, o.product_description) "
                 "AS product_description, COALESCE(o.section_name, o.subcategory_name) "
                 "AS subcategory_name, o.game_id, o.section_type_id, o.section_local_id, "
                 "o.buyer_id, o.buyer_username, " + _REPORTING_AMOUNT + " AS usd_amount")
_ARCHIVE_COLUMNS = ("l.record_key AS order_id, "
                    "COALESCE(o.lot_summary, o.product_description) AS product_description, "
                    "COALESCE(o.section_name, o.subcategory_name) AS subcategory_name, "
                    "o.game_id, o.section_type_id, o.section_local_id, "
                    "o.buyer_id, o.buyer_username, " + _REPORTING_AMOUNT + " AS usd_amount")
_SALES_CTE = (
    "sale_source AS ("
    "SELECT " + _SALE_COLUMNS + ", COALESCE(o.paid_at_utc, s.first_observed_at) AS sale_at "
    "FROM orders o JOIN order_status_observations s "
    "ON s.order_id = o.order_id AND s.status = 'CLOSED' "
    "WHERE o.current_status = 'CLOSED' "
    "AND (o.official_status IS NULL OR o.official_status NOT IN "
    "('refunded', 'partially_refunded')) "
    "UNION ALL SELECT " + _ARCHIVE_COLUMNS + ", "
    "COALESCE(o.paid_at_utc, l.recorded_at) AS sale_at "
    "FROM legacy_stats l LEFT JOIN orders o ON o.order_id = l.record_key "
    "WHERE l.kind = 'order' AND (o.current_status IS NULL OR o.current_status != 'REFUNDED') "
    "AND (o.official_status IS NULL OR o.official_status = 'closed') "
    "AND NOT EXISTS ("
    "SELECT 1 FROM order_status_observations s "
    "WHERE s.order_id = l.record_key AND s.status = 'CLOSED') "
    "UNION ALL SELECT " + _SALE_COLUMNS + ", "
    "COALESCE(o.paid_at_utc, o.closed_at_utc) AS sale_at "
    "FROM orders o WHERE o.current_status = 'CLOSED' "
    "AND (o.official_status IS NULL OR o.official_status NOT IN "
    "('refunded', 'partially_refunded')) "
    "AND NOT EXISTS (SELECT 1 FROM order_status_observations s "
    "WHERE s.order_id = o.order_id AND s.status = 'CLOSED') "
    "AND NOT EXISTS (SELECT 1 FROM legacy_stats l "
    "WHERE l.kind = 'order' AND l.record_key = o.order_id)"
    "), sales AS (SELECT * FROM sale_source WHERE "
    "(? IS NULL OR (sale_at >= ? AND sale_at < ?)))"
)


def normalize_reporting_currency(value: str) -> str:
    """Validate the explicitly selected ISO-style reporting code."""
    if type(value) is not str:
        raise StateError("Invalid reporting currency.")
    raw = value.strip()
    if re.fullmatch(r"[A-Za-z]{3}", raw) is None:
        raise StateError("Invalid reporting currency.")
    return raw.upper()


def _sales_cte(currency: str) -> str:
    return _SALES_CTE.replace("__CURRENCY__", normalize_reporting_currency(currency))


def _matches_reporting_currency(raw: str | None, selected: str) -> bool:
    if type(raw) is not str:
        return False
    code = raw.strip()
    return ((re.fullmatch(r"[A-Za-z]{3}", code) is not None
             and code.upper() == selected)
            or (selected == "USD" and code == "$"))


def _stored_currency(raw: str | None, *, allow_legacy_dollar: bool = True) -> str | None:
    """Only explicit three-letter codes (and the old dollar alias) carry money."""
    if raw == "$" and allow_legacy_dollar:
        return "USD"
    try:
        return normalize_reporting_currency(raw)
    except StateError:
        return None


_REFUNDS_CTE = (
    "refund_source AS ("
    "SELECT order_id, first_observed_at AS refund_at FROM order_status_observations "
    "WHERE status = 'REFUNDED' "
    "UNION ALL SELECT o.order_id, NULL AS refund_at FROM orders o "
    "WHERE (o.current_status = 'REFUNDED' OR o.official_status = 'refunded') "
    "AND NOT EXISTS ("
    "SELECT 1 FROM order_status_observations s "
    "WHERE s.order_id = o.order_id AND s.status = 'REFUNDED')"
    "), refunds AS (SELECT * FROM refund_source WHERE "
    "(? IS NULL OR (refund_at >= ? AND refund_at < ?)))"
)
_REVIEWS_CTE = (
    "review_source AS ("
    "SELECT l.record_key AS order_id, l.recorded_at AS review_at "
    "FROM legacy_stats l LEFT JOIN review_observations r ON r.order_id = l.record_key "
    "WHERE l.kind = 'review' AND r.review_hidden IS NOT 1 "
    "UNION ALL SELECT r.order_id, "
    "CASE WHEN r.time_known = 1 THEN r.observed_at END FROM review_observations r "
    "WHERE r.review_hidden IS NOT 1 AND NOT EXISTS (SELECT 1 FROM legacy_stats l "
    "WHERE l.kind = 'review' AND l.record_key = r.order_id)"
    "), reviews AS (SELECT * FROM review_source WHERE "
    "(? IS NULL OR (review_at >= ? AND review_at < ?)))"
)


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
                    history_columns = {
                        "buyer_username": "TEXT", "buyer_id": "INTEGER", "chat_id": "INTEGER",
                        "product_description": "TEXT", "quantity": "INTEGER",
                        "subcategory_name": "TEXT", "funpay_order_date_local": "TEXT",
                        "listed_price": "TEXT", "confirmed_currency": "TEXT",
                        "official_status": "TEXT", "created_at_utc": "INTEGER",
                        "paid_at_utc": "INTEGER", "refunded_at_utc": "INTEGER",
                        "partially_refunded_at_utc": "INTEGER",
                        "game_id": "INTEGER", "game_name": "TEXT",
                        "section_type_id": "TEXT", "section_local_id": "INTEGER",
                        "section_name": "TEXT", "lot_summary": "TEXT",
                        "lot_amount": "TEXT",
                    }
                    for name, sql_type in history_columns.items():
                        if name not in order_columns:
                            connection.execute(f"ALTER TABLE orders ADD COLUMN {name} {sql_type}")
                        elif order_columns[name][2].upper() != sql_type:
                            raise StateError("Persistent order schema is incompatible.")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS order_status_observations ("
                        "order_id TEXT NOT NULL, status TEXT NOT NULL "
                        "CHECK(status IN ('PAID', 'CLOSED', 'REFUNDED')), "
                        "first_observed_at INTEGER NOT NULL, "
                        "PRIMARY KEY(order_id, status))"
                    )
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
                    review_columns = {row[1] for row in connection.execute(
                        "PRAGMA table_info(review_observations)")}
                    if "rating" not in review_columns:
                        connection.execute("ALTER TABLE review_observations ADD COLUMN rating INTEGER")
                    if "time_known" not in review_columns:
                        connection.execute(
                            "ALTER TABLE review_observations ADD COLUMN time_known INTEGER NOT NULL DEFAULT 1")
                    if "review_hidden" not in review_columns:
                        connection.execute(
                            "ALTER TABLE review_observations ADD COLUMN review_hidden INTEGER "
                            "CHECK(review_hidden IN (0, 1))")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS review_requests ("
                        "order_id TEXT PRIMARY KEY, state TEXT NOT NULL "
                        "CHECK(state IN ('pending', 'sent', 'ambiguous')), "
                        "buyer TEXT NOT NULL, created_at INTEGER NOT NULL, "
                        "scheduled_at INTEGER NOT NULL, sent_at INTEGER)"
                    )
                    request_columns = {
                        row[1]: row for row in connection.execute("PRAGMA table_info(review_requests)")
                    }
                    if (request_columns.get("order_id", (None,) * 6)[5] != 1
                            or any(request_columns.get(name, (None,) * 6)[3] != 1
                                   for name in ("state", "buyer", "created_at", "scheduled_at"))):
                        raise StateError("Persistent review request schema is incompatible.")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS withdrawals ("
                        "transaction_id TEXT PRIMARY KEY, amount TEXT NOT NULL, "
                        "currency TEXT, observed_at INTEGER NOT NULL)"
                    )
                    # Уже доставленные отзывы — подтверждённый минимум истории.
                    connection.execute(
                        "INSERT OR IGNORE INTO review_observations (order_id, observed_at) "
                        "SELECT order_id, CAST(strftime('%s', delivered_at) AS INTEGER) "
                        "FROM review_receipts WHERE strftime('%s', delivered_at) IS NOT NULL"
                    )
                    self._import_legacy_stats(connection)
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_order_status_time "
                        "ON order_status_observations(status, first_observed_at, order_id)"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_legacy_stats_time "
                        "ON legacy_stats(kind, recorded_at, record_key)"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_review_observed_time "
                        "ON review_observations(observed_at, order_id)"
                    )
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS audit_events ("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
                        "actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, "
                        "result TEXT NOT NULL, details_safe TEXT)"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_audit_events_recent "
                        "ON audit_events(ts DESC, id DESC)"
                    )
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS critical_event_backlog ("
                        "event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, "
                        "entity_id TEXT NOT NULL, safe_payload TEXT NOT NULL, "
                        "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
                        "modifying_claimed INTEGER NOT NULL DEFAULT 0, "
                        "state TEXT NOT NULL CHECK(state IN "
                        "('pending', 'processing', 'done', 'ambiguous')))"
                    )
                    backlog_columns = {row[1] for row in connection.execute(
                        "PRAGMA table_info(critical_event_backlog)")}
                    if "modifying_claimed" not in backlog_columns:
                        connection.execute(
                            "ALTER TABLE critical_event_backlog ADD COLUMN "
                            "modifying_claimed INTEGER NOT NULL DEFAULT 0"
                        )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_critical_backlog_pending "
                        "ON critical_event_backlog(state, created_at, event_id)"
                    )
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
                    if type(raw_amount) in (int, float):
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
        self, order_id: str, observed_at_utc: int | None = None, rating: int | None = None,
    ) -> None:
        """Факт проверенного buyer review, независимо от Telegram delivery."""
        self._require_order_id(order_id)
        if observed_at_utc is None:
            observed_at_utc = int(time.time())
        if type(observed_at_utc) is not int or observed_at_utc < 0:
            raise StateError("Invalid review observation time.")
        if rating is not None and (type(rating) is not int or not 1 <= rating <= 5):
            rating = None
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        "INSERT INTO review_observations (order_id, observed_at, rating) "
                        "VALUES (?, ?, ?) ON CONFLICT(order_id) DO UPDATE SET "
                        "rating = COALESCE(excluded.rating, review_observations.rating), "
                        "observed_at = CASE WHEN review_observations.time_known = 0 "
                        "THEN excluded.observed_at ELSE review_observations.observed_at END, "
                        "time_known = 1",
                        (order_id, observed_at_utc, rating),
                    )
        except (sqlite3.Error, OSError):
            raise StateError("Persistent review observation write failed.") from None

    def schedule_review_request(self, order_id: str, buyer: str, scheduled_at: int) -> bool:
        self._require_order_id(order_id)
        if (type(buyer) is not str or not buyer.strip() or len(buyer) > 100
                or type(scheduled_at) is not int or scheduled_at < 0):
            raise StateError("Invalid review request context.")
        try:
            with closing(self._connect()) as connection:
                with connection:
                    result = connection.execute(
                        "INSERT OR IGNORE INTO review_requests "
                        "(order_id, state, buyer, created_at, scheduled_at) "
                        "SELECT ?, 'pending', ?, ?, ? WHERE EXISTS ("
                        "SELECT 1 FROM orders WHERE order_id = ? AND current_status = 'CLOSED') "
                        "AND NOT EXISTS (SELECT 1 FROM review_observations WHERE order_id = ?)",
                        (order_id, buyer.strip(), int(time.time()), scheduled_at, order_id, order_id),
                    )
                    return result.rowcount == 1
        except (sqlite3.Error, OSError):
            raise StateError("Persistent review request write failed.") from None

    def pending_review_requests(self) -> list[tuple[str, str, int]]:
        try:
            with closing(self._connect()) as connection:
                return connection.execute(
                    "SELECT order_id, buyer, scheduled_at FROM review_requests "
                    "WHERE state = 'pending' ORDER BY scheduled_at, order_id"
                ).fetchall()
        except (sqlite3.Error, OSError):
            raise StateError("Persistent review request read failed.") from None

    def has_review_observation(self, order_id: str) -> bool:
        self._require_order_id(order_id)
        try:
            with closing(self._connect()) as connection:
                return connection.execute(
                    "SELECT 1 FROM review_observations WHERE order_id = ?", (order_id,)
                ).fetchone() is not None
        except (sqlite3.Error, OSError):
            raise StateError("Persistent review observation read failed.") from None

    def discard_pending_review_request(self, order_id: str) -> None:
        self._require_order_id(order_id)
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        "DELETE FROM review_requests WHERE order_id = ? AND state = 'pending'",
                        (order_id,),
                    )
        except (sqlite3.Error, OSError):
            raise StateError("Persistent review request write failed.") from None

    def discard_unstarted_review_request(self, order_id: str) -> None:
        """Only call after the send gate confirms no network request began."""
        self._require_order_id(order_id)
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        "DELETE FROM review_requests WHERE order_id = ? "
                        "AND state = 'ambiguous' AND sent_at IS NULL", (order_id,),
                    )
        except (sqlite3.Error, OSError):
            raise StateError("Persistent review request write failed.") from None

    def release_unstarted_review_request(self, order_id: str) -> None:
        """Return a claimed request to pending only when a send gate denied transport."""
        self._require_order_id(order_id)
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        "UPDATE review_requests SET state = 'pending' "
                        "WHERE order_id = ? AND state = 'ambiguous' AND sent_at IS NULL",
                        (order_id,),
                    )
        except (sqlite3.Error, OSError):
            raise StateError("Persistent review request write failed.") from None

    def claim_review_request(self, order_id: str, now: int) -> bool:
        """Claim before network I/O; a crash leaves ambiguous, never auto-retry."""
        self._require_order_id(order_id)
        try:
            with closing(self._connect()) as connection:
                with connection:
                    result = connection.execute(
                        "UPDATE review_requests SET state = 'ambiguous' "
                        "WHERE order_id = ? AND state = 'pending' AND scheduled_at <= ? "
                        "AND EXISTS (SELECT 1 FROM orders WHERE order_id = ? "
                        "AND current_status = 'CLOSED') "
                        "AND NOT EXISTS (SELECT 1 FROM review_observations WHERE order_id = ?)",
                        (order_id, now, order_id, order_id),
                    )
                    return result.rowcount == 1
        except (sqlite3.Error, OSError):
            raise StateError("Persistent review request claim failed.") from None

    def mark_review_request_sent(self, order_id: str, sent_at: int) -> None:
        self._require_order_id(order_id)
        try:
            with closing(self._connect()) as connection:
                with connection:
                    result = connection.execute(
                        "UPDATE review_requests SET state = 'sent', sent_at = ? "
                        "WHERE order_id = ? AND state = 'ambiguous'", (sent_at, order_id),
                    )
                    if result.rowcount != 1:
                        raise StateError("Review request state changed.")
        except (sqlite3.Error, OSError):
            raise StateError("Persistent review request write failed.") from None

    def record_withdrawal_observation(
        self, transaction_id: str, amount, currency,
        observed_at_utc: int | None = None,
    ) -> None:
        """Один подтверждённо завершённый вывод на transaction_id."""
        if (not isinstance(transaction_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", transaction_id)):
            raise StateError("Invalid withdrawal ID.")
        try:
            if type(amount) not in (int, float, Decimal):
                raise ValueError
            value = Decimal(str(amount))
            if not value.is_finite() or value <= 0:
                raise ValueError
        except (InvalidOperation, ValueError):
            raise StateError("Invalid withdrawal amount.") from None
        if observed_at_utc is None:
            observed_at_utc = int(time.time())
        if type(observed_at_utc) is not int or observed_at_utc < 0:
            raise StateError("Invalid withdrawal observation time.")
        saved_currency = None
        if isinstance(currency, str) and 0 < len(currency.strip()) <= 16:
            cleaned = currency.strip()
            saved_currency = "USD" if cleaned.upper() in ("$", "USD") else cleaned
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        "INSERT OR IGNORE INTO withdrawals "
                        "(transaction_id, amount, currency, observed_at) "
                        "SELECT ?, ?, ?, ? WHERE NOT EXISTS ("
                        "SELECT 1 FROM legacy_stats WHERE kind = 'withdrawal' AND record_key = ?)",
                        (transaction_id, str(value), saved_currency,
                         observed_at_utc, transaction_id),
                    )
        except (sqlite3.Error, OSError):
            raise StateError("Persistent withdrawal write failed.") from None

    def record_order_observation(
        self, order_id: str, status: str, observed_at_utc: int | None = None,
        *, amount=None, currency=None, buyer_username=None, buyer_id=None,
        chat_id=None, product_description=None, quantity=None,
        subcategory_name=None, funpay_order_date_local=None, listed_price=None,
        confirmed_currency=None,
    ) -> bool:
        """Записывает статус; возвращает True лишь при первом наблюдении CLOSED."""
        self._require_order_id(order_id)
        if status not in ("PAID", "CLOSED", "REFUNDED"):
            raise StateError("Invalid order status.")
        if observed_at_utc is None:
            observed_at_utc = int(time.time())
        if type(observed_at_utc) is not int or observed_at_utc < 0:
            raise StateError("Invalid observation time.")
        def clean_text(value, limit):
            return value.strip() if type(value) is str and 0 < len(value.strip()) <= limit else None

        def clean_positive_int(value):
            return value if type(value) is int and value > 0 else None

        def clean_price(value):
            try:
                if type(value) in (int, float, Decimal):
                    parsed = Decimal(str(value))
                    if parsed.is_finite() and parsed >= 0:
                        return str(parsed)
            except InvalidOperation:
                pass
            return None

        metadata = (
            clean_text(buyer_username, 255), clean_positive_int(buyer_id),
            clean_positive_int(chat_id), clean_text(product_description, 4000),
            clean_positive_int(quantity), clean_text(subcategory_name, 255),
            clean_text(funpay_order_date_local, 40), clean_price(listed_price),
            clean_text(confirmed_currency, 16),
        )
        recorded_amount = None
        recorded_currency = None
        if status == "CLOSED":
            try:
                if type(amount) in (int, float, Decimal):
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
                    connection.execute("BEGIN IMMEDIATE")
                    previous = connection.execute(
                        "SELECT closed_at_utc FROM orders WHERE order_id = ?", (order_id,)
                    ).fetchone()
                    first_closed = status == "CLOSED" and (previous is None or previous[0] is None)
                    connection.execute(
                        "INSERT INTO orders (order_id, first_seen_at, last_seen_at, "
                        "current_status, closed_at_utc, amount, currency, "
                        "buyer_username, buyer_id, chat_id, product_description, quantity, "
                        "subcategory_name, funpay_order_date_local, listed_price, confirmed_currency) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(order_id) DO UPDATE SET "
                        "last_seen_at = MAX(orders.last_seen_at, excluded.last_seen_at), "
                        "closed_at_utc = COALESCE(orders.closed_at_utc, excluded.closed_at_utc), "
                        "amount = COALESCE(orders.amount, excluded.amount), "
                        "currency = COALESCE(orders.currency, excluded.currency), "
                        "buyer_username = COALESCE(orders.buyer_username, excluded.buyer_username), "
                        "buyer_id = COALESCE(orders.buyer_id, excluded.buyer_id), "
                        "chat_id = COALESCE(orders.chat_id, excluded.chat_id), "
                        "product_description = COALESCE(orders.product_description, excluded.product_description), "
                        "quantity = COALESCE(orders.quantity, excluded.quantity), "
                        "subcategory_name = COALESCE(orders.subcategory_name, excluded.subcategory_name), "
                        "funpay_order_date_local = COALESCE(orders.funpay_order_date_local, excluded.funpay_order_date_local), "
                        "listed_price = COALESCE(orders.listed_price, excluded.listed_price), "
                        "confirmed_currency = COALESCE(orders.confirmed_currency, excluded.confirmed_currency), "
                        "current_status = CASE WHEN excluded.last_seen_at >= orders.last_seen_at "
                        "THEN excluded.current_status ELSE orders.current_status END",
                        (order_id, observed_at_utc, observed_at_utc, status,
                         observed_at_utc if status == "CLOSED" else None,
                         recorded_amount, recorded_currency, *metadata),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO order_status_observations "
                        "(order_id, status, first_observed_at) VALUES (?, ?, ?)",
                        (order_id, status, observed_at_utc),
                    )
                    return first_closed
        except (sqlite3.Error, OSError):
            raise StateError("Persistent order write failed.") from None

    def list_order_history(self, status: str | None = None, page: int = 0) -> tuple[int, list[dict]]:
        if status not in (None, "CLOSED", "REFUNDED") or type(page) is not int or page < 0:
            raise StateError("Invalid order history query.")
        where = " WHERE current_status = ?" if status else ""
        params = (status,) if status else ()
        try:
            with closing(self._connect()) as connection:
                connection.row_factory = sqlite3.Row
                with connection:
                    count = connection.execute("SELECT COUNT(*) FROM orders" + where, params).fetchone()[0]
                    rows = connection.execute(
                        "SELECT order_id, current_status, official_status, "
                        "COALESCE(lot_summary, product_description) AS product_description, "
                        "last_seen_at "
                        "FROM orders" + where + " ORDER BY last_seen_at DESC, order_id DESC "
                        "LIMIT 10 OFFSET ?", (*params, page * 10),
                    ).fetchall()
                    return count, [dict(row) for row in rows]
        except (sqlite3.Error, OSError):
            raise StateError("Persistent order read failed.") from None

    def get_order_history(self, order_id: str) -> dict | None:
        self._require_order_id(order_id)
        try:
            with closing(self._connect()) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
                if row is None:
                    return None
                result = dict(row)
                result["observed_statuses"] = {
                    item[0]: item[1] for item in connection.execute(
                        "SELECT status, first_observed_at FROM order_status_observations WHERE order_id = ?",
                        (order_id,),
                    )
                }
                return result
        except (sqlite3.Error, OSError):
            raise StateError("Persistent order read failed.") from None


    @staticmethod
    def _analytics_window(period: str, now_utc: int | None):
        if period not in ("today", "7d", "30d", "all"):
            raise StateError("Invalid analytics period.")
        if now_utc is None:
            now_utc = int(time.time())
        if type(now_utc) is not int or now_utc < 0:
            raise StateError("Invalid analytics time.")
        if period == "all":
            return None, None, None, None
        if period == "today":
            today = datetime.fromtimestamp(now_utc).replace(
                hour=0, minute=0, second=0, microsecond=0)
            start = int(today.timestamp())
            previous = int((today - timedelta(days=1)).timestamp())
        else:
            days = 7 if period == "7d" else 30
            start = now_utc - days * 86400
            previous = start - days * 86400
        return start, now_utc + 1, previous, start

    @staticmethod
    def _window_args(start, end):
        return start, start, end

    def _analytics_connect(self):
        connection = self._connect()
        connection.create_aggregate("decimal_sum", 1, _DecimalSum)
        connection.create_collation("DECIMAL", _decimal_compare)
        return connection

    @staticmethod
    def _sales_totals(connection, start, end, currency="USD"):
        return connection.execute(
            "WITH " + _sales_cte(currency) + " SELECT COUNT(*), COUNT(usd_amount), "
            "decimal_sum(usd_amount) FROM sales",
            ReviewReceiptStore._window_args(start, end),
        ).fetchone()

    def get_sales_currencies(self) -> list[str]:
        """Currencies actually persisted on eligible historical sale orders."""
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT DISTINCT confirmed_currency FROM orders "
                    "WHERE confirmed_currency IS NOT NULL "
                    "AND (current_status IS NULL OR current_status = 'CLOSED') "
                    "AND (official_status IS NULL OR official_status NOT IN "
                    "('refunded', 'partially_refunded'))"
                ).fetchall()
            return sorted({code for (raw,) in rows
                           if (code := _stored_currency(raw, allow_legacy_dollar=False)) is not None})
        except (sqlite3.Error, OSError):
            raise StateError("Sales currency read failed.") from None

    def get_sales_turnover_by_currency(self, period: str,
                                       now_utc: int | None = None) -> dict[str, str]:
        """Separate confirmed turnover; never add monetary values across codes."""
        start, end, _, _ = self._analytics_window(period, now_utc)
        try:
            with closing(self._analytics_connect()) as connection:
                connection.execute("BEGIN")
                rows = connection.execute(
                    "SELECT DISTINCT confirmed_currency FROM orders "
                    "WHERE confirmed_currency IS NOT NULL"
                ).fetchall()
                codes = sorted({code for (raw,) in rows
                                if (code := _stored_currency(raw, allow_legacy_dollar=False)) is not None})
                result = {}
                for code in codes:
                    _, count, amount = self._sales_totals(connection, start, end, code)
                    if count:
                        result[code] = amount
                return result
        except (sqlite3.Error, OSError, InvalidOperation):
            raise StateError("Sales analytics read failed.") from None

    @staticmethod
    def _top_products(connection, start, end, sort="count", limit=10, currency="USD"):
        if sort not in ("count", "turnover") or type(limit) is not int or not 1 <= limit <= 10:
            raise StateError("Invalid analytics sort.")
        rank = ("orders_count DESC, product_description, subcategory_name" if sort == "count"
                else "usd_turnover COLLATE DECIMAL DESC, orders_count DESC, product_description")
        having = " HAVING COUNT(usd_amount) > 0" if sort == "turnover" else ""
        rows = connection.execute(
            "WITH " + _sales_cte(currency) + " SELECT product_description, "
            "MIN(subcategory_name) AS subcategory_name, "
            "COUNT(*) AS orders_count, decimal_sum(usd_amount) AS usd_turnover "
            "FROM sales WHERE product_description IS NOT NULL "
            "GROUP BY game_id, section_type_id, section_local_id, "
            "product_description, CASE WHEN game_id IS NOT NULL "
            "AND section_type_id IS NOT NULL AND section_local_id IS NOT NULL "
            "THEN NULL ELSE subcategory_name END" + having +
            " ORDER BY " + rank + " LIMIT ?",
            (*ReviewReceiptStore._window_args(start, end), limit),
        ).fetchall()
        return [dict(zip(("product", "subcategory", "orders", "usd_turnover"), row))
                for row in rows]

    @staticmethod
    def _top_buyers(connection, start, end, limit=10, currency="USD"):
        rows = connection.execute(
            "WITH " + _sales_cte(currency) + ", buyer_groups AS ("
            "SELECT CASE WHEN buyer_id IS NOT NULL THEN 'id:' || buyer_id "
            "ELSE 'name:' || buyer_username END AS buyer_key, "
            "MIN(buyer_username) AS username, COUNT(*) AS orders_count "
            "FROM sales WHERE buyer_id IS NOT NULL OR buyer_username IS NOT NULL "
            "GROUP BY buyer_key) "
            "SELECT username, orders_count FROM buyer_groups "
            "ORDER BY orders_count DESC, username LIMIT ?",
            (*ReviewReceiptStore._window_args(start, end), limit),
        ).fetchall()
        return [{"username": row[0], "orders": row[1]} for row in rows]

    @staticmethod
    def _best_day(connection, start, end, sort="count", currency="USD"):
        if sort not in ("count", "turnover"):
            raise StateError("Invalid analytics sort.")
        having = " HAVING COUNT(usd_amount) > 0" if sort == "turnover" else ""
        rank = ("orders_count DESC, day DESC" if sort == "count"
                else "usd_turnover COLLATE DECIMAL DESC, orders_count DESC, day DESC")
        row = connection.execute(
            "WITH " + _sales_cte(currency) + " SELECT date(sale_at, 'unixepoch', 'localtime') AS day, "
            "COUNT(*) AS orders_count, decimal_sum(usd_amount) AS usd_turnover "
            "FROM sales WHERE sale_at IS NOT NULL GROUP BY day" + having +
            " ORDER BY " + rank + " LIMIT 1",
            ReviewReceiptStore._window_args(start, end),
        ).fetchone()
        return dict(zip(("day", "orders", "usd_turnover"), row)) if row else None

    @staticmethod
    def _most_expensive(connection, start, end, currency="USD"):
        row = connection.execute(
            "WITH " + _sales_cte(currency) + " SELECT order_id, product_description, usd_amount "
            "FROM sales WHERE usd_amount IS NOT NULL "
            "ORDER BY usd_amount COLLATE DECIMAL DESC, order_id LIMIT 1",
            ReviewReceiptStore._window_args(start, end),
        ).fetchone()
        return dict(zip(("order_id", "product", "usd_amount"), row)) if row else None

    def get_sales_overview(self, period: str, now_utc: int | None = None,
                           currency: str = "USD") -> dict:
        currency = normalize_reporting_currency(currency)
        start, end, previous_start, previous_end = self._analytics_window(period, now_utc)
        try:
            with closing(self._analytics_connect()) as connection:
                connection.execute("BEGIN")
                orders, usd_orders, turnover = self._sales_totals(connection, start, end, currency)
                refunded, terminal = connection.execute(
                    "WITH " + _sales_cte(currency) + ", " + _REFUNDS_CTE +
                    " SELECT (SELECT COUNT(*) FROM refunds), "
                    "(SELECT COUNT(*) FROM (SELECT order_id FROM sales "
                    "UNION SELECT order_id FROM refunds))",
                    (*self._window_args(start, end), *self._window_args(start, end)),
                ).fetchone()
                partial_refunds = connection.execute(
                    "SELECT COUNT(*) FROM orders WHERE official_status = 'partially_refunded' "
                    "AND (? IS NULL OR (partially_refunded_at_utc >= ? "
                    "AND partially_refunded_at_utc < ?))",
                    self._window_args(start, end),
                ).fetchone()[0]
                reviews, reviewed_sales = connection.execute(
                    "WITH " + _sales_cte(currency) + ", " + _REVIEWS_CTE +
                    " SELECT (SELECT COUNT(*) FROM reviews), "
                    "(SELECT COUNT(*) FROM sales s WHERE EXISTS ("
                    "SELECT 1 FROM review_source r WHERE r.order_id = s.order_id))",
                    (*self._window_args(start, end), *self._window_args(start, end)),
                ).fetchone()
                buyers, repeat_buyers = connection.execute(
                    "WITH " + _sales_cte(currency) + ", buyer_groups AS ("
                    "SELECT CASE WHEN buyer_id IS NOT NULL THEN 'id:' || buyer_id "
                    "ELSE 'name:' || buyer_username END AS buyer_key, "
                    "COUNT(*) AS orders_count FROM sales "
                    "WHERE buyer_id IS NOT NULL OR buyer_username IS NOT NULL GROUP BY buyer_key) "
                    "SELECT COUNT(*), COALESCE(SUM(orders_count >= 2), 0) FROM buyer_groups",
                    self._window_args(start, end),
                ).fetchone()
                previous = (self._sales_totals(connection, previous_start, previous_end, currency)
                            if previous_start is not None else None)
                return {
                    "orders": orders, "usd_orders": usd_orders,
                    "usd_turnover": turnover if orders else "0",
                    "refunds": refunded, "terminal_orders": terminal,
                    "partial_refunds": partial_refunds,
                    "reviews": reviews, "reviewed_sales": reviewed_sales,
                    "buyers": buyers, "repeat_buyers": repeat_buyers,
                    "top_product": next(iter(self._top_products(
                        connection, start, end, limit=1, currency=currency)), None),
                    "top_turnover_product": next(iter(self._top_products(
                        connection, start, end, sort="turnover", limit=1,
                        currency=currency)), None),
                    "most_expensive": self._most_expensive(connection, start, end, currency),
                    "best_day": self._best_day(connection, start, end, currency=currency),
                    "previous_orders": previous[0] if previous else None,
                    "previous_usd_turnover": (previous[2] if previous[0] else "0")
                    if previous else None,
                }
        except (sqlite3.Error, OSError, InvalidOperation):
            raise StateError("Sales analytics read failed.") from None

    def get_sales_top_products(self, period: str, sort: str = "count",
                               now_utc: int | None = None,
                               currency: str = "USD") -> list[dict]:
        currency = normalize_reporting_currency(currency)
        start, end, _, _ = self._analytics_window(period, now_utc)
        try:
            with closing(self._analytics_connect()) as connection:
                return self._top_products(connection, start, end, sort=sort,
                                          currency=currency)
        except (sqlite3.Error, OSError, InvalidOperation):
            raise StateError("Sales analytics read failed.") from None

    def get_sales_buyers(self, period: str, now_utc: int | None = None,
                         currency: str = "USD") -> dict:
        currency = normalize_reporting_currency(currency)
        start, end, _, _ = self._analytics_window(period, now_utc)
        try:
            with closing(self._analytics_connect()) as connection:
                connection.execute("BEGIN")
                rows = self._top_buyers(connection, start, end, currency=currency)
                totals = connection.execute(
                    "WITH " + _sales_cte(currency) + ", buyer_groups AS ("
                    "SELECT CASE WHEN buyer_id IS NOT NULL THEN 'id:' || buyer_id "
                    "ELSE 'name:' || buyer_username END AS buyer_key, "
                    "COUNT(*) AS n FROM sales WHERE buyer_id IS NOT NULL "
                    "OR buyer_username IS NOT NULL GROUP BY buyer_key) "
                    "SELECT COUNT(*), COALESCE(SUM(n >= 2), 0) FROM buyer_groups",
                    self._window_args(start, end),
                ).fetchone()
                return {"unique": totals[0], "repeat": totals[1], "top": rows}
        except (sqlite3.Error, OSError, InvalidOperation):
            raise StateError("Sales analytics read failed.") from None

    def get_sales_reviews(self, period: str, now_utc: int | None = None,
                          currency: str = "USD") -> dict:
        currency = normalize_reporting_currency(currency)
        start, end, _, _ = self._analytics_window(period, now_utc)
        try:
            with closing(self._analytics_connect()) as connection:
                reviews, closed, reviewed = connection.execute(
                    "WITH " + _sales_cte(currency) + ", " + _REVIEWS_CTE +
                    " SELECT (SELECT COUNT(*) FROM reviews), "
                    "(SELECT COUNT(*) FROM sales), "
                    "(SELECT COUNT(*) FROM sales s WHERE EXISTS ("
                    "SELECT 1 FROM review_source r WHERE r.order_id = s.order_id))",
                    (*self._window_args(start, end), *self._window_args(start, end)),
                ).fetchone()
                rating_rows = connection.execute(
                    "WITH " + _REVIEWS_CTE +
                    " SELECT r.rating, COUNT(*) FROM reviews v "
                    "JOIN review_observations r ON r.order_id = v.order_id "
                    "WHERE r.rating BETWEEN 1 AND 5 GROUP BY r.rating",
                    self._window_args(start, end),
                ).fetchall()
                distribution = {stars: count for stars, count in rating_rows}
                rated = sum(distribution.values())
                average = (sum(stars * count for stars, count in distribution.items()) / rated
                           if rated else None)
                return {"reviews": reviews, "closed_orders": closed, "reviewed_orders": reviewed,
                        "average_rating": average, "rating_distribution": distribution}
        except (sqlite3.Error, OSError, InvalidOperation):
            raise StateError("Sales analytics read failed.") from None

    def get_sales_by_time(self, period: str, now_utc: int | None = None,
                          currency: str = "USD") -> dict:
        currency = normalize_reporting_currency(currency)
        start, end, _, _ = self._analytics_window(period, now_utc)
        try:
            with closing(self._analytics_connect()) as connection:
                connection.execute("BEGIN")
                weekday = connection.execute(
                    "WITH " + _sales_cte(currency) +
                    " SELECT CAST(strftime('%w', sale_at, 'unixepoch', 'localtime') AS INTEGER), "
                    "COUNT(*) AS n FROM sales WHERE sale_at IS NOT NULL "
                    "GROUP BY 1 ORDER BY n DESC, 1 LIMIT 1",
                    self._window_args(start, end),
                ).fetchone()
                hour = connection.execute(
                    "WITH " + _sales_cte(currency) +
                    " SELECT CAST(strftime('%H', sale_at, 'unixepoch', 'localtime') AS INTEGER), "
                    "COUNT(*) AS n FROM sales WHERE sale_at IS NOT NULL "
                    "GROUP BY 1 ORDER BY n DESC, 1 LIMIT 1",
                    self._window_args(start, end),
                ).fetchone()
                return {"best_day": self._best_day(connection, start, end,
                                                    currency=currency),
                        "best_turnover_day": self._best_day(
                            connection, start, end, "turnover", currency),
                        "weekday": {"weekday": weekday[0], "orders": weekday[1]} if weekday else None,
                        "hour": {"hour": hour[0], "orders": hour[1]} if hour else None}
        except (sqlite3.Error, OSError, InvalidOperation):
            raise StateError("Sales analytics read failed.") from None

    def get_sales_records(self, currency: str = "USD") -> dict:
        currency = normalize_reporting_currency(currency)
        try:
            with closing(self._analytics_connect()) as connection:
                connection.execute("BEGIN")
                start = end = None
                return {
                    "most_expensive": self._most_expensive(connection, start, end, currency),
                    "top_product": next(iter(self._top_products(
                        connection, start, end, limit=1, currency=currency)), None),
                    "top_turnover_product": next(iter(self._top_products(
                        connection, start, end, sort="turnover", limit=1,
                        currency=currency)), None),
                    "best_day": self._best_day(connection, start, end, currency=currency),
                    "best_turnover_day": self._best_day(
                        connection, start, end, "turnover", currency),
                    "top_buyer": next(iter(self._top_buyers(
                        connection, start, end, limit=1, currency=currency)), None),
                }
        except (sqlite3.Error, OSError, InvalidOperation):
            raise StateError("Sales analytics read failed.") from None

    def record_audit_event(self, actor: str, action: str, target: str,
                           result: str, details_safe: str | None = None) -> None:
        """Only fixed action metadata is accepted; never persist message bodies."""
        allowed_actions = {
            "AUTOBUMP", "NIGHT_MODE", "REVIEW_REQUEST", "SAFE_MODE",
            "REVIEW_REQUEST_SEND", "SETTINGS", "MODULES", "PROFILE", "RESTART",
        }
        allowed_results = {"ON", "OFF", "CREATED", "UPDATED", "DELETED",
                           "SUCCESS", "FAILED", "BLOCKED", "AMBIGUOUS",
                           "SAVED", "APPLIED", "REQUESTED"}
        allowed_targets = {"global", "order", "settings", "configuration",
                           "minimal", "seller", "all", "manual"}
        if (not (actor in ("system", "automation") or
                 (type(actor) is str and re.fullmatch(r"telegram:[1-9][0-9]{0,19}", actor)))
                or action not in allowed_actions or target not in allowed_targets
                or result not in allowed_results or details_safe is not None):
            raise StateError("Invalid audit metadata.")
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        "INSERT INTO audit_events (ts, actor, action, target, result, details_safe) "
                        "VALUES (?, ?, ?, ?, ?, NULL)",
                        (int(time.time()), actor, action, target, result),
                    )
        except (sqlite3.Error, OSError):
            raise StateError("Audit write failed.") from None

    def list_audit_events(self, page: int = 0) -> tuple[int, list[dict]]:
        if type(page) is not int or page < 0 or page > 100000:
            raise StateError("Invalid audit page.")
        try:
            with closing(self._connect()) as connection:
                connection.row_factory = sqlite3.Row
                count = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
                rows = connection.execute(
                    "SELECT id, ts, actor, action, target, result, details_safe "
                    "FROM audit_events ORDER BY ts DESC, id DESC LIMIT 20 OFFSET ?",
                    (page * 20,),
                ).fetchall()
                return count, [dict(row) for row in rows]
        except (sqlite3.Error, OSError):
            raise StateError("Audit read failed.") from None

    def get_audit_event(self, event_id: int) -> dict | None:
        if type(event_id) is not int or event_id <= 0:
            raise StateError("Invalid audit ID.")
        try:
            with closing(self._connect()) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT id, ts, actor, action, target, result, details_safe "
                    "FROM audit_events WHERE id = ?", (event_id,),
                ).fetchone()
                return dict(row) if row else None
        except (sqlite3.Error, OSError):
            raise StateError("Audit read failed.") from None

    def count_pending_review_requests(self) -> int:
        try:
            with closing(self._connect()) as connection:
                return connection.execute(
                    "SELECT COUNT(*) FROM review_requests WHERE state = 'pending'"
                ).fetchone()[0]
        except (sqlite3.Error, OSError):
            raise StateError("Persistent review request read failed.") from None

    def insert_critical_event(self, event_id: str, event_type: str,
                              entity_id: str, payload: dict) -> bool:
        """Idempotent durable observation, bounded by 4096 unfinished rows."""
        from runtime_events import hydrate_critical
        if (type(event_id) is not str or not 1 <= len(event_id) <= 160
                or not re.fullmatch(r"[A-Za-z0-9:_-]+", event_id)):
            raise StateError("Invalid critical event ID.")
        try:
            hydrate_critical(event_type, entity_id, payload)
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) > 10000:
                raise ValueError("Oversized critical event.")
            with closing(self._connect()) as connection:
                with connection:
                    if connection.execute(
                        "SELECT 1 FROM critical_event_backlog WHERE event_id = ?", (event_id,)
                    ).fetchone():
                        return False
                    count = connection.execute(
                        "SELECT COUNT(*) FROM critical_event_backlog "
                        "WHERE state IN ('pending', 'processing')"
                    ).fetchone()[0]
                    if count >= 4096:
                        raise StateError("Critical event backlog capacity exceeded.")
                    now = int(time.time())
                    connection.execute(
                        "INSERT INTO critical_event_backlog "
                        "(event_id, event_type, entity_id, safe_payload, created_at, updated_at, state) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                        (event_id, event_type, entity_id, encoded, now, now),
                    )
                    connection.execute(
                        "DELETE FROM critical_event_backlog WHERE state IN ('done', 'ambiguous') "
                        "AND updated_at < ?", (now - 30 * 86400,)
                    )
                    connection.execute(
                        "DELETE FROM critical_event_backlog WHERE event_id IN ("
                        "SELECT event_id FROM critical_event_backlog WHERE state IN ('done', 'ambiguous') "
                        "ORDER BY updated_at DESC, event_id DESC LIMIT -1 OFFSET 10000)"
                    )
                    return True
        except (sqlite3.Error, OSError, ValueError, TypeError):
            raise StateError("Critical event persist failed.") from None

    def recover_critical_events(self) -> None:
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        "UPDATE critical_event_backlog SET state = 'pending' "
                        "WHERE state = 'processing'"
                    )
        except (sqlite3.Error, OSError):
            raise StateError("Critical event recovery failed.") from None

    def pending_critical_events(self, limit: int, exclude: set[str] | None = None) -> list[dict]:
        if type(limit) is not int or not 0 <= limit <= 256:
            raise StateError("Invalid critical event page.")
        excluded = tuple(exclude or ())
        if len(excluded) > 256:
            raise StateError("Invalid critical event exclusion.")
        excluded_sql = (" AND event_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
                        if excluded else "")
        try:
            with closing(self._connect()) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT event_id, event_type, entity_id, safe_payload FROM critical_event_backlog "
                    "WHERE state = 'pending'" + excluded_sql +
                    " ORDER BY created_at, event_id LIMIT ?", (*excluded, limit),
                ).fetchall()
                return [dict(row) for row in rows]
        except (sqlite3.Error, OSError):
            raise StateError("Critical event read failed.") from None

    def mark_critical_processing(self, event_id: str) -> bool:
        return self._set_critical_state(event_id, "pending", "processing")

    def claim_critical_modifying_action(self, event_id: str) -> bool:
        """Durably claim before automatic HTTP; replay never repeats an uncertain send."""
        try:
            with closing(self._connect()) as connection:
                with connection:
                    result = connection.execute(
                        "UPDATE critical_event_backlog SET modifying_claimed = 1 "
                        "WHERE event_id = ? AND state = 'processing' AND modifying_claimed = 0",
                        (event_id,),
                    )
                    return result.rowcount == 1
        except (sqlite3.Error, OSError):
            raise StateError("Critical action claim failed.") from None

    def mark_critical_done(self, event_id: str) -> bool:
        return self._set_critical_state(event_id, "processing", "done")

    def mark_critical_ambiguous(self, event_id: str) -> bool:
        return self._set_critical_state(event_id, "pending", "ambiguous")

    def _set_critical_state(self, event_id: str, previous: str, state: str) -> bool:
        try:
            with closing(self._connect()) as connection:
                with connection:
                    result = connection.execute(
                        "UPDATE critical_event_backlog SET state = ?, updated_at = ? "
                        "WHERE event_id = ? AND state = ?",
                        (state, int(time.time()), event_id, previous),
                    )
                    return result.rowcount == 1
        except (sqlite3.Error, OSError):
            raise StateError("Critical event state write failed.") from None

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
        self, period: str, now_utc: int | float | None = None,
        currency: str = "USD",
    ) -> dict:
        """Архив старого бота + новые закрытия и проверенные buyer reviews."""
        currency = normalize_reporting_currency(currency)
        if period not in ("today", "week", "month"):
            raise StateError("Invalid statistics period.")
        if now_utc is None:
            now_utc = time.time()
        if type(now_utc) not in (int, float) or not math.isfinite(now_utc) or now_utc < 0:
            raise StateError("Invalid statistics time.")
        today_local = int(datetime.fromtimestamp(now_utc)
                          .replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        start = {"today": today_local, "week": now_utc - 7 * 86400,
                 "month": now_utc - 30 * 86400}[period]
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                archived_orders = connection.execute(
                    "SELECT COALESCE(o.amount, l.amount), "
                    "COALESCE(o.confirmed_currency, o.currency, l.currency) "
                    "FROM legacy_stats l LEFT JOIN orders o ON o.order_id = l.record_key "
                    "WHERE l.kind = 'order' "
                    "AND COALESCE(o.closed_at_utc, l.recorded_at) BETWEEN ? AND ? "
                    "AND (o.current_status IS NULL OR o.current_status != 'REFUNDED') "
                    "AND (o.official_status IS NULL OR o.official_status = 'closed')",
                    (start, now_utc),
                ).fetchall()
                current_orders = connection.execute(
                    "SELECT COALESCE(o.amount, o.listed_price), "
                    "COALESCE(o.confirmed_currency, o.currency) FROM orders AS o "
                    "WHERE o.current_status = 'CLOSED' "
                    "AND (o.official_status IS NULL OR o.official_status NOT IN "
                    "('refunded', 'partially_refunded')) "
                    "AND o.closed_at_utc BETWEEN ? AND ? AND NOT EXISTS ("
                    "SELECT 1 FROM legacy_stats AS l WHERE l.kind = 'order' "
                    "AND l.record_key = o.order_id)",
                    (start, now_utc),
                ).fetchall()
                archived_reviews = connection.execute(
                    "SELECT COUNT(*) FROM legacy_stats l "
                    "LEFT JOIN review_observations r ON r.order_id = l.record_key "
                    "WHERE l.kind = 'review' AND l.recorded_at BETWEEN ? AND ? "
                    "AND r.review_hidden IS NOT 1",
                    (start, now_utc),
                ).fetchone()[0]
                current_reviews = connection.execute(
                    "SELECT COUNT(*) FROM review_observations AS r "
                    "WHERE r.review_hidden IS NOT 1 AND r.observed_at BETWEEN ? AND ? "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM legacy_stats AS l WHERE l.kind = 'review' "
                    "AND l.record_key = r.order_id)",
                    (start, now_utc),
                ).fetchone()[0]
                archived_withdrawals = connection.execute(
                    "SELECT amount, currency FROM legacy_stats "
                    "WHERE kind = 'withdrawal' AND recorded_at BETWEEN ? AND ?",
                    (start, now_utc),
                ).fetchall()
                current_withdrawals = connection.execute(
                    "SELECT w.amount, w.currency FROM withdrawals AS w "
                    "WHERE w.observed_at BETWEEN ? AND ? AND NOT EXISTS ("
                    "SELECT 1 FROM legacy_stats AS l WHERE l.kind = 'withdrawal' "
                    "AND l.record_key = w.transaction_id)",
                    (start, now_utc),
                ).fetchall()
                # Legacy result keys retain their names for existing callers. Their
                # amounts now refer only to the selected reporting currency.
                usd_turnover = sum(
                    (Decimal(amount) for amount, source_currency in
                     archived_orders + current_orders
                     if amount is not None
                     and _matches_reporting_currency(source_currency, currency)),
                    Decimal(0),
                )
                usd_withdrawals = sum(
                    (Decimal(amount) for amount, source_currency in
                     archived_withdrawals + current_withdrawals
                     if amount is not None
                     and _matches_reporting_currency(source_currency, currency)),
                    Decimal(0),
                )
                turnover_by_currency = {}
                withdrawals_by_currency = {}
                for rows, target in ((archived_orders + current_orders, turnover_by_currency),
                                     (archived_withdrawals + current_withdrawals,
                                      withdrawals_by_currency)):
                    for amount, raw_currency in rows:
                        code = _stored_currency(raw_currency)
                        if code is None or amount is None:
                            continue
                        target[code] = target.get(code, Decimal(0)) + Decimal(amount)
                return {
                    "orders_count": len(archived_orders) + len(current_orders),
                    "reviews_count": archived_reviews + current_reviews,
                    "usd_turnover": usd_turnover,
                    "withdrawals_count": len(archived_withdrawals) + len(current_withdrawals),
                    "usd_withdrawals": usd_withdrawals,
                    "turnover_by_currency": turnover_by_currency,
                    "withdrawals_by_currency": withdrawals_by_currency,
                }
        except (sqlite3.Error, OSError):
            raise StateError("Persistent statistics read failed.") from None
