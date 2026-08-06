"""Change-feed keyset cursor + the on-demand "check now" lane.

These two are what let an SDK `watch()` run against a self-hosted coordinator:

* The cursor makes "everything after this point" EXACT. Without it a poller sees
  only the newest-first view, which silently drops changes whenever more than
  `limit` of them land between two polls — no error, the rows just never arrive.
* `check_now` is the only channel an HTTP-poll agent listens on. A DB flag never
  reaches it, so `POST /targets/{id}/run` writes a per-agent Redis set that the
  poll drains exactly once.

DB-backed: these use the shipped SQLite fixture (see conftest).
"""
import os
from datetime import datetime, timedelta, timezone

# Importing app code constructs Settings(), which refuses to build without a
# signing secret. Set the throwaway dev values BEFORE the imports below — the
# same convention the other app-importing suites use — so this file runs
# standalone and not just when an alphabetically earlier module happens to have
# set them first.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("ALLOW_INSECURE_DEV", "true")
os.environ.setdefault(
    "API_SECRET_KEY", "test-api-secret-0123456789abcdefABCDEF0123456789"
)

import pytest  # noqa: E402

from models.detected_change import DetectedChange  # noqa: E402
from models.target import Target  # noqa: E402
from routers.targets import (  # noqa: E402
    _apply_change_cursor,
    _parse_change_cursor,
)

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select  # noqa: E402

BASE = datetime(2026, 8, 5, 0, 0, 0, tzinfo=timezone.utc)


async def _target(db) -> Target:
    t = Target(url="https://example.com/pricing", check_type="content", enabled=True)
    db.add(t)
    await db.flush()
    return t


async def _change(db, target, *, seconds: int, first: int | None = None) -> DetectedChange:
    """One change row whose `last_detected_at` is BASE + `seconds`."""
    c = DetectedChange(
        target_id=target.id,
        content_hash=f"h{seconds}-{first}",
        diff_snippet=f"change at +{seconds}s",
        first_detected_at=BASE + timedelta(seconds=first if first is not None else seconds),
        last_detected_at=BASE + timedelta(seconds=seconds),
        agent_count=1,
    )
    db.add(c)
    await db.flush()
    return c


async def _walk(db, target, since_dt, since_id, limit):
    q = select(DetectedChange).where(DetectedChange.target_id == target.id)
    q = _apply_change_cursor(q, since_dt, since_id).limit(limit)
    return list((await db.execute(q)).scalars().all())


# --- cursor parsing ---------------------------------------------------------

def test_cursor_accepts_z_and_offset_forms():
    assert _parse_change_cursor("2026-08-05T00:00:00Z") == BASE
    assert _parse_change_cursor("2026-08-05T00:00:00+00:00") == BASE
    assert _parse_change_cursor(None) is None
    assert _parse_change_cursor("") is None


def test_naive_cursor_is_read_as_utc():
    # A caller that drops the zone must not silently shift the window.
    assert _parse_change_cursor("2026-08-05T00:00:00") == BASE


def test_garbage_cursor_is_rejected_not_ignored():
    # Ignoring it would downgrade an incremental poll to a full re-read, and the
    # caller would never know it just reprocessed its whole history.
    with pytest.raises(HTTPException) as exc:
        _parse_change_cursor("last-tuesday")
    assert exc.value.status_code == 422


# --- the walk ---------------------------------------------------------------

async def test_no_cursor_is_newest_first(db_session):
    t = await _target(db_session)
    for s in (1, 2, 3):
        await _change(db_session, t, seconds=s)

    rows = await _walk(db_session, t, None, None, 10)
    assert [r.last_detected_at for r in rows] == sorted(
        (r.last_detected_at for r in rows), reverse=True
    ), "the browsing view must stay newest-first"


async def test_cursor_walks_forward_without_gaps(db_session):
    t = await _target(db_session)
    for s in (1, 2, 3, 4, 5):
        await _change(db_session, t, seconds=s)

    # Page size 2 forces three pages — the exact case a newest-first poller drops
    # rows on.
    seen, since, since_id = [], _parse_change_cursor("1970-01-01T00:00:00Z"), 0
    for _ in range(5):
        page = await _walk(db_session, t, since, since_id, 2)
        if not page:
            break
        seen += [r.id for r in page]
        since, since_id = page[-1].last_detected_at, page[-1].id

    assert len(seen) == 5, f"cursor walk lost rows: {seen}"
    assert len(set(seen)) == 5, f"cursor walk repeated rows: {seen}"


async def test_ties_on_the_same_instant_all_arrive(db_session):
    # `last_detected_at` is NOT unique. Ordering on it alone lets two rows in the
    # same instant straddle the page boundary, and the trailing one is never
    # returned again — which is why the cursor compares (timestamp, id) as a pair.
    t = await _target(db_session)
    for first in (1, 2, 3):
        await _change(db_session, t, seconds=9, first=first)

    seen, since, since_id = [], _parse_change_cursor("1970-01-01T00:00:00Z"), 0
    for _ in range(5):
        page = await _walk(db_session, t, since, since_id, 1)  # one row per page
        if not page:
            break
        seen.append(page[0].id)
        since, since_id = page[0].last_detected_at, page[0].id

    assert len(seen) == 3 and len(set(seen)) == 3, f"tie-break lost or looped: {seen}"


async def test_redetection_resurfaces_as_a_new_detection(db_session):
    """A row is UPDATED (not re-inserted) when the same difference recurs.

    So an id already processed legitimately reappears with a later cursor value.
    That is a fresh detection, not a duplicate — and the walk must deliver it.
    """
    t = await _target(db_session)
    c = await _change(db_session, t, seconds=1)

    cursor = c.last_detected_at
    assert await _walk(db_session, t, cursor, c.id, 10) == [], "cursor row must not repeat"

    c.last_detected_at = BASE + timedelta(seconds=30)  # re-detected later
    await db_session.flush()

    again = await _walk(db_session, t, cursor, c.id, 10)
    assert [r.id for r in again] == [c.id], "a re-detection must be delivered"
