"""Check coalescing.

Any number of checks may target the same URL (including the same URL many times)
without colliding. The coordinator collapses *identical* checks into a single
physical fetch via a ``fetch_key`` so N targets watching the same page at the
same cadence cost ONE slot, then fans results back out per-target.

CORRECTNESS — what may share a physical fetch (and what must NOT):
  * Only ANONYMOUS checks coalesce. A target with a persona or a stored auth
    session renders a personalized page; sharing that render would leak one
    target's logged-in content to another. Such targets get a SINGLETON key
    (their own id) so they never group with anyone.
  * Only infra/shared agents run coalesced groups. User-hosted agents only ever
    run their own checks (enforced at distribution, not here).
  * Change detection / baselines / notifications / billing stay per-target,
    server-side. The shared unit is purely the page fetch/render.

The key intentionally includes everything that changes the bytes an agent would
fetch: url, check type, cadence, browser-vs-HTTP, region. Anything that differs
there is a different physical check.
"""
import hashlib
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

# Default cadence when a target has no explicit period (mirrors the distributor).
_DEFAULT_PERIOD_MS = 60000


def normalize_url(raw: str) -> str:
    """Canonicalize a URL for coalescing: lowercase scheme+host, drop the
    default port and any fragment, and strip a lone trailing slash. Path and
    query are preserved (they change what's fetched)."""
    if not raw:
        return raw
    try:
        parts = urlsplit(raw.strip())
    except Exception:
        return raw.strip()

    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    # Drop default ports.
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    else:
        netloc = host

    path = parts.path or ""
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    # A bare host should keep an empty path (not "/") for stable hashing.
    if path == "/":
        path = ""

    # Fragment dropped; query kept (it changes the response).
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def auth_context(target: Any) -> str:
    """Return the auth fingerprint for coalescing. Anonymous checks share the
    literal ``anon``; anything personalized is pinned to its own id so it never
    groups with another target."""
    has_persona = getattr(target, "persona_id", None) is not None
    has_session = getattr(target, "auth_session_encrypted", None) is not None
    # An inline setup-steps manifest (recorded login/navigate) renders an
    # authenticated/personalized page just like a persona/auth session — it must
    # never coalesce with another target's (or an anonymous) fetch of the same URL.
    _ss = getattr(target, "setup_steps", None)
    has_setup_steps = bool(_ss) and str(_ss).strip() not in ("", "null")
    if has_persona or has_session or has_setup_steps:
        return f"solo:{getattr(target, 'id', None)}"
    return "anon"


def compute_fetch_key(
    *,
    url: str,
    check_type: Optional[str],
    check_period_ms: Optional[int],
    requires_playwright: Optional[bool],
    preferred_region: Optional[str],
    auth: str,
) -> str:
    """Deterministic 32-hex key for the physical fetch this check belongs to.

    ``auth`` — ``auth_context(target)``: ``anon`` to allow coalescing, ``solo:<id>``
    to force a singleton.

    ``requires_playwright`` already separates the two execution paths: a visual
    viewport-zone check renders in a browser (requires_playwright=True) while a
    simple HTML-selector check fetches over HTTP (False), so they land on
    different keys and never coalesce even on the same URL."""
    norm = normalize_url(url)
    period = int(check_period_ms or _DEFAULT_PERIOD_MS)
    region = (preferred_region or "").lower()
    ctype = (check_type or "content").lower()
    pw = "1" if requires_playwright else "0"
    basis = f"{ctype}|{norm}|{period}|{pw}|{region}|{auth}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def fetch_key_for_target(target: Any) -> str:
    """Compute the fetch_key directly from a Target instance."""
    return compute_fetch_key(
        url=target.url,
        check_type=getattr(target, "check_type", None),
        check_period_ms=getattr(target, "check_period_ms", None),
        requires_playwright=getattr(target, "requires_playwright", None),
        preferred_region=getattr(target, "preferred_region", None),
        auth=auth_context(target),
    )


def _group_key(target: Any) -> str:
    """The key a target coalesces under. Singleton (never merges) when fetch_key
    isn't set yet (pre-backfill) so nothing merges by accident.

    Both content and uptime coalesce by fetch_key:
      * content → one fetch, selectors unioned, results fanned out per selector_id.
      * uptime  → one fetch; the single up/down result is fanned out at ingest to
        every same-fetch_key uptime target (see ``uptime_group_targets``)."""
    fk = getattr(target, "fetch_key", None)
    return fk if fk else f"target:{getattr(target, 'id', id(target))}"


async def uptime_group_targets(db: Any, target: Any) -> List[Any]:
    """All enabled uptime targets sharing this target's fetch_key — i.e. the
    targets whose uptime check was served by one coalesced physical fetch. The
    ingest fans the single result out to each so none goes stale.

    Same fetch_key ⇒ same url + interval + browser + region + anonymous, so the
    result is identical and every member is on the same cadence (billing each per
    result is correct). Falls back to just this target when fetch_key is unset."""
    from sqlalchemy import select
    from models.target import Target

    fk = getattr(target, "fetch_key", None)
    if not fk:
        return [target]
    rows = (await db.execute(
        select(Target).where(
            Target.fetch_key == fk,
            Target.check_type == "uptime",
            Target.enabled == True,  # noqa: E712
        )
    )).scalars().all()
    return list(rows) or [target]


def coalesce_assigned_targets(targets: List[Any]) -> List[tuple]:
    """Collapse an agent's assigned targets into one physical fetch per group.

    Returns a list of ``(representative_target, [member_target_ids])`` — one entry
    per fetch_key group. The representative is the lowest-id member (stable). The
    member ids let the caller union every member's selectors onto the single
    fetch, so one page load serves every target watching it; reports then fan out
    per selector_id back to each member target.

    Order is preserved by first appearance so the agent's list stays stable."""
    groups: Dict[str, List[Any]] = {}
    order: List[str] = []
    for t in targets:
        k = _group_key(t)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(t)

    out: List[tuple] = []
    for k in order:
        members = groups[k]
        rep = min(members, key=lambda t: getattr(t, "id", 0))
        member_ids = sorted(getattr(m, "id", 0) for m in members)
        out.append((rep, member_ids))
    return out
