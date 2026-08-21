"""Bounded serving for the extracted-data HOT paths.

The extracted-data endpoints (services/extracted_data_table.py) are read-time:
every request used to load the FULL payload of up to _DATA_SCAN_CAP runs from
the database and flatten them in Python, just to answer "the newest 50 rows" or
"which workflows have data". For a crawl dataset (hundreds of pages of captured
markdown per run window) that meant tens of megabytes of JSONB parsed per
request — and the Data page fires several of these concurrently on open, then
polls. This module serves the common requests from bounded reads instead:

  * WINDOW STUBS — one SQL pass over the scan window returning only
    ``(task_id, payload_size, recency)``: no payloads.
  * PER-TASK DIGESTS — ``{row count, column order, nested-collection counts}``
    per run, computed ONCE with the real extracted_data_table code (so the
    numbers can never drift from the flatten) and cached. Cache keys carry the
    payload size and a per-workflow delete epoch, so edits self-invalidate.
  * PAGE COVER — prefix sums over the digests pick the few tasks whose rows
    cover ``[offset, offset+limit)``; only THOSE payloads are fetched and
    flattened. Totals, column order and collection counts come from the digests
    and are exact.

Correctness stance: everything here reproduces the legacy full-scan responses
byte-for-byte for the request shapes it accepts (the parity suite in
a parity suite pins this against build_table). Callers
must treat any exception as "use the full path" — the fast path is an
optimization, never the only road to the data.

Storage: Redis when reachable (shared across workers, survives deploys) with a
process-local LRU fallback; digests are a few hundred bytes each. A cache miss
costs one bounded batch of payload loads and repairs itself.

BYTE-IDENTICAL TWIN: selfhost/coordinator/services/dataset_fastpath.py is a
verbatim copy of this file (enforced by tests/test_twin_service_identity.py).
Edit the backend copy and re-copy it; never edit the coordinator's.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from sqlalchemy import Text, cast, func, select

from services import extracted_data_table as edt

logger = logging.getLogger(__name__)

# Redis is optional: the cloud backend has utils.redis_client, the self-host
# coordinator ships an in-process shim behind the same import, and tests may
# have neither. Every redis call below is best-effort with the LRU as floor.
try:  # pragma: no cover - import shape differs per deployment
    from utils.redis_client import get_redis
except Exception:  # noqa: BLE001 - any import failure means "no redis"
    get_redis = None  # type: ignore[assignment]

# Cache-key namespace. Bump the version whenever the digest layout or the
# semantics feeding it change — stale-format entries must never be decoded.
_KEY_PREFIX = "writ:dsfp:1:"
_EPOCH_PREFIX = "writ:dsfpepoch:"
_REDIS_TTL_S = 14 * 24 * 3600

# Payload-load batching for digest misses: bound both row count and bytes so a
# cold cache over a heavy window streams through memory instead of spiking it.
_BATCH_MAX_TASKS = 64
_BATCH_MAX_BYTES = 16_000_000
# SQLite's default variable limit is 999; stay far under it for IN() chunks.
_IN_CHUNK = 200

# The picker's last_delta teaser is display-only and the API contract allows
# null ("unknown"), so it is size-gated: computing it exactly needs the full
# window's payloads, which is only reasonable for small datasets. The row gate
# mirrors extracted_data_table._PICKER_DELTA_MAX_ROWS.
PICKER_DELTA_MAX_ROWS = 20_000
PICKER_DELTA_MAX_BYTES = 3_000_000

# In-process fallback stores (per worker). Digest values are tiny dicts; the
# epoch map is one int per workflow.
_LRU_MAX = 200_000
_lru: "OrderedDict[str, dict]" = OrderedDict()
_local_epochs: dict[str, int] = {}


@dataclass(frozen=True)
class TaskStub:
    """One scan-window run, payload not loaded. ``at`` mirrors _run_meta's
    ``run_at`` source (coalesce(completed_at, created_at))."""

    id: int
    at: Any  # datetime | None
    size: int


def declared_fingerprint(declared: Optional[list]) -> str:
    """Digest-key component for the workflow's declared output fields — the
    flatten projects records to them, so a declared-schema edit must miss."""
    cols = edt.declared_columns(declared)
    # NOT a security primitive: this is a cache-key digest over the declared column
    # names, so collision resistance is irrelevant and a short prefix is fine.
    # `usedforsecurity=False` states that — and is also what keeps this working on a
    # FIPS-mode host, where an unflagged sha1() raises instead of hashing.
    return hashlib.sha1(
        json.dumps(cols, separators=(",", ":")).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:10]


def _size_expr(db, model):
    """Cheap per-row payload size. Postgres: the stored (TOAST) size via
    pg_column_size — no serialization. Elsewhere (SQLite): the JSON text length.
    Only used as a change signal and a byte budget, never as an exact count."""
    dialect = ""
    try:
        dialect = db.get_bind().dialect.name
    except Exception:  # noqa: BLE001 - unknown bind shapes default to portable SQL
        pass
    if dialect == "postgresql":
        return func.pg_column_size(model.result_data)
    return func.length(cast(model.result_data, Text))


async def window_stubs(db, model, where: Sequence, cap: int) -> list[TaskStub]:
    """The scan window as stubs, most-recent first with an id tie-break —
    the same visibility ``where`` and cap the full scan uses, minus payloads."""
    recency = func.coalesce(model.completed_at, model.created_at)
    stmt = (
        select(model.id, recency, _size_expr(db, model))
        .where(*where)
        .order_by(recency.desc(), model.id.desc())
        .limit(cap)
    )
    res = await db.execute(stmt)
    return [TaskStub(id=r[0], at=r[1], size=int(r[2] or 0)) for r in res.all()]


async def load_tasks(db, model, ids: Sequence[int]) -> list:
    """Full ORM rows for ``ids``, returned in the given order. A task deleted
    between the stub pass and this load is simply absent (treated as 0 rows)."""
    by_id: dict[int, Any] = {}
    for i in range(0, len(ids), _IN_CHUNK):
        chunk = list(ids[i : i + _IN_CHUNK])
        res = await db.execute(select(model).where(model.id.in_(chunk)))
        for t in res.scalars().all():
            by_id[t.id] = t
    return [by_id[i] for i in ids if i in by_id]


# ---------------------------------------------------------------------------
# Digest cache — {n, cols, colls} per (task, payload size, declared, variant,
# workflow delete-epoch).
# ---------------------------------------------------------------------------

def _digest_of(task, declared: Optional[list], redactor) -> dict:
    """One task's digest, via the SAME pipeline the flatten runs (run_entries
    applies the redactor, the meta strip and the declared projection), so the
    count, the first-seen column order and the collection totals are exactly
    what this task contributes to the full table."""
    entries = edt.run_entries([task], declared=declared, redactor=redactor)
    recs = entries[0]["records"]
    cols: list[str] = []
    for _idx, fields in recs:
        for k in fields.keys():
            if k not in cols:
                cols.append(k)
    colls = edt.discover_collections([{"fields": f} for _i, f in recs])
    return {"n": len(recs), "cols": cols, "colls": [[c["path"], c["count"]] for c in colls]}


def _lru_get(key: str) -> Optional[dict]:
    v = _lru.get(key)
    if v is not None:
        _lru.move_to_end(key)
    return v


def _lru_put(key: str, value: dict) -> None:
    _lru[key] = value
    _lru.move_to_end(key)
    while len(_lru) > _LRU_MAX:
        _lru.popitem(last=False)


async def _cache_get_many(keys: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for k in keys:
        v = _lru_get(k)
        if v is not None:
            out[k] = v
    remaining = [k for k in keys if k not in out]
    if remaining and get_redis is not None:
        try:
            raw = await get_redis().mget(remaining)
            for k, r in zip(remaining, raw):
                if not r:
                    continue
                v = json.loads(r)
                if isinstance(v, dict) and isinstance(v.get("n"), int):
                    out[k] = v
                    _lru_put(k, v)
        except Exception:  # noqa: BLE001 - redis outage degrades to recompute
            logger.debug("dataset_fastpath: redis mget failed", exc_info=True)
    return out


async def _cache_set_many(items: dict[str, dict]) -> None:
    for k, v in items.items():
        _lru_put(k, v)
    if items and get_redis is not None:
        try:
            redis = get_redis()
            pipe = redis.pipeline(transaction=False)
            for k, v in items.items():
                pipe.set(k, json.dumps(v, separators=(",", ":")), ex=_REDIS_TTL_S)
            await pipe.execute()
        except Exception:  # noqa: BLE001
            logger.debug("dataset_fastpath: redis set failed", exc_info=True)


async def get_epoch(scope_id) -> int:
    """Per-workflow delete epoch folded into digest keys. Row deletes rewrite a
    task's payload — the size key-component almost always changes with it, but
    the epoch makes invalidation deterministic rather than probabilistic."""
    key = f"{_EPOCH_PREFIX}{scope_id}"
    if get_redis is not None:
        try:
            v = await get_redis().get(key)
            if v is not None:
                epoch = int(v)
                _local_epochs[key] = max(_local_epochs.get(key, 0), epoch)
                return epoch
        except Exception:  # noqa: BLE001
            logger.debug("dataset_fastpath: redis epoch read failed", exc_info=True)
    return _local_epochs.get(key, 0)


async def bump_epoch(scope_id) -> None:
    """Call after mutating stored extracted data (row deletes). Best-effort:
    a failed bump only leaves size-keyed invalidation, never wrong data."""
    key = f"{_EPOCH_PREFIX}{scope_id}"
    _local_epochs[key] = _local_epochs.get(key, 0) + 1
    if get_redis is not None:
        try:
            await get_redis().incr(key)
        except Exception:  # noqa: BLE001
            logger.debug("dataset_fastpath: redis epoch bump failed", exc_info=True)


def _digest_key(stub: TaskStub, fp: str, variant: str, epoch: int) -> str:
    return f"{_KEY_PREFIX}{stub.id}:{stub.size}:{fp}:{variant}:{epoch}"


def _batches(stubs: list[TaskStub]) -> list[list[TaskStub]]:
    out: list[list[TaskStub]] = []
    cur: list[TaskStub] = []
    cur_bytes = 0
    for s in stubs:
        if cur and (len(cur) >= _BATCH_MAX_TASKS or cur_bytes + s.size > _BATCH_MAX_BYTES):
            out.append(cur)
            cur, cur_bytes = [], 0
        cur.append(s)
        cur_bytes += s.size
    if cur:
        out.append(cur)
    return out


async def task_digests(
    db,
    model,
    stubs: list[TaskStub],
    *,
    declared: Optional[list],
    redactor: Optional[Callable],
    variant: str,
    epoch: int,
) -> dict[int, dict]:
    """Digests for every stub (cache-first; misses loaded in bounded batches
    and computed with the real flatten pipeline). Keyed back by task id."""
    fp = declared_fingerprint(declared)
    keys = {s.id: _digest_key(s, fp, variant, epoch) for s in stubs}
    cached = await _cache_get_many(list(keys.values()))
    out: dict[int, dict] = {}
    missing: list[TaskStub] = []
    for s in stubs:
        v = cached.get(keys[s.id])
        if v is not None:
            out[s.id] = v
        else:
            missing.append(s)
    for batch in _batches(missing):
        tasks = await load_tasks(db, model, [s.id for s in batch])
        staged: dict[str, dict] = {}
        for task in tasks:
            d = _digest_of(task, declared, redactor)
            out[task.id] = d
            staged[keys[task.id]] = d
        await _cache_set_many(staged)
    return out


# ---------------------------------------------------------------------------
# Assembly — page cover, merged columns/collections, totals.
# ---------------------------------------------------------------------------

def _merged_columns(stubs: list[TaskStub], digests: dict[int, dict], declared: Optional[list]) -> list[str]:
    """Reproduces flatten's column list: declared fields verbatim when present,
    else first-seen key order across the window in scan order."""
    declared_cols = edt.declared_columns(declared)
    if declared_cols:
        return list(declared_cols)
    seen: list[str] = []
    for s in stubs:
        for c in (digests.get(s.id) or {}).get("cols", []):
            if c not in seen:
                seen.append(c)
    return seen


def _merged_collections(stubs: list[TaskStub], digests: dict[int, dict]) -> list[dict]:
    """Window-wide nested-collection counts; discover_collections orders by
    (-count, path), which summed per-task counts reproduce exactly."""
    counts: dict[str, int] = {}
    for s in stubs:
        for path, n in (digests.get(s.id) or {}).get("colls", []):
            counts[path] = counts.get(path, 0) + int(n)
    return [{"path": p, "count": c} for p, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _cover_for(
    stubs: list[TaskStub], digests: dict[int, dict], first: int, last: int
) -> tuple[list[TaskStub], int]:
    """The stubs whose rows intersect ``[first, last)`` in window order, plus
    the number of rows contributed by tasks before the cover."""
    cover: list[TaskStub] = []
    before = 0
    pos = 0
    for s in stubs:
        n = (digests.get(s.id) or {}).get("n", 0)
        if n <= 0:
            continue
        if pos + n > first and pos < last:
            if not cover:
                before = pos
            cover.append(s)
        pos += n
        if pos >= last:
            break
    return cover, before


async def fast_table_page(
    db,
    model,
    where: Sequence,
    cap: int,
    *,
    declared: Optional[list],
    redactor: Optional[Callable],
    variant: str,
    scope_id,
    offset: int,
    limit: int,
) -> tuple[dict, int, bool]:
    """The default table page (view=all, run_at desc, no search/filters/pivot)
    without materializing the window: returns ``(build_table-shaped dict,
    scanned_runs, truncated)``. Callers gate the request shape; anything this
    can't reproduce exactly must go through the full scan instead."""
    stubs = await window_stubs(db, model, where, cap)
    epoch = await get_epoch(scope_id)
    digests = await task_digests(
        db, model, stubs, declared=declared, redactor=redactor, variant=variant, epoch=epoch
    )
    total = sum((digests.get(s.id) or {}).get("n", 0) for s in stubs)
    columns = _merged_columns(stubs, digests, declared)
    collections = _merged_collections(stubs, digests)

    rows: list[dict] = []
    if offset < total and limit > 0:
        cover, before = _cover_for(stubs, digests, offset, offset + limit)
        tasks = await load_tasks(db, model, [s.id for s in cover])
        _cols, flat = edt.flatten(tasks, declared=declared, redactor=redactor)
        rows = flat[offset - before : offset - before + limit]

    table = {
        "columns": columns,
        "declared": bool(edt.declared_columns(declared)),
        "collection": None,
        "collections": collections,
        "rows": rows,
        "total": total,
    }
    return table, len(stubs), len(stubs) >= cap


async def fast_facets(
    db,
    model,
    where: Sequence,
    cap: int,
    *,
    declared: Optional[list],
    redactor: Optional[Callable],
    variant: str,
    scope_id,
    sample_rows: int = 2000,
    sample_bytes: int = 12_000_000,
) -> dict:
    """view=all facets. Small windows are computed exactly (over every row, as
    before). Past the sample budget the facets describe the NEWEST rows only:
    the response carries ``sampled: true``, ``row_count`` = rows actually
    faceted (keeping the UI's non_empty/row_count ratios coherent) and
    ``total_rows`` = the exact window total."""
    stubs = await window_stubs(db, model, where, cap)
    epoch = await get_epoch(scope_id)
    digests = await task_digests(
        db, model, stubs, declared=declared, redactor=redactor, variant=variant, epoch=epoch
    )
    total = sum((digests.get(s.id) or {}).get("n", 0) for s in stubs)
    columns = _merged_columns(stubs, digests, declared)
    collections = _merged_collections(stubs, digests)

    cover: list[TaskStub] = []
    covered_rows = 0
    covered_bytes = 0
    sampled = False
    for s in stubs:
        n = (digests.get(s.id) or {}).get("n", 0)
        if n <= 0:
            continue
        if cover and (covered_rows >= sample_rows or covered_bytes + s.size > sample_bytes):
            sampled = True
            break
        cover.append(s)
        covered_rows += n
        covered_bytes += s.size
    tasks = await load_tasks(db, model, [s.id for s in cover])
    _cols, rows = edt.flatten(tasks, declared=declared, redactor=redactor)
    return {
        "columns": columns,
        "facets": edt.compute_facets(columns, rows),
        "collections": collections,
        "row_count": len(rows),
        "total_rows": total,
        "sampled": sampled,
        "scanned_runs": len(stubs),
        "truncated": len(stubs) >= cap,
    }


def truncate_preview_rows(rows: list, chars: int) -> list:
    """Preview-size the rows of a table response: any TOP-LEVEL STRING field
    longer than ``chars`` is cut to ``chars`` characters and the row gains a
    ``_truncated: [field, ...]`` sibling so the UI knows to hydrate the full
    record on demand (expand / viewer / copy / send). Non-string values are
    untouched — objects and arrays are structural (client-side collection
    pivots and file stamps need them whole), and they are rarely the payload
    that makes content datasets heavy. Copy-on-write: engine/lineage
    structures are never mutated."""
    out: list = []
    for row in rows:
        fields = row.get("fields")
        if not isinstance(fields, dict):
            out.append(row)
            continue
        truncated: list[str] = []
        new_fields: dict = {}
        for k, v in fields.items():
            if isinstance(v, str) and len(v) > chars:
                new_fields[k] = v[:chars]
                truncated.append(k)
            else:
                new_fields[k] = v
        if truncated:
            out.append({**row, "fields": new_fields, "_truncated": truncated})
        else:
            out.append(row)
    return out


def parse_row_refs(refs: Optional[list], *, cap: int = 100) -> list[tuple[int, int]]:
    """Parse repeated ``ref=run_id:record_index`` params (the row-hydration
    endpoint). Malformed entries are skipped; more than ``cap`` refs raises
    ValueError (the caller 400s) — hydration is a per-page affordance, never a
    bulk-export lane."""
    out: list[tuple[int, int]] = []
    for raw in refs or []:
        if not isinstance(raw, str) or ":" not in raw:
            continue
        run_s, _, idx_s = raw.partition(":")
        try:
            out.append((int(run_s), int(idx_s)))
        except ValueError:
            continue
    if len(out) > cap:
        raise ValueError(f"at most {cap} refs per request")
    return out


async def rows_by_refs(
    db,
    model,
    where: Sequence,
    refs: list[tuple[int, int]],
    *,
    declared: Optional[list],
    redactor: Optional[Callable],
) -> list[dict]:
    """FULL (untruncated) rows for ``(run_id, record_index)`` refs, in the
    given order — the hydration lane behind ``truncate_preview_rows``. Built
    with the same flatten pipeline as the table (same redaction, same declared
    projection), scoped by the same visibility ``where``; unknown refs are
    simply absent from the result."""
    from sqlalchemy import select as _select

    ids = list(dict.fromkeys(r[0] for r in refs))
    if not ids:
        return []
    res = await db.execute(_select(model).where(*where).where(model.id.in_(ids)))
    tasks = list(res.scalars().all())
    _cols, flat = edt.flatten(tasks, declared=declared, redactor=redactor)
    by_ref = {(r["run_id"], r["record_index"]): r for r in flat}
    return [by_ref[ref] for ref in refs if ref in by_ref]


async def picker_summary(
    db,
    model,
    where: Sequence,
    cap: int,
    *,
    declared: Optional[list],
    redactor: Optional[Callable],
    variant: str,
    scope_id,
) -> Optional[dict]:
    """The Data-explorer picker's per-workflow line without loading its corpus:
    ``{run_count, last_data_at, last_delta}`` (run_count = runs whose payload
    flattens to at least one row — real record_count, cached). Returns None
    when NO window run bears a row, which is exactly the legacy hide rule.

    last_delta stays exact — computed with picker_last_delta over the full
    window entries — but only for datasets small enough to load (row + byte
    gates); beyond them it is null, the contract's "unknown"."""
    stubs = await window_stubs(db, model, where, cap)
    if not stubs:
        return None
    epoch = await get_epoch(scope_id)
    digests = await task_digests(
        db, model, stubs, declared=declared, redactor=redactor, variant=variant, epoch=epoch
    )
    bearing = [s for s in stubs if (digests.get(s.id) or {}).get("n", 0) > 0]
    if not bearing:
        return None
    total_rows = sum((digests.get(s.id) or {}).get("n", 0) for s in bearing)
    total_bytes = sum(s.size for s in stubs)
    last_at = max((s.at for s in bearing if s.at is not None), default=None)

    last_delta = None
    if total_rows <= PICKER_DELTA_MAX_ROWS and total_bytes <= PICKER_DELTA_MAX_BYTES:
        tasks = await load_tasks(db, model, [s.id for s in stubs])
        entries = edt.run_entries(tasks, declared=declared, redactor=redactor)
        last_delta = edt.picker_last_delta(entries, declared=declared)
    return {
        "run_count": len(bearing),
        "last_data_at": last_at.isoformat() if last_at is not None else None,
        "last_delta": last_delta,
    }
