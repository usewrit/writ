"""Structured schedule recurrence — pure helpers shared by workflows, monitors, automations.

Reference implementation (copy verbatim into both
`schedule-recurrence service` and
`selfhost/coordinator/services/schedule_recurrence.py`).

Structured recurrence, user-local timezone, DST-aware. No third-party deps (stdlib zoneinfo).
The existing "every N ms" behavior is `kind == "interval"` and is byte-identical to before.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VALID_KINDS = ("interval", "daily", "weekly")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_WEEKDAY_ABBR = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}


class ScheduleValidationError(ValueError):
    """Raised when a structured schedule is invalid; routers map this to HTTP 400."""


def _load_tz(tz_name: Optional[str]) -> ZoneInfo:
    if not tz_name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


def _parse_hhmm(time_hhmm: Optional[str]) -> Tuple[int, int]:
    if time_hhmm and _TIME_RE.match(time_hhmm):
        hh, mm = time_hhmm.split(":")
        return int(hh), int(mm)
    return 0, 0


def _as_utc(dt: datetime) -> datetime:
    """Normalize any datetime to an aware UTC datetime."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _local_at(tz: ZoneInfo, year: int, month: int, day: int, hh: int, mm: int) -> datetime:
    """Wall-clock hh:mm on a calendar date in tz (DST handled by zoneinfo)."""
    return datetime(year, month, day, hh, mm, tzinfo=tz)


def normalize_schedule(
    kind: Optional[str],
    time_hhmm: Optional[str],
    days: Optional[List[int]],
    tz_name: Optional[str],
) -> Tuple[str, Optional[str], Optional[List[int]], Optional[str]]:
    """Validate + normalize a structured schedule. Returns the tuple to persist.

    Raises ScheduleValidationError for daily/weekly with a bad time or empty/out-of-range days.
    `interval` returns (kind, None, None, None). tz is coerced to a valid IANA name (fallback UTC).
    """
    k = (kind or "interval").lower()
    if k not in VALID_KINDS:
        raise ScheduleValidationError(f"schedule_kind must be one of {VALID_KINDS}")

    if k == "interval":
        return "interval", None, None, None

    if not time_hhmm or not _TIME_RE.match(time_hhmm):
        raise ScheduleValidationError("schedule_time must be 'HH:MM' (00:00–23:59) for daily/weekly schedules")

    # Coerce tz to a valid IANA name, defaulting to UTC.
    resolved_tz = "UTC"
    if tz_name:
        try:
            ZoneInfo(tz_name)
            resolved_tz = tz_name
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            resolved_tz = "UTC"

    if k == "daily":
        return "daily", time_hhmm, None, resolved_tz

    # weekly
    cleaned = sorted({int(d) for d in (days or []) if isinstance(d, (int, float))})
    if not cleaned or any(d < 1 or d > 7 for d in cleaned):
        raise ScheduleValidationError("weekly schedule requires schedule_days as a non-empty subset of 1..7 (1=Mon)")
    return "weekly", time_hhmm, cleaned, resolved_tz


def compute_next_run(
    kind: Optional[str],
    now_utc: datetime,
    interval_ms: Optional[int],
    time_hhmm: Optional[str] = None,
    days: Optional[List[int]] = None,
    tz_name: Optional[str] = None,
) -> datetime:
    """Next fire time (aware UTC). See SPEC §4."""
    now_utc = _as_utc(now_utc)
    k = (kind or "interval").lower()

    if k == "interval":
        return now_utc + timedelta(milliseconds=int(interval_ms or 0))

    tz = _load_tz(tz_name)
    hh, mm = _parse_hhmm(time_hhmm)
    local_now = now_utc.astimezone(tz)

    if k == "daily":
        cand = _local_at(tz, local_now.year, local_now.month, local_now.day, hh, mm)
        if cand <= local_now:
            nxt = (local_now + timedelta(days=1))
            cand = _local_at(tz, nxt.year, nxt.month, nxt.day, hh, mm)
        return _as_utc(cand)

    # weekly
    allowed = {int(d) for d in (days or [])}
    for offset in range(0, 8):
        d = local_now + timedelta(days=offset)
        if d.isoweekday() in allowed:
            cand = _local_at(tz, d.year, d.month, d.day, hh, mm)
            if cand > local_now:
                return _as_utc(cand)
    # safety fallback (empty/none matched)
    return now_utc + timedelta(milliseconds=max(int(interval_ms or 0), 86_400_000))


def prev_occurrence(
    kind: Optional[str],
    now_utc: datetime,
    time_hhmm: Optional[str] = None,
    days: Optional[List[int]] = None,
    tz_name: Optional[str] = None,
) -> Optional[datetime]:
    """Latest scheduled occurrence <= now (aware UTC), or None. Used for automation due checks."""
    now_utc = _as_utc(now_utc)
    k = (kind or "interval").lower()
    if k == "interval":
        return None
    tz = _load_tz(tz_name)
    hh, mm = _parse_hhmm(time_hhmm)
    local_now = now_utc.astimezone(tz)

    if k == "daily":
        cand = _local_at(tz, local_now.year, local_now.month, local_now.day, hh, mm)
        if cand > local_now:
            prev = local_now - timedelta(days=1)
            cand = _local_at(tz, prev.year, prev.month, prev.day, hh, mm)
        return _as_utc(cand)

    allowed = {int(d) for d in (days or [])}
    for offset in range(0, 8):
        d = local_now - timedelta(days=offset)
        if d.isoweekday() in allowed:
            cand = _local_at(tz, d.year, d.month, d.day, hh, mm)
            if cand <= local_now:
                return _as_utc(cand)
    return None


def human_label(
    kind: Optional[str],
    interval_ms: Optional[int],
    time_hhmm: Optional[str] = None,
    days: Optional[List[int]] = None,
) -> str:
    """Short human summary for read-only displays."""
    k = (kind or "interval").lower()
    if k == "interval":
        ms = int(interval_ms or 0)
        if ms <= 0:
            return "—"
        mins = ms // 60000
        if mins < 60:
            return f"Every {mins} min"
        hours = mins / 60
        if hours < 24:
            return f"Every {int(hours)}h" if hours.is_integer() else f"Every {hours:.1f}h"
        return f"Every {int(hours // 24)}d"
    if k == "daily":
        return f"Daily at {time_hhmm or '00:00'}"
    labels = ", ".join(_WEEKDAY_ABBR.get(int(d), "?") for d in (days or []))
    return f"{labels or '—'} at {time_hhmm or '00:00'}"
