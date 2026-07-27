"""
Pure decision rules for auto-buy execution, shared as the canonical contract the
agents (Rust saas_bridge + Python desktop-agent) mirror. Kept dependency-free so
it is unit-testable in isolation.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def compute_commit_index(steps: List[Dict[str, Any]]) -> Optional[int]:
    """Index of the irreversible 'place order' step.

    An explicit per-step ``commit: true`` flag wins (top-level or under config);
    otherwise the last enabled step (a recorded checkout ends on the order click).
    Returns None for an empty list. In dry run the agent stops BEFORE this index.
    """
    for i, s in enumerate(steps):
        cfg = s.get("config") if isinstance(s.get("config"), dict) else {}
        if s.get("commit") or cfg.get("commit"):
            return i
    for i in range(len(steps) - 1, -1, -1):
        if steps[i].get("type") and steps[i].get("enabled", True):
            return i
    return None


def is_saved_card_selector(selector: Optional[str]) -> bool:
    """True if a fill selector targets a card field — skipped for saved-method/autofill."""
    s = (selector or "").lower()
    return any(t in s for t in ("card", "cvv", "cvc", "cc-num", "creditcard", "expir"))


def card_field_for_selector(selector: Optional[str]) -> Optional[str]:
    """Which issued virtual-card field a card-like fill selector maps to (or None)."""
    s = (selector or "").lower()
    if any(t in s for t in ("cvv", "cvc", "security")):
        return "vcard_cvv"
    if "exp" in s:
        return "vcard_exp"
    if "name" in s:
        return "vcard_name"
    if any(t in s for t in ("card", "cc-num", "creditcard", "number")):
        return "vcard_number"
    return None


def parse_amount(raw: Any) -> Optional[float]:
    """Best-effort numeric price from extracted text ('$1,299.00' -> 1299.0)."""
    if raw is None:
        return None
    try:
        cleaned = (
            str(raw).replace(",", "").replace("$", "").replace("€", "").replace("£", "").strip()
        )
        return float(cleaned)
    except (ValueError, TypeError):
        return None


# Rolling spend-cap window lengths, in seconds — single source for both the
# pre-issue reserve (unified_trigger_service) and the completion reconcile
# (routers.automation), so the two never disagree on the TTL of a period bucket.
SPEND_PERIOD_SECONDS: Dict[str, int] = {
    "day": 86400,
    "week": 604800,
    "month": 2592000,
}


def spend_period_seconds(period: Optional[str]) -> int:
    """TTL (seconds) for a rolling spend-cap period; defaults to one day."""
    return SPEND_PERIOD_SECONDS.get(str(period or "day"), 86400)
