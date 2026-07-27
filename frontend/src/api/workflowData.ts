import client from './client';

/**
 * Extracted-data table API — the data a workflow's runs produced, aggregated
 * across runs into a sortable/searchable grid (backend:
 * routers/automation.py get_workflow_data + services/extracted_data_table.py).
 * Change-tracking routes:
 *   GET /automation/workflows/{id}/data/runs                (dated snapshot index)
 *   GET /automation/workflows/{id}/data/records/{uid}/history (per-record change-points)
 */

export type LineageChange = 'new' | 'changed' | 'same' | 'missing';

/**
 * Per-record lineage, present on view=latest / view=run rows when the backend
 * supports change tracking (older backends omit it — the UI must degrade).
 * Rows carry this as the `_lineage` key (a sibling of `fields`; the underscore
 * can never collide with data columns, which are meta-stripped).
 */
export interface RowLineage {
  uid: string;
  change: LineageChange;
  /** Changed leaf dot-paths vs the previous version (only when change === "changed"). */
  changed_fields?: string[];
  /** Total changed leaves before any >20-per-key collapse. */
  changed_leaf_count?: number;
  first_seen_at: string | null;
  last_seen_at: string | null;
  /** Change-point count: first appearance + each appearance whose content differed. */
  versions: number;
  /** view=run only: the chain member this snapshot was diffed against. */
  prev_run_id?: number | null;
  /** Originating top-level list key for multi-list extractions (null = untagged). */
  source?: string | null;
}

/** How records are matched across runs. Echoed on every lineage-bearing response. */
export interface DataIdentity {
  mode: 'explicit' | 'auto' | 'singleton' | 'hash';
  fields: string[];
  /** Set when an explicit `key` was rejected (non-empty in <50% of records). */
  requested_key_rejected?: string[];
  coverage?: number;
}

/** view=latest summary — post-dedup, pre-search/filter. */
export interface LineageCounts {
  new: number;
  changed: number;
  same: number;
  missing: number;
}

/** view=run / snapshot-index delta vs the previous chain member. */
export interface SnapshotDelta {
  new: number;
  changed: number;
  removed: number;
  unchanged: number;
}

/** A record present in the previous snapshot but absent from the selected one. */
export interface RemovedRecord {
  uid: string;
  fields: Record<string, unknown>;
}

export interface DataRow {
  run_id: number;
  run_at: string | null;
  status: string | null;
  success: boolean | null;
  duration_ms: number | null;
  /** Index of this record within its run (0 for single-record runs, 0..N for list output). */
  record_index: number;
  fields: Record<string, unknown>;
  /** Lineage payload — only on view=latest / view=run rows from lineage-aware backends. */
  _lineage?: RowLineage;
}

/** A nested array-of-records reachable from the current view (e.g. posts.items). */
export interface DataCollection {
  path: string;
  count: number;
}

export interface WorkflowDataTable {
  workflow_id: number;
  workflow_name: string;
  /** Ordered column names (declared output fields first, then any extra keys runs produced). */
  columns: string[];
  /** True when the columns came from the workflow's declared output schema. */
  declared: boolean;
  /** The nested-array path this table is pivoted to, if any. */
  collection?: string | null;
  /** Nested array paths reachable from the current view, each with an item count. */
  collections?: DataCollection[];
  rows: DataRow[];
  /** Total rows after search, before pagination. */
  total: number;
  /** How many runs were scanned to build this table. */
  scanned_runs: number;
  /** True when the scan hit the server cap (older runs not included). */
  truncated: boolean;
  limit: number;
  offset: number;
  /** Lineage-bearing responses only (view=latest / view=run). */
  identity?: DataIdentity;
  /** view=latest only. */
  counts?: LineageCounts;
  /** view=run only. */
  delta?: SnapshotDelta;
  removed_records?: RemovedRecord[];
  prev_run_id?: number | null;
  /** view=latest/run: record count per originating list key ("" = untagged),
   * computed pre-search — powers the mixed-schema source chips. */
  sources?: Record<string, number>;
}

/** One dated snapshot in the workflow's chain (GET .../data/runs), newest first. */
export interface SnapshotRun {
  run_id: number;
  run_at: string | null;
  status: string | null;
  record_count: number;
  /** A successful run that extracted an explicitly empty dataset. */
  explicit_empty: boolean;
  /** Null for the oldest chain member (nothing to diff against). */
  delta: SnapshotDelta | null;
}

export interface WorkflowDataRuns {
  runs: SnapshotRun[];
  identity: DataIdentity;
  scanned_runs: number;
  truncated: boolean;
}

/** One change-point version of a record (GET .../records/{uid}/history). */
export interface RecordVersion {
  run_id: number;
  run_at: string | null;
  record_index: number;
  fields: Record<string, unknown>;
  /** Empty for the first version. */
  changed_fields: string[];
  changed_leaf_count: number;
}

export interface RecordHistoryResponse {
  record_uid: string;
  identity: DataIdentity;
  first_seen_at: string | null;
  last_seen_at: string | null;
  /** Oldest → newest, change-points only. */
  versions: RecordVersion[];
}

/** Picker hint: the newest snapshot's delta (null when unknown / <2 snapshots). */
export interface PickerLastDelta {
  new: number;
  changed: number;
  removed: number;
}

export interface DataWorkflowSummary {
  workflow_id: number;
  workflow_name: string;
  /** 'crawl' for a Dragnet crawl's synthetic dataset — the grid locks to the
   *  aggregated (all-shards) view for these. */
  workflow_type?: string | null;
  /** For a crawl dataset, its crawl id — the Data explorer links back to the crawl
   *  detail (/crawls/{id}) instead of the workflow page. Absent for non-crawls. */
  crawl_id?: number | null;
  run_count: number;
  last_data_at: string | null;
  last_delta?: PickerLastDelta | null;
}

/** A structured smart-filter clause (AND-combined across clauses server-side). */
export type FilterClause =
  | { col: string; op: 'contains'; value: string }
  | { col: string; op: 'eq' | 'ne'; value: string }
  | { col: string; op: 'in'; values: string[] }
  | { col: string; op: 'between'; min?: number; max?: number }
  | { col: string; op: 'empty' | 'nonempty' };

export type ColumnType = 'number' | 'boolean' | 'date' | 'url' | 'json' | 'text';

export interface ColumnFacet {
  type: ColumnType;
  non_empty: number;
  min?: number;
  max?: number;
  distinct_count?: number;
  /** Present only when the column is low-cardinality (a value pick-list). */
  distinct?: { value: string; count: number }[];
}

export interface WorkflowFacets {
  workflow_id: number;
  columns: string[];
  facets: Record<string, ColumnFacet>;
  row_count: number;
  scanned_runs: number;
  truncated: boolean;
}

export type DataView = 'latest' | 'run' | 'all';

export interface WorkflowDataParams {
  q?: string;
  /** Structured smart-filter clauses. */
  filters?: FilterClause[];
  sort_by?: string;
  sort_dir?: 'asc' | 'desc';
  limit?: number;
  offset?: number;
  /** Pivot to a nested array path (e.g. "posts.items"); composes ONLY with view=all. */
  collection?: string;
  /** Lens: latest (deduped current dataset) | run (one snapshot) | all (default flat grid). */
  view?: DataView;
  /** Required when view=run. */
  run_id?: number;
  /** Identity-key override / echo — field names, sent comma-separated. */
  key?: string[];
  /** view=latest: include records absent from the newest snapshot (default false). */
  include_missing?: boolean;
  /** view=latest/run: only records from this originating list key ("" = untagged). */
  source?: string;
}

function toParams(params: WorkflowDataParams): URLSearchParams {
  const sp = new URLSearchParams();
  if (params.q) sp.append('q', params.q);
  if (params.filters && params.filters.length) sp.append('filters', JSON.stringify(params.filters));
  if (params.sort_by) sp.append('sort_by', params.sort_by);
  if (params.sort_dir) sp.append('sort_dir', params.sort_dir);
  if (params.limit != null) sp.append('limit', String(params.limit));
  if (params.offset != null) sp.append('offset', String(params.offset));
  if (params.collection) sp.append('collection', params.collection);
  if (params.view) sp.append('view', params.view);
  if (params.run_id != null) sp.append('run_id', String(params.run_id));
  if (params.key && params.key.length) sp.append('key', params.key.join(','));
  if (params.include_missing != null) sp.append('include_missing', params.include_missing ? 'true' : 'false');
  // "" is meaningful (the untagged bucket) — only omit when unset.
  if (params.source != null) sp.append('source', params.source);
  return sp;
}

/** Serialize a key override for a bare (non-table) lineage endpoint. */
function keyParam(key?: string[]): string {
  if (!key || key.length === 0) return '';
  const sp = new URLSearchParams();
  sp.append('key', key.join(','));
  return `?${sp.toString()}`;
}

export const workflowDataApi = {
  /** A paginated, sorted, searched page of a workflow's extracted data. */
  getTable: async (workflowId: number, params: WorkflowDataParams = {}): Promise<WorkflowDataTable> => {
    const response = await client.get(`/automation/workflows/${workflowId}/data?${toParams(params).toString()}`);
    return response.data;
  },

  /** Per-column facets (types, distinct values, numeric ranges) for smart filters.
   * Lens params (view/run_id/key/include_missing/source) compute the facets over
   * exactly that lens's rowset; omit them for the all-rows grid. Older backends
   * ignore the params and answer with all-rows facets (contract-compatible). */
  getFacets: async (workflowId: number, params: WorkflowDataParams = {}): Promise<WorkflowFacets> => {
    const sp = toParams({
      view: params.view,
      run_id: params.run_id,
      key: params.key,
      include_missing: params.include_missing,
      source: params.source,
    });
    const qs = sp.toString();
    const response = await client.get(`/automation/workflows/${workflowId}/data/facets${qs ? `?${qs}` : ''}`);
    return response.data;
  },

  /** The dated snapshot index (chain members, newest first) with per-run deltas.
   * 404s on backends that predate change tracking — callers must degrade. */
  getRuns: async (workflowId: number, key?: string[]): Promise<WorkflowDataRuns> => {
    const response = await client.get(`/automation/workflows/${workflowId}/data/runs${keyParam(key)}`);
    return response.data;
  },

  /** A record's change-point history. `key` MUST echo the identity.fields of the
   * response the uid came from, so the auto-picked identity is pinned. */
  getHistory: async (workflowId: number, recordUid: string, key?: string[]): Promise<RecordHistoryResponse> => {
    const response = await client.get(
      `/automation/workflows/${workflowId}/data/records/${encodeURIComponent(recordUid)}/history${keyParam(key)}`,
    );
    return response.data;
  },

  /** Workflows the caller owns that have produced extracted data (global explorer picker). */
  listWorkflows: async (): Promise<DataWorkflowSummary[]> => {
    const response = await client.get('/automation/data/workflows');
    return response.data?.workflows || [];
  },

  /**
   * A small, REDACTED column/row sample for a workflow's extracted data — used by
   * the block editors' DataTablePicker to preview what a source produces before
   * wiring it into an export/append action. The data-table endpoint already
   * redacts server-side (strip_meta + secret-key redaction), so this reuses
   * getTable with a small page and flattens each row's `fields`.
   */
  preview: async (
    workflowId: number,
    params: { filters?: FilterClause[]; limit?: number } = {},
  ): Promise<{ columns: string[]; rows: Record<string, unknown>[] }> => {
    const table = await workflowDataApi.getTable(workflowId, {
      filters: params.filters,
      limit: Math.min(Math.max(params.limit ?? 5, 1), 50),
    });
    return {
      columns: table.columns || [],
      rows: (table.rows || []).map((r) => r.fields || {}),
    };
  },

  /** Remove extracted-data rows for a workflow. `records` refs identify single
   * rows by (run_id, record_index); `record_uids` deletes EVERY stored version
   * of those records across runs (uids resolved server-side in the same
   * transaction — pass `key` echoing the identity the uids were computed with).
   * A single-record run is cleared entirely when its record is removed. */
  deleteRows: async (
    workflowId: number,
    req: {
      records?: Array<{ run_id: number; record_index: number }>;
      record_uids?: string[];
      key?: string[];
    },
  ): Promise<{ deleted: number; resolved?: Record<string, number>; unmatched?: string[] }> => {
    const response = await client.delete(`/automation/workflows/${workflowId}/data`, {
      data: {
        records: req.records,
        record_uids: req.record_uids,
        key: req.key && req.key.length ? req.key.join(',') : undefined,
      },
    });
    return response.data || { deleted: 0 };
  },

  /** Download the full (search/sort applied) table as a CSV or JSON blob.
   * view=latest/run exports carry lineage columns (`_lineage` in JSON rows). */
  exportData: async (
    workflowId: number,
    format: 'csv' | 'json',
    params: WorkflowDataParams = {},
  ): Promise<Blob> => {
    const sp = toParams({
      q: params.q,
      filters: params.filters,
      sort_by: params.sort_by,
      sort_dir: params.sort_dir,
      view: params.view,
      run_id: params.run_id,
      key: params.key,
      include_missing: params.include_missing,
    });
    sp.append('format', format);
    const response = await client.get(
      `/automation/workflows/${workflowId}/data/export?${sp.toString()}`,
      { responseType: 'blob' },
    );
    return response.data;
  },
};
