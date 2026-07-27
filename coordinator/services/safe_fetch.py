"""
Centralized SSRF-hardened egress helper for the single-owner coordinator.

Every server-initiated outbound fetch that touches an *attacker-influenceable*
URL (crawl sitemap/robots discovery, robots_guard, admin AI-provider `base_url`,
etc.) MUST go through :func:`safe_get` / :func:`safe_fetch` rather than calling
httpx directly. This is the single choke point the audit's systemic SSRF theme
(#3) asks for: resolve the host, screen EVERY resolved IP, pin the connection to
a vetted address, and refuse (or per-hop re-validate) redirects.

What it defends against:
  - loopback / RFC1918 private / link-local (169.254.169.254 metadata) /
    reserved / multicast / unspecified (0.0.0.0) targets;
  - IPv4-mapped IPv6 (``::ffff:127.0.0.1``) that would otherwise smuggle a
    private v4 address past a naive v6 check;
  - decimal / octal / hex IP literals (``2130706433``, ``0x7f000001``,
    ``0177.0.0.1``) normalized through :mod:`ipaddress` before screening;
  - DNS names that resolve to any of the above;
  - DNS-rebinding TOCTOU: the socket is pinned to the exact IP we validated, so
    it cannot be re-resolved to an internal address between check and connect;
  - open redirects into internal space: ``follow_redirects`` is disabled at the
    transport and each hop is re-validated before we follow it.

Posture: FAIL-CLOSED. Any malformed URL, unresolvable host, or
private/internal target raises :class:`SSRFError`. ``allow_private`` defaults
to ``settings.allow_private_targets`` (env ALLOW_PRIVATE_TARGETS, default
False) — the screen is ON in EVERY environment, including development, and is
relaxed only by that explicit opt-in (for monitoring intranet apps), matching
``services.url_policy``.

Dependency-light: only ``httpx`` (already a hard dep), the stdlib ``ipaddress``
module, and the shared pooled client in ``utils.http_client``.
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx

logger = logging.getLogger(__name__)

# Default bounds — a control-plane GET of a text/JSON asset, never a large body.
DEFAULT_TIMEOUT = 8.0
DEFAULT_MAX_REDIRECTS = 5

_ALLOWED_SCHEMES = ("http", "https")


class SSRFError(Exception):
    """Raised when a URL is refused by the SSRF screen (bad scheme, unresolvable
    host, or a resolved IP in a private/internal/reserved range)."""

    def __init__(self, message: str, *, host: str = "", url: str = ""):
        super().__init__(message)
        self.host = host
        self.url = url


def _allow_private_default() -> bool:
    """Explicit-opt-in default (env ALLOW_PRIVATE_TARGETS), mirroring
    ``services.url_policy``. Deliberately NOT tied to ENVIRONMENT: the
    private-IP screen stays on even in development."""
    try:
        from config import settings
        return bool(settings.allow_private_targets)
    except Exception:
        return False


def _ip_is_blocked(ip: "ipaddress._BaseAddress") -> bool:
    """True if ``ip`` (already an ipaddress object) is in a range we must never
    reach. Unwraps IPv4-mapped IPv6 first so ``::ffff:10.0.0.1`` is judged as the
    private v4 address it really routes to."""
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            ip = mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _inet_aton_like(h: str) -> Optional["ipaddress._BaseAddress"]:
    """Parse an IPv4 literal in ANY notation ``inet_aton`` accepts — dotted-decimal,
    dotted-octal (``0177.0.0.1``), dotted-hex (``0x7f.0.0.1``), and the packed
    1/2/3-part short forms (``2130706433``, ``127.1``). Returns an IPv4Address or
    None. Screening the *normalized* value is what closes the decimal/octal/hex
    bypass regardless of the platform resolver's numeric-host handling."""
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


def _literal_ip(host: str) -> Optional["ipaddress._BaseAddress"]:
    """Parse ``host`` as an IP literal if it is one, normalizing the odd formats
    (decimal, hex, octal, short-form, bracketed/mapped IPv6) that bypass
    string-matching SSRF filters. Returns the normalized address, or None if
    ``host`` is a DNS name that must be resolved.

    Examples that all normalize to 127.0.0.1:
        "127.0.0.1", "2130706433", "0x7f000001", "0177.0.0.1", "127.1",
        "0x7f.0.0.1", "[::ffff:127.0.0.1]"
    """
    h = (host or "").strip()
    if not h:
        return None
    # Strip IPv6 brackets if present.
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]

    # Standard dotted-quad / full IPv6 form.
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass

    # IPv4 in decimal / octal / hex / short-form.
    return _inet_aton_like(h)


async def _resolve_and_screen(url: str, allow_private: bool) -> tuple[str, list[str]]:
    """Validate scheme, extract the host, and return (validated_url, [safe_ips]).

    Every candidate IP (from an IP literal, or from DNS resolution of a hostname)
    is screened; ANY blocked IP raises :class:`SSRFError` unless allow_private.
    The returned IP list lets the caller pin the TCP connection."""
    if not url or not isinstance(url, str):
        raise SSRFError("Empty or non-string URL.", url=str(url))

    parts = urlsplit(url.strip())
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise SSRFError(
            f"Only http/https URLs are allowed (got '{parts.scheme or 'no'}' scheme).",
            url=url,
        )
    host = (parts.hostname or "").strip().rstrip(".")
    if not host:
        raise SSRFError("The URL has no hostname.", url=url)

    candidate_ips: list[str] = []

    literal = _literal_ip(host)
    if literal is not None:
        if _ip_is_blocked(literal) and not allow_private:
            raise SSRFError(
                f"'{host}' resolves to a private/internal address ({literal}).",
                host=host, url=url,
            )
        candidate_ips.append(str(literal))
    else:
        # DNS name — resolve via the event loop's async resolver and screen
        # EVERY answer (a host can hand back several A/AAAA records).
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(host, parts.port or None)
        except Exception as e:
            raise SSRFError(f"Could not resolve host '{host}'.", host=host, url=url) from e

        seen: set[str] = set()
        for info in infos:
            sockaddr = info[4]
            ip_str = sockaddr[0]
            if ip_str in seen:
                continue
            seen.add(ip_str)
            try:
                ip_obj = ipaddress.ip_address(ip_str)
            except ValueError:
                # Unparseable answer → treat as hostile, fail closed.
                if not allow_private:
                    raise SSRFError(
                        f"'{host}' resolved to an unparseable address.",
                        host=host, url=url,
                    )
                continue
            if _ip_is_blocked(ip_obj) and not allow_private:
                raise SSRFError(
                    f"'{host}' resolves to a private/internal address ({ip_str}).",
                    host=host, url=url,
                )
            candidate_ips.append(ip_str)

        if not candidate_ips and not allow_private:
            raise SSRFError(f"Could not resolve host '{host}'.", host=host, url=url)

    return url, candidate_ips


def _pinned_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    safe_ips: list[str],
    headers: dict,
    timeout: float,
    request_kwargs: dict,
) -> httpx.Request:
    """Build an httpx.Request whose connection is pinned to a validated IP,
    preserving the original Host header (routing) and TLS SNI."""
    parts = urlsplit(url)
    req_headers = dict(headers)
    connect_url = url
    extensions: dict = {}

    if safe_ips:
        pin_ip = safe_ips[0]
        host_header = parts.hostname or ""
        if parts.port:
            host_header = f"{host_header}:{parts.port}"
        req_headers.setdefault("Host", host_header)
        # Bracket IPv6 literals for the netloc.
        try:
            netloc_ip = f"[{pin_ip}]" if ipaddress.ip_address(pin_ip).version == 6 else pin_ip
        except ValueError:
            netloc_ip = pin_ip
        netloc = f"{netloc_ip}:{parts.port}" if parts.port else netloc_ip
        connect_url = parts._replace(netloc=netloc).geturl()
        if parts.scheme == "https":
            extensions["sni_hostname"] = parts.hostname

    request = client.build_request(
        method, connect_url, headers=req_headers, timeout=timeout, **request_kwargs
    )
    if extensions:
        request.extensions.update(extensions)
    return request


# Headers that authenticate the caller. None of these may cross an origin
# boundary on a redirect. Matched case-insensitively.
_CREDENTIAL_HEADERS = frozenset({
    "authorization",
    "proxy-authorization",
    "cookie",
    "x-api-key",
    "api-key",
    "x-auth-token",
    "x-goog-api-key",
})


def _strip_credential_headers(headers: dict) -> dict:
    """Return `headers` without any credential-bearing entry."""
    return {k: v for k, v in headers.items() if k.lower() not in _CREDENTIAL_HEADERS}


def _is_cross_origin(from_url: str, to_url: str) -> bool:
    """True when scheme, host or port differ — i.e. a different security origin.

    An https -> http downgrade on the same host counts as cross-origin: sending a
    bearer token over cleartext is exactly what must not happen here.
    """
    a, b = urlsplit(from_url), urlsplit(to_url)
    default_port = {"http": 80, "https": 443}

    def _origin(parts):
        try:
            port = parts.port
        except ValueError:
            port = None
        return (
            (parts.scheme or "").lower(),
            (parts.hostname or "").lower(),
            port or default_port.get((parts.scheme or "").lower()),
        )

    return _origin(a) != _origin(b)


async def safe_fetch(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[dict] = None,
    timeout: float = DEFAULT_TIMEOUT,
    allow_private: Optional[bool] = None,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    client: Optional[httpx.AsyncClient] = None,
    **request_kwargs,
) -> httpx.Response:
    """SSRF-hardened outbound HTTP fetch.

    - Validates the URL, resolves the host, and screens EVERY resolved IP against
      private/loopback/link-local/reserved/multicast/unspecified ranges (incl.
      IPv4-mapped IPv6 and decimal/octal/hex IP literals).
    - Pins the connection to the validated IP (defeats DNS-rebinding TOCTOU).
    - Disables automatic redirect following and re-validates each hop before
      following it.

    Returns the final :class:`httpx.Response`. Raises :class:`SSRFError` if any
    hop fails the screen or the redirect chain is too long. ``allow_private``
    defaults to ``settings.allow_private_targets`` (off unless the operator
    explicitly opts in via ALLOW_PRIVATE_TARGETS). Extra kwargs
    (``json=``, ``content=``, ``params=`` …) pass through to the request.
    """
    if allow_private is None:
        allow_private = _allow_private_default()

    owns_client = client is None
    if client is None:
        from utils.http_client import get_http_client
        client = get_http_client()

    base_headers = dict(headers or {})
    current_url = url
    current_method = method
    current_kwargs = dict(request_kwargs)

    for _hop in range(max_redirects + 1):
        validated_url, safe_ips = await _resolve_and_screen(current_url, allow_private)
        request = _pinned_request(
            client, current_method, validated_url, safe_ips, base_headers, timeout, current_kwargs
        )
        response = await client.send(request, follow_redirects=False)

        if response.is_redirect:
            location = response.headers.get("location", "")
            status_code = response.status_code
            await response.aclose()
            if not location:
                raise SSRFError("Redirect without a Location header.", url=current_url)
            # Resolve relative redirects against the hop we just requested, then
            # loop so the new target is fully re-screened before we connect.
            next_url = urljoin(current_url, location)

            # Credentials must not follow a redirect off-origin. httpx strips
            # Authorization across origins when IT follows redirects; this loop
            # does the following by hand, so it has to do the stripping by hand
            # too. Callers pass provider API keys here (services/local_ai.py,
            # routers/settings.py), so a target that answers 302 to a host it
            # controls would otherwise be handed the key in the next request.
            if _is_cross_origin(current_url, next_url):
                base_headers = _strip_credential_headers(base_headers)

            # 301/302/303 downgrade the method to GET in every real client; carry
            # the body no further once that happens, or it leaks to the new origin.
            if status_code in (301, 302, 303) and current_method.upper() not in ("GET", "HEAD"):
                current_method = "GET"
                current_kwargs = {
                    k: v for k, v in current_kwargs.items()
                    if k not in ("json", "content", "data", "files")
                }

            current_url = next_url
            continue

        return response

    raise SSRFError(f"Too many redirects (>{max_redirects}).", url=url)


async def safe_get(
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: float = DEFAULT_TIMEOUT,
    allow_private: Optional[bool] = None,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    client: Optional[httpx.AsyncClient] = None,
    **request_kwargs,
) -> httpx.Response:
    """Convenience GET wrapper around :func:`safe_fetch`. See it for semantics."""
    return await safe_fetch(
        url,
        method="GET",
        headers=headers,
        timeout=timeout,
        allow_private=allow_private,
        max_redirects=max_redirects,
        client=client,
        **request_kwargs,
    )
