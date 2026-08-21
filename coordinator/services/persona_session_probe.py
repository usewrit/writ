"""Did the persona's login actually TAKE? — a behavioral check.

Every other test we apply to a warm session inspects its SHAPE: does it carry
cookies, is one of them HttpOnly, is an auth-looking name present
(`persona_login.session_is_usable` / `session_has_auth_material`). Shape is
enough to catch an empty capture, and nothing more — a great many sites hand an
anonymous visitor a perfectly well-formed HttpOnly session cookie, so a login
that silently failed produces a session that passes every shape test we have.

That gap cost a production crawl: a persona "signed in" successfully, its
session validated, and 1069 shards then fetched the site's sign-in page and
banked it as content. Nothing in the pipeline could tell, because the only
authority on whether a session is signed in is the SITE.

So ask the site. Replay the session against a page the crawl actually wants and
watch what comes back: a sign-in bounce means the login did not take, whatever
the cookie jar looks like. Verdicts are deliberately three-valued —

    True  — the session reached the page as a signed-in visitor
    False — the site bounced it to a sign-in page; the login did NOT take
    None  — could not tell (transport error, token-auth session we cannot
            replay over HTTP, ambiguous status)

— because the cost of the two mistakes is not symmetric. A false NEGATIVE
blocks a crawl that would have worked; a false POSITIVE costs the user a
thousand pages of sign-in HTML and the money to fetch it. `None` is therefore a
first-class answer and callers must treat it as "proceed", never as failure:
only `False`, which requires positive evidence of a bounce, may stop a crawl.
"""
# SELF-HOSTED EDITION. Ported from the cloud backend unchanged except for
# tenancy: this coordinator serves ONE owner, so `gated_url_for_persona` looks the
# login workflow up by id alone rather than id + tenant_id.
#
from __future__ import annotations

import logging
import os
import re
from typing import Optional, Tuple
from urllib.parse import urlparse, urljoin

logger = logging.getLogger(__name__)

# How long to wait for the probe. It runs once, in the crawl's pre-fanout path, so
# it must be short enough to never feel like a stall and long enough to survive a
# residential exit's first CONNECT (which pays DNS + TLS through the broker).
PROBE_TIMEOUT_SECONDS = float(os.getenv("WRIT_SESSION_PROBE_TIMEOUT", "20") or 20)

# Path shapes that mean "you are being asked to sign in", across the stacks and
# languages we actually meet. Only consulted for SAME-SITE redirects: an off-site
# hop to an identity provider is a different thing entirely (see _verdict_for_location).
_LOGIN_PATH_RE = re.compile(
    r"/(?:"
    r"login|log-in|log_in|signin|sign-in|sign_in|"
    r"connexion|se-connecter|identifiez-vous|"
    r"anmelden|einloggen|iniciar-sesion|acceder|accedi|entrar|"
    r"auth(?:/(?:login|signin|sign-in))?|"
    r"account/login|accounts/login|users/sign_in|user/login|"
    r"session/new|sessions/new|customer/account/login"
    r")(?:/|$|\?)",
    re.I,
)

# Hosts that mean the site handed us off to an identity provider. A logged-OUT
# visitor is exactly who gets sent here, so a bounce to one is the same evidence
# as a bounce to /login — and unlike a generic off-site redirect, these are
# unambiguous. (The production case that motivated this module bounced to
# accounts.google.com.)
_IDP_HOST_RE = re.compile(
    r"(?:^|\.)(?:"
    r"accounts\.google\.com|login\.microsoftonline\.com|login\.live\.com|"
    r"github\.com/login|gitlab\.com/users/sign_in|appleid\.apple\.com|"
    r"auth0\.com|okta\.com|onelogin\.com|login\.yahoo\.com|facebook\.com/login"
    r")",
    re.I,
)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _same_site(a: str, b: str) -> bool:
    """Do two hosts belong to the same registrable site? Falls back to a suffix
    comparison when the policy helper is unavailable, so the probe never depends
    on the domain list being loaded."""
    ha, hb = (a or "").lower(), (b or "").lower()
    if not ha or not hb:
        return False
    if ha == hb:
        return True
    try:
        from services import egress_policy

        ra, rb = egress_policy.registrable(f"https://{ha}"), egress_policy.registrable(f"https://{hb}")
        if ra and rb:
            return ra == rb
    except Exception:  # noqa: BLE001 — a helper miss must not change the verdict
        pass
    return ha.endswith("." + hb) or hb.endswith("." + ha)


def looks_like_login_url(url: str, *, site_host: str = "") -> bool:
    """Is this URL a sign-in destination? Used for the redirect verdict and to
    refuse probing a seed that IS the sign-in page (where a 200 proves nothing)."""
    if not url:
        return False
    try:
        p = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    if p.hostname and _IDP_HOST_RE.search(f"{p.hostname}{p.path or ''}"):
        return True
    if site_host and p.hostname and not _same_site(p.hostname, site_host):
        return False
    return bool(_LOGIN_PATH_RE.search(p.path or "/"))


def _verdict_for_location(location: str, *, from_url: str, login_url: Optional[str],
                          patterns: Optional[list]) -> Tuple[Optional[bool], Optional[str]]:
    """Read a redirect target. Returns (False, reason) only on positive evidence of
    a sign-in bounce; (None, None) for any other redirect (pagination, canonical
    host, trailing slash, locale) which says nothing about being signed in."""
    target = urljoin(from_url, location)
    try:
        tp, sp = urlparse(target), urlparse(from_url)
    except Exception:  # noqa: BLE001
        return None, None

    # STRONGEST signal: bounced to the exact page this persona's own login workflow
    # starts on. Site-specific, no heuristics, no false positives.
    if login_url:
        try:
            lp = urlparse(login_url)
            if (tp.hostname or "").lower() == (lp.hostname or "").lower() \
                    and (tp.path or "/").rstrip("/") == (lp.path or "/").rstrip("/"):
                return False, f"redirected to the persona's own sign-in page ({target})"
        except Exception:  # noqa: BLE001
            pass

    # The workflow's recorded login_url_patterns — the same list the replay engine
    # uses to notice a mid-run logout, so agreeing with it keeps one definition of
    # "this is the login page" across the product.
    for pat in (patterns or []):
        try:
            if pat and re.search(str(pat), target):
                return False, f"redirected to a known sign-in URL ({target})"
        except re.error:
            continue

    if looks_like_login_url(target, site_host=sp.hostname or ""):
        return False, f"redirected to a sign-in page ({target})"
    return None, None


async def probe_session_authenticated(
    *,
    url: str,
    session: Optional[dict],
    proxy: Optional[str] = None,
    login_url: Optional[str] = None,
    login_url_patterns: Optional[list] = None,
    user_agent: Optional[str] = None,
) -> Tuple[Optional[bool], Optional[str]]:
    """Replay `session` against `url` and report whether the site treated it as
    signed in. See the module docstring for the three-valued contract.

    `proxy` MUST be the same egress the crawl's shards will use: a session bound
    to the address it was minted from is logged out from anywhere else, so probing
    direct while the shards go residential would report a healthy session as dead
    (and vice versa). `user_agent` should be the persona's captured UA for the same
    reason. Never raises.
    """
    if not url:
        return None, None

    cookies = (session or {}).get("cookies") if isinstance(session, dict) else None
    if not (isinstance(cookies, list) and cookies):
        # Token-auth session (localStorage/headers only). Its credentials live in a
        # browser context we cannot reproduce with one HTTP request, so we have
        # nothing to replay and no right to an opinion.
        return None, "session carries no cookies to replay over HTTP"

    site_host = ""
    try:
        site_host = urlparse(url).hostname or ""
    except Exception:  # noqa: BLE001
        pass
    if looks_like_login_url(url, site_host=site_host):
        # Probing the sign-in page itself: a 200 is what BOTH a signed-in and a
        # signed-out visitor get, so the probe cannot say anything.
        return None, "target is itself a sign-in page"

    from services.crawl_orchestrator import _session_cookie_header

    cookie_header = _session_cookie_header(session, url)
    if not cookie_header:
        return None, "no cookie from this session is in scope for the target"

    headers = {
        "User-Agent": user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cookie": cookie_header,
    }
    # Captured auth headers (bearer tokens on an HTTP-lane login) ride along — a
    # session can authenticate with these INSTEAD of cookies, and dropping them here
    # would make the probe report a working session as bounced.
    captured = (session or {}).get("headers")
    if isinstance(captured, dict):
        for k, v in captured.items():
            if isinstance(k, str) and isinstance(v, str) and k.lower() not in ("cookie", "host"):
                headers.setdefault(k, v)

    try:
        import httpx

        # follow_redirects=False on purpose: the FIRST hop is the whole signal, and
        # following it would also mean re-vetting each target for SSRF.
        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT_SECONDS, follow_redirects=False,
            headers=headers, proxy=proxy, verify=True,
        ) as client:
            resp = await client.get(url)
    except TypeError:
        # httpx renamed proxies= -> proxy= across versions; retry the older spelling
        # rather than losing the egress (probing direct would compare the session
        # against a different address than the shards use).
        try:
            import httpx

            async with httpx.AsyncClient(
                timeout=PROBE_TIMEOUT_SECONDS, follow_redirects=False,
                headers=headers, proxies=proxy, verify=True,
            ) as client:
                resp = await client.get(url)
        except Exception as e:  # noqa: BLE001
            logger.info("[session-probe] %s: transport error (%s)", url, e)
            return None, None
    except Exception as e:  # noqa: BLE001
        logger.info("[session-probe] %s: transport error (%s)", url, e)
        return None, None

    status = resp.status_code
    if status in _REDIRECT_STATUSES:
        verdict, reason = _verdict_for_location(
            resp.headers.get("location") or "", from_url=url,
            login_url=login_url, patterns=login_url_patterns,
        )
        if verdict is False:
            return False, reason
        return None, None
    if status == 401:
        return False, "the site answered 401 Unauthorized"
    if 200 <= status < 300:
        return True, None
    # 403 is deliberately NOT a logged-out verdict: an anti-bot layer refusing the
    # probe's plain request looks identical, and failing the crawl for that would
    # block sites that work fine from the agent's real browser.
    return None, None


async def gated_url_for_persona(db, persona) -> Optional[str]:
    """A URL that REQUIRES the login — the only kind a session probe can learn from.

    PROBING A PUBLIC PAGE PROVES NOTHING. A public page never bounces to the sign-in
    page, so it answers "signed in" for an ANONYMOUS jar exactly as it does for a real
    one. That is not a hypothetical: a crawl seeded at a public listing page passed
    verification against its own seed URL and then banked six logged-out pages, while
    the same session probed against a gated path bounced straight to /login.

    Derived from the persona's OWN login workflow, which by construction ends on a
    signed-in page: the LAST `navigate` step's URL, else the workflow's entry URL when
    that is not the sign-in page itself. Returns None when nothing gated is known — the
    caller must then treat the probe as UNKNOWN rather than as proof of a live session.

    Never raises: verification is a safety net, and an internal fault here must not
    block a crawl or a run that would otherwise have worked.
    """
    try:
        wf_id = getattr(persona, "login_workflow_id", None)
        if not wf_id:
            return None
        from sqlalchemy import select
        from models.automation_workflow import AutomationWorkflow

        row = (await db.execute(
            select(AutomationWorkflow.entry_url, AutomationWorkflow.steps).where(
                AutomationWorkflow.id == wf_id,
            )
        )).first()
        if not row:
            return None
        entry_url, steps = row[0], row[1]

        host = ""
        try:
            from urllib.parse import urlparse
            host = urlparse(entry_url or "").hostname or ""
        except Exception:  # noqa: BLE001
            host = ""

        def _usable(u) -> bool:
            u = str(u or "").strip()
            if not u.lower().startswith(("http://", "https://")):
                return False
            # The sign-in page is the one page that is MEANT to render signed-out;
            # bouncing off it proves nothing either way.
            return not looks_like_login_url(u, site_host=host)

        # The last navigate in the recipe is where the sign-in flow LANDS, so it is
        # the page most likely to require the session we are testing.
        for step in reversed(steps or []):
            if not isinstance(step, dict):
                continue
            if step.get("type") not in ("navigate", "navigated_to"):
                continue
            cfg = step.get("config") or {}
            candidate = cfg.get("url") or step.get("url")
            if _usable(candidate):
                return str(candidate).strip()

        if _usable(entry_url):
            return str(entry_url).strip()
        return None
    except Exception:  # noqa: BLE001
        return None
