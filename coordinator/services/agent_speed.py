"""
Agent speed/capacity classification — single source of truth.

Every recorder/automation agent advertises, alongside its `max_sessions`
capacity, a small performance profile so the dispatcher can route work to the
RIGHT kind of box:

  * FAST boxes (high single-core clock, fewer slots)  -> latency-sensitive work:
    live streaming sessions, interactive recording, DIRECT dashboard runs.
  * THROUGHPUT boxes (many slots, lower per-core clock) -> batch work:
    SCHEDULED + CALLED (marketplace/API/webhook) workflow runs, content checks.
  * BALANCED -> anything; the default when we can't tell.

How a class is decided (hybrid, in precedence order):
  1. Explicit operator tag — the agent sends `speed_class` (from the
     AGENT_SPEED_CLASS env var on infra boxes). The operator knows which server
     is which, so an explicit tag always wins.
  2. Benchmark — a single-core `perf_score` the agent measures at startup,
     normalised so a modern fast core lands ~>=2000 and an old low-clock core
     ~<=1200. Works for BYO/user agents that aren't tagged.
  3. Clock/capacity heuristic — nominal CPU clock + slot count, as a last resort.
  4. "balanced" if nothing is known.

Lower `speed_rank` == faster (sorts first when we want the fastest box).
"""
from __future__ import annotations

from typing import Optional

# Class literals (kept as plain strings so they round-trip through JSON / JWT
# claims / Redis without any enum coupling across services).
FAST = "fast"
THROUGHPUT = "throughput"
BALANCED = "balanced"
VALID_CLASSES = (FAST, THROUGHPUT, BALANCED)

# Faster == smaller. Used as the primary dispatch sort key.
SPEED_RANK = {FAST: 0, BALANCED: 1, THROUGHPUT: 2}

# --- Heuristic thresholds (tunable; the ONLY place these live) ---------------
# perf_score is a normalised single-core benchmark: modern desktop-class cores
# (e.g. Xeon E-2288G @5GHz) land high; old low-power server cores (E5-2650L v2
# @1.7GHz) land low. See the agent benchmark for the normalisation.
FAST_PERF_SCORE = 2000          # >= this single-core score -> fast
SLOW_PERF_SCORE = 1300          # <= this -> candidate for throughput (if big)
# Capacity that, combined with a non-fast core, marks a "big batch" box.
THROUGHPUT_MIN_SESSIONS = 25
# Clock fallbacks (MHz) when no perf_score was reported.
FAST_CLOCK_MHZ = 3200
SLOW_CLOCK_MHZ = 2400


def normalize_class(value: Optional[str]) -> Optional[str]:
    """Return a valid class literal, or None if the value isn't recognised."""
    if not value:
        return None
    v = str(value).strip().lower()
    # A few friendly aliases the operator might type.
    if v in ("fast", "speed", "latency", "interactive"):
        return FAST
    if v in ("throughput", "big", "batch", "bulk", "slow"):
        return THROUGHPUT
    if v in ("balanced", "auto", "default", "mixed"):
        return BALANCED
    return None


def derive_speed_class(
    *,
    explicit: Optional[str] = None,
    perf_score: Optional[int] = None,
    max_sessions: Optional[int] = None,
    clock_mhz: Optional[int] = None,
) -> str:
    """Resolve an agent's speed class from whatever signals we have.

    Precedence: explicit operator tag > benchmark perf_score > clock/capacity
    heuristic > BALANCED. Never raises — always returns a valid class literal.
    """
    tagged = normalize_class(explicit)
    if tagged:
        return tagged

    slots = max_sessions or 0

    if perf_score and perf_score > 0:
        if perf_score >= FAST_PERF_SCORE:
            return FAST
        if perf_score <= SLOW_PERF_SCORE and slots >= THROUGHPUT_MIN_SESSIONS:
            return THROUGHPUT
        return BALANCED

    if clock_mhz and clock_mhz > 0:
        if clock_mhz >= FAST_CLOCK_MHZ:
            return FAST
        if clock_mhz <= SLOW_CLOCK_MHZ and slots >= THROUGHPUT_MIN_SESSIONS:
            return THROUGHPUT
        return BALANCED

    if slots >= THROUGHPUT_MIN_SESSIONS:
        return THROUGHPUT
    return BALANCED


def speed_rank(speed_class: Optional[str]) -> int:
    """Sort key — lower is faster. Unknown classes sort as BALANCED."""
    return SPEED_RANK.get(normalize_class(speed_class) or BALANCED, SPEED_RANK[BALANCED])


def profile_from_meta(meta: Optional[dict]) -> dict:
    """Pull the perf profile out of an agent.meta dict (defensive, never raises).

    Returns a normalised dict: {speed_class, perf_score, cpu_cores, cpu_threads,
    cpu_clock_mhz, ram_mb}. Re-derives the class if it wasn't stored, so old
    agents (pre-profiling) still classify from whatever capacity data exists.
    """
    meta = meta or {}
    perf = meta.get("perf") or {}
    perf_score = _as_int(perf.get("perf_score"))
    cpu_cores = _as_int(perf.get("cpu_cores"))
    cpu_threads = _as_int(perf.get("cpu_threads"))
    clock_mhz = _as_int(perf.get("cpu_clock_mhz"))
    ram_mb = _as_int(perf.get("ram_mb"))
    max_sessions = _as_int(meta.get("max_sessions")) or 0

    speed_class = normalize_class(meta.get("speed_class") or perf.get("speed_class"))
    if not speed_class:
        speed_class = derive_speed_class(
            perf_score=perf_score,
            max_sessions=max_sessions,
            clock_mhz=clock_mhz,
        )

    return {
        "speed_class": speed_class,
        "perf_score": perf_score or 0,
        "cpu_cores": cpu_cores or 0,
        "cpu_threads": cpu_threads or 0,
        "cpu_clock_mhz": clock_mhz or 0,
        "ram_mb": ram_mb or 0,
    }


def _as_int(value) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
