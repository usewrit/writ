"""Structured schedule recurrence unit tests (no DB, no network).

THE INVARIANT under test: services.schedule_recurrence computes the next fire
time identically to the cross-layer SPEC §4 (interval | daily | weekly), is
timezone/DST-aware, and normalize_schedule fail-closes on invalid daily/weekly
input. The 'interval' kind must stay byte-identical to the old
`now + timedelta(ms=interval_ms)` behaviour so existing schedules never shift.

Runnable with plain `python3 selfhost/coordinator/tests/test_schedule_recurrence.py`.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:  # pytest drives this under the suite; script-style runs without it.
    import pytest
except ModuleNotFoundError:  # pragma: no cover - script-style fallback
    pytest = None

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from services.schedule_recurrence import (  # noqa: E402
    ScheduleValidationError,
    compute_next_run,
    normalize_schedule,
    prev_occurrence,
    human_label,
)


# --- interval kind: byte-identical to now + interval_ms ---------------------

def test_interval_is_now_plus_interval_ms():
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    nxt = compute_next_run("interval", now, 900_000)  # 15 min
    assert nxt == now + timedelta(milliseconds=900_000)


def test_interval_naive_now_is_treated_as_utc():
    now_naive = datetime(2026, 7, 5, 12, 0, 0)
    nxt = compute_next_run("interval", now_naive, 60_000)
    assert nxt == now_naive.replace(tzinfo=timezone.utc) + timedelta(minutes=1)


def test_none_kind_defaults_to_interval():
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    assert compute_next_run(None, now, 60_000) == now + timedelta(minutes=1)


# --- daily: rolls to tomorrow once today's time has passed ------------------

def test_daily_rolls_to_tomorrow_when_time_passed():
    # 14:00 UTC now, fire at 09:00 UTC → tomorrow 09:00 UTC.
    now = datetime(2026, 7, 5, 14, 0, 0, tzinfo=timezone.utc)
    nxt = compute_next_run("daily", now, None, "09:00", None, "UTC")
    assert nxt == datetime(2026, 7, 6, 9, 0, 0, tzinfo=timezone.utc)


def test_daily_same_day_when_time_still_ahead():
    now = datetime(2026, 7, 5, 8, 0, 0, tzinfo=timezone.utc)
    nxt = compute_next_run("daily", now, None, "09:00", None, "UTC")
    assert nxt == datetime(2026, 7, 5, 9, 0, 0, tzinfo=timezone.utc)


# --- weekly: next matching weekday ------------------------------------------

def test_weekly_finds_next_allowed_weekday():
    # 2026-07-05 is a Sunday (isoweekday 7). Next Wednesday (3) is 2026-07-08.
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    nxt = compute_next_run("weekly", now, None, "13:00", [3], "UTC")
    assert nxt == datetime(2026, 7, 8, 13, 0, 0, tzinfo=timezone.utc)


def test_weekly_same_day_before_time():
    # Sunday 07-05 at 08:00; allowed day includes Sunday (7), fire 13:00 → today.
    now = datetime(2026, 7, 5, 8, 0, 0, tzinfo=timezone.utc)
    nxt = compute_next_run("weekly", now, None, "13:00", [7], "UTC")
    assert nxt == datetime(2026, 7, 5, 13, 0, 0, tzinfo=timezone.utc)


# --- non-UTC tz -------------------------------------------------------------

def test_daily_non_utc_timezone():
    # 09:00 in New York on 2026-07-05 (EDT, UTC-4) == 13:00 UTC.
    now = datetime(2026, 7, 5, 6, 0, 0, tzinfo=timezone.utc)  # 02:00 local NY
    nxt = compute_next_run("daily", now, None, "09:00", None, "America/New_York")
    assert nxt == datetime(2026, 7, 5, 13, 0, 0, tzinfo=timezone.utc)
    # And the local wall clock really is 09:00 in New York.
    assert nxt.astimezone(ZoneInfo("America/New_York")).hour == 9


def test_invalid_tz_falls_back_to_utc():
    now = datetime(2026, 7, 5, 8, 0, 0, tzinfo=timezone.utc)
    nxt = compute_next_run("daily", now, None, "09:00", None, "Not/AReal_Zone")
    assert nxt == datetime(2026, 7, 5, 9, 0, 0, tzinfo=timezone.utc)


# --- normalize_schedule: validation gate ------------------------------------

def test_normalize_interval_clears_structured_fields():
    assert normalize_schedule("interval", "09:00", [1, 2], "UTC") == (
        "interval", None, None, None,
    )


def test_normalize_daily_coerces_missing_tz_to_utc():
    assert normalize_schedule("daily", "09:00", None, None) == (
        "daily", "09:00", None, "UTC",
    )


def test_normalize_weekly_dedupes_and_sorts_days():
    kind, time_, days, tz = normalize_schedule("weekly", "13:00", [3, 3, 1], "UTC")
    assert (kind, time_, tz) == ("weekly", "13:00", "UTC")
    assert days == [1, 3]


def test_normalize_rejects_bad_time():
    for bad in ("", "25:00", "9:00", "09:60", "noon"):
        try:
            normalize_schedule("daily", bad, None, "UTC")
            assert False, f"expected rejection for time {bad!r}"
        except ScheduleValidationError:
            pass


def test_normalize_rejects_empty_weekly_days():
    for bad in (None, [], [0], [8]):
        try:
            normalize_schedule("weekly", "13:00", bad, "UTC")
            assert False, f"expected rejection for days {bad!r}"
        except ScheduleValidationError:
            pass


def test_normalize_rejects_unknown_kind():
    try:
        normalize_schedule("hourly", None, None, None)
        assert False, "expected rejection for unknown kind"
    except ScheduleValidationError:
        pass


# --- prev_occurrence (automation due helper) --------------------------------

def test_prev_occurrence_interval_is_none():
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    assert prev_occurrence("interval", now, None, None, None) is None


def test_prev_occurrence_daily_latest_past_time():
    # 14:00 UTC now, fire 09:00 → latest occurrence is today 09:00 UTC.
    now = datetime(2026, 7, 5, 14, 0, 0, tzinfo=timezone.utc)
    prev = prev_occurrence("daily", now, "09:00", None, "UTC")
    assert prev == datetime(2026, 7, 5, 9, 0, 0, tzinfo=timezone.utc)


def test_prev_occurrence_daily_rolls_back_when_time_ahead():
    # 08:00 UTC now, fire 09:00 → latest occurrence is yesterday 09:00 UTC.
    now = datetime(2026, 7, 5, 8, 0, 0, tzinfo=timezone.utc)
    prev = prev_occurrence("daily", now, "09:00", None, "UTC")
    assert prev == datetime(2026, 7, 4, 9, 0, 0, tzinfo=timezone.utc)


# --- human_label ------------------------------------------------------------

def test_human_label_interval_minutes_and_hours():
    assert human_label("interval", 900_000) == "Every 15 min"
    assert human_label("interval", 3_600_000) == "Every 1h"


def test_human_label_daily_and_weekly():
    assert human_label("daily", None, "12:00") == "Daily at 12:00"
    assert human_label("weekly", None, "13:00", [3, 5]) == "Wed, Fri at 13:00"


if __name__ == "__main__":  # pragma: no cover - script-style run
    import types

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and isinstance(fn, types.FunctionType):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {e!r}")
    print(f"\n{'PASS' if not failures else 'FAILURES: ' + str(failures)}")
    sys.exit(1 if failures else 0)
