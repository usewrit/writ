"""Traffic-class admission: reservations must protect against CONTENTION, not
fence off idle slots.

Regression cover for the bug that made Dragnet crawls never launch. Crawl shards
are minted with queue_traffic_type='scheduled'. On any fleet below ~6 slots the
leftover-slot distribution (direct → called → scheduled) leaves
reserved['scheduled'] == 0, so:

  * rule 1 (under own guarantee) can never pass — 0 < 0 is false;
  * rule 2 (borrow) subtracted the OTHER classes' full reservations from the free
    slots regardless of whether those classes had any work waiting, which on an
    idle fleet is everything.

→ a scheduled task was denied forever on an idle single-agent self-host, and the
crawl sat queued until its 2h expiry.
"""
import pytest

from services import recorder_capacity_manager as rcm

ADMIT = rcm.RecorderCapacityManager._admit
IDLE = {"direct": 0, "called": 0, "scheduled": 0}


@pytest.mark.parametrize("total_slots", [1, 2, 3, 4, 5, 6, 10])
def test_scheduled_starts_on_an_idle_fleet_of_any_size(total_slots):
    """The core regression: a crawl shard must dispatch whenever the fleet is idle
    and nothing else is queued — at EVERY fleet size, not only >= 6 slots."""
    reserved = rcm._class_reservations(total_slots)
    waiting = {"direct": 0, "called": 0, "scheduled": 1}   # only the crawl shard
    assert ADMIT("scheduled", total_slots, reserved, IDLE, waiting) is True, (
        f"idle {total_slots}-slot fleet denied a scheduled task; reserved={reserved}"
    )


def test_the_old_contention_blind_rule_is_what_deadlocked_small_fleets():
    """Pin the old behavior so the fix can't silently regress: with no `waiting`
    tally, a 2-slot idle fleet still refuses scheduled work."""
    reserved = rcm._class_reservations(2)
    assert reserved["scheduled"] == 0
    assert ADMIT("scheduled", 2, reserved, IDLE, None) is False


def test_reservation_still_protects_a_starved_class_under_contention():
    """Free slots ARE held for another class that actually has work queued.

    `scheduled` must be AT its guarantee for this to test anything — below it,
    rule 1 admits before the borrow test is reached.
    """
    reserved = {"direct": 4, "called": 4, "scheduled": 2}
    running = {"direct": 8, "called": 0, "scheduled": 2}
    waiting = {"direct": 0, "called": 3, "scheduled": 5}
    # 'called' is starved and waiting → the 2 free slots are not borrowable.
    assert ADMIT("scheduled", 2, reserved, running, waiting) is False


def test_slots_are_borrowable_when_the_other_class_has_no_demand():
    """Same shape as above, but nothing is queued for 'called' → borrow allowed."""
    reserved = {"direct": 4, "called": 4, "scheduled": 2}
    running = {"direct": 8, "called": 0, "scheduled": 2}
    waiting = {"direct": 0, "called": 0, "scheduled": 5}
    assert ADMIT("scheduled", 2, reserved, running, waiting) is True


def test_a_class_under_its_own_guarantee_is_always_admitted():
    reserved = {"direct": 4, "called": 4, "scheduled": 2}
    running = {"direct": 2, "called": 9, "scheduled": 9}
    waiting = {"direct": 1, "called": 9, "scheduled": 9}
    assert ADMIT("direct", 1, reserved, running, waiting) is True


def test_no_free_slot_is_never_admitted():
    reserved = {"direct": 4, "called": 4, "scheduled": 2}
    assert ADMIT("scheduled", 0, reserved, IDLE, {"direct": 0, "called": 0}) is False


def test_interactive_work_is_not_starved_by_a_running_crawl():
    """The reservation's whole point: a crawl saturating the fleet must not lock the
    owner out of their own dashboard run."""
    reserved = rcm._class_reservations(10)          # {direct:4, called:4, scheduled:2}
    running = {"direct": 0, "called": 0, "scheduled": 9}
    waiting = {"direct": 1, "called": 0, "scheduled": 20}
    assert ADMIT("direct", 1, reserved, running, waiting) is True
    # ...and the crawl cannot take that last slot while direct work waits.
    assert ADMIT("scheduled", 1, reserved, running, waiting) is False


def test_reservations_still_sum_to_the_fleet():
    for n in range(0, 40):
        assert sum(rcm._class_reservations(n).values()) == n
