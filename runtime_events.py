"""Small, safe representations for internal actions and durable Runner observations."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
import math
import re
import secrets

import FunPayAPI


class ActionKind(str, Enum):
    NOTIFY_MESSAGE = "notify_message"
    NOTIFY_ORDER = "notify_order"
    NIGHT_MESSAGE = "night_message"
    NIGHT_ORDER = "night_order"
    REVIEW_CHECK_NOW = "review_check_now"
    REVIEW_CHECK_LATER = "review_check_later"


@dataclass(frozen=True)
class ActionEvent:
    kind: ActionKind
    text: str | None = None
    chat_id: int | None = None
    order_id: str | None = None
    buyer: str | None = None


@dataclass(frozen=True)
class CriticalSnapshot:
    event_id: str
    event_type: str
    entity_id: str
    payload: dict


@dataclass(frozen=True)
class QueuedCriticalEvent:
    event_id: str
    event: object


@dataclass(frozen=True)
class ReplayOrder:
    id: str
    status: object
    buyer_username: str | None
    buyer_id: int | None
    chat_id: int | None
    description: str | None
    subcategory_name: str | None
    price: Decimal | None
    sum: Decimal | None
    currency: str | None
    amount: int | None
    date: datetime | None


@dataclass(frozen=True)
class ReplayEvent:
    type: object
    order: ReplayOrder


@dataclass(frozen=True)
class ReviewCheckEvent:
    order_id: str


_ORDER_ID = re.compile(r"[A-Z0-9]{8}\Z")
_ORDER_FIELDS = ("buyer_username", "buyer_id", "chat_id", "description",
                 "subcategory_name", "price", "sum", "currency", "amount", "date")


def _short_text(value, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (str, int, float, Decimal)) or isinstance(value, bool):
        return None
    if (type(value) is float and not math.isfinite(value)) or (
            type(value) is Decimal and not value.is_finite()):
        return None
    result = str(value)
    return result if len(result) <= limit else None


def _native_id(event, nested, *, review_object: bool) -> str | None:
    candidates = (getattr(event, "id", None), getattr(nested, "last_message_id", None))
    if review_object:
        candidates += (getattr(nested, "id", None),)
    for candidate in candidates:
        if type(candidate) is int and candidate > 0:
            return str(candidate)
        if type(candidate) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", candidate):
            return candidate
    return None


def critical_snapshot(event) -> CriticalSnapshot | None:
    """Persist only order snapshots and review triggers, never message bodies."""
    event_types = FunPayAPI.enums.EventTypes
    if event.type in (event_types.NEW_ORDER, event_types.ORDER_STATUS_CHANGED):
        order = getattr(event, "order", None)
        order_id = getattr(order, "id", None)
        if type(order_id) is not str or not _ORDER_ID.fullmatch(order_id):
            return None
        status_names = ((FunPayAPI.types.OrderStatuses.PAID, "PAID"),
                        (FunPayAPI.types.OrderStatuses.CLOSED, "CLOSED"),
                        (FunPayAPI.types.OrderStatuses.REFUNDED, "REFUNDED"))
        status = next((name for enum, name in status_names
                       if getattr(order, "status", None) == enum), None)
        if status is None:
            return None
        payload = {"status": status}
        for field in _ORDER_FIELDS:
            value = getattr(order, field, None)
            if field == "date":
                payload[field] = value.isoformat() if isinstance(value, datetime) else None
            elif field in ("buyer_id", "chat_id", "amount"):
                payload[field] = value if type(value) is int and value >= 0 else None
            else:
                payload[field] = _short_text(value, 4000 if field == "description" else 500)
        kind = "NEW_ORDER" if event.type is event_types.NEW_ORDER else "ORDER_STATUS_CHANGED"
        return CriticalSnapshot(f"order:{kind}:{order_id}:{status}", kind, order_id, payload)

    order_id = None
    nested = None
    kind = None
    if event.type is event_types.LAST_CHAT_MESSAGE_CHANGED:
        nested = getattr(event, "chat", None)
        message_type = getattr(nested, "last_message_type", None)
        types = FunPayAPI.types.MessageTypes
        if (getattr(types, "NEW_FEEDBACK", None) is not None
                and message_type is types.NEW_FEEDBACK):
            kind = "NEW_FEEDBACK"
        elif (getattr(types, "FEEDBACK_CHANGED", None) is not None
              and message_type is types.FEEDBACK_CHANGED):
            kind = "FEEDBACK_CHANGED"
        if kind:
            body = getattr(nested, "last_message_text", None)
            match = re.search(r"#([A-Z0-9]{8})", body) if type(body) is str else None
            order_id = match.group(1) if match else None
    elif (getattr(event_types, "NEW_REVIEW", None) is not None
          and event.type is event_types.NEW_REVIEW):
        nested = getattr(event, "review", None)
        order_id = getattr(nested, "order_id", None)
        kind = "NEW_REVIEW"
    if kind and type(order_id) is str and _ORDER_ID.fullmatch(order_id):
        native_id = _native_id(event, nested, review_object=kind == "NEW_REVIEW")
        # Without a native ID, this is a durable trigger, not an exactly-once identity.
        identity = native_id or secrets.token_hex(16)
        return CriticalSnapshot(f"review:{kind}:{order_id}:{identity}", kind,
                                order_id, {"order_id": order_id})
    return None


def hydrate_critical(event_type: str, entity_id: str, payload: dict):
    """Reject malformed rows before they reach normal event dispatch."""
    if type(entity_id) is not str or not _ORDER_ID.fullmatch(entity_id) or type(payload) is not dict:
        raise ValueError("Invalid critical event.")
    if event_type in ("NEW_FEEDBACK", "FEEDBACK_CHANGED", "NEW_REVIEW"):
        if payload != {"order_id": entity_id}:
            raise ValueError("Invalid review trigger.")
        return ReviewCheckEvent(entity_id)
    if event_type not in ("NEW_ORDER", "ORDER_STATUS_CHANGED"):
        raise ValueError("Invalid critical event type.")
    if set(payload) != set(_ORDER_FIELDS) | {"status"}:
        raise ValueError("Invalid order payload.")
    status_name = payload["status"]
    if status_name not in ("PAID", "CLOSED", "REFUNDED"):
        raise ValueError("Invalid order status.")
    for field in ("buyer_username", "description", "subcategory_name", "price", "sum", "currency"):
        value = payload[field]
        if value is not None and (type(value) is not str or len(value) > 4000):
            raise ValueError("Invalid order field.")
    for field in ("buyer_id", "chat_id", "amount"):
        value = payload[field]
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("Invalid order field.")
    date = payload["date"]
    if date is not None:
        if type(date) is not str or len(date) > 64:
            raise ValueError("Invalid order date.")
        date = datetime.fromisoformat(date)
    values = dict(payload)
    for field in ("price", "sum"):
        if values[field] is not None:
            try:
                parsed = Decimal(values[field])
            except InvalidOperation:
                raise ValueError("Invalid order price.") from None
            if not parsed.is_finite() or parsed < 0:
                raise ValueError("Invalid order price.")
            values[field] = parsed
    order = ReplayOrder(entity_id, getattr(FunPayAPI.types.OrderStatuses, status_name),
                        *(values[field] for field in _ORDER_FIELDS[:-1]), date)
    return ReplayEvent(getattr(FunPayAPI.enums.EventTypes, event_type), order)
