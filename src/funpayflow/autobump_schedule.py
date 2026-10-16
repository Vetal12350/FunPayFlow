"""Pure, local-time autobump policy; this module never changes runtime state."""

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone, tzinfo
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_AUTOBUMP_SCHEDULE = {
    "enabled": False,
    "start": "07:00",
    "stop": "01:00",
    "timezone": "Europe/Moscow",
}


def _schedule_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        # Windows Python does not necessarily include an IANA database. Moscow
        # has used UTC+3 without seasonal changes since 2014; this fallback is
        # for present-day schedules, not historical timezone conversion.
        if name == "Europe/Moscow":
            return timezone(timedelta(hours=3), name)
        raise ValueError(f"Unavailable IANA timezone: {name}") from exc
    except ValueError as exc:
        raise ValueError(f"Invalid IANA timezone: {name}") from exc


def normalize_schedule(value: Mapping | None = None) -> dict:
    """Return a fresh validated schedule; omitted fields use safe defaults.

    Invalid persisted values should be handled by the settings owner rather
    than silently turning an enabled restriction off. Equal start and stop are
    rejected: disable the schedule explicitly to permit the full day.
    """
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError("Autobump schedule must be an object")
    if set(value) - DEFAULT_AUTOBUMP_SCHEDULE.keys():
        raise ValueError("Unknown autobump schedule fields")
    result = {**DEFAULT_AUTOBUMP_SCHEDULE, **value}
    if type(result["enabled"]) is not bool:
        raise ValueError("Schedule enabled must be a boolean")
    for field in ("start", "stop"):
        clock = result[field]
        if not isinstance(clock, str) or not re.fullmatch(
            r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock
        ):
            raise ValueError(f"Schedule {field} must use HH:MM (00:00–23:59)")
    if result["start"] == result["stop"]:
        raise ValueError("Schedule start and stop must differ")
    name = result["timezone"]
    if not isinstance(name, str) or not name:
        raise ValueError("Schedule timezone must be an IANA timezone name")
    _schedule_timezone(name)
    return result


def allows_now(schedule: Mapping | None, now: datetime | None = None) -> bool:
    """Whether local wall time is inside [start, stop), including overnight.

    DST follows the timezone's local wall clock: repeated minutes have the same
    policy and skipped minutes do not occur. An explicit clock must be aware.
    """
    policy = normalize_schedule(schedule)
    if now is not None and (
        not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
    ):
        raise ValueError("Schedule clock must be an aware datetime")
    if not policy["enabled"]:
        return True
    instant = now if now is not None else datetime.now(timezone.utc)
    local = instant.astimezone(_schedule_timezone(policy["timezone"]))
    minute = local.hour * 60 + local.minute
    start = int(policy["start"][:2]) * 60 + int(policy["start"][3:])
    stop = int(policy["stop"][:2]) * 60 + int(policy["stop"][3:])
    if start < stop:
        return start <= minute < stop
    return minute >= start or minute < stop


def effective(
    user_enabled: bool, schedule: Mapping | None, now: datetime | None = None
) -> bool:
    """Manual enable and the schedule must both permit autobump."""
    if type(user_enabled) is not bool:
        raise ValueError("Autobump enabled must be a boolean")
    return user_enabled and allows_now(schedule, now)


def describe_schedule(schedule: Mapping | None) -> str:
    """Return a compact description suitable for the settings UI."""
    policy = normalize_schedule(schedule)
    state = "включено" if policy["enabled"] else "выключено"
    return f"{policy['start']}–{policy['stop']} ({policy['timezone']}), {state}"
