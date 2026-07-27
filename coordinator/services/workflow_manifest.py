"""
workflow_manifest — pure-function derivation of a workflow's data-LESS slot
requirements (the "manifest").

THE INVERSION's contract surface. A published/sold workflow is a RECIPE only; the
creator's saved data (personas, credentials, secrets, sessions, form_data values)
is STRIPPED and NEVER used in a buyer's run. The manifest declares — by NAME and
TYPE only, with ZERO creator values — what data the BUYER must attach (their own
personas / secrets / input values) before they can run the installed recipe.

`derive_data_manifest(workflow)` REUSES the existing run-path detection helpers so
slot detection stays consistent with how a run actually resolves data:
  - _workflow_has_login (routers/automation.py)  — persona slot gating
  - _extract_placeholders (routers/automation.py) — input + inline secret: keys
  - decrypt_credentials (routers/automation.py)   — creator credential field NAMES
  - _derive_target_domain (local helper)          — login domain
  - PersonaService.linked_secret_refs             — persona vault BASE names

`derive_output_manifest(workflow)` is the OUTPUT side of the same contract: it
declares — by NAME / TYPE / DESCRIPTION only, with ZERO creator values — what data
a run PRODUCES (so a buyer can see "what data you get" before installing). It NEVER
surfaces a VALUE, a JS body, a JSONPath, or a CSS selector (THE INVERSION applies to
outputs too). It scans STORED step JSON + functions exhaustively and engine-
agnostically (recorder / AI / desktop shapes). Its result is folded into
`derive_data_manifest(workflow)` under the `"output_fields"` key, but is EXCLUDED
from `recipe_hash` (outputs are display-only and must never churn the install-drift
hash or trigger an isolation-tier reclassification).

Output shape (all names/types, zero values):
  {persona_slots:[...], secret_slots:[...], input_slots:[...],
   output_fields:[{key,type,description,source,dynamic?}],
   manifest_version:int, has_login:bool}

`manifest_hash(manifest)` = sha256 of the sorted slot keys; drift detection if a
creator edits steps post-publish (new required slots => potential secret-harvest
attempt => flag re-review). It reads only persona/secret/input slot keys —
`output_fields` is NOT part of the security-relevant slot drift hash.

`output_hash(output_manifest)` = sha256 of the sorted output field KEYS — a stable
display-only signal that the set of produced fields changed.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlparse
from typing import Optional

# Current manifest schema version. Bump when the slot shape changes.
MANIFEST_VERSION = 1

# Output (data-LESS "what you get") manifest schema version.
OUTPUT_MANIFEST_VERSION = 1

# An output NAME must be a short identifier — guards against treating a JS body
# (extract:computed config.value) or a literal value as a field name.
_OUTPUT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,60}$")

# Field names treated as secrets when they appear as creator credential keys or
# as bare placeholder keys without the explicit "secret:" prefix.
_SECRET_FIELD_HINTS = {
    "password", "passwd", "pass", "pwd",
    "api_key", "apikey", "api-key", "secret", "token", "access_token",
    "client_secret", "private_key", "totp_seed", "otp_secret",
}

# Login-credential field names that a buyer's PERSONA auto-fills at run time
# (PersonaService.resolve_login_credentials supplies these, and automation.py
# folds them into the run's credentials). When a workflow has a login persona
# slot, slots with these keys are "persona_satisfiable": attaching a persona
# covers them, so they must NOT be separately required — but a buyer who prefers
# not to use a persona can still fill them manually / from a vault secret.
_PERSONA_LOGIN_FIELDS = {
    "username", "user", "user_name", "login", "loginid", "login_id", "userid",
    "user_id", "account", "email", "e_mail", "mail", "emailaddress",
    "email_address", "password", "passwd", "pwd", "pass", "phone",
    "phone_number", "mobile",
}


def _derive_target_domain(workflow) -> Optional[str]:
    """Host of the workflow's entry URL (fallback: first navigate step's URL).

    Reads NAMES/URLs only — never a creator VALUE.
    """
    candidate = getattr(workflow, "entry_url", None)
    if not candidate:
        for step in (getattr(workflow, "steps", None) or []):
            if isinstance(step, dict) and step.get("type") == "navigate":
                candidate = step.get("url") or (step.get("options") or {}).get("url")
                if candidate:
                    break
    if not candidate:
        return None
    try:
        host = urlparse(str(candidate)).netloc.lower()
    except Exception:
        return None
    return host or None


def _norm_field(key: Optional[str]) -> str:
    """Normalize a slot/field key for credential matching (lowercase, non-alnum
    collapsed to underscores, trimmed)."""
    return re.sub(r"[^a-z0-9]+", "_", (key or "").lower()).strip("_")


def _is_persona_login_field(key: Optional[str]) -> bool:
    return _norm_field(key) in _PERSONA_LOGIN_FIELDS


def _registrable_domain(host: Optional[str]) -> Optional[str]:
    """Best-effort registrable domain (last two labels) of a host. Pure, no deps.

    e.g. 'www.shop.amazon.com' -> 'amazon.com'. Good enough for grouping distinct
    login domains; matches the granularity used elsewhere for target_domain.
    """
    if not host:
        return None
    host = host.lower().strip()
    # strip credentials/port if a full URL netloc slipped through
    host = host.split("@")[-1].split(":")[0]
    labels = [p for p in host.split(".") if p]
    if len(labels) <= 2:
        return host or None
    return ".".join(labels[-2:])


_LOGIN_FIELD_RE = re.compile(
    r"type=['\"]?password|autocomplete=['\"]?(current-password|new-password)"
    r"|name=['\"]?(password|passwd|pwd)|\bpassword\b",
    re.I,
)


def _distinct_login_domains(steps) -> list[str]:
    """Scan distinct `navigate` step hosts that PRECEDE a password/login field and
    emit one registrable domain per distinct login boundary.

    Walks steps in order, tracking the most recent navigate host; when a step that
    looks like a login/password field is seen, the current host's registrable
    domain is recorded.
    """
    domains: list[str] = []
    seen: set[str] = set()
    current_host: Optional[str] = None
    for step in (steps or []):
        if not isinstance(step, dict):
            continue
        stype = step.get("type")
        config = step.get("config") or {}
        if stype == "navigate":
            url = step.get("url") or config.get("url") or (step.get("options") or {}).get("url")
            if url:
                try:
                    current_host = urlparse(str(url)).netloc.lower()
                except Exception:
                    current_host = None
            continue
        # Does this step touch a password/login field?
        blob = json.dumps(step)
        if _LOGIN_FIELD_RE.search(blob):
            reg = _registrable_domain(current_host)
            if reg and reg not in seen:
                seen.add(reg)
                domains.append(reg)
    return domains


def derive_data_manifest(workflow) -> dict:
    """Compute the data-LESS slot requirements of a workflow.

    Pure function: reads only NAMES/TYPES from the workflow; never copies a creator
    VALUE (no decrypted secret values, no form_data values, no persona usernames,
    no session blobs) into the output.
    """
    # Import inside the function to avoid a circular import at module load
    # (routers.automation imports services which import models...).
    from routers.automation import (
        _workflow_has_login,
        _extract_placeholders,
        decrypt_credentials,
    )
    from services.persona_service import PersonaService

    steps = getattr(workflow, "steps", None) or []
    form_data = getattr(workflow, "form_data", None) or {}
    credentials_encrypted = getattr(workflow, "credentials_encrypted", None)
    functions = getattr(workflow, "functions", None) or []
    default_persona = getattr(workflow, "default_persona", None)

    has_login = bool(
        _workflow_has_login(steps, form_data, credentials_encrypted)
    )

    # ------------------------------------------------------------------
    # PERSONA SLOTS
    # ------------------------------------------------------------------
    persona_slots: list[dict] = []
    if has_login:
        # twofa hint: expose only the METHOD NAME from the creator persona — never
        # the seed/mailbox/relay.
        twofa_method = None
        if default_persona is not None:
            m = getattr(default_persona, "twofa_method", None)
            if m and m != "none":
                twofa_method = m

        login_domains = _distinct_login_domains(steps)
        if not login_domains:
            primary = _derive_target_domain(workflow)
            login_domains = [_registrable_domain(primary)] if primary else [None]

        # SINGLE persona slot only. The consumer run path currently binds and
        # applies exactly ONE persona (persona_ids[0]); advertising a per-domain
        # slot for each distinct login boundary would promise a multi-persona
        # capability the run path can't satisfy (the 2nd login would silently
        # fail). Collapse multi-domain login to one slot until per-domain persona
        # dispatch is supported. The primary login domain anchors the slot.
        primary_dom = login_domains[0]
        persona_slots.append({
            "slot": "login",
            "kind": "persona",
            "target_domain": primary_dom,
            "label": f"Login for {primary_dom}" if primary_dom else "Login",
            # NAME only — buyer's own persona supplies the actual 2FA.
            "twofa": twofa_method or "none",
        })

    # ------------------------------------------------------------------
    # SECRET SLOTS — union of bare secret: placeholder names, creator credential
    # field NAMES (values dropped), and persona linked-secret BASE names.
    # ------------------------------------------------------------------
    # key -> {label, persona_satisfiable}. persona_satisfiable means a login
    # persona, once attached, supplies this field at run time (so it isn't a
    # separate required attachment, just an optional manual override).
    secret_keys: dict[str, dict] = {}

    def _add_secret(key: str, label: str, persona_satisfiable: bool):
        if key in secret_keys:
            secret_keys[key]["persona_satisfiable"] = (
                secret_keys[key]["persona_satisfiable"] or persona_satisfiable
            )
        else:
            secret_keys[key] = {"label": label, "persona_satisfiable": persona_satisfiable}

    placeholders = _extract_placeholders(steps, form_data)

    # (a) bare names of {{secret:...}} placeholders. Only persona-satisfiable when
    #     the workflow logs in AND the key is a known login-credential field
    #     (e.g. {{secret:password}}); a standalone {{secret:api_key}} is NOT
    #     supplied by a persona and stays independently required.
    for ph in placeholders:
        key = ph.get("key") or ""
        if key.startswith("secret:"):
            bare = key[len("secret:"):].strip()
            if bare:
                _add_secret(
                    bare, ph.get("label") or bare,
                    has_login and _is_persona_login_field(bare),
                )

    # (b) creator credentials_encrypted field NAMES (decrypt keys, DROP values).
    #     These ARE the login credentials, so a persona supplies them.
    if credentials_encrypted:
        try:
            creds = decrypt_credentials(credentials_encrypted)
            if isinstance(creds, dict):
                for fname in creds.keys():
                    if isinstance(fname, str) and fname:
                        _add_secret(fname, fname.replace("_", " "), has_login)
        except Exception:
            # A corrupted/rotated key must not leak — just skip; the run path
            # re-validates attachments anyway.
            pass

    # (c) creator persona linked_secret_refs BASE names, reframed as buyer-suppliable.
    #     A buyer's own persona supplies its own linked secrets (2FA seed, etc.).
    if default_persona is not None:
        try:
            for _field, base in (PersonaService.linked_secret_refs(default_persona) or {}).items():
                if isinstance(base, str) and base:
                    _add_secret(base, base.replace("_", " "), has_login)
        except Exception:
            pass

    secret_slots = [
        {
            "key": k,
            "label": v["label"],
            "kind": "secret",
            "persona_satisfiable": v["persona_satisfiable"],
        }
        for k, v in sorted(secret_keys.items())
    ]

    # ------------------------------------------------------------------
    # INPUT SLOTS — non-secret placeholders + functions[].input_variables.
    # form_data VALUES are never read (only keys).
    # ------------------------------------------------------------------
    input_slots: list[dict] = []
    input_seen: set[str] = set()

    for ph in placeholders:
        key = ph.get("key") or ""
        if not key or key.startswith("secret:") or key.startswith("__"):
            continue
        if key in input_seen:
            continue
        input_seen.add(key)
        input_slots.append({
            "key": key,
            "label": ph.get("label") or key.replace("_", " "),
            "field_type": ph.get("field_type"),
            "required": True,
            "source": "form_data",
            "persona_satisfiable": has_login and _is_persona_login_field(key),
        })

    for fn in functions:
        if not isinstance(fn, dict):
            continue
        fname = fn.get("name") or "function"
        for iv in (fn.get("input_variables") or []):
            # input_variables entries may be plain strings or {name,...} dicts.
            if isinstance(iv, dict):
                key = iv.get("name") or iv.get("key")
                label = iv.get("label") or (key.replace("_", " ") if key else None)
                ftype = iv.get("field_type") or iv.get("type")
            else:
                key = str(iv)
                label = key.replace("_", " ")
                ftype = None
            if not key or key in input_seen:
                continue
            input_seen.add(key)
            input_slots.append({
                "key": key,
                "label": label or key,
                "field_type": ftype,
                "required": True,
                "source": f"function:{fname}",
                "persona_satisfiable": has_login and _is_persona_login_field(key),
            })

    # ------------------------------------------------------------------
    # FILE SLOTS (§4.2) — upload steps that declare a buyer-bound file SLOT.
    # A creator NEVER ships a concrete file (only a config.file_slot name); the
    # buyer binds their own StoredFile to the slot at install/run time. Reads NAMES
    # only (no file bytes, no creator file_id leaves the workflow).
    # ------------------------------------------------------------------
    file_slots: list[dict] = []
    file_seen: set[str] = set()
    for step in steps:
        if not isinstance(step, dict) or step.get("type") != "upload":
            continue
        cfg = step.get("config") or {}
        slot = cfg.get("file_slot")
        if not slot or not isinstance(slot, str) or slot in file_seen:
            continue
        file_seen.add(slot)
        file_slots.append({
            "slot": slot,
            "kind": "file",
            "label": (cfg.get("label") or slot.replace("_", " ")),
            "is_multiple": bool(cfg.get("is_multiple")),
            "required": True,
        })

    # Annotate the persona slot with the credential fields it covers, so the UI
    # can explain "attaching a persona auto-fills login / username / password"
    # instead of listing those as separate inputs.
    if persona_slots:
        covers = [s["key"] for s in secret_slots if s.get("persona_satisfiable")]
        covers += [s["key"] for s in input_slots if s.get("persona_satisfiable")]
        persona_slots[0]["covers_fields"] = covers

    # Stamp the creator's per-input VALIDATION rules onto their slots so the rule
    # flows into listing.data_manifest -> install snapshot -> the run-time guard,
    # and is shown to the creator + buyer. A regex is creator-authored metadata
    # (pattern/flags/message NAMES only — never a creator secret VALUE), so the
    # recipe stays data-LESS. Enforced in automation._apply_consumer_run_inversion.
    from services.input_rules import rules_of as _rules_of, public_rule as _public_rule
    _rules = _rules_of(workflow)
    if _rules:
        for _slot in input_slots:
            _pub = _public_rule(_rules.get(_slot.get("key")))
            if _pub:
                _slot["validation"] = _pub
        for _slot in secret_slots:
            _pub = _public_rule(_rules.get(_slot.get("key")))
            if _pub:
                _slot["validation"] = _pub

    return {
        "persona_slots": persona_slots,
        "secret_slots": secret_slots,
        "input_slots": input_slots,
        # FILE SLOTS (§4.2): buyer-bound upload-step file slots (names only).
        "file_slots": file_slots,
        # OUTPUT side of the contract — what the run PRODUCES (names/types/desc only).
        # Folded in here so it flows automatically through serve_recipe() ->
        # listing.data_manifest -> install snapshot -> get_listing_detail input_schema.
        # EXCLUDED from recipe_hash (see recipe_hash) so it never churns install drift.
        "output_fields": derive_output_manifest(workflow).get("output_fields", []),
        "manifest_version": MANIFEST_VERSION,
        "has_login": has_login,
    }


def _clean_output_name(val) -> Optional[str]:
    """Coerce a candidate output NAME: return it only if it is a short, identifier-
    like string. Drops JS bodies, JSONPaths, literal values, and anything non-name."""
    if not isinstance(val, str):
        return None
    v = val.strip()
    return v if _OUTPUT_NAME_RE.match(v) else None


def derive_output_manifest(workflow) -> dict:
    """Data-LESS OUTPUT contract — what data a run PRODUCES, by NAME/TYPE/
    DESCRIPTION only. NEVER a value, JS body, JSONPath, or selector (THE INVERSION).
    Returns {output_fields:[{key,type,description,source,dynamic?}], output_version:int}.
    Engine-agnostic: unions every name location across recorder/AI/desktop shapes.

    Pure function: reads only NAMES/TYPES from the stored step/function JSON; never
    copies a creator VALUE, a JS script body, a JSONPath, or a CSS selector.
    """
    steps = getattr(workflow, "steps", None) or []
    functions = getattr(workflow, "functions", None) or []

    # Ordered, dedup-by-key accumulator. First occurrence wins on type/description.
    ordered: dict[str, dict] = {}

    def _add(key, type_, source, description=None, dynamic=False, clean=True):
        """Add a field. By default the name is coerced through _clean_output_name;
        pass clean=False for creator-declared function output_fields names (which may
        be added as-is, still string-coerced)."""
        if clean:
            key = _clean_output_name(key)
        else:
            key = key if isinstance(key, str) and key.strip() else None
            if key is not None:
                key = key.strip()
        if not key or key in ordered:
            return
        entry = {
            "key": key,
            "type": type_,
            "description": description if isinstance(description, str) and description else None,
            "source": source,
        }
        if dynamic:
            entry["dynamic"] = True
        ordered[key] = entry

    # Collected return-step whitelists (each a list of identifier strings).
    return_whitelist: list[str] = []

    # ------------------------------------------------------------------
    # STEPS
    # ------------------------------------------------------------------
    for step in steps:
        if not isinstance(step, dict):
            continue
        opts = step.get("options") or {}
        config = step.get("config") or {}
        cfg_opts = config.get("options") or {}
        stype = step.get("type")
        desc = step.get("description")

        if stype == "extract":
            # A computed extract stores a JS body in value/config.value — never a name.
            computed = (
                opts.get("extract_type") == "computed"
                or cfg_opts.get("extract_type") == "computed"
                or config.get("script") is not None
            )
            name = (
                _clean_output_name(opts.get("output_name"))
                or _clean_output_name(opts.get("variable"))
                or _clean_output_name(cfg_opts.get("output_name"))
                or _clean_output_name(config.get("variable"))
                or _clean_output_name(config.get("output_key"))
            )
            if name is None and not computed:
                # last-resort value/config.value fallback — MUST be identifier-like.
                name = _clean_output_name(step.get("value")) or _clean_output_name(config.get("value"))
            _add(name, "string", "extract", desc)

        elif stype in ("evaluate", "evaluate_js"):
            name = _clean_output_name(config.get("variable")) or _clean_output_name(opts.get("variable"))
            if name:
                _add(name, "object", "evaluate", desc)
            else:
                # Fields are determined at runtime by the script (never executed).
                _add(_clean_output_name(desc) or "result", "object", "evaluate", desc, dynamic=True)

        elif stype == "api_call":
            name = _clean_output_name(config.get("variable"))
            _add(name, "string", "api_call", desc)
            # response_extractions KEYS are additional named outputs — NEVER the
            # JSONPath VALUES.
            re_map = config.get("response_extractions")
            if isinstance(re_map, dict):
                for k in re_map.keys():
                    _add(k, "string", "api_call", desc)

        elif stype == "wait_for_change":
            name = (
                _clean_output_name(opts.get("output_name"))
                or _clean_output_name(opts.get("variable"))
                or _clean_output_name(config.get("variable"))
            )
            # Base key only — don't enumerate _changed/_hash/_previous companions.
            _add(name, "object", "wait_for_change", desc)

        elif stype == "return":
            for of in (opts.get("output_fields") or []):
                nm = _clean_output_name(of)
                if nm:
                    return_whitelist.append(nm)

    # ------------------------------------------------------------------
    # FUNCTIONS
    # ------------------------------------------------------------------
    for fn in functions:
        if not isinstance(fn, dict):
            continue
        for of in (fn.get("output_fields") or []):
            if isinstance(of, str):
                # creator-declared; add as-is (string-coerced, not identifier-gated).
                _add(of, "string", "function", clean=False)
            elif isinstance(of, dict):
                # Project to name/type/description; selector is creator IP — dropped.
                _add(
                    of.get("name"), of.get("type") or "string", "function",
                    of.get("description"), clean=False,
                )
        re_map = fn.get("response_extractions")
        if isinstance(re_map, dict):
            for k in re_map.keys():
                _add(k, "string", "function")

    output_fields = list(ordered.values())

    # ------------------------------------------------------------------
    # RETURN-step whitelist override: when present, the delivered set is filtered
    # to the whitelisted names (a return step explicitly names what ships).
    # ------------------------------------------------------------------
    if return_whitelist:
        wl = list(dict.fromkeys(return_whitelist))  # dedup, preserve order
        wl_set = set(wl)
        kept = {f["key"]: f for f in output_fields if f["key"] in wl_set}
        result: list[dict] = []
        for nm in wl:
            if nm in kept:
                result.append(kept[nm])
            else:
                result.append({"key": nm, "type": "string", "description": None, "source": "return"})
        output_fields = result

    return {
        "output_fields": output_fields,
        "output_version": OUTPUT_MANIFEST_VERSION,
    }


def manifest_hash(manifest: dict) -> str:
    """sha256 of the sorted slot keys across all slot kinds — stable drift signal.

    Keys only (not labels/types) so cosmetic label edits don't churn the hash, but
    any added/removed/renamed required slot (the security-relevant change) does.
    """
    keys: list[str] = []
    for s in (manifest.get("persona_slots") or []):
        keys.append("persona:" + str(s.get("slot")))
    for s in (manifest.get("secret_slots") or []):
        keys.append("secret:" + str(s.get("key")))
    for s in (manifest.get("input_slots") or []):
        keys.append("input:" + str(s.get("key")))
    digest = hashlib.sha256("\n".join(sorted(keys)).encode("utf-8")).hexdigest()
    return digest


def output_hash(output_manifest: dict) -> str:
    """sha256 over sorted output field KEYS — stable drift signal for outputs."""
    keys = sorted(str(f.get("key")) for f in (output_manifest.get("output_fields") or []))
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def recipe_hash(recipe: dict) -> str:
    """sha256 over the data-LESS recipe (serve_recipe output) — a stable signal that
    the CREATOR edited the source recipe in ANY way (steps / raw_replay / entry_url /
    exit / timeout / retry / functions / manifest), not just a slot change. An install
    compares this against the LIVE source recipe to surface 'update available'. Keyed
    on the recipe content the buyer actually runs; canonical JSON (sorted keys) so the
    hash is stable across dict ordering.

    NOTE: data_manifest["output_fields"] is EXCLUDED from the hash — outputs are a
    display-only ("what data you get") signal, so adding/refreshing them must NOT
    churn the install-drift hash (would cause a one-time false 'update available' and
    flip an install to the ISOLATED cloud tier via _apply_consumer_run_inversion)."""
    _dm = dict(recipe.get("data_manifest") or {})
    _dm.pop("output_fields", None)  # outputs are display-only; exclude from drift hash
    payload = {
        "steps": recipe.get("steps") or [],
        "raw_replay": recipe.get("raw_replay") or [],
        "entry_url": recipe.get("entry_url"),
        "exit_condition": recipe.get("exit_condition"),
        "timeout_ms": recipe.get("timeout_ms"),
        "retry_count": recipe.get("retry_count"),
        "functions": recipe.get("functions") or [],
        "data_manifest": _dm,
    }
    # STREAMING recipe: a streaming workflow's live recipe is its streaming_config
    # (advanced_script / handlers / openai_compat) + buyer-faithful runtime knobs. Fold
    # them into the drift hash so a creator edit to the STREAMING recipe (not just
    # steps) surfaces 'update available' to buyers and re-tiers a drifted install to
    # ISOLATED — the SAME drift semantics a steps edit has. Only present in a streaming
    # recipe (serve_recipe omits these keys for non-streaming workflows), so this is a
    # no-op for one-shot recipes and does NOT churn their hash.
    if recipe.get("streaming_config") is not None:
        payload["streaming_config"] = recipe.get("streaming_config")
    for _f in ("session_persistence", "session_ttl_seconds",
               "login_url_patterns", "headless", "fast_mode"):
        if _f in recipe:
            payload[_f] = recipe.get(_f)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
