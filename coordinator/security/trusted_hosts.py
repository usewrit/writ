"""Live Host-header allowlist.

Starlette ships ``TrustedHostMiddleware``, and this module deliberately replaces
it. Two reasons, both of which were real defects:

1. **It reads its allowlist once.** The middleware captures ``allowed_hosts`` when
   the ASGI stack is built, so the "Trusted hosts" field in Settings → Network
   (``services.coordinator_settings.set_network``) persisted a value that nothing
   ever enforced. A security-shaped control wired to nothing is worse than no
   control, because the operator believes it took effect. ``apply()`` below is
   called from ``set_network``, so saving the form now changes enforcement
   immediately — the same live-apply contract ``public_url`` already had.

2. **The allowlist it was given was wrong.** It was built from
   ``settings.frontend_url``, whose default is ``http://localhost:3000``. A
   production install on a real domain therefore trusted only ``localhost`` and
   answered **400 Invalid host header** to every request from its own users. The
   composition now lives in ``settings.allowed_hosts_list``, which derives the
   hostname from ``WRIT_PUBLIC_URL`` and always includes loopback (the container
   healthcheck curls ``http://localhost:8000/health`` from inside the container,
   so evicting localhost turns a healthy coordinator into a restart loop).

Matching rules, kept identical in spirit to Starlette's so the swap is not a
behaviour change beyond the two fixes above:

  * the port is stripped before comparison (``writ.example.com:443`` matches
    ``writ.example.com``) and comparison is case-insensitive;
  * ``*`` allows any host and disables the check entirely;
  * a leading-wildcard entry ``*.example.com`` matches any subdomain AND the bare
    ``example.com``;
  * a missing/blank Host header is rejected, since every HTTP/1.1 client sends
    one and its absence is a probe.
"""
from __future__ import annotations

import logging
import threading
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Guarded by _LOCK because apply() runs from a request handler (the settings
# PUT) while readers run on every other request. The values are swapped
# wholesale rather than mutated in place, so readers never observe a half-built
# allowlist.
_LOCK = threading.Lock()
_ALLOWED: tuple[str, ...] = ()
_ALLOW_ANY: bool = False
_ENFORCED: bool = False


def configure(hosts: Iterable[str], *, enforced: bool) -> None:
    """Install the allowlist. ``enforced=False`` makes every check pass.

    Called once at import from ``main`` with ``enforced=settings.is_production``:
    a development install accepts any Host so that LAN testing, ngrok tunnels and
    container hostnames all just work.
    """
    global _ALLOWED, _ALLOW_ANY, _ENFORCED
    normalised = tuple(_normalise(h) for h in hosts if _normalise(h))
    with _LOCK:
        _ALLOWED = normalised
        _ALLOW_ANY = "*" in normalised
        _ENFORCED = enforced


def apply(extra_hosts: Iterable[str]) -> list[str]:
    """Merge operator-supplied hosts over the configured base and return the result.

    ``extra_hosts`` comes from the persisted Settings → Network value. It is
    merged with (never replaces) the derived base from ``settings.allowed_hosts_list``
    so that an operator cannot lock themselves out by saving an empty list, and
    cannot drop the loopback entry the healthcheck depends on.
    """
    from config import settings

    merged: list[str] = list(settings.allowed_hosts_list)
    for host in extra_hosts:
        h = _normalise(host)
        if h and h not in merged:
            merged.append(h)
    configure(merged, enforced=settings.is_production)
    logger.info("Trusted host allowlist updated: %s", merged)
    return merged


def current() -> list[str]:
    """The allowlist in effect right now (for diagnostics and the settings API)."""
    with _LOCK:
        return list(_ALLOWED)


def is_enforced() -> bool:
    with _LOCK:
        return _ENFORCED and not _ALLOW_ANY


def _normalise(host: Optional[str]) -> str:
    """Lowercase, strip whitespace, and drop any ``:port`` suffix."""
    h = (host or "").strip().lower()
    if not h:
        return ""
    # Bracketed IPv6 literal, optionally with a port: [::1] / [::1]:8000.
    if h.startswith("["):
        end = h.find("]")
        if end != -1:
            return h[: end + 1]
        return h
    # Split a port off, but only when the remainder is a real port. A bare IPv6
    # literal ("::1") also contains colons and must not be truncated to "".
    if h.count(":") == 1:
        name, _, port = h.partition(":")
        if port.isdigit():
            return name
    return h


def is_allowed(host: Optional[str]) -> bool:
    """True when this Host header may be served."""
    with _LOCK:
        if not _ENFORCED or _ALLOW_ANY:
            return True
        allowed = _ALLOWED

    candidate = _normalise(host)
    if not candidate:
        # Every HTTP/1.1 client sends a Host header; an absent one is a scanner.
        return False

    for entry in allowed:
        if entry == candidate:
            return True
        if entry.startswith("*."):
            suffix = entry[1:]           # "*.example.com" -> ".example.com"
            bare = entry[2:]             # "*.example.com" -> "example.com"
            if candidate == bare or candidate.endswith(suffix):
                return True
    return False
