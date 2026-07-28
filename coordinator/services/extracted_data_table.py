"""Aggregate the data a workflow's runs extracted into one sortable table.

Every run persists what it scraped/extracted in ``AutomationTask.result_data``
under the ``extracted_data`` key. That value is either:

  * a single record (a ``dict`` — one row), or
  * a list of records (the canonical list/detail/pagination scraper output —
    one row per item).

This module flattens the runs of a workflow into a uniform ``(columns, rows)``
table so the UI can render a sortable/searchable grid and offer CSV/JSON export.
Each emitted row carries the run it came from (run id, when, status) alongside
the extracted fields, so a user can trace any value back to its run.

The column set is derived from the workflow's DECLARED output fields
(``_declared_output_fields`` — the union of ``output_fields`` across the
workflow's callable functions) when the workflow declares one; declared columns
come first and in declared order. Any extra keys a run actually produced are
appended after, so nothing a run extracted is ever hidden. When the workflow
declares nothing, columns are the union of keys across the scanned runs, in
first-seen order.

Search/sort/paginate happen over the materialized rows in Python: the shapes are
dynamic JSONB so there is no clean SQL projection, and the caller bounds the scan
(``scanned_runs`` / ``truncated``) to keep it cheap.

Beyond the flat grid (``view=all``) this module also implements the record
LINEAGE engine (DATA_REDESIGN_SPEC.md Part 1): each successful data-bearing run
is a dated snapshot, records get identity across snapshots (explicit key → auto
column scoring → singleton → whole-record hash), and the lens builders derive
new/changed/same/missing per record, per-run deltas, and change-point version
histories — all read-time, no new storage. The normative reference
implementation is shared/data_lineage_golden_gen.py; the golden vectors in
shared/data_lineage_golden.json pin this file's behavior byte-for-byte across
the backend, the self-host coordinator (byte-identical twin of this file) and
the Rust daemon.
"""

from __future__ import annotations

import csv
import hashlib
import html as html_lib
import io
import json
import math
import re
from typing import Any, Callable, Optional

# Keys under result_data (besides extracted_data) that may carry the declared
# fields at the TOP level rather than nested — a workflow can surface a single
# declared field directly on result_data. We fall back to these only when there
# is no usable extracted_data.
_RESERVED_TOPLEVEL = {
    "extracted_data",
    "steps",
    "steps_completed",
    "screenshots",
    "success",
    "error",
    "auth_session",
    "needs_reassignment",
    "ai_session_id",
    "workflow_id",
}

# The automation engine injects execution metadata / flags INTO extracted_data
# (automation_engine.py: _error_context, _captcha_info, dry_run,
# stopped_before_commit, …). Those are NOT extracted data — strip them so the
# table shows only the real result fields, never the run's error/protocol envelope.
_META_FLAG_KEYS = {
    "dry_run",
    "stopped_before_commit",
    "captcha_info",
    "error_context",
}

# Clearly-internal / response-envelope / execution-control keys that are NOT
# user-facing extracted data. Hidden even when a workflow declares no output
# schema, so the table never surfaces the raw response, session/capture plumbing,
# or the engine's repair/captcha/auth control fields.
_INTERNAL_KEYS = {
    # session / capture plumbing
    "auth_session",
    "session_state",
    "session_storage",
    "local_storage",
    "localstorage",
    "cookies",
    "screenshot",
    "screenshots",
    "fingerprint",
    "headers",
    # raw response envelope
    "raw_response",
    "raw_html",
    "response",
    "response_body",
    "responsebody",
    "raw",
    "raw_data",
    "page_html",
    "page_source",
    "har",
    "network_log",
    "console_log",
    "step_results",
    # execution-control flags the engine merges into the result
    "session_expired",
    "needs_reassignment",
    "reassignment_reason",
    "auth_failed",
    "login_success",
    "logged_in",
    "is_logged_in",
    "stopped_before_commit",
}

# Key PREFIXES that mark engine control/metadata (e.g. ai_repair_attempted,
# ai_repair_data, captcha_detected, captcha_blocked).
_META_PREFIXES = ("ai_repair", "captcha")

# Generic wrapper keys an extractor may nest the real records under (evaluate_js
# stores its value under output_name, default "extracted_data"). When the cleaned
# payload is just one of these wrapping a dict, unwrap to the inner record.
_WRAPPER_KEYS = {
    "extracted_data",
    "result",
    "results",
    "data",
    "items",
    "records",
    "rows",
    "output",
    "outputs",
    "extracted",
    "values",
}


def _is_meta_key(k: Any) -> bool:
    # Underscore-prefixed (e.g. _error_context), a known flag/internal key, or a
    # control prefix (ai_repair_*, captcha_*) — none are user-facing extracted data.
    if not isinstance(k, str):
        return False
    kl = k.lower()
    if k.startswith("_") or kl in _META_FLAG_KEYS or kl in _INTERNAL_KEYS:
        return True
    return any(kl.startswith(p) for p in _META_PREFIXES)


def _strip_meta(d: dict) -> dict:
    return {k: v for k, v in d.items() if not _is_meta_key(k)}


def _wrap_scalar_list(items: list) -> list[dict]:
    """A list of records -> those dicts; a list of scalars -> one {"value": x} each.
    (Nested-collection pivots only — the top-level flatten uses ``_coerce_list``,
    which keeps stored-slot indices.)"""
    out: list[dict] = []
    for x in items:
        if isinstance(x, dict):
            out.append(x)
        elif x is not None:
            out.append({"value": x})
    return out


# Strict numeric-looking test (spec 1.2): integers/decimals/exponents only —
# float()'s extras ("inf", "1_000", whitespace forms) must NOT normalize.
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$")


def _is_scalar(v: Any) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


def _looks_numeric(s: str) -> bool:
    return bool(_NUM_RE.match(s.strip()))


def _detect_table(lst: Any) -> Optional[list]:
    """The header row when ``lst`` is an array-of-arrays TABLE (spec 1.1(a)):
    >=2 rows, every row all-scalar, header all non-empty strings with >=50%
    non-numeric-looking and all distinct after trimming, and >=90% of the data
    rows exactly header-width. None when the shape doesn't qualify."""
    if not isinstance(lst, list) or len(lst) < 2:
        return None
    if not all(isinstance(r, list) and all(_is_scalar(c) for c in r) for r in lst):
        return None
    h = lst[0]
    if not h or not all(isinstance(c, str) and c.strip() != "" for c in h):
        return None
    non_numeric = sum(1 for c in h if not _looks_numeric(c))
    if non_numeric * 2 < len(h):  # ≥50% non-numeric-looking
        return None
    trimmed = [c.strip() for c in h]
    if len(set(trimmed)) != len(trimmed):
        return None
    rest = lst[1:]
    same_len = sum(1 for r in rest if len(r) == len(h))
    if same_len * 10 < len(rest) * 9:  # ≥90%
        return None
    return h


def _coerce_list(lst: list) -> list[tuple[int, dict]]:
    """A list value → [(record_index, fields)] with record_index = the STORED
    slot index, so GET rows and DELETE (run_id, record_index) refs stay
    coherent. A table's header lives at slot 0 and is never emitted (first data
    row has record_index 1); an all-arrays list that fails the header test
    becomes {col_1: ..} records; scalars (None included — slots must not shift)
    wrap as {"value": x}."""
    header = _detect_table(lst)
    if header:
        out: list[tuple[int, dict]] = []
        for i, row in enumerate(lst[1:], start=1):
            rec = {}
            for j, name in enumerate(header):
                rec[name] = row[j] if j < len(row) else None
            out.append((i, rec))
        return out
    if lst and all(isinstance(r, list) and all(_is_scalar(c) for c in r) for r in lst):
        # all-arrays but the header test failed → col_N records, own row lengths
        return [(i, {f"col_{j+1}": c for j, c in enumerate(row)}) for i, row in enumerate(lst)]
    out = []
    for i, item in enumerate(lst):
        if isinstance(item, dict):
            out.append((i, item))
        else:
            out.append((i, {"value": item}))
    return out


def _coerce_records(ed: Any) -> tuple[list[tuple[int, dict]], bool, dict[int, str]]:
    """Turn a run's extracted_data into ``([(record_index, fields)], data_bearing,
    sources)``:
      - a list -> one record per stored slot (tables / col_N via ``_coerce_list``);
        an EMPTY list is an explicit-empty dataset (0 records, data-bearing);
      - a dict with list-valued keys -> EVERY non-empty list unwrapped in sorted
        key order, record_index = global running counter across the expansion;
        all lists empty = explicit-empty; non-list siblings are dropped;
      - a dict whose only real key is a generic WRAPPER over a dict -> the inner
        record (unwraps a `{"result": {...}}`-style response envelope);
      - otherwise the dict itself is one record.
    ``data_bearing=False`` marks the {} / meta-only runs the snapshot chain
    skips entirely. Execution-metadata keys are stripped throughout.

    ``sources`` maps record_index -> the originating list KEY, populated only by
    the dict list-key expansion (spec 3.1); single-list/wrapper/dict shapes stay
    untagged (empty map -> source null in the lineage views).
    """
    if isinstance(ed, list):
        if len(ed) == 0:
            return [], True, {}  # explicit empty dataset
        return _coerce_list(ed), True, {}
    if isinstance(ed, dict):
        stripped = _strip_meta(ed)
        if not stripped:
            return [], False, {}  # meta-only / {} — not data-bearing
        list_keys = sorted(k for k, v in stripped.items() if isinstance(v, list))
        if list_keys:
            nonempty = [k for k in list_keys if stripped[k]]
            if not nonempty:
                return [], True, {}  # explicit-empty wrapper (e.g. {"posts": [], "total": 0})
            out: list[tuple[int, dict]] = []
            sources: dict[int, str] = {}
            idx = 0
            for k in nonempty:
                for _, rec in _coerce_list(stripped[k]):
                    out.append((idx, rec))
                    sources[idx] = k
                    idx += 1
            return out, True, sources
        # A single generic wrapper over a dict -> the inner record (response envelope).
        if len(stripped) == 1:
            (only_key, only_val), = stripped.items()
            if isinstance(only_val, dict) and str(only_key).lower() in _WRAPPER_KEYS:
                inner = _strip_meta(only_val)
                return ([(0, inner)], True, {}) if inner else ([], False, {})
        return [(0, stripped)], True, {}
    return [], False, {}


# ---------------------------------------------------------------------------
# Nested collections — a record commonly holds one or more nested arrays of
# records (e.g. ``posts.items``, ``categories``). A caller can pivot the table
# to any such path so the nested array becomes the rows (one per item), or
# address a single item with a numeric path segment (``posts.items.2``).
# ---------------------------------------------------------------------------

def resolve_path(obj: Any, path: str) -> Any:
    """Resolve a dot-path within a record. A numeric segment indexes into a list
    (``items.2`` -> 3rd element); a name segment reads a dict key. Returns None
    when any segment doesn't exist."""
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, list):
            try:
                idx = int(seg)
            except (ValueError, TypeError):
                return None
            if -len(cur) <= idx < len(cur):
                cur = cur[idx]
            else:
                return None
        elif isinstance(cur, dict):
            cur = cur.get(seg)
        else:
            return None
    return cur


def _records_at(target: Any) -> list[dict]:
    """The record-dicts at a resolved collection target: a list -> its items
    (scalars wrapped as ``{"value": x}``); a single dict -> one record; a scalar
    -> one ``{"value": x}``; None/missing -> []."""
    if target is None:
        return []
    if isinstance(target, list):
        return _wrap_scalar_list(target)
    if isinstance(target, dict):
        return [target]
    return [{"value": target}]


def discover_collections(rows: list[dict], max_depth: int = 3) -> list[dict]:
    """Find the nested array-of-record paths within the flattened rows' fields
    (e.g. ``posts.items``, ``categories``), each with a total item count, so a
    caller can discover what `collection` values are available to pivot to."""
    counts: dict[str, int] = {}

    def visit(val: Any, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(val, list):
            if val and all(isinstance(x, dict) for x in val):
                counts[prefix] = counts.get(prefix, 0) + len(val)
            return
        if isinstance(val, dict):
            for k, v in val.items():
                visit(v, f"{prefix}.{k}" if prefix else str(k), depth + 1)

    for r in rows:
        fields = r.get("fields") or {}
        for k, v in fields.items():
            visit(v, str(k), 1)
    return [{"path": p, "count": c} for p, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


# ---------------------------------------------------------------------------
# Run INPUT values — the form/parameter values a run was dispatched with, so a
# consumer can fetch only the rows produced by a given input (e.g. city=Paris).
# Inputs are stored on the task's trigger_context (the workflow-queue stashes
# them under `_queued_form_data`; direct dispatch carries `form_data`; the
# marketplace consumer inversion produces `merged_form_data`). Input columns are
# namespaced with INPUT_PREFIX so they never collide with an extracted field of
# the same name, and so a caller can target them explicitly in filters/sort.
# ---------------------------------------------------------------------------
INPUT_PREFIX = "input."

# trigger_context keys that may carry the run's input/form values, in priority
# order. Only dict values are accepted; the first non-empty one wins.
_INPUT_CTX_KEYS = ("_queued_form_data", "merged_form_data", "form_data", "inputs")

# Input KEYS whose values are secret/sensitive and must never be surfaced as a
# filterable column (auth workflows take passwords/OTPs/tokens as inputs). These
# keys are dropped entirely — neither the value nor the column name is exposed.
_SECRET_INPUT_RE = re.compile(
    r"(password|passwd|pwd|secret|token|api[_-]?key|apikey|otp|cvv|cvc|ssn"
    r"|private[_-]?key|credential|card[_-]?number|\bpan\b)",
    re.IGNORECASE,
)


def _task_inputs(task) -> dict:
    """The input/form values a run was dispatched with, or {} when none are
    recorded. Read from the task's trigger_context (see _INPUT_CTX_KEYS)."""
    ctx = getattr(task, "trigger_context", None)
    if not isinstance(ctx, dict):
        return {}
    for k in _INPUT_CTX_KEYS:
        v = ctx.get(k)
        if isinstance(v, dict) and v:
            return v
    return {}


def redact_inputs(inputs: dict) -> dict:
    """Drop secret-shaped input keys (password/token/otp/…) so surfacing run
    inputs as columns can't leak credentials. Keeps only string-keyed entries."""
    if not isinstance(inputs, dict):
        return {}
    return {
        k: v
        for k, v in inputs.items()
        if isinstance(k, str) and not _SECRET_INPUT_RE.search(k)
    }


def _run_meta(task) -> dict:
    """Per-run metadata stamped onto every row a run produces."""
    when = getattr(task, "completed_at", None) or getattr(task, "created_at", None)
    return {
        "run_id": task.id,
        "run_at": when.isoformat() if when else None,
        "status": getattr(task, "status", None),
        "success": getattr(task, "success", None),
        "duration_ms": getattr(task, "duration_ms", None),
    }


def _records_from_result(
    result_data: Optional[dict], declared: list[str]
) -> tuple[list[tuple[int, dict]], bool, dict[int, str]]:
    """Extract ``([(record_index, fields)], data_bearing, sources)`` for a single
    run (``sources`` = record_index -> originating list key, see ``_coerce_records``).

    ``record_index`` is the coercion's stored-slot / running index (GET↔DELETE
    coherent) and survives the cleaning below: a record that cleans to empty is
    dropped WITHOUT renumbering its siblings (so the sources map stays valid).
    When extracted_data is absent (or meta-only — never when explicit-empty,
    which IS a real empty dataset), falls back to declared fields surfaced at
    result_data's top level.
    """
    if not isinstance(result_data, dict):
        return [], False, {}

    records, bearing, sources = _coerce_records(result_data.get("extracted_data"))

    if not records and not bearing:
        # Fallback: data surfaced at the top level of result_data rather than under
        # extracted_data. Drop reserved/meta keys, then run the SAME unwrap so a
        # top-level data list (e.g. {"posts": [...]}) becomes one row per item.
        top = {
            k: v
            for k, v in result_data.items()
            if not _is_meta_key(k) and k not in _RESERVED_TOPLEVEL
        }
        if top:
            records, bearing, sources = _coerce_records(top)

    # Final clean: strip metadata, then — when the workflow DECLARES output fields
    # — project each record to ONLY those declared fields. This is the authoritative
    # definition of "what the workflow extracts": the raw response / internal
    # envelope is never surfaced, only the declared outputs. Drop empties.
    allow = declared_columns(declared)
    cleaned: list[tuple[int, dict]] = []
    for idx, r in records:
        if not isinstance(r, dict):
            continue
        c = _strip_meta(r)
        if allow:
            c = {k: c[k] for k in allow if k in c}
        if c:
            cleaned.append((idx, c))
    return cleaned, bearing, sources


def record_count(result_data: Optional[dict], declared: Optional[list[str]] = None) -> int:
    """How many extracted-data rows a single run's result would flatten to — 0 for
    an empty ``{}`` / meta-only / no-extracted-data / AI-chat payload. Mirrors the
    desktop daemon's ``data_query::record_count``; used to hide zero-row runs from
    the Data-page picker + the runs feed's "View data" link (redaction never
    changes the row COUNT, so it's counted on the raw value)."""
    records, _bearing, _sources = _records_from_result(result_data, declared or [])
    return len(records)


def _sort_key_for(value: Any) -> tuple:
    """Typed sort key: numbers before strings before missing, each ordered
    naturally. Numeric-looking strings sort as numbers so a column of "12"/"3"
    orders 3 < 12, not lexically."""
    if value is None:
        return (3, "")
    if isinstance(value, bool):
        return (1, float(value))
    if isinstance(value, (int, float)):
        return (1, float(value))
    if isinstance(value, str):
        s = value.strip()
        if s == "":
            return (3, "")
        try:
            return (1, float(s))
        except (ValueError, TypeError):
            return (2, s.lower())
    # dict / list — stable string ordering
    try:
        return (2, json.dumps(value, sort_keys=True, default=str).lower())
    except (TypeError, ValueError):
        return (2, str(value).lower())


def _cell_text(value: Any) -> str:
    """Flatten a cell value to searchable / CSV text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _row_value(row: dict, key: str) -> Any:
    """Resolve a sort/search value for a column key, covering the run-meta
    pseudo-columns, the extracted fields, the run-input columns addressed as
    ``input.<name>``, and (on lineage-bearing rows) the ``_lineage`` values a
    lens view may sort by. A data column of the same name wins over lineage."""
    if key in ("run_at", "status", "duration_ms", "run_id", "success"):
        return row.get(key)
    if key.startswith(INPUT_PREFIX):
        return (row.get("inputs") or {}).get(key[len(INPUT_PREFIX):])
    fields = row.get("fields") or {}
    if key in fields:
        return fields[key]
    if key in ("uid", "change", "first_seen_at", "last_seen_at", "versions"):
        lin = row.get("_lineage")
        if isinstance(lin, dict):
            return lin.get(key)
    return fields.get(key)


def _row_matches(row: dict, needle: str) -> bool:
    fields = row.get("fields") or {}
    for v in fields.values():
        if needle in _cell_text(v).lower():
            return True
    # Run inputs are searchable too, so q can narrow by an input value.
    for v in (row.get("inputs") or {}).values():
        if needle in _cell_text(v).lower():
            return True
    # Also match the run status / id so "failed" or a run number narrows rows.
    if needle in str(row.get("status") or "").lower():
        return True
    if needle in str(row.get("run_id") or "").lower():
        return True
    return False


def declared_columns(declared: Optional[list[str]]) -> list[str]:
    """De-dupe declared field names, preserving order."""
    out: list[str] = []
    for f in declared or []:
        if isinstance(f, str) and f and f not in out:
            out.append(f)
    return out


# ---------------------------------------------------------------------------
# Type inference + facets — so the UI can offer smart, data-aware filters
# (numeric range for number columns, value pick-lists for low-cardinality
# columns, true/false for booleans, contains for free text) instead of a single
# generic text box.
# ---------------------------------------------------------------------------

def _is_empty(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == ""
    if isinstance(v, (list, dict, set, tuple)):
        return len(v) == 0
    return False


def _to_number(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except (ValueError, TypeError):
            return None
    return None


def _is_numeric_str(s: str) -> bool:
    return _to_number(s) is not None


def _is_bool_str(s: str) -> bool:
    return s.strip().lower() in ("true", "false", "yes", "no")


def _is_url_str(s: str) -> bool:
    t = s.strip()
    return t.startswith("http://") or t.startswith("https://")


def _is_date_str(s: str) -> bool:
    import re as _re
    # ISO-ish date or datetime: 2026-06-21 [T/space HH:MM...]
    return bool(_re.match(r"^\d{4}-\d{2}-\d{2}([T\s]\d{2}:\d{2})?", s.strip()))


# Columns with at most this many distinct values are offered as a pick-list.
_FACET_MAX_DISTINCT = 40
# Sample at most this many values per column when inferring a type.
_TYPE_SAMPLE = 250


def _infer_type(values: list) -> str:
    """Infer a column's type from its present values: one of
    number | boolean | date | url | json | text."""
    nums = bools = urls = dates = objs = n = 0
    for v in values[:_TYPE_SAMPLE]:
        if _is_empty(v):
            continue
        n += 1
        if isinstance(v, bool):
            bools += 1
        elif isinstance(v, (int, float)):
            nums += 1
        elif isinstance(v, (dict, list)):
            objs += 1
        elif isinstance(v, str):
            s = v.strip()
            if _is_numeric_str(s):
                nums += 1
            elif _is_bool_str(s):
                bools += 1
            elif _is_url_str(s):
                urls += 1
            elif _is_date_str(s):
                dates += 1
    if n == 0:
        return "text"
    f = lambda c: c / n
    if objs and f(objs) >= 0.5:
        return "json"
    if f(bools) >= 0.8:
        return "boolean"
    if f(nums) >= 0.8:
        return "number"
    if f(dates) >= 0.8:
        return "date"
    if f(urls) >= 0.8:
        return "url"
    return "text"


def compute_facets(columns: list[str], rows: list[dict]) -> dict:
    """For each column (data columns + the `status` run-meta column), derive a
    facet the UI uses to render a smart filter: inferred type, non-empty count,
    numeric min/max, and the distinct value set when the column is low
    cardinality. Computed over ALL present rows (pre-filter) so options are
    stable as the user filters."""
    facet_cols = ["status"] + [c for c in columns if c != "status"]
    facets: dict = {}
    for col in facet_cols:
        vals = []
        for r in rows:
            v = _row_value(r, col)
            if not _is_empty(v):
                vals.append(v)
        ftype = "text" if col == "status" else _infer_type(vals)
        facet: dict = {"type": ftype, "non_empty": len(vals)}
        if ftype == "number":
            nums = [x for x in (_to_number(v) for v in vals) if x is not None]
            if nums:
                facet["min"] = min(nums)
                facet["max"] = max(nums)
        # Distinct value pick-list (skip json — not meaningfully enumerable).
        if ftype != "json":
            counts: dict[str, int] = {}
            overflow = False
            for v in vals:
                key = _cell_text(v).strip()
                if key == "":
                    continue
                if key not in counts and len(counts) >= _FACET_MAX_DISTINCT + 1:
                    overflow = True
                    break
                counts[key] = counts.get(key, 0) + 1
            facet["distinct_count"] = len(counts) if not overflow else _FACET_MAX_DISTINCT + 1
            if not overflow and len(counts) <= _FACET_MAX_DISTINCT:
                facet["distinct"] = [
                    {"value": k, "count": c}
                    for k, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
                ]
        facets[col] = facet
    return facets


def _clause_matches(row: dict, clause: dict) -> bool:
    """Apply one structured filter clause to a row. Operators:
    contains | eq | ne | in | between (min/max numeric) | empty | nonempty."""
    col = clause.get("col")
    op = clause.get("op")
    if not col or not op:
        return True
    val = _row_value(row, col)

    if op == "empty":
        return _is_empty(val)
    if op == "nonempty":
        return not _is_empty(val)
    if op == "contains":
        return str(clause.get("value", "")).strip().lower() in _cell_text(val).lower()
    if op in ("eq", "ne"):
        target = _cell_text(clause.get("value")).strip().lower()
        hit = _cell_text(val).strip().lower() == target
        return hit if op == "eq" else not hit
    if op == "in":
        wanted = {_cell_text(x).strip().lower() for x in (clause.get("values") or [])}
        return _cell_text(val).strip().lower() in wanted
    if op == "between":
        n = _to_number(val)
        if n is None:
            return False
        mn = clause.get("min")
        mx = clause.get("max")
        if mn is not None and n < _to_number(mn):
            return False
        if mx is not None and n > _to_number(mx):
            return False
        return True
    return True


def _refine_rows(
    rows: list[dict],
    *,
    q: Optional[str] = None,
    col_filters: Optional[dict[str, str]] = None,
    filters: Optional[list[dict]] = None,
) -> list[dict]:
    """Apply the shared search + filter pipeline (global substring, legacy
    per-column substrings, structured clauses — all AND-combined) to rows.
    Used by every view: the flat grid and the lineage lenses filter identically."""
    if q:
        needle = q.strip().lower()
        if needle:
            rows = [r for r in rows if _row_matches(r, needle)]

    # Per-column substring filters (legacy `filter=col:substr` API), AND-combined.
    if col_filters:
        active = {
            c: v.strip().lower()
            for c, v in col_filters.items()
            if isinstance(v, str) and v.strip()
        }
        for col, sub in active.items():
            rows = [r for r in rows if sub in _cell_text(_row_value(r, col)).lower()]

    # Structured smart-filter clauses, AND-combined.
    if filters:
        for clause in filters:
            if isinstance(clause, dict) and clause.get("col") and clause.get("op"):
                rows = [r for r in rows if _clause_matches(r, clause)]
    return rows


def flatten(
    tasks: list,
    *,
    declared: Optional[list[str]] = None,
    redactor: Optional[Callable[[Optional[dict]], Optional[dict]]] = None,
    include_inputs: bool = False,
    collection: Optional[str] = None,
) -> tuple[list[str], list[dict]]:
    """Flatten tasks into (columns, rows). One row per extracted record (a run
    that extracted a LIST contributes one row per item). Columns are the declared
    output fields first (stable order), then any extra keys runs produced.

    When ``include_inputs`` is set, each run's (secret-redacted) input/form values
    are attached to its rows and exposed as trailing ``input.<name>`` columns, so
    they can be searched / filtered / sorted like any other column.

    When ``collection`` (a dot-path like ``posts.items``) is set, the table is
    PIVOTED to that nested array: each base record's value at the path is expanded
    so one row = one nested item, with columns being the nested items' keys. The
    run provenance (and inputs, when included) is carried onto each nested row. A
    numeric path segment (``posts.items.2``) addresses a single item."""
    declared_cols = declared_columns(declared)
    seen_cols: list[str] = []
    seen_input_cols: list[str] = []
    rows: list[dict] = []
    for task in tasks:
        rd = task.result_data
        if redactor is not None:
            rd = redactor(rd)
        records, _bearing, _sources = _records_from_result(rd, declared_cols)
        if not records:
            continue
        meta = _run_meta(task)
        inputs = redact_inputs(_task_inputs(task)) if include_inputs else None
        if inputs:
            for k in inputs.keys():
                if k not in seen_input_cols:
                    seen_input_cols.append(k)
        # record_index comes from the coercion (stored slot / running index),
        # NOT the emitted position — DELETE refs resolve against it.
        for idx, fields in records:
            for k in fields.keys():
                if k not in seen_cols:
                    seen_cols.append(k)
            row = {**meta, "record_index": idx, "fields": fields}
            if inputs:
                row["inputs"] = inputs
            rows.append(row)

    if collection:
        # Pivot to the nested array/item: one row per nested record, columns from
        # the nested items' keys (declared output fields don't apply to nested data).
        nested_seen: list[str] = []
        nested_rows: list[dict] = []
        for br in rows:
            target = resolve_path(br.get("fields") or {}, collection)
            for nidx, item in enumerate(_records_at(target)):
                fields = item if isinstance(item, dict) else {"value": item}
                for k in fields.keys():
                    if k not in nested_seen:
                        nested_seen.append(k)
                nrow = {
                    "run_id": br.get("run_id"),
                    "run_at": br.get("run_at"),
                    "status": br.get("status"),
                    "success": br.get("success"),
                    "duration_ms": br.get("duration_ms"),
                    "record_index": nidx,
                    "fields": fields,
                }
                if "inputs" in br:
                    nrow["inputs"] = br["inputs"]
                nested_rows.append(nrow)
        columns = list(nested_seen)
        if include_inputs and seen_input_cols:
            columns = columns + [INPUT_PREFIX + c for c in seen_input_cols]
        return columns, nested_rows

    if declared_cols:
        # Strict: surface EXACTLY the declared output fields (in declared order) —
        # records were already projected to these, so no response keys leak in.
        columns = list(declared_cols)
    else:
        columns = list(seen_cols)
    if include_inputs and seen_input_cols:
        columns = columns + [INPUT_PREFIX + c for c in seen_input_cols]
    return columns, rows


def build_table(
    tasks: list,
    *,
    declared: Optional[list[str]] = None,
    q: Optional[str] = None,
    col_filters: Optional[dict[str, str]] = None,
    filters: Optional[list[dict]] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = "desc",
    offset: int = 0,
    limit: Optional[int] = 50,
    redactor: Optional[Callable[[Optional[dict]], Optional[dict]]] = None,
    include_inputs: bool = False,
    collection: Optional[str] = None,
) -> dict:
    """Flatten ``tasks`` into a columns+rows table.

    ``tasks`` should already be the visible, most-recent-first set the caller
    wants scanned. ``declared`` is the workflow's declared output fields (used
    for stable column ordering). ``q`` is a global substring filter across all
    fields; ``col_filters`` maps a column name to a per-column substring filter
    (legacy); ``filters`` is the structured smart-filter clause list (contains /
    eq / ne / in / between / empty / nonempty), AND-combined. ``redactor`` strips
    secrets before flattening. ``include_inputs`` additionally exposes each run's
    input/form values as ``input.<name>`` columns (filterable/sortable). Returns
    ``columns``, ``rows`` (the requested page), ``total`` (rows after filtering),
    and ``declared`` (bool).
    """
    columns, rows = flatten(
        tasks,
        declared=declared,
        redactor=redactor,
        include_inputs=include_inputs,
        collection=collection,
    )

    # Nested array paths reachable from the current scope, so a caller can pivot
    # deeper (records -> posts.items -> …). Computed pre-filter over all rows.
    collections = discover_collections(rows)

    rows = _refine_rows(rows, q=q, col_filters=col_filters, filters=filters)

    total = len(rows)

    # Sort. Default: most recent run first. A valid sort_by is a data column or a
    # run-meta pseudo-column; anything else falls back to run_at.
    valid_sort = set(columns) | {"run_at", "status", "duration_ms", "run_id"}
    key = sort_by if (sort_by in valid_sort) else "run_at"
    reverse = (sort_dir or "desc").lower() != "asc"
    if key == "run_at":
        rows.sort(key=lambda r: (r.get("run_at") or ""), reverse=reverse)
    else:
        rows.sort(key=lambda r: _sort_key_for(_row_value(r, key)), reverse=reverse)

    if limit is None:
        page = rows[offset:]
    else:
        page = rows[offset : offset + limit]

    return {
        "columns": columns,
        # Nested-collection columns are inferred from the items, never declared.
        "declared": bool(declared_columns(declared)) and not collection,
        "collection": collection or None,
        "collections": collections,
        "rows": page,
        "total": total,
    }


# ---------------------------------------------------------------------------
# Record lineage (DATA_REDESIGN_SPEC.md Part 1) — identity across snapshots,
# per-run deltas, change-point histories. Behavior is pinned by the golden
# vectors in shared/data_lineage_golden.json (reference implementation:
# shared/data_lineage_golden_gen.py); do not diverge without regenerating them
# for ALL engines.
# ---------------------------------------------------------------------------

def _norm_number(x: float) -> str:
    """Spec 1.2 number rendering: integral & |x| < 2^53 → integer decimal
    string; else shortest round-trip repr with exponent cleanup (lowercase e,
    no '+', no exponent leading zeros)."""
    if math.isfinite(x) and float(x).is_integer() and abs(x) < 2**53:
        return str(int(x))
    s = repr(float(x))
    if "e" in s or "E" in s:
        mant, _, exp = s.lower().partition("e")
        exp = exp.lstrip("+")
        neg = exp.startswith("-")
        digits = exp[1:] if neg else exp
        digits = digits.lstrip("0") or "0"
        s = mant + "e" + ("-" if neg else "") + digits
    return s


def norm(v: Any) -> str:
    """The ONE normalization used for BOTH uid hashing and change comparison
    (spec 1.2). null / missing / whitespace-only are all "" (no phantom churn);
    numeric-looking strings collapse to their number form so "42" == 42;
    bool-ish STRINGS are not coerced; composites encode via sorted-key JSON with
    every scalar rendered as its norm string."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _norm_number(float(v))
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return ""
        if _NUM_RE.match(s):
            try:
                f = float(s)
                if math.isfinite(f):
                    return _norm_number(f)
            except (ValueError, OverflowError):
                pass
        return s
    return _encode_composite(v)


def _encode_composite(v: Any) -> str:
    """Objects/arrays: JSON with byte-order sorted keys, no whitespace, norm()
    applied recursively to every scalar; arrays order-sensitive."""
    if isinstance(v, dict):
        items = sorted(v.items(), key=lambda kv: kv[0])
        return "{" + ",".join(
            json.dumps(k, ensure_ascii=False) + ":" + _encode_value(val) for k, val in items
        ) + "}"
    if isinstance(v, list):
        return "[" + ",".join(_encode_value(x) for x in v) + "]"
    return _encode_value(v)


def _encode_value(v: Any) -> str:
    """Scalars rendered as JSON strings of their norm form; composites recurse."""
    if isinstance(v, (dict, list)):
        return _encode_composite(v)
    return json.dumps(norm(v), ensure_ascii=False)


def _record_hash(fields: dict) -> str:
    """Whole-record content hash — orders same-run duplicate-identity groups so
    exact-content duplicates pair as "same" across runs regardless of position."""
    return hashlib.sha256(_encode_composite(fields).encode("utf-8")).hexdigest()


def uid_digest_input(identity_fields: list, fields: dict, ordinal: int = 0,
                     singleton: bool = False) -> str:
    """The exact byte string record_uid hashes (spec 1.3): field/norm(value)
    pairs joined with \\x1F / \\x1E in identity order; duplicate group members
    (2nd onward) append "\\x1E#<ordinal>"; singleton mode hashes the literal
    "__singleton__"."""
    if singleton:
        base = "__singleton__"
    else:
        base = "\x1e".join(f + "\x1f" + norm(fields.get(f)) for f in identity_fields)
    if ordinal >= 1:
        base += "\x1e#" + str(ordinal)
    return base


def record_uid(identity_fields: list, fields: dict, ordinal: int = 0,
               singleton: bool = False) -> str:
    """First 16 hex chars of SHA-256 over the uid digest input."""
    return hashlib.sha256(
        uid_digest_input(identity_fields, fields, ordinal, singleton).encode("utf-8")
    ).hexdigest()[:16]


# Identity ladder tiers (spec 1.3): first tier with a qualifying column wins;
# within a tier the leftmost column in canonical order wins. None = the
# suffix tier (_id / _url).
_IDENTITY_TIERS = [
    {"id", "uid", "uuid", "key"},
    {"url", "link", "href", "permalink"},
    {"sku", "slug", "email", "handle"},
    None,
    {"name", "title"},
]

# Auto-identity scores over at most this many newest chain runs (spec 1.6
# performance bound).
_IDENTITY_SAMPLE_RUNS = 50


def _auto_identity(runs_records: list[list[dict]], columns_order: list[str]) -> Optional[str]:
    """The auto-picked identity column, or None. A column qualifies iff
    non-empty in >=90% of sampled records AND mean per-run uniqueness ratio
    >=0.95 (runs with <2 non-empty values count as 1.0)."""
    all_records = [r for run in runs_records for r in run]
    if not all_records:
        return None

    def qualifies(col: str) -> bool:
        non_empty = sum(1 for r in all_records if norm(r.get(col)) != "")
        if non_empty * 10 < len(all_records) * 9:
            return False
        ratios = []
        for run in runs_records:
            vals = [norm(r.get(col)) for r in run]
            vals = [v for v in vals if v != ""]
            if len(vals) < 2:
                ratios.append(1.0)
            else:
                ratios.append(len(set(vals)) / len(vals))
        return (sum(ratios) / len(ratios)) >= 0.95 if ratios else False

    for tier in _IDENTITY_TIERS:
        if tier is None:
            cands = [c for c in columns_order if c.endswith("_id") or c.endswith("_url")]
        else:
            cands = [c for c in columns_order if c in tier]
        for c in cands:  # leftmost in canonical order
            if qualifies(c):
                return c
    return None


def _choose_identity(
    runs_records: list[list[dict]], columns_order: list[str], explicit_key: Optional[str] = None
) -> tuple[str, list[str], Optional[dict]]:
    """The identity ladder (spec 1.3): explicit key (rejected + fall back to
    auto when non-empty in <50% of records) → singleton (>=95% of record-bearing
    runs have exactly 1 record) → auto column → whole-record hash. Returns
    (mode, fields, rejection-info-or-None)."""
    all_records = [r for run in runs_records for r in run]
    rejection: Optional[dict] = None
    if explicit_key:
        fields = [f.strip() for f in explicit_key.split(",") if f.strip()]
        if fields and all_records:
            cov = sum(
                1 for r in all_records if any(norm(r.get(f)) != "" for f in fields)
            ) / len(all_records)
            if cov >= 0.5:
                return "explicit", fields, None
            rejection = {"requested_key_rejected": fields, "coverage": round(cov, 4)}
        elif fields:
            rejection = {"requested_key_rejected": fields, "coverage": 0.0}
    bearing = [run for run in runs_records if run]
    if bearing and sum(1 for run in bearing if len(run) == 1) * 100 >= len(bearing) * 95:
        return "singleton", [], rejection
    f = _auto_identity(runs_records, columns_order)
    if f:
        return "auto", [f], rejection
    return "hash", list(columns_order), rejection


def _assign_uids(
    records: list[tuple[int, dict]], mode: str, fields: list[str]
) -> list[tuple[str, int, dict]]:
    """One run's [(record_index, fields)] → [(uid, record_index, fields)] in
    record order. Duplicate-identity groups get content-first ordinals (spec
    1.3 — best-effort duplicate lineage). Singleton mode keeps only the
    record_index-lowest record."""
    if mode == "singleton":
        if not records:
            return []
        idx, rec = records[0]
        return [(record_uid([], rec, singleton=True), idx, rec)]
    groups: dict[str, list] = {}
    for idx, rec in records:
        groups.setdefault(uid_digest_input(fields, rec), []).append((idx, rec))
    out: list[tuple[str, int, dict]] = []
    for members in groups.values():
        members_sorted = sorted(members, key=lambda m: (_record_hash(m[1]), m[0]))
        for ordinal, (idx, rec) in enumerate(members_sorted):
            out.append((record_uid(fields, rec, ordinal), idx, rec))
    out.sort(key=lambda t: t[1])
    return out


def _diff_fields(old: dict, new: dict, max_depth: int = 3,
                 collapse_at: int = 20) -> tuple[list[str], int]:
    """Leaf dot-paths where norm() differs (spec 1.4): recursive to depth 3,
    >collapse_at leaves under one top-level key collapse to that key alone.
    Returns (paths, total leaf count before collapse)."""
    leaves: list[str] = []

    def walk(o: Any, n: Any, path: list[str], depth: int) -> None:
        if isinstance(o, dict) and isinstance(n, dict) and depth < max_depth:
            for k in sorted(set(o) | set(n)):
                walk(o.get(k), n.get(k), path + [k], depth + 1)
            return
        ov = _encode_value(o) if isinstance(o, (dict, list)) else norm(o)
        nv = _encode_value(n) if isinstance(n, (dict, list)) else norm(n)
        if ov != nv:
            leaves.append(".".join(path))

    for k in sorted(set(old) | set(new)):
        walk(old.get(k), new.get(k), [k], 1)
    leaf_count = len(leaves)
    by_top: dict[str, list[str]] = {}
    for p in leaves:
        by_top.setdefault(p.split(".")[0], []).append(p)
    paths: list[str] = []
    for top in sorted(by_top):
        ps = by_top[top]
        if len(ps) > collapse_at:
            paths.append(top)
        else:
            paths.extend(ps)
    return paths, leaf_count


def run_entries(tasks: list, *, declared: Optional[list[str]] = None,
                redactor: Optional[Callable[[Optional[dict]], Optional[dict]]] = None) -> list[dict]:
    """Per-run entries (run meta + coerced records + data_bearing + the
    record_index->source-list-key map), in the order given. The lineage/lens
    builders and the picker all start here so the coercion happens exactly once
    per task."""
    declared_cols = declared_columns(declared)
    entries: list[dict] = []
    for task in tasks:
        rd = task.result_data
        if redactor is not None:
            rd = redactor(rd)
        records, bearing, sources = _records_from_result(rd, declared_cols)
        entries.append({**_run_meta(task), "records": records, "data_bearing": bearing,
                        "sources": sources})
    return entries


def _is_chain_member(entry: dict) -> bool:
    """Snapshot-chain membership (spec 1.4): the run succeeded (success is not
    False, status not failed/error) AND is data-bearing (>=1 record, or an
    explicit-empty dataset). Meta-only runs never advance the chain."""
    return (
        entry["success"] is not False
        and entry["status"] not in ("failed", "error")
        and entry["data_bearing"]
    )


def _chain_sort_key(entry: dict) -> tuple:
    # ascending (coalesce(completed_at, created_at), run_id) — run_id tie-break.
    return (entry["run_at"] or "", entry["run_id"])


def canonical_columns(entries: list[dict], declared: Optional[list[str]] = None) -> list[str]:
    """Canonical column order for identity scoring: declared fields first, then
    first-seen across runs in ASCENDING chain order (the reference-impl order —
    the auto-identity tie-break and hash-mode field order depend on it).
    ``entries`` must already be sorted ascending."""
    seen = list(declared_columns(declared))
    for e in entries:
        for _idx, rec in e["records"]:
            for k in rec:
                if k not in seen:
                    seen.append(k)
    return seen


_CHANGE_RANK = {"new": 0, "changed": 1, "same": 2, "missing": 3}


def compute_lineage(chain: list[dict], mode: str, fields: list[str]) -> dict:
    """One O(rows) oldest→newest walk over the snapshot chain. Returns the
    golden-pinned views — ``runs_index`` (newest first), ``latest``
    (default-sorted: change rank, last_seen desc, uid asc), ``histories``
    (change-point versions only) — plus the per-run annotations (``run_views``)
    and uid ``appearances`` the run lens and DELETE-by-uid need."""
    last_version: dict[str, dict] = {}
    first_seen: dict[str, Any] = {}
    last_seen: dict[str, Any] = {}
    change_points: dict[str, list] = {}
    appearances: dict[str, list] = {}
    latest_status: dict[str, tuple] = {}
    runs_index: list[dict] = []
    run_views: dict = {}
    prev_map: Optional[dict] = None  # uid -> that uid's fields in the PREVIOUS snapshot
    prev_run_id = None
    for ri, run in enumerate(chain):
        uids_here = _assign_uids(run["records"], mode, fields)
        cur: dict[str, dict] = {}
        delta = {"new": 0, "changed": 0, "removed": 0, "unchanged": 0}
        annotated: list[dict] = []
        for uid, idx, rec in uids_here:
            cur[uid] = rec
            if uid not in first_seen:
                first_seen[uid] = run["run_at"]
                delta["new"] += 1
                latest_status[uid] = ("new", [], 0)
                change_points.setdefault(uid, []).append({
                    "run_id": run["run_id"], "run_at": run["run_at"], "record_index": idx,
                    "fields": rec, "changed_fields": [], "changed_leaf_count": 0})
            else:
                paths, leafc = _diff_fields(last_version[uid]["fields"], rec)
                if paths:
                    delta["changed"] += 1
                    latest_status[uid] = ("changed", paths, leafc)
                    change_points.setdefault(uid, []).append({
                        "run_id": run["run_id"], "run_at": run["run_at"], "record_index": idx,
                        "fields": rec, "changed_fields": paths, "changed_leaf_count": leafc})
                else:
                    delta["unchanged"] += 1
                    latest_status[uid] = ("same", [], 0)
            last_seen[uid] = run["run_at"]
            last_version[uid] = {"fields": rec, "run_id": run["run_id"],
                                 "run_at": run["run_at"], "record_index": idx}
            appearances.setdefault(uid, []).append((run["run_id"], idx))
            change, paths, leafc = latest_status[uid]
            annotated.append({
                "uid": uid, "change": change, "changed_fields": paths,
                "changed_leaf_count": leafc, "first_seen_at": first_seen[uid],
                "last_seen_at": run["run_at"], "versions": len(change_points[uid]),
                "record_index": idx, "fields": rec})
        removed_records: list[dict] = []
        if prev_map is not None:
            for u in prev_map:  # previous snapshot's record order (deterministic)
                if u not in cur:
                    delta["removed"] += 1
                    removed_records.append({"uid": u, "fields": prev_map[u]})
        runs_index.append({
            "run_id": run["run_id"], "run_at": run["run_at"],
            "record_count": len(run["records"]),
            "explicit_empty": len(run["records"]) == 0,
            "delta": None if ri == 0 else delta})
        run_views[run["run_id"]] = {
            "prev_run_id": prev_run_id, "delta": None if ri == 0 else delta,
            "records": annotated, "removed_records": removed_records}
        prev_map = cur
        prev_run_id = run["run_id"]

    newest = set(prev_map or {})
    latest_rows: list[dict] = []
    for uid, lv in last_version.items():
        if uid in newest:
            change, paths, leafc = latest_status[uid]
        else:
            change, paths, leafc = "missing", [], 0
        latest_rows.append({
            "uid": uid, "change": change, "changed_fields": paths, "changed_leaf_count": leafc,
            "first_seen_at": first_seen[uid], "last_seen_at": last_seen[uid],
            "versions": len(change_points[uid]), "fields": lv["fields"],
            "run_id": lv["run_id"], "record_index": lv["record_index"]})
    # default sort: change rank asc, last_seen desc, uid asc — golden-pinned.
    latest_rows.sort(key=lambda r: (_CHANGE_RANK[r["change"]],
                                    tuple(-ord(c) for c in (r["last_seen_at"] or "")),
                                    r["uid"]))
    runs_index.reverse()  # newest first
    return {"runs_index": runs_index, "latest": latest_rows, "histories": change_points,
            "first_seen": first_seen, "last_seen": last_seen,
            "appearances": appearances, "run_views": run_views}


def lineage_bundle(tasks: list, *, declared: Optional[list[str]] = None,
                   redactor: Optional[Callable[[Optional[dict]], Optional[dict]]] = None,
                   key: Optional[str] = None) -> dict:
    """entries → chain → identity → lineage: the shared core of every lens view.
    ``identity`` echoes the ladder result (+ rejection info when an explicit
    ``key`` was refused and auto took over)."""
    entries = run_entries(tasks, declared=declared, redactor=redactor)
    asc = sorted(entries, key=_chain_sort_key)
    chain = [e for e in asc if _is_chain_member(e)]
    columns = canonical_columns(asc, declared)
    sample = [[rec for _idx, rec in e["records"]] for e in chain][-_IDENTITY_SAMPLE_RUNS:]
    mode, fields, rejection = _choose_identity(sample, columns, key)
    identity = {"mode": mode, "fields": fields}
    if rejection:
        identity.update(rejection)
    bundle = compute_lineage(chain, mode, fields)
    bundle["entries"] = entries
    bundle["chain"] = chain
    bundle["identity"] = identity
    bundle["columns_canonical"] = columns
    return bundle


class LineageRunNotFound(LookupError):
    """view=run addressed a run_id that is not a member of the snapshot chain."""


def _lineage_payload(rec: dict) -> dict:
    """The per-row ``_lineage`` API payload (spec 1.5); ``changed_fields`` ships
    only when the record actually changed."""
    lin = {
        "uid": rec["uid"],
        "change": rec["change"],
        "changed_leaf_count": rec["changed_leaf_count"],
        "first_seen_at": rec["first_seen_at"],
        "last_seen_at": rec["last_seen_at"],
        "versions": rec["versions"],
    }
    if rec["change"] == "changed":
        lin["changed_fields"] = rec["changed_fields"]
    return lin


def _sort_lineage_rows(rows: list[dict], columns: list[str],
                       sort_by: Optional[str], sort_dir: str) -> list[dict]:
    """Explicit sort ONLY — the lens default order (latest: change rank /
    run: snapshot record order) is already applied. Beyond the data + run-meta
    columns, sort_by accepts the lineage pseudo-columns."""
    valid = set(columns) | {"run_at", "status", "duration_ms", "run_id",
                            "first_seen_at", "last_seen_at", "versions"}
    if not sort_by or sort_by not in valid:
        return rows
    reverse = (sort_dir or "desc").lower() != "asc"
    return sorted(rows, key=lambda r: _sort_key_for(_row_value(r, sort_by)), reverse=reverse)


def build_lens_table(
    tasks: list,
    *,
    view: str,
    run_id: Optional[int] = None,
    declared: Optional[list[str]] = None,
    redactor: Optional[Callable[[Optional[dict]], Optional[dict]]] = None,
    key: Optional[str] = None,
    include_missing: bool = False,
    source: Optional[str] = None,
    q: Optional[str] = None,
    col_filters: Optional[dict[str, str]] = None,
    filters: Optional[list[dict]] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = "desc",
    offset: int = 0,
    limit: Optional[int] = 50,
) -> dict:
    """The lineage lenses over one workflow's runs (spec 1.5(1)):

    - ``view="latest"``: one row per unique record (its newest version), rows
      carry ``_lineage``; adds ``identity`` + ``counts`` (post-dedup,
      pre-search/filter). ``missing`` records (absent from the newest snapshot)
      are excluded unless ``include_missing``.
    - ``view="run"``: one snapshot's records annotated vs the previous chain
      member; adds ``identity``, ``delta``, ``removed_records``, ``prev_run_id``.
      Raises LineageRunNotFound when run_id is not a chain member.

    Both views also expose record SOURCES (spec 3.1): each row's
    ``_lineage.source`` is the originating list key of a multi-list expansion
    (null when untagged), the response carries a ``sources: {key|"": count}``
    envelope (computed over the lens rowset BEFORE the source filter + search;
    "" buckets untagged records), and ``source`` filters the rowset to one list
    key ("" = untagged) before search/filters/pagination.

    Search/filters/pagination apply AFTER dedup; explicit sort_by overrides the
    lens default order. Response keys mirror build_table's so the grid renders
    either interchangeably."""
    bundle = lineage_bundle(tasks, declared=declared, redactor=redactor, key=key)
    declared_cols = declared_columns(declared)
    columns = list(declared_cols) if declared_cols else list(bundle["columns_canonical"])
    meta_by_run = {e["run_id"]: e for e in bundle["entries"]}

    def api_row(rec: dict, rid, extra_lineage: Optional[dict] = None) -> dict:
        meta = meta_by_run[rid]
        lin = _lineage_payload(rec)
        # The originating list key of a multi-list expansion (spec 3.1) — looked
        # up on the record's OWN run so the golden-pinned lineage structures
        # (latest/histories) stay source-free; null for untagged shapes.
        lin["source"] = (meta.get("sources") or {}).get(rec["record_index"])
        if extra_lineage:
            lin.update(extra_lineage)
        return {
            "run_id": rid,
            "run_at": meta["run_at"],
            "status": meta["status"],
            "success": meta["success"],
            "duration_ms": meta["duration_ms"],
            "record_index": rec["record_index"],
            "fields": rec["fields"],
            "_lineage": lin,
        }

    out: dict = {
        "columns": columns,
        "declared": bool(declared_cols),
        "collection": None,
        "identity": bundle["identity"],
    }

    if view == "run":
        rv = bundle["run_views"].get(run_id)
        if rv is None:
            raise LineageRunNotFound(run_id)
        rows = [api_row(rec, run_id, {"prev_run_id": rv["prev_run_id"]}) for rec in rv["records"]]
        out["delta"] = rv["delta"]
        out["removed_records"] = rv["removed_records"]
        out["prev_run_id"] = rv["prev_run_id"]
    else:  # latest
        rows = [api_row(rec, rec["run_id"]) for rec in bundle["latest"]]
        counts = {"new": 0, "changed": 0, "same": 0, "missing": 0}
        for r in rows:
            counts[r["_lineage"]["change"]] += 1
        out["counts"] = counts
        if not include_missing:
            rows = [r for r in rows if r["_lineage"]["change"] != "missing"]

    # Source buckets over the lens rowset — post-dedup (latest, after the
    # missing filter) / per-snapshot (run), BEFORE the source filter + search so
    # chip counts stay stable while one source is selected. "" = untagged.
    sources: dict[str, int] = {}
    for r in rows:
        bucket = r["_lineage"].get("source") or ""
        sources[bucket] = sources.get(bucket, 0) + 1
    out["sources"] = sources
    if source is not None:
        rows = [r for r in rows if (r["_lineage"].get("source") or "") == source]

    out["collections"] = discover_collections(rows)
    rows = _refine_rows(rows, q=q, col_filters=col_filters, filters=filters)
    out["total"] = len(rows)
    rows = _sort_lineage_rows(rows, columns, sort_by, sort_dir)
    out["rows"] = rows[offset:] if limit is None else rows[offset:offset + limit]
    return out


def build_runs_index(tasks: list, *, declared: Optional[list[str]] = None,
                     redactor: Optional[Callable[[Optional[dict]], Optional[dict]]] = None,
                     key: Optional[str] = None) -> dict:
    """The /data/runs snapshot index (spec 1.5(2)): chain members newest first,
    each with record_count / explicit_empty / vs-previous delta (null for the
    oldest) + run status, plus the identity echo."""
    bundle = lineage_bundle(tasks, declared=declared, redactor=redactor, key=key)
    status_by_run = {e["run_id"]: e["status"] for e in bundle["entries"]}
    runs = [{**ri, "status": status_by_run.get(ri["run_id"])} for ri in bundle["runs_index"]]
    return {"runs": runs, "identity": bundle["identity"]}


def build_record_history(tasks: list, uid: str, *, declared: Optional[list[str]] = None,
                         redactor: Optional[Callable[[Optional[dict]], Optional[dict]]] = None,
                         key: Optional[str] = None) -> tuple[Optional[dict], dict]:
    """The /history payload for one record uid (spec 1.5(3)): CHANGE-POINT
    versions only, oldest→newest (first entry has changed_fields=[]). Returns
    (payload, identity); payload is None for an unknown uid — the caller 404s
    with the identity so the FE can re-key and refetch."""
    bundle = lineage_bundle(tasks, declared=declared, redactor=redactor, key=key)
    versions = bundle["histories"].get(uid)
    if not versions:
        return None, bundle["identity"]
    return {
        "record_uid": uid,
        "identity": bundle["identity"],
        "first_seen_at": bundle["first_seen"][uid],
        "last_seen_at": bundle["last_seen"][uid],
        "versions": versions,
    }, bundle["identity"]


def resolve_record_uids(tasks: list, uids: list[str], *, declared: Optional[list[str]] = None,
                        redactor: Optional[Callable[[Optional[dict]], Optional[dict]]] = None,
                        key: Optional[str] = None) -> tuple[dict, dict, list, dict]:
    """DELETE record_uids semantics (spec 1.5(6)): a uid deletes ALL its
    appearances in the chain. Returns (by_run: {run_id: [record_index]},
    resolved: {uid: n_versions}, unmatched: [uids], identity). Must be called
    inside the same transaction as the mutation."""
    bundle = lineage_bundle(tasks, declared=declared, redactor=redactor, key=key)
    by_run: dict = {}
    resolved: dict[str, int] = {}
    unmatched: list[str] = []
    for u in uids:
        apps = bundle["appearances"].get(u)
        if not apps:
            unmatched.append(u)
            continue
        resolved[u] = len(apps)
        for rid, idx in apps:
            by_run.setdefault(rid, []).append(idx)
    return by_run, resolved, unmatched, bundle["identity"]


# Above this many flattened rows the picker skips its delta teaser (spec 1.5(7)
# — identity scoring over a huge scan isn't worth a list-page hint).
_PICKER_DELTA_MAX_ROWS = 20000


def picker_last_delta(entries: list[dict], *, declared: Optional[list[str]] = None) -> Optional[dict]:
    """The picker's ``last_delta: {new, changed, removed}`` teaser, computed from
    the NEWEST TWO chain snapshots only (from the entries already flattened for
    the picker — no second scan). None when <2 chain snapshots exist or the
    flattened rows exceed _PICKER_DELTA_MAX_ROWS."""
    if sum(len(e["records"]) for e in entries) > _PICKER_DELTA_MAX_ROWS:
        return None
    asc = sorted(entries, key=_chain_sort_key)
    chain = [e for e in asc if _is_chain_member(e)]
    if len(chain) < 2:
        return None
    columns = canonical_columns(asc, declared)
    sample = [[rec for _idx, rec in e["records"]] for e in chain][-_IDENTITY_SAMPLE_RUNS:]
    mode, fields, _rejection = _choose_identity(sample, columns, None)
    prev = {u: rec for u, _i, rec in _assign_uids(chain[-2]["records"], mode, fields)}
    cur = {u: rec for u, _i, rec in _assign_uids(chain[-1]["records"], mode, fields)}
    return {
        "new": sum(1 for u in cur if u not in prev),
        "changed": sum(1 for u, rec in cur.items() if u in prev and _diff_fields(prev[u], rec)[0]),
        "removed": sum(1 for u in prev if u not in cur),
    }


def pop_extracted_slots(result_data: dict, record_indices: list) -> int:
    """Delete the extracted rows that GET row ``record_index`` values address
    from ONE run's ``result_data``, IN PLACE. The index→stored-slot mapping
    mirrors ``_coerce_records`` exactly (GET↔DELETE coherence):

    - extracted_data is a LIST: record_index IS the stored slot. A coerced
      table's header lives at slot 0 and is never addressable; if only the
      header remains after popping, extracted_data is nulled (a lone header row
      is no data).
    - dict with >=1 non-empty list key: record_index is the global running
      index across every non-empty list in sorted key order — mapped back to a
      (key, stored-slot) per list, so popping NEVER destroys a sibling dataset.
    - dict with no list keys (single record) / scalar: any index clears
      extracted_data entirely.

    Returns the number of rows removed."""
    if not isinstance(result_data, dict) or "extracted_data" not in result_data:
        return 0
    ext = result_data.get("extracted_data")
    if ext is None:
        return 0
    idxs = sorted({int(i) for i in record_indices})
    if not idxs:
        return 0

    if isinstance(ext, list):
        header = _detect_table(ext)
        low = 1 if header else 0
        slots = [i for i in idxs if low <= i < len(ext)]
        if not slots:
            return 0
        for i in reversed(slots):
            del ext[i]
        if not ext or (header and len(ext) == 1):
            result_data["extracted_data"] = None
        return len(slots)

    if isinstance(ext, dict):
        non_meta = {k: v for k, v in ext.items() if not _is_meta_key(k)}
        list_keys = sorted(k for k, v in non_meta.items() if isinstance(v, list))
        nonempty = [k for k in list_keys if ext[k]]
        if nonempty:
            # Per-list offset arithmetic over the SAME expansion order the
            # flatten used; never clear-all when list keys exist.
            removed = 0
            emitted = [(k, _coerce_list(ext[k])) for k in nonempty]
            slots_by_key: dict[str, set] = {}
            for gi in idxs:
                offset = 0
                for k, recs in emitted:
                    if gi < offset + len(recs):
                        slots_by_key.setdefault(k, set()).add(recs[gi - offset][0])
                        removed += 1
                        break
                    offset += len(recs)
            for k, slots in slots_by_key.items():
                lst = ext[k]
                header = _detect_table(lst)
                for i in sorted(slots, reverse=True):
                    if 0 <= i < len(lst):
                        del lst[i]
                if header and len(lst) == 1:
                    ext[k] = []  # only the header row remains → dataset emptied
            # If nothing meaningful remains, drop the whole envelope.
            if not any(v for kk, v in ext.items() if not _is_meta_key(kk)):
                result_data["extracted_data"] = None
            return removed
        if list_keys:
            # explicit-empty wrapper — flattens to 0 records; nothing to delete.
            return 0
        # single-record dict — any index clears the run's extracted_data.
        result_data["extracted_data"] = None
        return 1

    # scalar extracted_data — treated as one opaque record.
    result_data["extracted_data"] = None
    return 1


_LINEAGE_CSV_COLUMNS = ["record_uid", "change", "changed_fields",
                        "first_seen", "last_seen", "versions"]

# Leading characters that spreadsheet apps (Excel, Google Sheets, LibreOffice)
# interpret as the start of a formula when a cell is opened/imported as CSV.
_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _csv_shield(value: Any) -> Any:
    """Neutralize CSV / formula injection (OWASP CSV Injection) for a single
    cell at the CSV-serialization boundary. If the cell's text form begins
    with a formula-triggering character (``=``, ``+``, ``-``, ``@``) or a
    tab/CR, prefix it with a single quote so spreadsheet apps render it as
    literal text instead of evaluating it as a formula. Only affects the CSV
    output — the underlying row data (used for JSON export, search, sort,
    etc.) is left untouched."""
    if value is None:
        return value
    s = value if isinstance(value, str) else str(value)
    if s[:1] in _CSV_FORMULA_TRIGGERS:
        return "'" + s
    return value


def to_csv(columns: list[str], rows: list[dict], *, lineage: bool = False) -> str:
    """Serialize the flattened rows to CSV. Run metadata leads each row, then —
    for the lineage lenses — the ``_lineage`` columns (spec 1.5(5)), then the
    data columns in order. Non-scalar cell values are JSON-encoded. Every cell
    is passed through ``_csv_shield`` immediately before ``writerow`` to
    neutralize spreadsheet formula injection from scraped content."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    header = ["run_id", "run_at", "status"]
    if lineage:
        header += _LINEAGE_CSV_COLUMNS
    header += list(columns)
    writer.writerow([_csv_shield(h) for h in header])
    for r in rows:
        line = [
            r.get("run_id"),
            r.get("run_at") or "",
            r.get("status") or "",
        ]
        if lineage:
            lin = r.get("_lineage") or {}
            line += [
                lin.get("uid") or "",
                lin.get("change") or "",
                ",".join(lin.get("changed_fields") or []),
                lin.get("first_seen_at") or "",
                lin.get("last_seen_at") or "",
                lin.get("versions") if lin.get("versions") is not None else "",
            ]
        for c in columns:
            line.append(_cell_text(_row_value(r, c)))
        writer.writerow([_csv_shield(v) for v in line])
    return buf.getvalue()


def to_json_records(columns: list[str], rows: list[dict], *, lineage: bool = False) -> list[dict]:
    """Serialize rows to flat JSON records (run metadata + fields) for export.
    Lineage-lens rows additionally carry their ``_lineage`` payload."""
    out: list[dict] = []
    for r in rows:
        rec = {
            "run_id": r.get("run_id"),
            "run_at": r.get("run_at"),
            "status": r.get("status"),
        }
        for c in columns:
            rec[c] = _row_value(r, c)
        if lineage and "_lineage" in r:
            rec["_lineage"] = r["_lineage"]
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Markdown / HTML output (`?format=markdown|html`)
#
# CONTENT-AWARE: a dataset whose records carry a long-form content column (a
# Dragnet crawl page has `markdown`) renders as DOCUMENTS — one section per
# record, heading + source link + the content itself. Anything else (structured
# scraper output) renders as a TABLE, mirroring the CSV/JSON columns exactly.
# One rule, so `format=markdown` "does the right thing" for both shapes.
#
# SECURITY: `format=html` echoes SCRAPED THIRD-PARTY content back as text/html
# from our own origin, so it is a stored-XSS surface. Two defenses live here (the
# routers add the transport ones — nosniff + a `default-src 'none'` CSP):
#   1. markdown-it runs with html=False → raw HTML in the source markdown is
#      ESCAPED, never passed through.
#   2. `_safe_link` allowlists link/image URL schemes (http/https/mailto and
#      data:image), so `[x](javascript:…)` cannot survive into an href.
# ---------------------------------------------------------------------------

#: Columns that may hold long-form document content, in preference order.
_CONTENT_FIELDS = ("markdown", "content", "text", "body")
#: Columns to title a document with, in preference order.
_TITLE_FIELDS = ("title", "name", "heading", "url")
#: Columns naming a document's source, in preference order.
_URL_FIELDS = ("url", "link", "source_url", "source")
#: A content column must carry at least this many chars to count as long-form.
_CONTENT_MIN_CHARS = 200
#: URL schemes permitted in rendered links/images.
_SAFE_URL_SCHEMES = ("http://", "https://", "mailto:", "data:image/")


def search_results_to_table(payload: dict) -> tuple[list[str], list[dict]]:
    """Re-shape a ``dataset_search.search_records`` payload into the
    ``(columns, rows)`` the renderers consume.

    Lives here (not in the router) because BOTH the Datasets API's ``?format=``
    and the AI concierge's answer-from-datasets fold need it — the concierge
    renders the matches to Markdown rather than JSON-dumping them, since a crawl
    page's markdown escaped inside JSON costs far more tokens than the prose.
    Columns are the union of result field keys, in first-seen order.
    """
    columns: list[str] = []
    rows: list[dict] = []
    for r in payload.get("results") or []:
        fields = r.get("fields") or {}
        for k in fields:
            if k not in columns:
                columns.append(k)
        rows.append({
            "run_id": r.get("run_id"),
            "run_at": r.get("run_at"),
            "status": r.get("status") or "success",
            "record_index": r.get("record_index"),
            "fields": fields,
        })
    return columns, rows


def content_column(columns: list[str], rows: list[dict]) -> Optional[str]:
    """The column holding long-form content, or None when this dataset is not
    document-shaped.

    Requires that at least half the rows carry a genuinely long string there, so
    a short incidental `text`/`content` column on a structured dataset does NOT
    flip the whole dataset into document mode.
    """
    if not rows:
        return None
    for cand in _CONTENT_FIELDS:
        if cand not in columns:
            continue
        long_enough = sum(
            1
            for r in rows
            if isinstance(_row_value(r, cand), str)
            and len(_row_value(r, cand)) >= _CONTENT_MIN_CHARS
        )
        if long_enough * 2 >= len(rows):
            return cand
    return None


def _first_present(row: dict, keys: tuple[str, ...], skip: Optional[str] = None) -> Optional[str]:
    """The first non-empty scalar value among `keys` on this row."""
    for k in keys:
        if k == skip:
            continue
        v = _row_value(row, k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _safe_link(url: str) -> Optional[str]:
    """A link/image URL, or None when its scheme is not allowlisted. Blocks
    `javascript:` / `vbscript:` / `file:` / bare `data:` payloads."""
    u = (url or "").strip()
    if not u:
        return None
    low = u.lower()
    # A scheme-relative or relative URL carries no scheme to abuse.
    if low.startswith("//") or low.startswith("/") or low.startswith("#"):
        return u
    if "://" not in low and ":" not in low.split("/")[0]:
        return u  # relative path
    return u if low.startswith(_SAFE_URL_SCHEMES) else None


def _md_to_html(text: str) -> str:
    """Render CommonMark to HTML with raw HTML escaped and unsafe link schemes
    dropped. Mirrors the daemon's `pulldown-cmark` renderer."""
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": False, "linkify": False})
    # markdown-it already blocks javascript:/vbscript:/file:; tighten to an
    # explicit allowlist so anything exotic is dropped too.
    md.validateLink = lambda url: _safe_link(url) is not None  # type: ignore[assignment]
    return md.render(text or "")


def _md_escape_cell(value: Any) -> str:
    """A table cell that cannot break the Markdown table grid: pipes escaped,
    newlines collapsed."""
    return _cell_text(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


#: A leading YAML front-matter block: `---` on its own first line, then keys, then
#: a closing `---` line. Anchored to the very start so a mid-document thematic
#: break is never mistaken for it.
_FRONT_MATTER_RE = re.compile(r"\A﻿?[ \t]*---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def _strip_front_matter(text: str) -> str:
    """Drop a leading YAML front-matter block from document content.

    A crawl stores each page's markdown WITH front matter (`title`, `url`,
    `published`, …). The document renderers already hoist title + url into the
    heading and source link, so leaving the block in would duplicate them — and in
    HTML it renders as a stray `<hr>` followed by a junk `title: "…"` heading
    rather than as metadata. Strip it and let the heading speak.
    """
    if not text:
        return text
    return _FRONT_MATTER_RE.sub("", text, count=1).lstrip("\r\n")


def _doc_parts(row: dict, ccol: str) -> tuple[str, Optional[str], str]:
    """(title, url, content) for one document-shaped row."""
    title = _first_present(row, _TITLE_FIELDS, skip=ccol) or f"Record {row.get('record_index', '')}".strip()
    url = _first_present(row, _URL_FIELDS, skip=ccol)
    content = _row_value(row, ccol)
    content = content if isinstance(content, str) else _cell_text(content)
    return title, url, _strip_front_matter(content)


def to_markdown(
    columns: list[str],
    rows: list[dict],
    *,
    lineage: bool = False,
    title: Optional[str] = None,
) -> str:
    """Serialize rows to Markdown — documents when the dataset is document-shaped
    (see `content_column`), otherwise a Markdown table."""
    out: list[str] = []
    if title:
        out.append(f"# {title}\n")
    ccol = content_column(columns, rows)

    if ccol:
        for r in rows:
            doc_title, url, content = _doc_parts(r, ccol)
            out.append(f"## {doc_title}")
            if url:
                out.append(f"\n<{url}>")
            out.append(f"\n{content.strip()}\n")
            out.append("\n---\n")
        return "\n".join(out).rstrip("\n-\n ") + "\n"

    # Table shape — same columns as CSV/JSON so the formats stay comparable.
    header = ["run_id", "run_at", "status"]
    if lineage:
        header += _LINEAGE_CSV_COLUMNS
    header += list(columns)
    out.append("| " + " | ".join(header) + " |")
    out.append("| " + " | ".join("---" for _ in header) + " |")
    for r in rows:
        line = [
            _md_escape_cell(r.get("run_id")),
            _md_escape_cell(r.get("run_at") or ""),
            _md_escape_cell(r.get("status") or ""),
        ]
        if lineage:
            lin = r.get("_lineage") or {}
            line += [
                _md_escape_cell(lin.get("uid") or ""),
                _md_escape_cell(lin.get("change") or ""),
                _md_escape_cell(",".join(lin.get("changed_fields") or [])),
                _md_escape_cell(lin.get("first_seen_at") or ""),
                _md_escape_cell(lin.get("last_seen_at") or ""),
                _md_escape_cell(lin.get("versions") if lin.get("versions") is not None else ""),
            ]
        line += [_md_escape_cell(_row_value(r, c)) for c in columns]
        out.append("| " + " | ".join(line) + " |")
    return "\n".join(out) + "\n"


_HTML_CSS = (
    "body{font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
    "max-width:52rem;margin:2rem auto;padding:0 1rem;color:#18181b}"
    "table{border-collapse:collapse;width:100%;font-size:13px}"
    "th,td{border:1px solid #e4e4e7;padding:6px 8px;text-align:left;vertical-align:top}"
    "th{background:#fafafa}"
    "article{margin:0 0 2rem;padding:0 0 2rem;border-bottom:1px solid #e4e4e7}"
    "img{max-width:100%}pre{overflow-x:auto;background:#fafafa;padding:.75rem;border-radius:6px}"
    "@media(prefers-color-scheme:dark){body{background:#09090b;color:#e4e4e7}"
    "th,td,article{border-color:#27272a}th,pre{background:#18181b}}"
)


def to_html(
    columns: list[str],
    rows: list[dict],
    *,
    lineage: bool = False,
    title: Optional[str] = None,
) -> str:
    """Serialize rows to a standalone HTML document — rendered documents when the
    dataset is document-shaped, otherwise a table. All values are escaped; see the
    module-section note for the XSS posture."""
    esc = html_lib.escape
    doc_title = esc(title or "Dataset")
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{doc_title}</title><style>{_HTML_CSS}</style></head><body>",
    ]
    if title:
        parts.append(f"<h1>{doc_title}</h1>")
    ccol = content_column(columns, rows)

    if ccol:
        for r in rows:
            t, url, content = _doc_parts(r, ccol)
            parts.append("<article>")
            parts.append(f"<h2>{esc(t)}</h2>")
            safe = _safe_link(url) if url else None
            if safe:
                parts.append(f'<p><a href="{esc(safe)}" rel="noreferrer nofollow">{esc(url or "")}</a></p>')
            elif url:
                parts.append(f"<p>{esc(url)}</p>")
            parts.append(_md_to_html(content))
            parts.append("</article>")
        parts.append("</body></html>")
        return "\n".join(parts)

    header = ["run_id", "run_at", "status"]
    if lineage:
        header += _LINEAGE_CSV_COLUMNS
    header += list(columns)
    parts.append("<table><thead><tr>")
    parts += [f"<th>{esc(h)}</th>" for h in header]
    parts.append("</tr></thead><tbody>")
    for r in rows:
        cells = [
            _cell_text(r.get("run_id")),
            _cell_text(r.get("run_at") or ""),
            _cell_text(r.get("status") or ""),
        ]
        if lineage:
            lin = r.get("_lineage") or {}
            cells += [
                _cell_text(lin.get("uid") or ""),
                _cell_text(lin.get("change") or ""),
                ",".join(lin.get("changed_fields") or []),
                _cell_text(lin.get("first_seen_at") or ""),
                _cell_text(lin.get("last_seen_at") or ""),
                _cell_text(lin.get("versions") if lin.get("versions") is not None else ""),
            ]
        cells += [_cell_text(_row_value(r, c)) for c in columns]
        parts.append("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in cells) + "</tr>")
    parts.append("</tbody></table></body></html>")
    return "\n".join(parts)
