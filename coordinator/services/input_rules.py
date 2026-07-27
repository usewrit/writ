"""Creator-authored per-input regex validation rules for marketplace listings.

A creator can attach a regex rule to any required INPUT or SECRET slot of a
workflow they sell. On every CALLED / consumer run the passed value must FULLY
match its rule, or the run is rejected before dispatch (fail-closed). This is the
backend-authoritative guard — it lives where the run is assembled
(automation._apply_consumer_run_inversion), never on the agent (see the
"never trust BYO agents" rule).

Storage (AutomationWorkflow.input_rules):
    {"version": 1, "rules": {"<slot_key>": {"pattern": str,
        "flags": "<subset of ims>", "message": str | None}}}

The rules are stamped onto the data manifest (slot["validation"]) by
workflow_manifest.derive_data_manifest, so they flow into the listing detail
(shown to the creator AND the buyer) and the install snapshot. Enforcement reads
the LIVE listing manifest so a newly-set rule applies immediately to every
install — a buyer cannot bypass a format guard by withholding a sync.

ReDoS safety: a pattern is validated (length / nesting / structural caps + a
timeout-bounded probe) at SAVE time via security.validation.InputValidator, and
matched under a hard wall-clock timeout at RUN time via safe_regex_search (the
third-party ``regex`` module releases the GIL and honours ``timeout=``; the
stdlib ``re`` cannot be timed out). A matcher timeout counts as NO match, so the
run fails closed.
"""
from typing import Optional

import re

from security.validation import InputValidator

# Bumped if the stored shape changes.
RULES_VERSION = 1
# Defensive caps on the stored blob.
MAX_RULES = 200
MAX_MESSAGE_LEN = 200
# Inline regex flags a creator may set (case-insensitive / multiline / dotall).
_ALLOWED_FLAGS = "ims"
_FLAG_BITS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}

# Which keys of a stored rule are safe to ship in the data-less manifest.
_PUBLIC_RULE_KEYS = ("pattern", "flags", "message")


def _clean_flags(flags) -> str:
    """Keep only allowed, de-duplicated flag characters (order-stable)."""
    if not isinstance(flags, str):
        return ""
    out = []
    for ch in flags.lower():
        if ch in _ALLOWED_FLAGS and ch not in out:
            out.append(ch)
    return "".join(out)


def _flags_to_int(flags: str) -> int:
    bits = 0
    for ch in _clean_flags(flags):
        bits |= _FLAG_BITS[ch]
    return bits


def normalize_input_rules(raw) -> Optional[dict]:
    """Validate + normalize creator-supplied rules into the stored shape.

    Accepts ``{"rules": {key: {pattern, flags?, message?}}}``, a flat
    ``{key: {pattern,...}}``, or ``{key: "pattern"}``. Empty/blank patterns are
    dropped (a cleared field = no rule). Each surviving pattern is ReDoS-validated
    via ``InputValidator.validate_regex`` which raises ``HTTPException(400)`` on a
    bad or dangerous pattern — call this BEFORE any DB mutation so the 400 stays
    transaction-clean.

    Returns the normalized ``{"version", "rules"}`` dict, or ``None`` when there
    are no usable rules (so the caller can clear the column).
    """
    if raw is None:
        return None
    rules_in = raw.get("rules") if (isinstance(raw, dict) and "rules" in raw) else raw
    if not isinstance(rules_in, dict):
        return None

    out: dict = {}
    for key, spec in list(rules_in.items())[:MAX_RULES]:
        if not isinstance(key, str) or not key.strip():
            continue
        if isinstance(spec, str):
            pattern, flags, message = spec, "", None
        elif isinstance(spec, dict):
            pattern = spec.get("pattern")
            flags = _clean_flags(spec.get("flags"))
            message = spec.get("message")
        else:
            continue
        if not isinstance(pattern, str) or not pattern.strip():
            continue  # cleared field — no rule for this slot

        # ReDoS-safe validation (length / nesting / structural + timeout probe).
        # Raises HTTPException(400) on a bad or dangerous pattern.
        InputValidator.validate_regex(pattern)

        rule = {"pattern": pattern}
        if flags:
            rule["flags"] = flags
        if isinstance(message, str) and message.strip():
            rule["message"] = message.strip()[:MAX_MESSAGE_LEN]
        out[key.strip()] = rule

    if not out:
        return None
    return {"version": RULES_VERSION, "rules": out}


def rules_of(workflow) -> dict:
    """The {slot_key: rule} map stored on a workflow (or {} when none)."""
    blob = getattr(workflow, "input_rules", None) or {}
    rules = blob.get("rules") if isinstance(blob, dict) else None
    return rules if isinstance(rules, dict) else {}


def public_rule(rule) -> Optional[dict]:
    """Project a stored rule down to the manifest-safe (pattern/flags/message) view."""
    if not isinstance(rule, dict) or not rule.get("pattern"):
        return None
    return {k: v for k, v in rule.items() if k in _PUBLIC_RULE_KEYS}


def _full_match(pattern: str, flags: str, value: str) -> bool:
    """True iff ``value`` FULLY matches ``pattern`` (whole-value, ReDoS-safe).

    Anchored with ``\\A(?:…)\\Z`` so a top-level alternation in the creator
    pattern is still fully anchored and the entire value must conform. Runs under
    safe_regex_search's hard timeout (validate=False — the pattern was validated
    at save time); a timeout returns None ⇒ no match ⇒ fail-closed.
    """
    anchored = r"\A(?:%s)\Z" % pattern
    m = InputValidator.safe_regex_search(
        anchored, value, flags=_flags_to_int(flags), validate=False
    )
    return bool(m)


def find_violations(manifest, *, inputs: dict, secrets: dict) -> list:
    """Return the slots whose passed value violates the creator's regex rule.

    ``manifest`` is a data manifest with ``validation`` stamped onto input/secret
    slots (see derive_data_manifest). ``inputs`` is the resolved buyer input map
    (merged_form_data); ``secrets`` is the resolved secret map (all_creds). A
    missing / empty value is SKIPPED here — required-but-missing is handled by the
    caller's own missing-field check; this guard only constrains PROVIDED values.

    Each violation: ``{"slot_kind": "input"|"secret", "slot_key": str,
    "message": str|None}``. The offending value is never echoed back.
    """
    if not isinstance(manifest, dict):
        return []

    violations: list = []

    def _check(slot, value, kind: str):
        if not isinstance(slot, dict):
            return
        rule = slot.get("validation")
        if not isinstance(rule, dict):
            return
        pattern = rule.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return
        if value is None:
            return
        sval = value if isinstance(value, str) else str(value)
        if sval == "":
            return  # empty/missing is the required-field check's job, not ours
        try:
            ok = _full_match(pattern, rule.get("flags") or "", sval)
        except Exception:
            ok = False  # fail-closed on any matcher error
        if not ok:
            violations.append({
                "slot_kind": kind,
                "slot_key": slot.get("key"),
                "message": rule.get("message") or None,
            })

    _inputs = inputs or {}
    _secrets = secrets or {}
    for s in (manifest.get("input_slots") or []):
        key = s.get("key")
        # Primary bucket = inputs; fall back to secrets so a secret-typed input
        # (resolved into the credential map) is still validated.
        value = _inputs.get(key)
        if value is None:
            value = _secrets.get(key)
        _check(s, value, "input")
    for s in (manifest.get("secret_slots") or []):
        key = s.get("key")
        value = _secrets.get(key)
        if value is None:
            value = _inputs.get(key)
        _check(s, value, "secret")
    return violations
