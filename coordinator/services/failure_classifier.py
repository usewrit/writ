"""Backend failure classification for billing (project: failure-classified billing).

A failed marketplace/cloud run is NOT all the same. WHO pays — and whether the
creator's reputation takes the hit — depends on WHY it failed:

  * CREATOR fault  — the recipe is broken (selector gone, assertion/exit-condition
                     failed, the recipe's own JS threw). The buyer asked for a
                     working product; they must NOT pay for the creator's bug.
                     Platform absorbs the compute; the creator's fault rate rises.
  * INFRA  fault   — the platform/network failed the run (navigation timeout,
                     connection/proxy error, anti-bot/captcha block, agent or
                     browser crash, SSRF refusal). Not the buyer's problem and not
                     the creator's bug → buyer pays NOTHING, platform absorbs.
  * BUYER  fault   — the recipe + infra worked; the BUYER's input failed it (bad
                     login credentials, expired session, their 2FA, an input that
                     violated the listing's rules). The buyer consumed real
                     compute with bad input → buyer pays the compute. Creator
                     earns $0 (no contracted output was delivered).
  * UNKNOWN        — the failure cannot be confidently attributed. Per product
                     decision the buyer pays compute (status quo), and we LOG the
                     unknown rate so the heuristics can be tuned.

TRUST (feedback_never_trust_byo_agents): the VERDICT is computed here, on the
trusted backend, from backend-observed signals (the engine's reported error
string + result flags). The agent never sends a `fault_domain`. For a monetized
run the executor is the platform's own cloud fleet, so those signals are
trustworthy; even so the policy lives on the trusted side. The classifier is a
PURE function (no DB, no I/O) so it is cheap to call on every completion and easy
to unit-test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional


# ── Fault domains (persisted in AutomationTask.fault_domain) ────────────────
FAULT_CREATOR = "creator"
FAULT_INFRA = "infra"
FAULT_BUYER = "buyer"
FAULT_UNKNOWN = "unknown"

# Whether the BUYER is charged the compute time for a failure in this domain.
# Locked product decisions (2026-06-22):
#   creator/infra → platform absorbs (buyer pays nothing)
#   buyer/unknown → buyer pays compute (unknown defaults to status quo)
_BILL_BUYER = {
    FAULT_CREATOR: False,
    FAULT_INFRA: False,
    FAULT_BUYER: True,
    FAULT_UNKNOWN: True,
}

# Fine-grained category → fault domain. Persisted in AutomationTask.failure_category
# for analytics / tuning. Keep these stable; dashboards key off them.
_CATEGORY_FAULT = {
    # creator (broken recipe)
    "selector_not_found": FAULT_CREATOR,
    "script_error": FAULT_CREATOR,
    "assertion_failed": FAULT_CREATOR,
    "exit_condition_failed": FAULT_CREATOR,
    # infra (platform / network / anti-bot)
    "captcha_block": FAULT_INFRA,
    "ssrf_block": FAULT_INFRA,
    "connection_error": FAULT_INFRA,
    "proxy_error": FAULT_INFRA,
    "nav_timeout": FAULT_INFRA,
    "agent_crash": FAULT_INFRA,
    "browser_crash": FAULT_INFRA,
    "gateway_timeout": FAULT_INFRA,
    # buyer (their input / data)
    "auth_rejected": FAULT_BUYER,
    "twofa_failed": FAULT_BUYER,
    "missing_credentials": FAULT_BUYER,
    "input_rule_violation": FAULT_BUYER,
    # unknown / ambiguous
    "extraction_empty": FAULT_UNKNOWN,
    "unknown": FAULT_UNKNOWN,
}


@dataclass(frozen=True)
class FailureClassification:
    fault_domain: str         # creator | infra | buyer | unknown
    failure_category: str     # fine-grained (see _CATEGORY_FAULT)
    bill_buyer: bool          # charge the buyer compute time?
    confidence: float         # 0..1 — how sure the heuristic is (for tuning/logs)
    reason: str               # short human-readable rationale


def bill_buyer_for(fault_domain: Optional[str]) -> bool:
    """Billing decision for a fault domain. Unknown / None → bill (status quo)."""
    return _BILL_BUYER.get(fault_domain or FAULT_UNKNOWN, True)


def _result(*candidates) -> dict:
    for c in candidates:
        if isinstance(c, dict):
            return c
    return {}


# Compiled in (category, regex, confidence) ORDER. First match wins, so the most
# specific / least-ambiguous signatures come first. Notably the CREATOR
# selector/script patterns precede INFRA `nav_timeout` so a "Timeout waiting for
# selector" (a recipe locator that never resolved) is creator-fault, not infra;
# while clearly-infra signatures (captcha, DNS/connection, proxy, crash) precede
# everything because they are unambiguous regardless of where they occur.
_RULES: list[tuple[str, "re.Pattern[str]", float]] = [
    # ── INFRA — unambiguous platform/network/anti-bot ──────────────────────
    ("ssrf_block", re.compile(
        r"ssrf|internal/private address|targets internal|url .*not allowed|blocked.*internal",
        re.I), 0.95),
    ("captcha_block", re.compile(
        r"captcha|recaptcha|hcaptcha|turnstile|cloudflare|just a moment|"
        r"checking your browser|cf-challenge|are you (a )?human|bot detection|"
        r"access denied.*cloudflare",
        re.I), 0.85),
    ("connection_error", re.compile(
        r"net::err|err_name_not_resolved|err_connection|err_internet_disconnected|"
        r"err_address_unreachable|err_aborted|connection refused|connection reset|"
        r"econnrefused|econnreset|enotfound|getaddrinfo|socket hang up|"
        r"\bdns\b|ssl error|cert(ificate)? (error|verify)|network is unreachable",
        re.I), 0.85),
    ("proxy_error", re.compile(
        r"\bproxy\b|err_tunnel|err_proxy|tunnel connection failed|"
        r"proxy authentication|upstream connect error",
        re.I), 0.8),
    ("browser_crash", re.compile(
        r"target (page|frame|context|browser)?.{0,12}closed|page crashed|"
        r"browser has (been )?closed|context (was )?closed|session closed|"
        r"connection closed while reading|websocket closed|browser disconnected",
        re.I), 0.8),
    ("agent_crash", re.compile(
        r"agent (disconnected|unavailable|crashed|not (available|connected))|"
        r"no agent (available|online)|executor (gone|disconnected)|"
        r"worker (died|crashed)|dispatch (failed|timed out)",
        re.I), 0.8),

    # ── CREATOR — broken recipe (checked before nav_timeout / auth) ─────────
    ("selector_not_found", re.compile(
        r"element not found|node ?not ?found|nodenotfound|"
        r"selector resolved to|locator resolved to|strict mode violation|"
        r"no element(s)? match|waiting for selector|waiting for locator|"
        r"element is not (attached|visible|enabled)|"
        r"unable to (find|locate) (element|selector)|"
        r"timeout.*(waiting for|exceeded).*(selector|locator|element)",
        re.I), 0.85),
    ("script_error", re.compile(
        r"syntaxerror|referenceerror|\btypeerror\b|rangeerror|"
        r"is not defined|is not a function|unexpected token|"
        r"evaluate(_js)? (failed|error)|evaluation failed|cannot read propert",
        re.I), 0.8),
    ("exit_condition_failed", re.compile(
        r"exit condition|exit-condition", re.I), 0.85),
    ("assertion_failed", re.compile(
        r"assertion|assert (failed|error)|did not match expected|"
        r"expected .*(but|to be)|validation step failed",
        re.I), 0.7),

    # ── BUYER — their credentials / 2FA / input ────────────────────────────
    # 2FA: only buyer-fault when it's NOT a platform-side OTP-retrieval failure.
    ("twofa_failed", re.compile(
        r"\b2fa\b|twofa|two-factor|\botp\b|totp|one-time (code|password)|"
        r"verification code|authenticator",
        re.I), 0.7),
    ("input_rule_violation", re.compile(
        r"input rule|does not match required pattern|input validation|"
        r"required input|failed input validation|input_rules",
        re.I), 0.85),
    ("auth_rejected", re.compile(
        r"\b401\b|\b403\b|unauthor|forbidden|invalid credentials|"
        r"incorrect (password|username)|login failed|sign[- ]?in failed|"
        r"authentication failed|session expired|session_expired|"
        r"please (log|sign) ?in|access denied|account (locked|disabled)",
        re.I), 0.7),

    # ── INFRA — generic navigation timeout (after selector-wait timeouts) ───
    ("nav_timeout", re.compile(
        r"(goto|navigation|page\.goto|load state|networkidle|domcontentloaded)"
        r".{0,40}(timeout|timed out|exceeded)|"
        r"(timeout|timed out|exceeded).{0,40}(goto|navigation|load|networkidle)|"
        r"navigation timeout",
        re.I), 0.7),
]

# A 2FA error that is actually the PLATFORM failing to retrieve/mint the OTP is
# infra, not the buyer's fault. Detect that sub-case to override twofa_failed.
_TWOFA_INFRA = re.compile(
    r"twofa_not_available|retriev|coordinator|mint|http [45]\d\d|"
    r"timed out (fetching|retrieving)|otp (unavailable|service)",
    re.I)


def classify_failure(
    workflow: Any,
    task: Any,
    *,
    error: Optional[str],
    result_data: Optional[dict] = None,
) -> FailureClassification:
    """Classify a FAILED run into a fault domain + billing decision.

    `error` is the engine's reported error string (may be None). `result_data` is
    the engine's result payload (flags like session_expired / captcha_detected).
    Both are backend-observed inputs; the verdict is computed here. Never reads
    from an agent-supplied verdict field.
    """
    rd = _result(result_data, getattr(task, "result_data", None))
    err = (error or "").strip()
    err_l = err.lower()

    # 1) Strong structured flags from the result payload take precedence over the
    #    freeform error string (they are explicit engine signals).
    if rd.get("captcha_detected"):
        return _mk("captcha_block", 0.9, "anti-bot/captcha challenge on the page")

    # 2) Pattern-match the error string in priority order.
    if err_l:
        for category, rx, conf in _RULES:
            if rx.search(err_l):
                if category == "twofa_failed" and _TWOFA_INFRA.search(err_l):
                    return _mk("gateway_timeout", conf,
                               "2FA failed on the platform OTP path (infra)")
                return _mk(category, conf, f"matched {category} signature in error")

    # 3) session_expired flag with no clearer error → buyer auth rejected (their
    #    credentials/session were refused; a broken login FORM would have surfaced
    #    a selector error above and been attributed to the creator).
    if rd.get("session_expired"):
        return _mk("auth_rejected", 0.6,
                   "session expired / auth rejected (buyer credentials)")

    # 4) No error string and a non-success: the run produced no / non-conforming
    #    output. This is genuinely ambiguous (recipe drift vs. the buyer's account
    #    legitimately having no data), so it stays UNKNOWN → buyer pays (status quo).
    if not err_l:
        return _mk("extraction_empty", 0.3,
                   "no error reported but output empty/non-conforming")

    # 5) An error we couldn't attribute. Unknown → buyer pays (status quo); logged
    #    so the unknown rate can be monitored and the heuristics tuned.
    return _mk("unknown", 0.2, f"unrecognized failure: {err[:120]}")


def _mk(category: str, confidence: float, reason: str) -> FailureClassification:
    fault = _CATEGORY_FAULT.get(category, FAULT_UNKNOWN)
    return FailureClassification(
        fault_domain=fault,
        failure_category=category,
        bill_buyer=_BILL_BUYER.get(fault, True),
        confidence=confidence,
        reason=reason,
    )
