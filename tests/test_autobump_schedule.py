"""Schedule checks use explicit clocks and never run the application."""

from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from funpayflow.autobump_schedule import (
    DEFAULT_AUTOBUMP_SCHEDULE,
    allows_now,
    describe_schedule,
    effective,
    normalize_schedule,
)


def instant(hour, minute=0, second=0):
    return datetime(2026, 10, 16, hour, minute, second, tzinfo=timezone.utc)


def policy(start="07:00", stop="01:00"):
    return normalize_schedule({"enabled": True, "start": start, "stop": stop})


def test_defaults_are_disabled_and_independent():
    first = normalize_schedule()
    assert first == DEFAULT_AUTOBUMP_SCHEDULE
    first["enabled"] = True
    assert normalize_schedule()["enabled"] is False
    assert allows_now(None, instant(0))


@pytest.mark.parametrize("hour,minute,second,allowed", [
    (3, 59, 59, False), (4, 0, 0, True), (6, 59, 59, True),
    (7, 0, 0, False), (20, 0, 0, False),
])
def test_same_day_boundaries_in_moscow(hour, minute, second, allowed):
    assert allows_now(policy("07:00", "10:00"), instant(hour, minute, second)) is allowed


@pytest.mark.parametrize("hour,minute,allowed", [
    (20, 0, True), (21, 0, True), (21, 59, True),
    (22, 0, False), (3, 59, False), (4, 0, True),
])
def test_overnight_boundaries_and_local_midnight(hour, minute, allowed):
    assert allows_now(policy(), instant(hour, minute)) is allowed


def test_manual_off_always_wins_and_disabled_schedule_is_unrestricted():
    assert not effective(False, policy(), instant(4))
    assert not effective(True, policy(), instant(0))
    assert effective(True, {"enabled": False}, instant(0))


def test_json_restart_roundtrip_has_no_runtime_cursor_or_mutation():
    original = policy()
    serialized = json.dumps(original)
    for clock in (instant(22), instant(4), instant(21), instant(22)):
        assert allows_now(original, clock) == allows_now(json.loads(serialized), clock)
    assert json.dumps(original) == serialized


@pytest.mark.parametrize("value", [
    [], True, "07:00", {"enabled": 1}, {"enabled": "false"},
    {"start": "7:00"}, {"start": "24:00"}, {"stop": "12:60"},
    {"stop": "01:00\n"}, {"start": 700}, {"start": None},
    {"start": "07:00", "stop": "07:00"},
    {"timezone": None}, {"timezone": 3}, {"timezone": ""},
    {"timezone": "Does/NotExist"}, {"timezone": "../Europe/Moscow"},
    {"unexpected": True},
])
def test_invalid_settings_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_schedule(value)


@pytest.mark.parametrize("clock", [datetime(2026, 10, 16), "2026-10-16", 0])
def test_explicit_clock_must_be_aware(clock):
    with pytest.raises(ValueError):
        allows_now(policy(), clock)


def test_manual_flag_requires_actual_boolean():
    with pytest.raises(ValueError):
        effective("false", policy(), instant(4))


def test_moscow_fallback_without_iana_database():
    with patch("funpayflow.autobump_schedule.ZoneInfo", side_effect=ZoneInfoNotFoundError):
        assert allows_now(policy(), instant(4))
        assert not allows_now(policy(), instant(22))
        with pytest.raises(ValueError, match="Unavailable IANA timezone"):
            normalize_schedule({"timezone": "Europe/Berlin"})


def test_dst_fold_and_spring_gap_follow_local_wall_clock():
    try:
        ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        pytest.skip("IANA timezone database is not installed")
    schedule = {"enabled": True, "start": "01:00", "stop": "02:00",
                "timezone": "America/New_York"}
    # Both occurrences of 01:30 during the repeated autumn hour are allowed.
    for hour in (5, 6):
        assert allows_now(schedule, datetime(2026, 11, 1, hour, 30, tzinfo=timezone.utc))
    assert not allows_now(schedule, datetime(2026, 11, 1, 7, tzinfo=timezone.utc))
    schedule.update(start="01:30", stop="03:00")
    assert allows_now(schedule, datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc))
    assert not allows_now(schedule, datetime(2026, 3, 8, 7, tzinfo=timezone.utc))


def test_aware_datetime_in_other_offset_and_description():
    clock = datetime(2026, 10, 16, 6, tzinfo=timezone(timedelta(hours=2)))
    assert allows_now(policy(), clock)
    assert describe_schedule(policy()) == "07:00–01:00 (Europe/Moscow), включено"
