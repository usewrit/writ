"""
Minimal URL policy for the single-owner coordinator — SSRF screen + (empty)
domain blocklist, applied at the DISPATCHER layer (crawl seed admission, per-URL
frontier admission). Agents never enforce this themselves: every URL a shard is
handed passed this choke point first.

Two failure postures, deliberately different:
- Blocklist (abuse control)      → FAIL-OPEN, same policy as services.domain_guard
  (which is a permanent no-op on this single-owner build).
- SSRF screen (security boundary) → FAIL-CLOSED on malformed/non-http URLs and on
  private/internal targets (loopback, RFC1918, link-local/metadata, reserved —
  security.validation.InputValidator ranges). ``allow_private`` defaults to
  ``settings.allow_private_targets`` (env ALLOW_PRIVATE_TARGETS, default False):
  the screen is on in EVERY environment, including development, and is relaxed
  only by that explicit opt-in (for crawling/monitoring intranet apps).

This is a lean, single-tenant carve of the cloud ``services.url_policy`` — no
org scoping, no DNS-resolution pass, no audit rows — just the synchronous,
no-DB, no-DNS ``check_url_sync`` the crawl hot path needs.
"""
import ipaddress
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from services import domain_guard

logger = logging.getLogger(__name__)

# Hostname suffixes that are internal by convention — never resolvable public
# targets, always refused regardless of DNS (mDNS/.local, k8s/.internal, etc.).
_INTERNAL_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa")
_LOCAL_HOSTNAMES = {"localhost", "0.0.0.0", "metadata", "metadata.google.internal"}

CODE_DOMAIN_BLOCKED = "domain_blocked"
CODE_SSRF_BLOCKED = "ssrf_blocked"
CODE_INVALID_URL = "invalid_url"


@dataclass
class Verdict:
    allowed: bool
    code: str = ""
    host: str = ""
    reason: str = ""      # admin-entered blocklist reason (domain_blocked only)
    message: str = ""     # user-facing explanation, safe to surface verbatim


_ALLOW = Verdict(allowed=True)


def _default_allow_private() -> bool:
    # Explicit opt-in only (env ALLOW_PRIVATE_TARGETS) — deliberately NOT tied
    # to ENVIRONMENT, so the private-IP screen stays on even in development.
    try:
        from config import settings
        return bool(settings.allow_private_targets)
    except Exception:
        return False


def is_templated(url: str) -> bool:
    """URL contains a {{placeholder}} — resolvable only at runtime, so the static
    sweep skips it (the resolved URL still hits the runtime dispatch chokes)."""
    return "{{" in url


def _ssrf_verdict(host: str, detail: str) -> Verdict:
    return Verdict(
        allowed=False,
        code=CODE_SSRF_BLOCKED,
        host=host,
        message=f"'{host}' {detail} — crawls may only touch public websites.",
    )


def _inet_aton_like(h: str):
    """Parse an IPv4 literal in ANY of the notations ``inet_aton`` accepts —
    dotted-decimal, dotted-octal (``0177.0.0.1``), dotted-hex (``0x7f.0.0.1``),
    and the packed 1/2/3-part short forms (``2130706433``, ``127.1``). Returns an
    ``ipaddress.IPv4Address`` or None. This is what a resolver/agent will actually
    connect to, so screening the *normalized* value is what closes the
    decimal/octal/hex bypass."""
    parts = h.split(".")
    if not (1 <= len(parts) <= 4):
        return None
    vals = []
    for p in parts:
        if p == "":
            return None
        try:
            pl = p.lower()
            if pl.startswith("0x"):
                v = int(pl, 16)
            elif len(p) > 1 and p.startswith("0"):
                v = int(p, 8)
            else:
                v = int(p, 10)
        except ValueError:
            return None
        if v < 0:
            return None
        vals.append(v)
    n = len(vals)
    if n == 1:
        num = vals[0]
    elif n == 2:  # a.b  → b occupies the low 24 bits
        if vals[0] > 0xFF or vals[1] > 0xFFFFFF:
            return None
        num = (vals[0] << 24) | vals[1]
    elif n == 3:  # a.b.c → c occupies the low 16 bits
        if vals[0] > 0xFF or vals[1] > 0xFF or vals[2] > 0xFFFF:
            return None
        num = (vals[0] << 24) | (vals[1] << 16) | vals[2]
    else:  # a.b.c.d
        if any(v > 0xFF for v in vals):
            return None
        num = (vals[0] << 24) | (vals[1] << 16) | (vals[2] << 8) | vals[3]
    if not (0 <= num <= 0xFFFFFFFF):
        return None
    try:
        return ipaddress.ip_address(num)
    except (ValueError, TypeError):
        return None


def _normalize_ip_literal(host: str):
    """Return an ``ipaddress`` object if ``host`` is an IP literal in ANY notation
    (standard dotted / full IPv6 / bracketed IPv6 / decimal / octal / hex /
    IPv4-mapped IPv6), else None (a real DNS name). Normalizing through
    ``ipaddress`` is what stops ``2130706433``, ``0x7f000001``, ``0177.0.0.1`` and
    ``[::ffff:127.0.0.1]`` from smuggling an internal address past a string
    filter."""
    h = (host or "").strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if not h:
        return None
    try:
        return ipaddress.ip_address(h)  # standard IPv4 + full IPv6
    except ValueError:
        pass
    return _inet_aton_like(h)  # decimal / octal / hex / short-form IPv4


def _ip_blocked(ip) -> bool:
    """True if ``ip`` is in a range we must never reach. Unwraps IPv4-mapped IPv6
    so ``::ffff:10.0.0.1`` is judged as the private v4 address it routes to. Uses
    the full ``ipaddress`` classification (private/loopback/link-local/reserved/
    multicast/unspecified) — broader than the old regex/known-name list, which
    missed e.g. 0.0.0.0/8, CGNAT 100.64/10, and IPv6 forms."""
    try:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    except Exception:
        return True  # fail-closed: unclassifiable → treat as blocked


def check_url_sync(url: Optional[str], *, allow_private: Optional[bool] = None) -> Verdict:
    """No-DB, no-DNS check: scheme + blocklist + literal private/internal host.

    Safe on hot paths. The blocklist is fail-open (permanently empty on this
    single-owner build); the SSRF screen is fail-closed.
    """
    if not url or not isinstance(url, str):
        return _ALLOW
    url = url.strip()
    if not url or is_templated(url):
        return _ALLOW
    if allow_private is None:
        allow_private = _default_allow_private()

    try:
        parsed = urlparse(url)
    except Exception:
        return Verdict(allowed=False, code=CODE_INVALID_URL, message="The URL could not be parsed.")

    if parsed.scheme not in ("http", "https"):
        return Verdict(
            allowed=False, code=CODE_INVALID_URL, host=parsed.hostname or "",
            message=f"Only http/https URLs are allowed (got '{parsed.scheme or 'no'}' scheme).",
        )
    host = (parsed.hostname or "").lower().strip().rstrip(".")
    if not host:
        return Verdict(allowed=False, code=CODE_INVALID_URL, message="The URL has no hostname.")

    # Blocklist first — the operator's rule (and its reason) wins the message.
    reason = domain_guard.is_blocked(host)
    if reason is not None:
        return Verdict(
            allowed=False,
            code=CODE_DOMAIN_BLOCKED,
            host=host,
            reason=reason or "",
            message=(
                f"The domain '{host}' is blocked by the administrator"
                + (f": {reason}" if reason else ".")
            ),
        )

    # SSRF: normalize any IP-literal notation, then screen. Non-literal hosts are
    # matched against the internal-by-convention name list. (No DNS here — this is
    # the no-DNS hot-path check; use ``check_url`` for a resolving screen.)
    if not allow_private:
        ip_obj = _normalize_ip_literal(host)
        if ip_obj is not None:
            if _ip_blocked(ip_obj):
                return _ssrf_verdict(host, "is a private/internal address")
        elif host in _LOCAL_HOSTNAMES or any(host.endswith(s) for s in _INTERNAL_HOST_SUFFIXES):
            return _ssrf_verdict(host, "is an internal hostname")

    return _ALLOW


async def check_url(url: Optional[str], *, allow_private: Optional[bool] = None) -> Verdict:
    """DNS-resolving SSRF screen: the full :func:`check_url_sync` check PLUS a
    resolution pass that catches DNS names pointing into private/internal space
    (``http://name-that-resolves-to-127.0.0.1/``). Await-able, so it is safe off
    the tight admission loop; the runtime egress helper (:mod:`services.safe_fetch`)
    still pins + re-screens at connect time, so this is defense-in-depth.

    Confirmed private resolutions are refused; a transient resolution failure
    falls through to the (already-passed) sync verdict rather than breaking a
    legitimate public crawl — the connect-time screen would still catch a rebind.
    """
    verdict = check_url_sync(url, allow_private=allow_private)
    if not verdict.allowed or not url:
        return verdict
    if allow_private is None:
        allow_private = _default_allow_private()
    if allow_private:
        return verdict

    try:
        parsed = urlparse(url.strip())
    except Exception:
        return verdict
    host = (parsed.hostname or "").lower().strip().rstrip(".")
    # A literal IP was already screened synchronously; only DNS names need a lookup.
    if not host or _normalize_ip_literal(host) is not None:
        return verdict

    try:
        import asyncio
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, parsed.port or None)
    except Exception:
        return verdict  # fail-open on transient DNS error (connect-time screen backs us up)

    for info in infos:
        ip_str = info[4][0]
        try:
            if _ip_blocked(ipaddress.ip_address(ip_str)):
                return _ssrf_verdict(host, "resolves to a private/internal address")
        except ValueError:
            return _ssrf_verdict(host, "resolved to an unparseable address")
    return verdict
