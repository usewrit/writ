"""
PATCH /targets/{id} must reconcile the target_selectors row create_target mints.

Agents are shipped ONLY target_selectors rows (routers/agents.py builds each
check's selector list from `target.selectors`; the flat Target.selector column
never reaches an agent). Before the reconciliation, editing the flat selector
was therefore a silent no-op at the agent: the API reported the new selector
while the fleet kept checking the old one. These tests pin the repair:

- the minted row is renamed (or minted, for pre-repair flat-only targets),
- stale baselines are cleared target-level AND row-level so the next agent
  report SEEDS a fresh baseline (services/report_ingest.py) instead of firing
  a bogus change_detected against the old selector's content,
- the flat ignore_regex is mirrored onto the row carrying the flat selector,
- a viewport-zone selector promotes the target to the browser lane,
- malformed CSS is rejected with a 400 instead of erroring on every check.
"""
import os
from datetime import datetime

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

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select  # noqa: E402

from models.target import Target  # noqa: E402
from models.target_selector import TargetSelector  # noqa: E402
from routers.targets import UpdateTargetRequest, update_target  # noqa: E402

API_KEY = {"id": 1, "label": "test-key"}  # no "scopes" key -> scope check passes


async def _content_target(db, *, selector="#old-price", enabled=True, **overrides) -> Target:
    fields = dict(
        url="https://example.com/pricing",
        check_type="content",
        selector=selector,
        check_period_ms=900000,
        enabled=enabled,
        baseline_hash="a" * 64,
        baseline_content="99.00",
        baseline_fetched_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    fields.update(overrides)
    target = Target(**fields)
    db.add(target)
    await db.flush()
    return target


async def _minted_row(db, target: Target, *, selector=None, name=None, **overrides) -> TargetSelector:
    """A row exactly as create_target mints it: named after its selector text."""
    selector = selector if selector is not None else target.selector
    fields = dict(
        target_id=target.id,
        name=name if name is not None else selector[:255],
        selector=selector,
        content_type="text",
        enabled=True,
        priority=0,
        ignore_regex=target.ignore_regex,
        baseline_hash="b" * 64,
        baseline_content="99.00",
        baseline_fetched_at=datetime.utcnow(),
    )
    fields.update(overrides)
    row = TargetSelector(**fields)
    db.add(row)
    await db.flush()
    return row


async def _rows(db, target: Target) -> list[TargetSelector]:
    return (
        await db.execute(
            select(TargetSelector).where(TargetSelector.target_id == target.id)
        )
    ).scalars().all()


@pytest.mark.asyncio
async def test_selector_edit_renames_minted_row_and_clears_baselines(db_session):
    target = await _content_target(db_session)
    await _minted_row(db_session, target)

    info = await update_target(
        target.id, UpdateTargetRequest(selector="#new-price"), db_session, API_KEY
    )

    assert info.selector == "#new-price"
    # Target-level baseline cleared -> next report seeds, never diffs.
    assert target.baseline_hash is None
    assert target.baseline_content is None
    assert target.baseline_fetched_at is None

    rows = await _rows(db_session, target)
    assert len(rows) == 1
    row = rows[0]
    # The row agents actually check now carries the NEW selector...
    assert row.selector == "#new-price"
    # ...the auto-minted name follows the create_target convention...
    assert row.name == "#new-price"
    assert row.content_type == "text"
    # ...and its baseline is cleared so the next report re-seeds it.
    assert row.baseline_hash is None
    assert row.baseline_content is None
    assert row.baseline_fetched_at is None
    assert row.baseline_screenshot is None


@pytest.mark.asyncio
async def test_selector_edit_keeps_user_authored_row_name(db_session):
    target = await _content_target(db_session)
    await _minted_row(db_session, target, name="Price box")

    await update_target(
        target.id, UpdateTargetRequest(selector="#new-price"), db_session, API_KEY
    )

    rows = await _rows(db_session, target)
    assert len(rows) == 1
    assert rows[0].selector == "#new-price"
    assert rows[0].name == "Price box"


@pytest.mark.asyncio
async def test_selector_edit_mints_row_for_flat_only_target(db_session):
    # A target from before create_target minted rows: flat selector, zero rows,
    # never actually checked by any agent. disabled here so the test exercises
    # the mint without walking into the redistribution machinery.
    target = await _content_target(db_session, enabled=False, ignore_regex=r"\d+ views")
    assert await _rows(db_session, target) == []

    await update_target(
        target.id, UpdateTargetRequest(selector="#new-price"), db_session, API_KEY
    )

    rows = await _rows(db_session, target)
    assert len(rows) == 1
    row = rows[0]
    assert row.selector == "#new-price"
    assert row.name == "#new-price"
    assert row.enabled is True
    assert row.content_type == "text"
    # The flat ignore_regex travels with the minted row, as on create.
    assert row.ignore_regex == r"\d+ views"
    assert row.baseline_hash is None


@pytest.mark.asyncio
async def test_ignore_regex_only_patch_mirrors_to_row(db_session):
    # Regression: this PATCH used to 500 (InputValidator was only imported
    # inside the `if 'url'` branch) and, once past that, never reached the row
    # agents check — the old regex kept masking/unmasking content at the agent.
    target = await _content_target(db_session)
    row = await _minted_row(db_session, target)

    await update_target(
        target.id, UpdateTargetRequest(ignore_regex=r"\d+ watchers"), db_session, API_KEY
    )

    assert target.ignore_regex == r"\d+ watchers"
    assert row.ignore_regex == r"\d+ watchers"
    # What the regex hides changed -> stored baselines are stale on both levels.
    assert target.baseline_hash is None
    assert row.baseline_hash is None
    assert row.baseline_content is None


@pytest.mark.asyncio
async def test_url_edit_clears_every_row_baseline(db_session):
    target = await _content_target(db_session)
    await _minted_row(db_session, target)
    await _minted_row(db_session, target, selector="#stock", name="Stock")

    await update_target(
        target.id,
        UpdateTargetRequest(url="https://example.com/other-page"),
        db_session,
        API_KEY,
    )

    rows = await _rows(db_session, target)
    assert len(rows) == 2
    for row in rows:
        assert row.baseline_hash is None
        assert row.baseline_content is None
        assert row.baseline_fetched_at is None
        assert row.baseline_screenshot is None


@pytest.mark.asyncio
async def test_viewport_zone_selector_promotes_browser_lane(db_session):
    target = await _content_target(db_session, requires_playwright=False)
    await _minted_row(db_session, target)

    zone = 'viewport-zone:{"x":0,"y":0,"width":800,"height":600}'
    await update_target(
        target.id, UpdateTargetRequest(selector=zone), db_session, API_KEY
    )

    rows = await _rows(db_session, target)
    assert len(rows) == 1
    assert rows[0].selector == zone
    assert rows[0].content_type == "visual"
    # A screenshot region can only be captured in a real browser.
    assert target.requires_playwright is True


@pytest.mark.asyncio
async def test_selector_edit_onto_api_managed_row_does_not_duplicate(db_session):
    target = await _content_target(db_session)
    await _minted_row(db_session, target)
    await _minted_row(db_session, target, selector="#new-price", name="New price")

    await update_target(
        target.id, UpdateTargetRequest(selector="#new-price"), db_session, API_KEY
    )

    rows = await _rows(db_session, target)
    # No third row, no rename of the old row onto the taken selector (which
    # would violate the (target_id, selector) unique constraint).
    assert sorted(r.selector for r in rows) == ["#new-price", "#old-price"]


@pytest.mark.asyncio
async def test_malformed_selector_is_rejected_with_400(db_session):
    target = await _content_target(db_session)
    await _minted_row(db_session, target)

    with pytest.raises(HTTPException) as exc_info:
        await update_target(
            target.id, UpdateTargetRequest(selector="div[unclosed"), db_session, API_KEY
        )
    assert exc_info.value.status_code == 400
    assert "invalid css selector" in str(exc_info.value.detail).lower()
    # The bad PATCH must not have half-applied.
    rows = await _rows(db_session, target)
    assert rows[0].selector == "#old-price"
