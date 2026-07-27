import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import i18n from '../../i18n';
import { Link } from 'react-router-dom';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import {
  ChevronUpIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  ChevronUpDownIcon,
  ArrowDownTrayIcon,
  ArrowsPointingOutIcon,
  ArrowsPointingInIcon,
  MagnifyingGlassIcon,
  ArrowPathIcon,
  TableCellsIcon,
  Squares2X2Icon,
  XMarkIcon,
  ClockIcon,
  CommandLineIcon,
  DocumentDuplicateIcon,
  RectangleStackIcon,
  CheckIcon,
  MinusIcon,
  PaperAirplaneIcon,
  TrashIcon,
  ViewColumnsIcon,
} from '@heroicons/react/24/outline';
import { Modal } from '../ui/Modal';
import { DatasetApiModal } from './DatasetApiModal';
import { Select, Expand } from '../ui';
import { useQuery } from '../../hooks/useQuery';
import { Q } from '../../stores/queryKeys';
import { automationApi } from '../../api/endpoints';
import type { AutomationWorkflow } from '../../types/api';
import {
  workflowDataApi,
  type DataRow,
  type WorkflowDataParams,
  type FilterClause,
  type ColumnFacet,
  type ColumnType,
  type DataView,
  type RowLineage,
  type WorkflowDataRuns,
} from '../../api/workflowData';
import { statusStyle } from '../../utils/statusStyle';
import { formatRelativeTime, downloadBlob, copyToClipboard, uiLocale } from '../../utils/format';
import { classifyValue, previewText, type ValueKind } from '../../utils/valueType';
import {
  unionColumns,
  recordsToCsv,
  recordsToTsv,
  recordsToMarkdown,
  recordsToJson,
} from '../../utils/recordExport';
import { ValueViewer } from './ValueViewer';
import { ModeToggle } from './ModeToggle';
import { ColumnFilterMenu } from './ColumnFilterMenu';
import { TableScroller } from './TableScroller';
import { Popover } from './Popover';
import { ChangePill, ChangeDot } from './ChangePill';
import { RecordHistory } from './RecordHistory';
import { SnapshotNav } from './SnapshotNav';
import { IdentityChip } from './IdentityChip';
import { shelfFilterChipClass, shelfFilterCountClass, RowsSkeleton } from '../library/shelf';

/**
 * True on the render where any entry of `deps` changed identity since the last
 * one — React's documented "adjust state during render" memory
 * (react.dev/reference/react/useState#storing-information-from-previous-renders).
 *
 * This is what a `useEffect` whose entire body is a reset `setState` should be:
 * the reset lands in the SAME commit as the change that caused it, so the grid
 * never paints a frame holding e.g. a page number from the previous filter set,
 * and there is no second render pass per change. Comparison is by identity, so
 * it reproduces a dependency array exactly. Seeded with the first render's
 * values: mount is not a change (the `useState` initializers already produced
 * that state).
 */
function useDepsChanged(deps: readonly unknown[]): boolean {
  const [prev, setPrev] = useState(deps);
  if (prev.length !== deps.length || deps.some((d, i) => !Object.is(d, prev[i]))) {
    setPrev(deps);
    return true;
  }
  return false;
}

/** Debounce a fast-changing value (search input) to limit refetches. */
function useDebouncedValue<T>(value: T, delayMs = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(t);
  }, [value, delayMs]);
  return debounced;
}

const PAGE_SIZE = 50;
// When pivoting to a nested collection we flatten the loaded records' items
// client-side; pull a wider page of records so more items are in reach.
const NESTED_RECORD_LIMIT = 200;

// Fixed-layout column widths (px). Data columns get per-type defaults (see
// defaultColWidth) and are user-resizable down to their header-label fit.
const W_GUTTER = 44;
const W_RUN = 60;
const W_WHEN = 108;
const W_STATUS = 108;
// Slim change-signal dot column — present only when the window has signal.
const W_DOT = 28;
const W_VERSIONS = 76;
const MIN_COL = 80;
// Full-screen only: the results card stops being a block that grows with its rows
// and becomes the height-owning column, so the grid inside it can scroll on both
// axes (sticky header, reachable horizontal scrollbar). The min-height keeps it
// from collapsing to nothing when the lens/toolbar chrome above is unusually tall
// on a short window — the overlay body scrolls instead.
const CARD_SHELL_MAX = 'flex min-h-[240px] min-w-0 flex-1 flex-col';
// Data-column cap: everything past it hides behind the "Fields" manager.
const MAX_VISIBLE_COLS = 8;
// Rows sampled for per-column type/emptiness stats when facets aren't loaded.
const STAT_SAMPLE = 50;

// The three lenses over the dataset — Current (view=latest) | By date (view=run)
// | All rows (today's flat grid). Persisted per workflow in localStorage; NEVER
// in searchParams (kept-alive pages must not write the shared URL).
const LENSES: DataView[] = ['latest', 'run', 'all'];
const lensStoreKey = (id: number) => `writ.dataLens.${id}`;
const keyStoreKey = (id: number) => `writ.dataKey.${id}`;
const colsStoreKey = (id: number) => `writ.dataCols.${id}`;
// Run info is a viewing habit, not a per-dataset fact — one global key.
const RUN_INFO_KEY = 'writ.dataRunInfo';

function readStoredLens(id: number): DataView | null {
  try {
    const v = localStorage.getItem(lensStoreKey(id));
    return v && (LENSES as string[]).includes(v) ? (v as DataView) : null;
  } catch {
    return null;
  }
}

function readStoredKey(id: number): string[] | null {
  try {
    const v = localStorage.getItem(keyStoreKey(id));
    const fields = (v || '').split(',').map((s) => s.trim()).filter(Boolean);
    return fields.length ? fields : null;
  } catch {
    return null;
  }
}

/** The user's explicit visible-column choice (null = heuristic cap applies). */
function readStoredCols(id: number): string[] | null {
  try {
    const raw = localStorage.getItem(colsStoreKey(id));
    if (!raw) return null;
    const arr: unknown = JSON.parse(raw);
    return Array.isArray(arr) && arr.length > 0 && arr.every((x) => typeof x === 'string')
      ? (arr as string[])
      : null;
  } catch {
    return null;
  }
}

function readStoredRunInfo(): boolean {
  try {
    return localStorage.getItem(RUN_INFO_KEY) === 'true';
  } catch {
    return false;
  }
}

/** Human-readable label for an active filter chip. */
function describeClause(c: FilterClause): string {
  switch (c.op) {
    case 'contains':
      return `${c.col} ⊃ “${c.value}”`;
    case 'eq':
      return `${c.col} = ${c.value}`;
    case 'ne':
      return `${c.col} ≠ ${c.value}`;
    case 'in':
      return `${c.col}: ${c.values.slice(0, 3).join(', ')}${c.values.length > 3 ? ` +${c.values.length - 3}` : ''}`;
    case 'between':
      if (c.min != null && c.max != null) return `${c.col} ${c.min}–${c.max}`;
      if (c.min != null) return `${c.col} ≥ ${c.min}`;
      if (c.max != null) return `${c.col} ≤ ${c.max}`;
      return c.col;
    case 'empty':
      return i18n.t('{{col}} is empty', { col: c.col });
    case 'nonempty':
      return i18n.t('{{col}} has value', { col: c.col });
    default:
      return '';
  }
}

/** Compact absolute run date+time, e.g. "Jun 21, 10:03". */
function fmtRunDate(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return (
    d.toLocaleDateString(uiLocale(), { month: 'short', day: 'numeric' }) +
    ' ' +
    d.toLocaleTimeString(uiLocale(), { hour: '2-digit', minute: '2-digit' })
  );
}

// Field-name hints used to pick which column heads a record card.
const TITLE_HINT = /\b(name|title|headline|company|product|subject|label|heading|position|role|full_?name)\b|name$|title$/i;
const IMAGE_HINT = /\b(image|img|photo|avatar|thumb|thumbnail|logo|picture|pic|icon|banner|cover)\b/i;

/** A displayable / selectable row, whatever the scope it came from. */
interface RecordLike {
  run_id: number;
  run_at: string | null;
  status: string | null;
  record_index: number;
  fields: Record<string, unknown>;
  /** Change-tracking payload (view=latest / view=run rows only). */
  lineage?: RowLineage;
}
interface ViewRow extends RecordLike {
  /** Stable key for selection + React. */
  key: string;
  /** By-date lens: a record the previous snapshot had but this one doesn't. */
  removed?: boolean;
}

/** What the expanded record needs to fetch /history (null = lens has no lineage). */
interface HistoryCtx {
  workflowId: number;
  /** Identity echo — the fields the visible uids were computed with. */
  keyFields: string[];
  truncated?: boolean;
  scannedRuns?: number;
}

interface ColumnRoles {
  titleCol: string | null;
  imageCol: string | null;
}

function isPlainObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** Resolve a dot-path (e.g. "posts.items") within a record's fields. */
function resolvePath(obj: Record<string, unknown>, path: string): unknown {
  return path.split('.').reduce<unknown>((acc, k) => (isPlainObj(acc) ? acc[k] : undefined), obj);
}

/**
 * Find every nested array-of-records inside the loaded rows (e.g. `posts.items`,
 * `categories`), so the view can pivot to any of them. A record commonly holds
 * several such collections — this is how the user picks which one to work with.
 */
function discoverCollections(rows: DataRow[]): { path: string; count: number }[] {
  const counts = new Map<string, number>();
  const visit = (val: unknown, prefix: string, depth: number) => {
    if (depth > 3) return;
    if (Array.isArray(val)) {
      if (val.length > 0 && val.every(isPlainObj)) counts.set(prefix, (counts.get(prefix) || 0) + val.length);
      return;
    }
    if (isPlainObj(val)) {
      for (const [k, v] of Object.entries(val)) visit(v, prefix ? `${prefix}.${k}` : k, depth + 1);
    }
  };
  for (const r of rows) {
    const f = r.fields || {};
    for (const [k, v] of Object.entries(f)) visit(v, k, 1);
  }
  return [...counts.entries()].map(([path, count]) => ({ path, count })).sort((a, b) => b.count - a.count);
}

/**
 * Pick which columns head a record card: a title (a short, name-like text
 * column) and an optional image. Derived from a sample value per column so it
 * works whether or not the workflow declared an output schema.
 */
function pickColumnRoles(columns: string[], rows: RecordLike[]): ColumnRoles {
  if (columns.length === 0) return { titleCol: null, imageCol: null };
  const sample: Record<string, unknown> = {};
  for (const c of columns) {
    for (const r of rows) {
      const v = (r.fields || {})[c];
      if (v !== null && v !== undefined && v !== '') {
        sample[c] = v;
        break;
      }
    }
  }
  const kindOf = (c: string) => classifyValue(sample[c]).kind;

  const imageCol =
    columns.find((c) => kindOf(c) === 'image') ||
    columns.find((c) => IMAGE_HINT.test(c) && kindOf(c) === 'url') ||
    null;
  const titleCol =
    columns.find((c) => c !== imageCol && TITLE_HINT.test(c) && (kindOf(c) === 'text' || kindOf(c) === 'markdown')) ||
    columns.find((c) => c !== imageCol && (kindOf(c) === 'text' || kindOf(c) === 'markdown')) ||
    columns.find((c) => c !== imageCol && kindOf(c) === 'url') ||
    null;

  return { titleCol, imageCol };
}

// Identity plumbing, not reading material — demoted to the column tail.
const ID_LIKE_RE = /^(id|uid|uuid)$|_id$/i;

/** Per-column display stats feeding the signal ranking + width defaults. */
interface ColStat {
  type: ColumnType | 'image';
  /** Non-empty count shown in the Fields manager. */
  nonEmpty: number;
  /** Non-empty ratio 0..1 (facets when loaded, else the visible sample). */
  ratio: number;
  /** Mean one-line preview length of non-empty samples (short-text width cue). */
  avgLen: number;
  idLike: boolean;
}

function computeColumnStats(
  columns: string[],
  rows: RecordLike[],
  facets: Record<string, ColumnFacet>,
  facetRowCount: number,
): Map<string, ColStat> {
  const out = new Map<string, ColStat>();
  const sample = rows.slice(0, STAT_SAMPLE);
  for (const col of columns) {
    let nonEmpty = 0;
    let lenSum = 0;
    let sampleKind: ValueKind | null = null;
    for (const r of sample) {
      const v = (r.fields || {})[col];
      const k = classifyValue(v).kind;
      if (k === 'empty') continue;
      nonEmpty += 1;
      lenSum += previewText(v).length;
      if (sampleKind === null) sampleKind = k;
    }
    const facet = facets[col];
    const type: ColStat['type'] =
      facet?.type ??
      (sampleKind === 'image'
        ? 'image'
        : sampleKind === 'markdown' || sampleKind === null
          ? 'text'
          : sampleKind);
    out.set(col, {
      type,
      nonEmpty: facet ? facet.non_empty : nonEmpty,
      ratio:
        facet && facetRowCount > 0 ? facet.non_empty / facetRowCount : nonEmpty / Math.max(1, sample.length),
      avgLen: nonEmpty > 0 ? lenSum / nonEmpty : 0,
      idLike: ID_LIKE_RE.test(col),
    });
  }
  return out;
}

/**
 * Signal-ranked column order (every lens, All rows included): title first,
 * image second, then non-empty ratio desc with a type preference
 * (text/url/date/number > boolean > json); json blobs sink to just before the
 * id-like tail. Deterministic tie-break: the server's column order.
 */
function rankColumns(columns: string[], stats: Map<string, ColStat>, roles: ColumnRoles): string[] {
  const prior = new Map(columns.map((c, i) => [c, i]));
  const groupOf = (c: string) => {
    const s = stats.get(c);
    return s?.idLike ? 2 : s?.type === 'json' ? 1 : 0;
  };
  const typePref = (c: string) => {
    const ty = stats.get(c)?.type;
    return ty === 'boolean' ? 1 : ty === 'json' ? 2 : 0;
  };
  const head = [roles.titleCol, roles.imageCol].filter((c): c is string => !!c && columns.includes(c));
  const rest = columns
    .filter((c) => !head.includes(c))
    .sort(
      (a, b) =>
        groupOf(a) - groupOf(b) ||
        (stats.get(b)?.ratio ?? 0) - (stats.get(a)?.ratio ?? 0) ||
        typePref(a) - typePref(b) ||
        (prior.get(a) ?? 0) - (prior.get(b) ?? 0),
    );
  return [...head, ...rest];
}

// Header labels never truncate: every column's floor fits its label. Measured
// once per label via canvas (the header row renders at 12px font-medium);
// +64 covers cell padding, the sort caret and the filter-menu trigger.
let headerMeasureCtx: CanvasRenderingContext2D | null | undefined;
function headerMinWidth(label: string): number {
  if (headerMeasureCtx === undefined) {
    try {
      headerMeasureCtx = document.createElement('canvas').getContext('2d');
      if (headerMeasureCtx) headerMeasureCtx.font = `500 12px ${getComputedStyle(document.body).fontFamily}`;
    } catch {
      headerMeasureCtx = null;
    }
  }
  const w = headerMeasureCtx ? headerMeasureCtx.measureText(label).width : label.length * 7.2;
  return Math.ceil(w) + 64;
}

/** A compact, type-aware value for a record card field (one line). */
const InlineValue: React.FC<{ value: unknown }> = React.memo(({ value }) => {
  const c = classifyValue(value);
  if (c.kind === 'empty') return <span className="text-tertiary/60">—</span>;
  if (c.kind === 'image') {
    return <img src={String(value)} alt="" loading="lazy" className="h-6 w-6 rounded border border-border object-cover" />;
  }
  if (c.kind === 'json') {
    const v = c.json;
    const label = Array.isArray(v) ? `[${v.length}]` : `{${Object.keys(v as object).length}}`;
    return <code className="rounded bg-canvas px-1.5 py-0.5 text-[11px] text-secondary">{label}</code>;
  }
  if (c.kind === 'url') {
    const s = String(value);
    return (
      <a
        href={s}
        target="_blank"
        rel="noopener noreferrer"
        onClick={(e) => e.stopPropagation()}
        className="truncate text-ink underline underline-offset-2"
        title={s}
      >
        {s}
      </a>
    );
  }
  if (c.kind === 'number' || c.kind === 'boolean') {
    return <span className="tabular-nums text-ink">{String(value)}</span>;
  }
  const text = previewText(value);
  return (
    <span className="truncate text-ink" title={text.length > 60 ? text : undefined}>
      {text}
    </span>
  );
});
InlineValue.displayName = 'InlineValue';

/** A small tri-state selection checkbox. `onChange` receives the mouse event so
 * callers can read Shift/Cmd/Ctrl modifiers for range / toggle semantics. */
const SelectBox: React.FC<{
  checked: boolean;
  indeterminate?: boolean;
  onChange: (e?: React.MouseEvent) => void;
  onMouseDown?: (e: React.MouseEvent) => void;
  onMouseEnter?: (e: React.MouseEvent) => void;
  label: string;
}> = ({ checked, indeterminate, onChange, onMouseDown, onMouseEnter, label }) => (
  <button
    type="button"
    role="checkbox"
    aria-checked={indeterminate ? 'mixed' : checked}
    aria-label={label}
    onClick={(e) => {
      e.stopPropagation();
      onChange(e);
    }}
    onMouseDown={onMouseDown}
    onMouseEnter={onMouseEnter}
    className={clsx(
      'flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors',
      checked || indeterminate ? 'border-ink bg-ink text-white' : 'border-zinc-300 bg-surface hover:border-zinc-400',
    )}
  >
    {indeterminate ? <MinusIcon className="h-3 w-3" /> : checked ? <CheckIcon className="h-3 w-3" /> : null}
  </button>
);

/** A left-aligned item inside a Popover menu. */
const MenuItem: React.FC<{ onClick: () => void; children: React.ReactNode }> = ({ onClick, children }) => (
  <button
    onClick={onClick}
    className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12px] text-secondary transition-colors hover:bg-hover hover:text-ink"
  >
    {children}
  </button>
);

/** A pill in the collections bar — one click pivots the view to that scope. */
const ScopeChip: React.FC<{ active: boolean; label: string; count: number; mono?: boolean; onClick: () => void }> = ({
  active,
  label,
  count,
  mono,
  onClick,
}) => (
  <button
    onClick={onClick}
    className={clsx(
      'inline-flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1 text-[12px] transition-colors',
      active ? 'border-ink bg-ink text-white' : 'border-border bg-surface text-secondary hover:border-zinc-400 hover:text-ink',
    )}
    title={label}
  >
    <span className={clsx('max-w-[220px] truncate font-medium', mono && 'font-mono text-[11px]')}>{label}</span>
    <span
      className={clsx(
        'rounded-full px-1.5 py-px text-[10px] tabular-nums',
        active ? 'bg-white/20 text-white' : 'bg-canvas text-tertiary',
      )}
    >
      {count}
    </span>
  </button>
);

interface Props {
  workflowId: number;
  /** Crawl datasets aggregate MANY shard runs into ONE logical result set. The
   *  Current/By-date lenses (temporal snapshot dedup across re-runs) are
   *  meaningless there — a crawl runs once, fanned into parallel shards — so we
   *  lock the grid to All-rows and hide the lens switcher. Without this the grid
   *  defaults to `latest` and shows only the most recent SHARD (~one shard's
   *  pages), forcing the user to page through runs to see the rest. */
  isCrawl?: boolean;
}

/**
 * The data a workflow's runs extracted, as a scannable, selectable workspace.
 * Switch between a card list and a dense grid; pivot the view to any nested
 * collection a record holds (e.g. `posts.items`); select rows and copy, export,
 * or push them onward. A run that extracted a LIST contributes one row per
 * record. Powers the per-workflow Data tab and the global Data explorer.
 */
export const ExtractedDataTable: React.FC<Props> = ({ workflowId, isCrawl = false }) => {
  const { t } = useTranslation();

  const [searchInput, setSearchInput] = useState('');
  const [filters, setFilters] = useState<FilterClause[]>([]);
  // null = the lens's server default order (Current: change rank — golden-pinned).
  const [sortBy, setSortBy] = useState<string | null>(() => (isCrawl || (readStoredLens(workflowId) ?? 'latest') === 'all') ? 'run_at' : null);
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');
  const [page, setPage] = useState(0);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [colWidths, setColWidths] = useState<Record<string, number>>({});
  // Provenance columns (Run/Date/Status — lineage lens: First/Last seen +
  // Versions) are power-user chrome: default OFF in every lens, persisted
  // globally so the preference follows the user across datasets.
  const [showRunInfo, setShowRunInfo] = useState(readStoredRunInfo);
  useEffect(() => {
    try {
      localStorage.setItem(RUN_INFO_KEY, showRunInfo ? 'true' : 'false');
    } catch {
      /* ignore */
    }
  }, [showRunInfo]);
  // "Get via API" — show a copy-paste curl for the public Workflow Data API.
  const [apiModalOpen, setApiModalOpen] = useState(false);
  // Cards (a scannable list of records) vs the dense spreadsheet. Cards is the
  // default — extracted data usually reads as a list. Sticky via localStorage.
  const [view, setView] = useState<'cards' | 'table'>(() => {
    if (typeof window === 'undefined') return 'cards';
    return localStorage.getItem('writ.dataView') === 'table' ? 'table' : 'cards';
  });
  useEffect(() => {
    try {
      localStorage.setItem('writ.dataView', view);
    } catch {
      /* ignore */
    }
  }, [view]);
  // Full-screen grid. A wide dataset is unreadable in a detail pane that's half
  // the window — this hands it the whole window, and (because the overlay owns
  // the height) is the one mode where the sticky header and the horizontal
  // scrollbar are both on screen at once. Deliberately NOT persisted: full screen
  // is a "let me look at this right now" gesture, not a standing preference.
  const [maximized, setMaximized] = useState(false);

  // Lens state (Current | By date | All rows). `lensChosen` distinguishes an
  // explicit user pick (persisted) from the computed default, so a singleton
  // dataset can still flip the default to By date after identity is known.
  const [lens, setLens] = useState<DataView>(() => isCrawl ? 'all' : (readStoredLens(workflowId) ?? 'latest'));
  const [lensChosen, setLensChosen] = useState(() => isCrawl || readStoredLens(workflowId) != null);
  // null = capability unknown; false = the backend predates change tracking →
  // hide the switcher entirely and behave exactly like the old flat grid.
  const [lineageSupported, setLineageSupported] = useState<boolean | null>(null);
  // By-date snapshot selection — ephemeral, never URL/localStorage. Held as
  // state rather than derived from the chain so it SURVIVES the moments the
  // chain isn't loaded (the runs query re-keys whenever the identity echo
  // lands); deriving it would blank the grid back to a skeleton each time.
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [includeMissing, setIncludeMissing] = useState(false);
  // Identity-key override (user-chosen, persisted) vs the pinned auto-pick
  // (echoed back on follow-up lineage calls so auto identity can't drift
  // between the list fetch and a history/delete call).
  const [keyOverride, setKeyOverride] = useState<string[] | null>(() => readStoredKey(workflowId));
  const [pinnedKey, setPinnedKey] = useState<string[] | null>(null);
  // The last lineage response's identity.fields, ANY mode — echoed on /data/runs
  // so every follow-up lineage call pins the identity (spec 1.5). Distinct from
  // pinnedKey (auto-picks only), which also feeds the view=run table request.
  const [echoKey, setEchoKey] = useState<string[] | null>(null);
  const [keyRejectedNotice, setKeyRejectedNotice] = useState<string | null>(null);
  // Mixed-schema source filter (lineage lenses) — ephemeral, never persisted.
  const [sourceFilter, setSourceFilter] = useState<string | null>(null);
  // Explicit visible-column choice from the Fields manager. null = the ranked
  // 8-column heuristic; a user pick always wins and persists per workflow.
  const [fieldPrefs, setFieldPrefs] = useState<string[] | null>(() => readStoredCols(workflowId));
  // The 0-record-newest-snapshot amber banner auto-enables "Include gone" once.
  // A state flag, not a ref: the one-shot guard is read during render.
  const [autoMissingDone, setAutoMissingDone] = useState(false);

  // Re-read the per-workflow persisted bits when the workflow changes in place.
  // Adjusted during render rather than in an effect so the new workflow's very
  // first paint already uses its own lens/sort/key instead of briefly showing
  // the previous workflow's — and `isCrawl` is read live, which the effect's
  // dependency list could not do.
  if (useDepsChanged([workflowId])) {
    const stored = readStoredLens(workflowId);
    setLens(isCrawl ? 'all' : (stored ?? 'latest'));
    setLensChosen(isCrawl || stored != null);
    setSortBy((isCrawl || (stored ?? 'latest') === 'all') ? 'run_at' : null);
    setSortDir('desc');
    setKeyOverride(readStoredKey(workflowId));
    setPinnedKey(null);
    setEchoKey(null);
    setLineageSupported(null);
    setSelectedRunId(null);
    setIncludeMissing(false);
    setKeyRejectedNotice(null);
    setSourceFilter(null);
    setAutoMissingDone(false);
  }

  // Which collection the view is pivoted to: 'records' (top-level rows) or a
  // nested array path. Nested scopes are rendered client-side from loaded rows.
  const [scope, setScope] = useState('records');
  // Client-side sort/page for nested scopes (server sort/page only fit records).
  const [nestedSort, setNestedSort] = useState<{ by: string | null; dir: 'asc' | 'desc' }>({ by: null, dir: 'asc' });
  const [nestedPage, setNestedPage] = useState(0);

  // Field choices persist per workflow for the records scope; a nested
  // collection has its own column set, so its choices stay session-ephemeral.
  if (useDepsChanged([scope, workflowId])) {
    setFieldPrefs(scope === 'records' ? readStoredCols(workflowId) : null);
  }

  // Selected row keys + the "send to automation" modal.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [sendOpen, setSendOpen] = useState(false);
  const [sendTargets, setSendTargets] = useState<AutomationWorkflow[] | null>(null);
  const [sendTargetId, setSendTargetId] = useState<number | null>(null);
  const [sending, setSending] = useState(false);

  // Delete-selected confirmation modal + in-flight guard.
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteRows, setDeleteRows] = useState<ViewRow[] | null>(null);

  const q = useDebouncedValue(searchInput, 350);
  const isNested = scope !== 'records';

  // A nested collection scope forces All rows (change tracking operates on
  // top-level records only), and a legacy backend collapses everything to it.
  const effLens: DataView = isCrawl || isNested || lineageSupported === false ? 'all' : lens;
  const lensIsLineage = effLens === 'latest' || effLens === 'run';

  // Reset to the first page whenever the result set changes shape.
  if (useDepsChanged([q, filters, sortBy, sortDir, effLens, selectedRunId, includeMissing, sourceFilter])) {
    setPage(0);
  }
  if (useDepsChanged([scope, searchInput, nestedSort])) setNestedPage(0);

  // Snapshot index — powers the By-date nav + the reconciliation line, and
  // doubles as the capability probe (a pre-lineage backend 404s the route).
  // After the first load it echoes the resolved identity.fields regardless of
  // mode (same unconditional policy as /history and DELETE-by-uid).
  const runsKeyFields = keyOverride ?? echoKey ?? pinnedKey ?? undefined;
  const { data: runsResult, refresh: refreshRuns } = useQuery<WorkflowDataRuns | { unsupported: true }>(
    `workflow:${workflowId}:data:runs:${(runsKeyFields || []).join(',')}`,
    async () => {
      try {
        return await workflowDataApi.getRuns(workflowId, runsKeyFields);
      } catch (err: unknown) {
        if ((err as { response?: { status?: number } }).response?.status === 404) return { unsupported: true };
        throw err;
      }
    },
    { silent: true, enabled: !isNested && lineageSupported !== false },
  );
  const runsData = runsResult && !('unsupported' in runsResult) ? runsResult : null;
  // Adopt the capability answer as soon as a probe response lands. Keyed on the
  // response object — exactly what the old effect's dependency list watched —
  // so it runs once per response instead of costing a render pass for each.
  const [probedRuns, setProbedRuns] = useState<typeof runsResult>(null);
  if (runsResult && runsResult !== probedRuns) {
    setProbedRuns(runsResult);
    setLineageSupported(!('unsupported' in runsResult));
  }
  // Legacy backend: collapse to today's exact behavior, default sort included.
  if (useDepsChanged([lineageSupported]) && lineageSupported === false) {
    setSortBy((cur) => cur ?? 'run_at');
  }

  // Default the By-date selection to the newest snapshot (ephemeral state), and
  // re-home a pick that fell out of the chain. Adjusted during render so the
  // very first paint of the lens already targets a run — as an effect it cost a
  // whole render showing the "no snapshot picked" skeleton. `!== newestRunId`
  // keeps it self-terminating: with an EMPTY chain both sides are null, so this
  // settles instead of re-queueing the same null every pass.
  const newestRunId = runsData?.runs[0]?.run_id ?? null;
  if (
    effLens === 'run' &&
    runsData &&
    selectedRunId !== newestRunId &&
    (selectedRunId == null || !runsData.runs.some((r) => r.run_id === selectedRunId))
  ) {
    setSelectedRunId(newestRunId);
  }

  const params: WorkflowDataParams = useMemo(() => {
    if (isNested) return { sort_by: 'run_at', sort_dir: 'desc', limit: NESTED_RECORD_LIMIT, offset: 0 };
    const base: WorkflowDataParams = {
      q: q || undefined,
      filters: filters.length ? filters : undefined,
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
      // Omitting sort_by keeps the lens's server default (Current: change rank).
      sort_by: sortBy ?? (effLens === 'all' ? 'run_at' : undefined),
      sort_dir: sortBy || effLens === 'all' ? sortDir : undefined,
    };
    if (effLens === 'latest') {
      return {
        ...base,
        view: 'latest',
        include_missing: includeMissing,
        key: keyOverride ?? undefined,
        source: sourceFilter ?? undefined,
      };
    }
    if (effLens === 'run') {
      return {
        ...base,
        view: 'run',
        run_id: selectedRunId ?? undefined,
        key: keyOverride ?? pinnedKey ?? undefined,
        source: sourceFilter ?? undefined,
      };
    }
    return base;
  }, [isNested, q, filters, sortBy, sortDir, page, effLens, includeMissing, keyOverride, pinnedKey, selectedRunId, sourceFilter]);

  const paramKey = useMemo(() => JSON.stringify(params), [params]);
  const isFirstView = !isNested && page === 0 && !q && filters.length === 0;
  // view=run can't fetch until the snapshot index has picked a run.
  const tableEnabled = effLens !== 'run' || selectedRunId != null;

  const { data, loading, refresh } = useQuery(
    Q.workflowData(workflowId, paramKey),
    () => workflowDataApi.getTable(workflowId, params),
    // Poll the default first view so freshly-completed runs appear; pause once
    // the user is searching/paging (a moving table under them is jarring).
    { pollInterval: isFirstView ? 30_000 : undefined, enabled: tableEnabled },
  );

  // A lineage-lens response without an identity echo = backend predates the API.
  // Same once-per-response adoption as the /data/runs probe above, and it still
  // runs after it, so a table response keeps winning when both land together.
  const [probedData, setProbedData] = useState<typeof data>(null);
  if (data && lensIsLineage && data !== probedData) {
    setProbedData(data);
    setLineageSupported(Boolean(data.identity));
  }

  const identity = (lensIsLineage ? data?.identity : undefined) ?? runsData?.identity ?? null;
  const counts = effLens === 'latest' ? data?.counts : undefined;

  // Pin the resolved identity so follow-up calls (runs / history / delete)
  // can't drift when a new run lands mid-session. echoKey remembers the fields
  // for EVERY mode (the /data/runs echo); pinnedKey only auto-picks (or a
  // user-chosen explicit key), because it also feeds the view=run table request
  // — echoing hash fields there would flip the response mode to "explicit" and
  // lose the hash-mode identity-chip copy.
  // Both writes compare CONTENT against what is already pinned, so they settle
  // in a single pass; running them during render (rather than in an effect that
  // re-ran on the same compare) just drops the wasted pass.
  if (identity) {
    const f = identity.fields;
    if (f.length && echoKey?.join(',') !== f.join(',')) setEchoKey(f);
    if (
      f.length &&
      (identity.mode === 'auto' || (identity.mode === 'explicit' && keyOverride)) &&
      pinnedKey?.join(',') !== f.join(',')
    ) {
      setPinnedKey(f);
    }
  }

  // Singleton datasets (one record per run) default to the By-date lens.
  if (!lensChosen && identity?.mode === 'singleton' && lens !== 'run') setLens('run');

  // The server rejected a stored custom key (fields mostly empty) → drop it.
  // Clearing the override makes this self-terminating (the next render fails the
  // `keyOverride` guard).
  const rejectedKeyFields = identity?.requested_key_rejected;
  if (rejectedKeyFields?.length && keyOverride) {
    setKeyRejectedNotice(rejectedKeyFields.join(', '));
    setKeyOverride(null);
  }
  // Dropping the PERSISTED copy is a write to an external system, which is what
  // an effect is actually for — only the state half above had to leave it.
  useEffect(() => {
    if (!keyRejectedNotice) return;
    try {
      localStorage.removeItem(keyStoreKey(workflowId));
    } catch {
      /* ignore */
    }
  }, [keyRejectedNotice, workflowId]);

  // Newest snapshot came back empty: show last known data (dimmed) instead of
  // an empty grid. Auto-enable once so the user's own toggle still sticks — the
  // one-shot flag is what stops this from re-firing after they turn it back off.
  if (
    effLens === 'latest' &&
    counts &&
    !autoMissingDone &&
    !includeMissing &&
    counts.new + counts.changed + counts.same === 0 &&
    counts.missing > 0
  ) {
    setAutoMissingDone(true);
    setIncludeMissing(true);
  }

  const switchLens = useCallback(
    (next: DataView) => {
      setLens(next);
      setLensChosen(true);
      try {
        localStorage.setItem(lensStoreKey(workflowId), next);
      } catch {
        /* ignore */
      }
      // Picking a lens resets scope to Records; sort returns to the lens default.
      setScope('records');
      setSortBy(next === 'all' ? 'run_at' : null);
      setSortDir('desc');
      setPage(0);
      setExpanded(null);
      setSourceFilter(null);
    },
    [workflowId],
  );

  // Facets (column types, distinct values, numeric ranges) for the smart
  // filters — computed over the ACTIVE lens's rowset (spec 1.5(4)) so the
  // filters and column-type signals describe exactly the rows on screen.
  // Nested scopes and All rows keep the plain all-rows facets; page/sort/search
  // never enter (facets describe the whole rowset, not the visible page).
  const facetParams: WorkflowDataParams = useMemo(() => {
    if (isNested || !lensIsLineage) return {};
    if (effLens === 'latest') {
      return {
        view: 'latest',
        include_missing: includeMissing,
        key: keyOverride ?? undefined,
        source: sourceFilter ?? undefined,
      };
    }
    return {
      view: 'run',
      run_id: selectedRunId ?? undefined,
      key: keyOverride ?? pinnedKey ?? undefined,
      source: sourceFilter ?? undefined,
    };
  }, [isNested, lensIsLineage, effLens, includeMissing, keyOverride, pinnedKey, selectedRunId, sourceFilter]);
  const facetKey = useMemo(() => JSON.stringify(facetParams), [facetParams]);
  const { data: facetData, refresh: refreshFacets } = useQuery(
    Q.workflowDataFacets(workflowId, facetKey === '{}' ? undefined : facetKey),
    () => workflowDataApi.getFacets(workflowId, facetParams),
    { silent: true, enabled: tableEnabled },
  );
  const facets: Record<string, ColumnFacet> = useMemo(() => facetData?.facets || {}, [facetData]);

  const columns = useMemo(() => data?.columns || [], [data]);
  const rows = useMemo(() => data?.rows || [], [data]);
  const total = data?.total ?? 0;

  // Collections a record holds, for the scope picker.
  const collections = useMemo(() => discoverCollections(rows), [rows]);
  // Drop a nested scope that no longer exists (e.g. after switching workflows) —
  // but ONLY once its data has actually loaded. Selecting a collection changes the
  // query key, so `rows` is briefly empty during that refetch; reverting then would
  // bounce the scope back to Records (a flicker that needs a second click). Gate on
  // a settled, non-empty result so we only revert a genuinely-absent collection.
  // Reverting during render is self-terminating (`records` is never nested) and
  // spares the grid a frame rendered against the vanished scope.
  if (isNested && !loading && rows.length > 0 && !collections.some((c) => c.path === scope)) {
    setScope('records');
  }

  // Top-level records as view rows. Lineage rows key by uid (stable across the
  // dedup — a Current row's backing run/index moves as new versions land).
  const recordViewRows: ViewRow[] = useMemo(
    () =>
      rows.map((r) => ({
        key: r._lineage?.uid ? `uid:${r._lineage.uid}` : `${r.run_id}-${r.record_index}`,
        fields: r.fields || {},
        run_id: r.run_id,
        run_at: r.run_at,
        status: r.status,
        record_index: r.record_index,
        lineage: r._lineage,
      })),
    [rows],
  );

  // By date: records the previous snapshot had but this one doesn't — rendered
  // as a dimmed trailing group (display-only: not selectable, not expandable).
  const removedViewRows: ViewRow[] = useMemo(() => {
    if (effLens !== 'run' || !data?.removed_records?.length) return [];
    return data.removed_records.map((r) => ({
      key: `removed:${r.uid}`,
      fields: r.fields || {},
      run_id: data.prev_run_id ?? -1,
      run_at: null,
      status: null,
      record_index: -1,
      removed: true,
    }));
  }, [effLens, data]);

  // Nested collection flattened from the loaded records.
  const nestedAll: ViewRow[] = useMemo(() => {
    if (!isNested) return [];
    const out: ViewRow[] = [];
    for (const r of rows) {
      const arr = resolvePath(r.fields || {}, scope);
      if (Array.isArray(arr)) {
        arr.forEach((item, i) => {
          if (isPlainObj(item)) {
            out.push({
              key: `${r.run_id}-${r.record_index}::${scope}#${i}`,
              fields: item,
              run_id: r.run_id,
              run_at: r.run_at,
              status: r.status,
              record_index: i,
            });
          }
        });
      }
    }
    return out;
  }, [isNested, rows, scope]);

  const nestedColumns = useMemo(() => unionColumns(nestedAll.map((v) => v.fields)), [nestedAll]);

  // Client-side search + sort for the nested scope.
  const nestedSorted: ViewRow[] = useMemo(() => {
    if (!isNested) return [];
    let list = nestedAll;
    // Use the DEBOUNCED search (`q`), not the raw input, so we don't re-filter +
    // re-sort the whole flattened collection (up to NESTED_RECORD_LIMIT records) on
    // every keystroke — matching the records path, which already searches on `q`.
    const needle = q.trim().toLowerCase();
    if (needle) {
      list = list.filter((v) => nestedColumns.some((c) => previewText(v.fields[c]).toLowerCase().includes(needle)));
    }
    if (nestedSort.by) {
      const col = nestedSort.by;
      list = [...list].sort((a, b) => {
        const av = a.fields[col];
        const bv = b.fields[col];
        let cmp: number;
        if (typeof av === 'number' && typeof bv === 'number') cmp = av - bv;
        else cmp = previewText(av).localeCompare(previewText(bv));
        return nestedSort.dir === 'asc' ? cmp : -cmp;
      });
    }
    return list;
  }, [isNested, nestedAll, q, nestedColumns, nestedSort]);

  // Unified view: columns, the full in-scope list (for select-all + export),
  // the visible page, and pagination/totals — server-driven for records,
  // client-driven for nested.
  const viewColumns = isNested ? nestedColumns : columns;
  const allInScope: ViewRow[] = isNested ? nestedSorted : recordViewRows;
  const viewRows: ViewRow[] = isNested
    ? nestedSorted.slice(nestedPage * PAGE_SIZE, nestedPage * PAGE_SIZE + PAGE_SIZE)
    : recordViewRows;
  const viewTotal = isNested ? nestedSorted.length : total;
  const curPage = isNested ? nestedPage : page;
  const start = viewTotal === 0 ? 0 : curPage * PAGE_SIZE + 1;
  const end = curPage * PAGE_SIZE + viewRows.length;
  const hasNext = end < viewTotal;
  const hasPrev = curPage > 0;

  const hasFiltersOrSearch = !!searchInput || filters.length > 0;
  const roles = useMemo(() => pickColumnRoles(viewColumns, viewRows), [viewColumns, viewRows]);

  // Column intelligence: per-column stats → signal-ranked order → the
  // 8-column cap. An explicit Fields-manager choice always wins the heuristic.
  const colStats = useMemo(
    () =>
      computeColumnStats(viewColumns, allInScope, isNested ? {} : facets, isNested ? 0 : facetData?.row_count ?? 0),
    [viewColumns, allInScope, isNested, facets, facetData],
  );
  const rankedColumns = useMemo(() => rankColumns(viewColumns, colStats, roles), [viewColumns, colStats, roles]);
  const visibleColumns = useMemo(() => {
    if (fieldPrefs) {
      const set = new Set(fieldPrefs);
      const kept = rankedColumns.filter((c) => set.has(c));
      if (kept.length > 0) return kept;
    }
    return rankedColumns.slice(0, MAX_VISIBLE_COLS);
  }, [rankedColumns, fieldPrefs]);
  const hiddenColCount = rankedColumns.length - visibleColumns.length;

  const applyColPrefs = useCallback(
    (cols: string[] | null) => {
      setFieldPrefs(cols);
      if (scope !== 'records') return;
      try {
        if (cols && cols.length) localStorage.setItem(colsStoreKey(workflowId), JSON.stringify(cols));
        else localStorage.removeItem(colsStoreKey(workflowId));
      } catch {
        /* ignore */
      }
    },
    [workflowId, scope],
  );
  const toggleColumnVisible = (col: string) => {
    const cur = new Set(visibleColumns);
    if (cur.has(col)) {
      if (cur.size === 1) return; // never hide the last data column
      cur.delete(col);
    } else {
      cur.add(col);
    }
    applyColPrefs(rankedColumns.filter((c) => cur.has(c)));
  };

  // Title fallback chain: an empty title cell shows the next non-empty ranked
  // column's value (rendered secondary) so the row anchor is never blank.
  const titleFallbackFor = useCallback(
    (fields: Record<string, unknown>): string | undefined => {
      for (const c of rankedColumns) {
        if (c === roles.titleCol || c === roles.imageCol) continue;
        const v = fields[c];
        if (v !== null && v !== undefined && v !== '') return previewText(v);
      }
      return undefined;
    },
    [rankedColumns, roles],
  );

  // Anchor for Shift+click range selection. Set on plain / Cmd click; used by
  // Shift+click to pick every row between anchor and the newly-clicked one.
  const [anchorKey, setAnchorKey] = useState<string | null>(null);

  // Clear selection + drop the anchor when the in-scope set changes meaningfully.
  // Done during render so the action bar can never paint a count for rows that
  // are no longer on screen.
  const filtersKey = useMemo(() => JSON.stringify(filters), [filters]);
  if (useDepsChanged([scope, searchInput, filtersKey, workflowId, effLens, selectedRunId, sourceFilter])) {
    setSelected(new Set());
    setAnchorKey(null);
  }

  const selectedRows = useMemo(() => allInScope.filter((v) => selected.has(v.key)), [allInScope, selected]);
  const allSelected = allInScope.length > 0 && selectedRows.length === allInScope.length;
  const someSelected = selectedRows.length > 0 && !allSelected;

  // Drag-paint session: mousedown on a checkbox opens it; every checkbox the
  // pointer enters flips to the same "add" / "remove" state (decided by the
  // starting row's initial selection).
  const [dragMode, setDragMode] = useState<'add' | 'remove' | null>(null);
  const dragModeRef = useRef<'add' | 'remove' | null>(null);
  dragModeRef.current = dragMode;

  // Stable so the memoized RecordCard/rows don't re-render every parent render.
  const toggleRow = useCallback((key: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    }), []);
  // Stable expand toggle (functional update so it doesn't close over `expanded`).
  const toggleExpanded = useCallback(
    (key: string) => setExpanded((prev) => (prev === key ? null : key)),
    [],
  );
  const toggleAll = () => {
    setSelected((prev) => (prev.size >= allInScope.length ? new Set() : new Set(allInScope.map((v) => v.key))));
    setAnchorKey(null);
  };

  // In-scope order → range from anchor to key (inclusive). No anchor → plain toggle.
  const allInScopeKeys = useMemo(() => allInScope.map((v) => v.key), [allInScope]);
  const selectRange = useCallback(
    (fromKey: string, toKey: string) => {
      const fi = allInScopeKeys.indexOf(fromKey);
      const ti = allInScopeKeys.indexOf(toKey);
      if (fi < 0 || ti < 0) {
        toggleRow(toKey);
        return;
      }
      const [lo, hi] = fi <= ti ? [fi, ti] : [ti, fi];
      const range = allInScopeKeys.slice(lo, hi + 1);
      setSelected((prev) => {
        const next = new Set(prev);
        for (const k of range) next.add(k);
        return next;
      });
    },
    [allInScopeKeys, toggleRow],
  );

  // Modifier-aware select: Shift → range from anchor, plain / Cmd / Ctrl → toggle
  // (and move the anchor to the row that was clicked).
  const handleRowSelect = useCallback(
    (key: string, e?: React.MouseEvent | React.KeyboardEvent) => {
      const shift = !!e && 'shiftKey' in e && e.shiftKey;
      if (shift && anchorKey && anchorKey !== key) {
        selectRange(anchorKey, key);
        return;
      }
      toggleRow(key);
      setAnchorKey(key);
    },
    [anchorKey, selectRange, toggleRow],
  );

  // Drag-paint. Starting row's current state → mode: selected → deselect, else select.
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const handleRowMouseDown = useCallback((key: string, e: React.MouseEvent) => {
    if (e.button !== 0 || e.shiftKey) return;
    const startsSelected = selectedRef.current.has(key);
    setDragMode(startsSelected ? 'remove' : 'add');
  }, []);
  const handleRowMouseEnter = useCallback((key: string) => {
    const mode = dragModeRef.current;
    if (!mode) return;
    setSelected((prev) => {
      const next = new Set(prev);
      if (mode === 'add') next.add(key);
      else next.delete(key);
      return next;
    });
  }, []);
  useEffect(() => {
    if (!dragMode) return;
    const stop = () => setDragMode(null);
    document.addEventListener('mouseup', stop);
    return () => document.removeEventListener('mouseup', stop);
  }, [dragMode]);

  const clauseFor = (col: string) => filters.find((f) => f.col === col);
  const setClauseFor = (col: string, clause: FilterClause | null) => {
    setFilters((prev) => {
      const rest = prev.filter((f) => f.col !== col);
      return clause ? [...rest, clause] : rest;
    });
  };
  // Active sort (server for records, client for nested).
  const activeSortBy = isNested ? nestedSort.by : sortBy;
  const activeSortDir = isNested ? nestedSort.dir : sortDir;
  const sortDirFor = (col: string): 'asc' | 'desc' | null => (activeSortBy === col ? activeSortDir : null);
  const setSort = (col: string, dir: 'asc' | 'desc') => {
    if (isNested) setNestedSort({ by: col, dir });
    else {
      setSortBy(col);
      setSortDir(dir);
    }
  };
  const toggleSort = (col: string) => {
    if (activeSortBy === col) setSort(col, activeSortDir === 'asc' ? 'desc' : 'asc');
    else setSort(col, ['run_at', 'first_seen_at', 'last_seen_at', 'versions'].includes(col) ? 'desc' : 'asc');
  };

  // First snapshot: nothing to compare against yet — suppress all change UI.
  const chainCount = runsData?.runs.length ?? null;
  const quietFirstSnapshot = lensIsLineage && (chainCount != null ? chainCount <= 1 : data?.scanned_runs === 1);
  // Lineage machinery (uids, history, delete-by-uid) is live past the first
  // snapshot…
  const lineageActive = lensIsLineage && !quietFirstSnapshot;
  // …but change CHROME (dot column, pills, tint, sort chip) only earns its
  // pixels when the loaded window actually has something to say.
  const windowHasSignal =
    effLens === 'latest'
      ? !!counts && counts.new + counts.changed + counts.missing > 0
      : !!data?.delta && data.delta.new + data.delta.changed + data.delta.removed > 0;
  const showChangeUi = lineageActive && windowHasSignal;

  // Per-type width defaults; every column's floor fits its header label so
  // header text never truncates (user resize honors the same floor).
  const defaultColWidth = (col: string): number => {
    const s = colStats.get(col);
    let w: number;
    if (col === roles.titleCol || s?.type === 'url') w = 240;
    else if (s?.type === 'json') w = 130;
    else if (s?.type === 'boolean' || s?.type === 'image') w = 80;
    else if (s?.type === 'number' || s?.type === 'date') w = 100;
    else w = s && s.avgLen > 0 && s.avgLen <= 12 ? 140 : 200;
    return Math.max(w, headerMinWidth(col));
  };
  const widthFor = (col: string) => colWidths[col] ?? defaultColWidth(col);
  // Provenance columns live behind the Run info toggle in every lens; Current
  // swaps run provenance for lineage provenance (Run#/Status live in the
  // expanded record's "View run" link instead).
  const metaCols: { key: string; label: string; width: number; filterable: boolean }[] = !showRunInfo
    ? []
    : effLens === 'latest'
      ? [
          { key: 'first_seen_at', label: t('First seen'), width: W_WHEN, filterable: false },
          { key: 'last_seen_at', label: t('Last seen'), width: W_WHEN, filterable: false },
          { key: 'versions', label: t('Versions'), width: W_VERSIONS, filterable: false },
        ]
      : [
          { key: 'run_id', label: t('Run'), width: W_RUN, filterable: false },
          { key: 'run_at', label: t('Date'), width: W_WHEN, filterable: false },
          { key: 'status', label: t('Status'), width: W_STATUS, filterable: true },
        ];
  const totalWidth =
    W_GUTTER +
    (showChangeUi ? W_DOT : 0) +
    visibleColumns.reduce((s, c) => s + widthFor(c), 0) +
    metaCols.reduce((s, m) => s + m.width, 0);

  // Holds the teardown for an in-progress column drag so it can be detached if the
  // component unmounts mid-drag (navigating away before mouseup) — otherwise the
  // document-level listeners leak and onMove keeps calling setColWidths on a gone
  // component, and `body.userSelect` stays pinned to 'none'.
  const resizeCleanupRef = useRef<(() => void) | null>(null);
  useEffect(() => () => resizeCleanupRef.current?.(), []);

  const startResize = (col: string) => (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const startW = widthFor(col);
    // Resizing can shrink past the type default but never truncates the header.
    const minW = Math.max(MIN_COL, headerMinWidth(col));
    const onMove = (ev: MouseEvent) =>
      setColWidths((p) => ({ ...p, [col]: Math.max(minW, startW + (ev.clientX - startX)) }));
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      document.body.style.userSelect = '';
      resizeCleanupRef.current = null;
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    document.body.style.userSelect = 'none';
    resizeCleanupRef.current = onUp;
  };

  const SortIcon: React.FC<{ col: string }> = ({ col }) => {
    if (activeSortBy !== col) return <ChevronUpDownIcon className="h-3 w-3 shrink-0 text-tertiary opacity-50" />;
    return activeSortDir === 'asc' ? (
      <ChevronUpIcon className="h-3 w-3 shrink-0 text-ink" />
    ) : (
      <ChevronDownIcon className="h-3 w-3 shrink-0 text-ink" />
    );
  };

  // A data/meta column header: click the label to sort, caret opens the menu
  // (smart filters — records scope only), drag the right edge to resize.
  const HeaderCell: React.FC<{
    col: string;
    label: string;
    filterable?: boolean;
    resizable?: boolean;
    showMenu?: boolean;
  }> = ({ col, label, filterable = true, resizable = true, showMenu = true }) => (
    <th className="relative overflow-hidden border-b border-border px-3 py-2 text-left align-middle font-medium text-tertiary">
      <div className="flex items-center gap-1">
        <button
          onClick={() => toggleSort(col)}
          className="flex min-w-0 items-center gap-1 hover:text-ink transition-colors"
          title={t('Sort by {{col}}', { col: label })}
        >
          <span className="truncate">{label}</span>
          <SortIcon col={col} />
        </button>
        <div className="flex-1" />
        {showMenu && (
          <ColumnFilterMenu
            col={col}
            facet={facets[col]}
            clause={clauseFor(col)}
            sortDir={sortDirFor(col)}
            onSort={(dir) => setSort(col, dir)}
            onApply={(clause) => setClauseFor(col, clause)}
            filterable={filterable}
          />
        )}
      </div>
      {resizable && (
        <div
          onMouseDown={startResize(col)}
          className="absolute right-0 top-0 h-full w-1.5 cursor-col-resize hover:bg-zinc-300/70"
        />
      )}
    </th>
  );

  const baseName =
    (data?.workflow_name || `workflow-${workflowId}`)
      .replace(/[^A-Za-z0-9._-]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 60) || 'workflow';

  // Whole-dataset export. Records scope downloads the full set from the server
  // (exportScope 'lens' reproduces the active lens; 'all' is always the flat
  // grid); a nested scope exports the in-scope items we hold client-side.
  const doExport = async (format: 'csv' | 'json', exportScope: 'lens' | 'all' = 'lens') => {
    if (isNested) {
      const objs = allInScope.map((v) => v.fields);
      const cols = unionColumns(objs, viewColumns);
      const text = format === 'csv' ? recordsToCsv(objs, cols) : recordsToJson(objs);
      const blob = new Blob([text], { type: format === 'csv' ? 'text/csv;charset=utf-8' : 'application/json' });
      const safe = scope.replace(/[^A-Za-z0-9._-]+/g, '-');
      downloadBlob(blob, `${baseName}-${safe}.${format}`);
      return;
    }
    const lensExport = exportScope === 'lens' && lensIsLineage;
    setExporting(true);
    try {
      const blob = await workflowDataApi.exportData(workflowId, format, {
        q: q || undefined,
        filters: filters.length ? filters : undefined,
        sort_by: sortBy ?? undefined,
        sort_dir: sortBy || !lensExport ? sortDir : undefined,
        ...(lensExport && effLens === 'latest'
          ? { view: 'latest' as const, include_missing: includeMissing, key: keyOverride ?? undefined }
          : {}),
        ...(lensExport && effLens === 'run'
          ? { view: 'run' as const, run_id: selectedRunId ?? undefined, key: keyOverride ?? pinnedKey ?? undefined }
          : {}),
      });
      const suffix = !lensExport ? 'data' : effLens === 'latest' ? 'current' : `run${selectedRunId}`;
      downloadBlob(blob, `${baseName}-${suffix}.${format}`);
    } catch {
      toast.error(t('Export failed'));
    } finally {
      setExporting(false);
    }
  };

  // Export / copy just the selected rows (client-side, always exactly what's
  // selected — no hidden truncation).
  const exportSelected = (format: 'csv' | 'json') => {
    const objs = selectedRows.map((v) => v.fields);
    const cols = unionColumns(objs, viewColumns);
    const text = format === 'csv' ? recordsToCsv(objs, cols) : recordsToJson(objs);
    const blob = new Blob([text], { type: format === 'csv' ? 'text/csv;charset=utf-8' : 'application/json' });
    downloadBlob(blob, `${baseName}-selected.${format}`);
  };
  const copySelected = async (fmt: 'json' | 'tsv' | 'md') => {
    const objs = selectedRows.map((v) => v.fields);
    const cols = unionColumns(objs, viewColumns);
    const text = fmt === 'json' ? recordsToJson(objs) : fmt === 'tsv' ? recordsToTsv(objs, cols) : recordsToMarkdown(objs, cols);
    const ok = await copyToClipboard(text);
    if (ok) toast.success(t('Copied {{n}} records', { n: objs.length }));
    else toast.error(t('Copy failed'));
  };

  // Send the selected rows to another workflow: one run per record, the
  // record's fields supplied as the run's input form data.
  const openSend = async () => {
    setSendOpen(true);
    if (sendTargets === null) {
      try {
        const list = await automationApi.listWorkflows();
        setSendTargets(list);
      } catch {
        setSendTargets([]);
      }
    }
  };
  const doSend = async () => {
    if (!sendTargetId) return;
    setSending(true);
    const payloads = selectedRows.map((v) => {
      const formData: Record<string, string> = {};
      for (const [k, val] of Object.entries(v.fields)) {
        if (val === null || val === undefined || val === '') continue;
        formData[k] = typeof val === 'string' ? val : typeof val === 'object' ? JSON.stringify(val) : String(val);
      }
      return formData;
    });
    // Fire with bounded concurrency so a large selection doesn't stampede.
    let ok = 0;
    let fail = 0;
    const queue = [...payloads];
    const worker = async () => {
      for (;;) {
        const fd = queue.shift();
        if (!fd) return;
        try {
          await automationApi.runWorkflowWithData(sendTargetId, 'auto', fd);
          ok += 1;
        } catch {
          fail += 1;
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(4, payloads.length) }, worker));
    setSending(false);
    setSendOpen(false);
    if (fail === 0) toast.success(t('Started {{n}} runs', { n: ok }));
    else toast.error(t('Started {{ok}} runs · {{fail}} failed', { ok, fail }));
  };

  // Ask before deleting: extracted data is derived from run records, so removing
  // a row is destructive (the underlying run's ``extracted_data`` is edited).
  const askDeleteSelected = () => {
    if (selectedRows.length === 0) return;
    setDeleteRows(selectedRows);
    setDeleteOpen(true);
  };
  const askDeleteOne = useCallback((row: ViewRow) => {
    setDeleteRows([row]);
    setDeleteOpen(true);
  }, []);
  // Current-lens delete removes EVERY stored version of the records (by uid,
  // echoing the response's identity so resolution can't drift); other lenses
  // keep the single-row (run_id, record_index) semantics.
  const deleteByUid = effLens === 'latest' && (deleteRows || []).length > 0 && (deleteRows || []).every((r) => r.lineage?.uid);
  const deleteVersionTotal = (deleteRows || []).reduce((s, r) => s + (r.lineage?.versions ?? 1), 0);
  const doDelete = async () => {
    const rows = deleteRows || [];
    if (rows.length === 0) {
      setDeleteOpen(false);
      return;
    }
    setDeleting(true);
    try {
      if (deleteByUid) {
        const res = await workflowDataApi.deleteRows(workflowId, {
          record_uids: rows.map((r) => r.lineage!.uid),
          key: identity?.fields?.length ? identity.fields : keyOverride ?? pinnedKey ?? undefined,
        });
        if (res.unmatched && res.unmatched.length > 0) {
          toast.error(t('Some records changed since loading — refresh and retry.'));
        } else {
          toast.success(t('Deleted {{n}} records', { n: rows.length }));
        }
      } else {
        const refs = rows.filter((r) => !r.removed).map((r) => ({ run_id: r.run_id, record_index: r.record_index }));
        const { deleted } = await workflowDataApi.deleteRows(workflowId, { records: refs });
        toast.success(t('Deleted {{n}} records', { n: deleted }));
      }
      const goneKeys = new Set(rows.map((r) => r.key));
      setSelected((prev) => new Set([...prev].filter((k) => !goneKeys.has(k))));
      setExpanded((cur) => (cur && goneKeys.has(cur) ? null : cur));
      refresh();
      refreshFacets();
      refreshRuns();
    } catch {
      toast.error(t('Delete failed'));
    } finally {
      setDeleting(false);
      setDeleteOpen(false);
      setDeleteRows(null);
    }
  };

  const clearAll = () => {
    setSearchInput('');
    setFilters([]);
  };

  // Jump from a record's nested field straight into the actionable collection.
  const openCollection = useCallback((path: string) => {
    setScope(path);
    setExpanded(null);
  }, []);

  // The public REST surface for this dataset. The dashboard host serves it under
  // `/api/v1`; a dataset id == its workflow id, so every capability below is
  // reachable with a `workflows:read` API key.
  const apiBase = `${typeof window !== 'undefined' ? window.location.origin : 'https://your-instance.com'}/api/v1`;
  const keyPlaceholder = 'wt_YOUR_KEY';

  // A copy-paste-ready curl for GET /api/v1/workflows/{id}/data that reproduces
  // the current view (search + smart filters + sort).
  const apiSnippet = useMemo(() => {
    const parts: [string, string][] = [];
    // In a nested scope, reproduce it with ?collection=<path> + the client search/sort.
    if (isNested) parts.push(['collection', scope]);
    // Non-all lenses reproduce the change-tracked view.
    if (!isNested && effLens === 'latest') {
      parts.push(['view', 'latest']);
      if (includeMissing) parts.push(['include_missing', 'true']);
      if (keyOverride?.length) parts.push(['key', keyOverride.join(',')]);
    } else if (!isNested && effLens === 'run' && selectedRunId != null) {
      parts.push(['view', 'run'], ['run_id', String(selectedRunId)]);
      if (keyOverride?.length) parts.push(['key', keyOverride.join(',')]);
    }
    if (!isNested && (effLens === 'latest' || effLens === 'run') && sourceFilter != null) {
      parts.push(['source', sourceFilter]);
    }
    const effQ = isNested ? searchInput.trim() : q;
    if (effQ) parts.push(['q', effQ]);
    if (!isNested && filters.length) parts.push(['filters', JSON.stringify(filters)]);
    const effSortBy = isNested ? nestedSort.by : sortBy;
    const effSortDir = isNested ? nestedSort.dir : sortDir;
    if (effSortBy) parts.push(['sort_by', effSortBy]);
    if (effSortBy || effLens === 'all' || isNested) parts.push(['sort_dir', effSortDir]);
    parts.push(['limit', String(PAGE_SIZE)]);
    const esc = (s: string) => s.replace(/'/g, `'\\''`);
    const segs = [
      `curl -G "${apiBase}/workflows/${workflowId}/data"`,
      `-H "Authorization: Bearer ${keyPlaceholder}"`,
      ...parts.map(([k, v]) => `--data-urlencode '${k}=${esc(v)}'`),
    ];
    return segs.join(' \\\n  ');
  }, [apiBase, keyPlaceholder, workflowId, q, filters, sortBy, sortDir, isNested, scope, searchInput, nestedSort, effLens, includeMissing, keyOverride, selectedRunId, sourceFilter]);




  // A 0-record newest snapshot with survivors is NOT an empty dataset — the
  // auto-enabled "Include gone" refetch is about to surface the dimmed records,
  // and an explicit-empty snapshot still has its removed group to show.
  const pristineEmpty =
    !loading &&
    viewTotal === 0 &&
    !hasFiltersOrSearch &&
    sourceFilter == null &&
    !isNested &&
    !(counts && counts.missing > 0) &&
    removedViewRows.length === 0;

  // Reconciliation figures: Σ record_count over the chain = the All-rows total.
  const rowsTotal = useMemo(
    () => (runsData ? runsData.runs.reduce((s, r) => s + r.record_count, 0) : null),
    [runsData],
  );
  const uniqueRecords = counts ? counts.new + counts.changed + counts.same + counts.missing : null;
  const prevRun =
    effLens === 'run' && data?.prev_run_id != null ? runsData?.runs.find((r) => r.run_id === data.prev_run_id) : undefined;

  // Mixed-schema datasets: one chip per originating list key. Rendered only
  // when ≥2 sources have records (single-source = zero new chrome; older
  // backends omit `sources` entirely). Kept while a source filter is active so
  // there's always a way back to All.
  const sourceChips = useMemo(() => {
    if (!lensIsLineage || isNested) return null;
    const src = data?.sources;
    if (!src) return null;
    const entries = Object.entries(src).filter(([, n]) => n > 0);
    if (entries.length < 2 && sourceFilter == null) return null;
    entries.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    return { entries, total: entries.reduce((s, [, n]) => s + n, 0) };
  }, [data?.sources, lensIsLineage, isNested, sourceFilter]);

  // Identity echo for /history + delete-by-uid: always the fields the visible
  // uids were computed with (override > pinned auto as a fallback).
  const historyCtx = useMemo(
    () =>
      lineageActive
        ? {
            workflowId,
            keyFields: identity?.fields?.length ? identity.fields : keyOverride ?? pinnedKey ?? [],
            truncated: data?.truncated,
            scannedRuns: data?.scanned_runs,
          }
        : null,
    [lineageActive, workflowId, identity, keyOverride, pinnedKey, data?.truncated, data?.scanned_runs],
  );

  const applyKey = useCallback(
    (fields: string[] | null) => {
      setKeyRejectedNotice(null);
      setKeyOverride(fields);
      try {
        if (fields?.length) localStorage.setItem(keyStoreKey(workflowId), fields.join(','));
        else localStorage.removeItem(keyStoreKey(workflowId));
      } catch {
        /* ignore */
      }
    },
    [workflowId],
  );

  // Full-screen housekeeping: Escape closes, and the page behind stops scrolling.
  // Escape is bound on the DOCUMENT, not the overlay, because the user reaches
  // full screen by clicking a toolbar button and focus can easily be nowhere
  // inside the grid. It yields to whatever is layered on top of it — an open
  // modal, or a live selection (which spends Escape clearing itself first).
  const escBlocked = sendOpen || deleteOpen || apiModalOpen || selected.size > 0;
  useEffect(() => {
    if (!maximized) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !escBlocked) setMaximized(false);
    };
    document.addEventListener('keydown', onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [maximized, escBlocked]);

  // Pagination + provenance footer, shared by the cards and table views.
  const footer = (
    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-3 py-2 text-[11px] text-tertiary">
      <div className="flex items-center gap-2">
        <span>
          {viewTotal > 0 ? t('Showing {{start}}–{{end}} of {{total}}', { start, end, total: viewTotal }) : t('No rows')}
        </span>
        {!isNested && data?.truncated && (
          <span className="rounded bg-amber-50 px-1.5 py-0.5 text-amber-700">
            {t('latest {{n}} runs', { n: data.scanned_runs })}
          </span>
        )}
        {isNested && (
          <span className="text-tertiary/70">{t('from {{n}} loaded records', { n: rows.length })}</span>
        )}
        {!isNested && data && !data.declared && columns.length > 0 && (
          <span className="text-tertiary/70">{t('columns inferred from runs')}</span>
        )}
      </div>
      <div className="flex items-center gap-1">
        <button
          disabled={!hasPrev}
          onClick={() => (isNested ? setNestedPage((p) => Math.max(0, p - 1)) : setPage((p) => Math.max(0, p - 1)))}
          className="rounded-md border border-border px-2 py-1 font-medium text-secondary hover:text-ink disabled:opacity-40"
        >
          {t('Prev')}
        </button>
        <button
          disabled={!hasNext}
          onClick={() => (isNested ? setNestedPage((p) => p + 1) : setPage((p) => p + 1))}
          className="rounded-md border border-border px-2 py-1 font-medium text-secondary hover:text-ink disabled:opacity-40"
        >
          {t('Next')}
        </button>
      </div>
    </div>
  );

  // Table-scoped keyboard shortcuts: Cmd/Ctrl+A → select all in-scope rows,
  // Escape → clear selection, Delete/Backspace → open the delete confirm. Bound
  // on the outer container; skipped when focus is inside a text input so users
  // can still type freely in the search box + modals.
  const handleContainerKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement | null;
    const tag = target?.tagName?.toLowerCase() ?? '';
    const isEditable = tag === 'input' || tag === 'textarea' || target?.isContentEditable === true;
    if (isEditable) return;

    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'a') {
      if (allInScope.length === 0) return;
      e.preventDefault();
      setSelected(new Set(allInScope.map((v) => v.key)));
      setAnchorKey(allInScope[allInScope.length - 1]?.key ?? null);
      return;
    }
    if (e.key === 'Escape' && selected.size > 0) {
      e.preventDefault();
      setSelected(new Set());
      return;
    }
    if ((e.key === 'Delete' || e.key === 'Backspace') && selectedRows.length > 0 && !isNested) {
      e.preventDefault();
      setDeleteRows(selectedRows);
      setDeleteOpen(true);
      return;
    }
  };

  // Everything below the chrome, shared by the inline grid and the full-screen
  // one. A fragment, so the wrapper can be a `space-y-3` block (inline) or a
  // height-owning flex column (full screen) without duplicating the tree.
  const body = (
    <>
      {/* Lens: Current (deduped) | By date (one snapshot) | All rows (flat).
          Hidden entirely on backends that predate change tracking — and for
          crawls, whose shards are one dataset, not temporal snapshots (locked to
          All rows above). */}
      {!pristineEmpty && lineageSupported === true && !isCrawl && (
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <div
              className="inline-flex overflow-hidden rounded-lg border border-border"
              title={isNested ? t('Change tracking works on top-level records') : undefined}
            >
              {(
                [
                  ['latest', t('Current data')],
                  ['run', t('By date')],
                  ['all', t('All rows')],
                ] as [DataView, string][]
              ).map(([id, label], idx) => (
                <button
                  key={id}
                  onClick={() => switchLens(id)}
                  disabled={isNested}
                  className={clsx(
                    'px-2.5 py-1.5 text-[12px] font-medium transition-colors disabled:opacity-40',
                    idx > 0 && 'border-l border-border',
                    effLens === id ? 'bg-hover text-ink' : 'text-secondary hover:text-ink',
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="flex-1" />
            {effLens === 'latest' && (counts?.missing ?? 0) > 0 && (
              <button onClick={() => setIncludeMissing((v) => !v)} className={shelfFilterChipClass(includeMissing)}>
                {t('Include gone ({{count}})', { count: counts?.missing ?? 0 })}
              </button>
            )}
            {lensIsLineage && identity && (
              <IdentityChip
                workflowId={workflowId}
                identity={identity}
                columns={columns}
                overrideKey={keyOverride}
                onApply={applyKey}
                rowsTotal={rowsTotal}
              />
            )}
          </div>

          {/* Reconciliation: one 12px line tying records ↔ runs ↔ rows together */}
          {effLens === 'latest' && uniqueRecords != null && chainCount != null && (
            <div className="flex flex-wrap items-center gap-2 text-[12px] text-secondary">
              <span>
                {t('{{records}} unique records from {{runs}} runs', { records: uniqueRecords, runs: chainCount })}{' '}
                <button
                  onClick={() => switchLens('all')}
                  className="text-tertiary underline-offset-2 hover:text-ink hover:underline"
                  title={t('Show every row from every run')}
                >
                  {t('({{count}} rows)', { count: rowsTotal ?? 0 })}
                </button>
              </span>
              {showChangeUi &&
                (sortBy === null ? (
                  <span className={shelfFilterChipClass(false)}>{t('Sorted by changes')}</span>
                ) : (
                  <button
                    onClick={() => setSortBy(null)}
                    className={shelfFilterChipClass(true)}
                    title={t('Restore the default change-first sort')}
                  >
                    {t('Sorted by {{col}}', { col: sortBy })}
                  </button>
                ))}
            </div>
          )}

          {/* By date: snapshot stepper + delta vs the previous snapshot */}
          {effLens === 'run' && runsData && runsData.runs.length > 0 && (
            <div className="flex flex-wrap items-center gap-3">
              <SnapshotNav runs={runsData.runs} selectedRunId={selectedRunId} onSelect={setSelectedRunId} />
              {data?.delta &&
                prevRun &&
                (data.delta.new + data.delta.changed + data.delta.removed === 0 ? (
                  <span className="text-[12px] text-secondary">
                    {t('vs {{date}}: no changes', { date: prevRun.run_at ? fmtRunDate(prevRun.run_at) : `#${prevRun.run_id}` })}
                  </span>
                ) : (
                  <span className="tabular-nums text-[12px] text-secondary">
                    {t('vs {{date}}: +{{new}} new · {{changed}} changed · {{removed}} removed', {
                      date: prevRun.run_at ? fmtRunDate(prevRun.run_at) : `#${prevRun.run_id}`,
                      new: data.delta.new,
                      changed: data.delta.changed,
                      removed: data.delta.removed,
                    })}
                  </span>
                ))}
            </div>
          )}

          {quietFirstSnapshot && (
            <p className="text-[11px] text-tertiary">{t('First snapshot — changes will appear after the next run')}</p>
          )}
          {keyRejectedNotice && (
            <p className="text-[11px] text-amber-700">
              {t('Your custom key ({{fields}}) is empty in most records — matching automatically instead.', {
                fields: keyRejectedNotice,
              })}
            </p>
          )}
          {effLens === 'latest' &&
            includeMissing &&
            counts &&
            counts.new + counts.changed + counts.same === 0 &&
            counts.missing > 0 && (
              <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-[12px] text-amber-800">
                {t('Latest run extracted 0 records — showing last known data, dimmed.')}
              </p>
            )}
        </div>
      )}

      {/* Collections bar: jump straight into any nested structure */}
      {!pristineEmpty && collections.length > 0 && (
        <div className="flex items-center gap-1.5 overflow-x-auto pb-1">
          <span className="inline-flex shrink-0 items-center gap-1 pr-1 text-[10px] font-semibold uppercase tracking-wide text-tertiary">
            <RectangleStackIcon className="h-3.5 w-3.5" />
            {t('Collections')}
          </span>
          <ScopeChip active={!isNested} label={t('Records')} count={total} onClick={() => setScope('records')} />
          {collections.map((c) => (
            <ScopeChip
              key={c.path}
              active={scope === c.path}
              label={c.path}
              count={c.count}
              mono
              onClick={() => setScope(c.path)}
            />
          ))}
        </div>
      )}

      {/* Overview: count + view toggle */}
      {!pristineEmpty && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="text-[12px] text-secondary">
            <span className="font-semibold text-ink">{viewTotal.toLocaleString(uiLocale())}</span>{' '}
            {isNested ? scope.split('.').pop() : t('records')}
            {viewColumns.length > 0 && (
              <>
                {' · '}
                <span className="font-semibold text-ink">{viewColumns.length}</span> {t('fields')}
              </>
            )}
          </div>
          <div className="flex items-center gap-2">
            <div className="inline-flex overflow-hidden rounded-lg border border-border">
              <button
                onClick={() => setView('cards')}
                className={clsx(
                  'inline-flex items-center gap-1 px-2.5 py-1.5 text-[12px] font-medium transition-colors',
                  view === 'cards' ? 'bg-hover text-ink' : 'text-secondary hover:text-ink',
                )}
                title={t('Card view')}
              >
                <Squares2X2Icon className="h-3.5 w-3.5" />
                <span className="hidden @pair/stage:inline">{t('Cards')}</span>
              </button>
              <button
                onClick={() => setView('table')}
                className={clsx(
                  'inline-flex items-center gap-1 border-l border-border px-2.5 py-1.5 text-[12px] font-medium transition-colors',
                  view === 'table' ? 'bg-hover text-ink' : 'text-secondary hover:text-ink',
                )}
                title={t('Table view')}
              >
                <TableCellsIcon className="h-3.5 w-3.5" />
                <span className="hidden @pair/stage:inline">{t('Table')}</span>
              </button>
            </div>
            {/* Hand the grid the whole window — the answer to a 12-column dataset
                in a half-width detail pane. */}
            <button
              onClick={() => setMaximized((v) => !v)}
              aria-pressed={maximized}
              className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-[12px] font-medium text-secondary transition-colors hover:text-ink"
              title={maximized ? t('Exit full screen') : t('Maximize')}
            >
              {maximized ? (
                <ArrowsPointingInIcon className="h-3.5 w-3.5" />
              ) : (
                <ArrowsPointingOutIcon className="h-3.5 w-3.5" />
              )}
              <span className="sr-only">{maximized ? t('Exit full screen') : t('Maximize')}</span>
            </button>
          </div>
        </div>
      )}

      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[180px]">
          <MagnifyingGlassIcon className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-tertiary" />
          <input
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            placeholder={t('Search extracted data…')}
            className="w-full rounded-lg border border-border bg-surface py-1.5 pl-8 pr-3 text-[12px] text-ink placeholder:text-tertiary focus:border-zinc-400 focus:outline-none"
          />
        </div>
        <button
          onClick={() => setShowRunInfo((v) => !v)}
          className={clsx(
            'inline-flex items-center gap-1 rounded-lg border px-2.5 py-1.5 text-[12px] font-medium transition-colors',
            showRunInfo ? 'border-zinc-400 bg-hover text-ink' : 'border-border text-secondary hover:text-ink',
          )}
          title={
            effLens === 'latest'
              ? t('Show first seen, last seen and versions columns')
              : t('Show run date, status and id columns')
          }
        >
          <ClockIcon className="h-3.5 w-3.5" />
          {t('Run info')}
        </button>
        {view === 'table' && rankedColumns.length > 0 && (
          <Popover
            width={264}
            trigger={
              <span
                className={clsx(
                  'inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-[12px] font-medium transition-colors',
                  hiddenColCount > 0 ? 'bg-hover text-ink' : 'text-secondary hover:text-ink',
                )}
                title={t('Choose which fields show as columns')}
              >
                <ViewColumnsIcon className="h-3.5 w-3.5" />
                {t('Fields')}
                {hiddenColCount > 0 && (
                  <span className="tabular-nums text-[10px] text-tertiary">
                    · {t('{{count}} hidden', { count: hiddenColCount })}
                  </span>
                )}
              </span>
            }
          >
            {() => (
              <div className="space-y-1">
                <div className="flex items-center justify-between gap-2 px-1 pb-1 pt-0.5">
                  <span className="text-[10px] font-semibold uppercase tracking-wide text-tertiary">{t('Fields')}</span>
                  <span className="flex items-center gap-2">
                    <button
                      onClick={() => applyColPrefs([...rankedColumns])}
                      className="text-[11px] font-medium text-tertiary transition-colors hover:text-ink"
                    >
                      {t('Show all')}
                    </button>
                    <button
                      onClick={() => applyColPrefs(null)}
                      className="text-[11px] font-medium text-tertiary transition-colors hover:text-ink"
                      title={t('Back to the automatic column pick')}
                    >
                      {t('Reset')}
                    </button>
                  </span>
                </div>
                <div className="max-h-72 space-y-0.5 overflow-y-auto">
                  {rankedColumns.map((c) => {
                    const on = visibleColumns.includes(c);
                    return (
                      <button
                        key={c}
                        onClick={() => toggleColumnVisible(c)}
                        className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12px] text-secondary transition-colors hover:bg-hover hover:text-ink"
                      >
                        <span
                          className={clsx(
                            'flex h-4 w-4 shrink-0 items-center justify-center rounded border',
                            on ? 'border-ink bg-ink text-white' : 'border-zinc-300 bg-surface',
                          )}
                        >
                          {on && <CheckIcon className="h-3 w-3" />}
                        </span>
                        <span className="min-w-0 flex-1 truncate">{c}</span>
                        <span className="shrink-0 tabular-nums text-[10px] text-tertiary">
                          {colStats.get(c)?.nonEmpty ?? 0}
                        </span>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </Popover>
        )}
        <button
          onClick={() => {
            refresh();
            refreshFacets();
            if (lineageSupported) refreshRuns();
          }}
          className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-[12px] font-medium text-secondary hover:text-ink transition-colors"
          title={t('Refresh')}
        >
          <ArrowPathIcon className={clsx('h-3.5 w-3.5', loading && 'animate-spin')} />
        </button>
        <button
          onClick={() => setApiModalOpen(true)}
          className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-[12px] font-medium text-secondary hover:text-ink transition-colors"
          title={t('Fetch this data with your API key')}
        >
          <CommandLineIcon className="h-3.5 w-3.5" />
          {t('Get via API')}
        </button>
        {lensIsLineage ? (
          // Per-lens export menu: the active lens's rowset or the full flat grid.
          <Popover
            width={230}
            trigger={
              <span className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-[12px] font-medium text-secondary transition-colors hover:text-ink">
                <ArrowDownTrayIcon className="h-3.5 w-3.5" />
                {t('Export')}
                <ChevronDownIcon className="h-3 w-3" />
              </span>
            }
          >
            {(close) => (
              <div className="space-y-0.5">
                <div className="px-2 pb-0.5 pt-1 text-[10px] font-semibold uppercase tracking-wide text-tertiary">
                  {effLens === 'latest'
                    ? t('Current view ({{count}} records)', { count: viewTotal })
                    : t('This snapshot ({{count}} records)', { count: viewTotal })}
                </div>
                <MenuItem onClick={() => { doExport('csv'); close(); }}>{t('Download CSV')}</MenuItem>
                <MenuItem onClick={() => { doExport('json'); close(); }}>{t('Download JSON')}</MenuItem>
                <div className="px-2 pb-0.5 pt-1.5 text-[10px] font-semibold uppercase tracking-wide text-tertiary">
                  {rowsTotal != null ? t('All rows ({{count}} rows)', { count: rowsTotal }) : t('All rows')}
                </div>
                <MenuItem onClick={() => { doExport('csv', 'all'); close(); }}>{t('Download CSV')}</MenuItem>
                <MenuItem onClick={() => { doExport('json', 'all'); close(); }}>{t('Download JSON')}</MenuItem>
              </div>
            )}
          </Popover>
        ) : (
          <div className="inline-flex overflow-hidden rounded-lg border border-border">
            <button
              disabled={exporting || viewTotal === 0}
              onClick={() => doExport('csv')}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 text-[12px] font-medium text-secondary hover:bg-hover hover:text-ink transition-colors disabled:opacity-40"
            >
              <ArrowDownTrayIcon className="h-3.5 w-3.5" />
              {t('CSV')}
            </button>
            <button
              disabled={exporting || viewTotal === 0}
              onClick={() => doExport('json')}
              className="border-l border-border px-2.5 py-1.5 text-[12px] font-medium text-secondary hover:bg-hover hover:text-ink transition-colors disabled:opacity-40"
            >
              {t('JSON')}
            </button>
          </div>
        )}
      </div>

      {/* Selection action bar */}
      {selectedRows.length > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-ink/20 bg-hover px-3 py-2 bulkbar-enter">
          <div className="flex items-center gap-2 text-[12px]">
            <SelectBox checked={allSelected} indeterminate={someSelected} onChange={toggleAll} label={t('Select all')} />
            <span className="font-medium text-ink">{t('{{n}} selected', { n: selectedRows.length })}</span>
            <button onClick={() => setSelected(new Set())} className="text-tertiary hover:text-ink">
              {t('Clear')}
            </button>
            <span
              className="hidden @rail/stage:inline text-[10px] text-tertiary"
              title={t('Shift+click for range · Cmd/Ctrl+click to toggle · drag over rows to paint · ⌘A select all · Esc clear · Del delete')}
            >
              {t('Shift+click, drag, ⌘A / Esc / Del')}
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            <Popover
              width={220}
              trigger={
                <span className="inline-flex items-center gap-1 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-[12px] font-medium text-secondary hover:text-ink">
                  <DocumentDuplicateIcon className="h-3.5 w-3.5" />
                  {t('Copy')}
                  <ChevronDownIcon className="h-3 w-3" />
                </span>
              }
            >
              {(close) => (
                <div className="space-y-0.5">
                  <MenuItem onClick={() => { copySelected('json'); close(); }}>{t('Copy as JSON')}</MenuItem>
                  <MenuItem onClick={() => { copySelected('tsv'); close(); }}>{t('Copy for Sheets (TSV)')}</MenuItem>
                  <MenuItem onClick={() => { copySelected('md'); close(); }}>{t('Copy as Markdown table')}</MenuItem>
                </div>
              )}
            </Popover>
            <Popover
              width={200}
              trigger={
                <span className="inline-flex items-center gap-1 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-[12px] font-medium text-secondary hover:text-ink">
                  <ArrowDownTrayIcon className="h-3.5 w-3.5" />
                  {t('Export')}
                  <ChevronDownIcon className="h-3 w-3" />
                </span>
              }
            >
              {(close) => (
                <div className="space-y-0.5">
                  <MenuItem onClick={() => { exportSelected('csv'); close(); }}>{t('Download CSV')}</MenuItem>
                  <MenuItem onClick={() => { exportSelected('json'); close(); }}>{t('Download JSON')}</MenuItem>
                </div>
              )}
            </Popover>
            <button
              onClick={openSend}
              className="inline-flex items-center gap-1 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-[12px] font-medium text-secondary hover:text-ink"
              title={t('Send selected rows to a workflow as input')}
            >
              <PaperAirplaneIcon className="h-3.5 w-3.5" />
              {t('Send to…')}
            </button>
            <button
              onClick={askDeleteSelected}
              disabled={isNested}
              className="inline-flex items-center gap-1 rounded-lg border border-red-200 bg-surface px-2.5 py-1.5 text-[12px] font-medium text-red-600 hover:bg-red-50 hover:text-red-700 disabled:opacity-40"
              title={
                isNested
                  ? t('Delete is only available on the top-level records view')
                  : t('Delete selected rows')
              }
            >
              <TrashIcon className="h-3.5 w-3.5" />
              {t('Delete')}
            </button>
          </div>
        </div>
      )}

      {/* Send selected rows to another workflow (one run per record) */}
      <Modal
        isOpen={sendOpen}
        onClose={() => (sending ? null : setSendOpen(false))}
        title={t('Send to automation')}
        subtitle={t('Start a run for each selected record, using its fields as the run’s input.')}
        size="md"
      >
        <div className="space-y-4">
          <div>
            <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-tertiary">
              {t('Destination workflow')}
            </label>
            {sendTargets === null ? (
              <p className="text-[12px] text-tertiary">{t('Loading…')}</p>
            ) : sendTargets.length === 0 ? (
              <p className="text-[12px] text-tertiary">{t('No workflows available.')}</p>
            ) : (
              <Select<number>
                value={sendTargetId ?? undefined}
                onChange={(v) => setSendTargetId(v)}
                placeholder={t('Choose a workflow…')}
                options={sendTargets.map((w) => ({ value: w.id, label: w.name }))}
                className="w-full"
              />
            )}
          </div>
          <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[12px] leading-relaxed text-amber-800">
            {t('This starts {{n}} separate runs and may consume credits. Each record’s fields are passed as input; fields the workflow doesn’t expect are ignored.', { n: selectedRows.length })}
          </p>
          <div className="flex items-center justify-end gap-2">
            <button
              onClick={() => setSendOpen(false)}
              disabled={sending}
              className="rounded-lg border border-border px-3 py-1.5 text-[12px] font-medium text-secondary hover:text-ink disabled:opacity-40"
            >
              {t('Cancel')}
            </button>
            <button
              onClick={doSend}
              disabled={sending || !sendTargetId}
              className="inline-flex items-center gap-1.5 rounded-lg bg-accent-strong px-3 py-1.5 text-[12px] font-medium text-accent-on hover:bg-accent-strong/90 disabled:opacity-40"
            >
              {sending && <ArrowPathIcon className="h-3.5 w-3.5 animate-spin" />}
              {t('Start {{n}} runs', { n: selectedRows.length })}
            </button>
          </div>
        </div>
      </Modal>

      {/* Delete extracted-data rows — destructive; asks first */}
      <Modal
        isOpen={deleteOpen}
        onClose={() => (deleting ? null : setDeleteOpen(false))}
        title={t('Delete extracted data')}
        subtitle={t('This removes the selected records from their runs. The runs themselves stay in your history.')}
        size="sm"
      >
        <div className="space-y-4">
          {deleteByUid ? (
            <div className="space-y-1.5">
              <p className="text-[13px] leading-relaxed text-secondary">
                {t('Delete {{records}} record(s) and all their history ({{versions}} dated versions)? Runs stay in your history.', {
                  records: (deleteRows || []).length,
                  versions: deleteVersionTotal,
                })}
              </p>
              <p className="text-[11px] text-tertiary">{t('A future run can re-extract them as new.')}</p>
            </div>
          ) : (
            <p className="text-[13px] leading-relaxed text-secondary">
              {t('Delete {{n}} extracted record(s)? This can’t be undone.', { n: (deleteRows || []).length })}
            </p>
          )}
          <div className="flex items-center justify-end gap-2">
            <button
              onClick={() => setDeleteOpen(false)}
              disabled={deleting}
              className="rounded-lg border border-border px-3 py-1.5 text-[12px] font-medium text-secondary hover:text-ink disabled:opacity-40"
            >
              {t('Cancel')}
            </button>
            <button
              onClick={doDelete}
              disabled={deleting}
              className="inline-flex items-center gap-1.5 rounded-lg bg-red-600 px-3 py-1.5 text-[12px] font-medium text-white hover:bg-red-700 disabled:opacity-40"
            >
              {deleting && <ArrowPathIcon className="h-3.5 w-3.5 animate-spin" />}
              <TrashIcon className="h-3.5 w-3.5" />
              {t('Delete')}
            </button>
          </div>
        </div>
      </Modal>

      {/* "Get via API" — the dataset's whole REST surface. Shared with the crawl
          detail view so every entry point opens the SAME modal. */}
      <DatasetApiModal
        isOpen={apiModalOpen}
        onClose={() => setApiModalOpen(false)}
        datasetId={workflowId}
        viewSnippet={apiSnippet}
        lineageSupported={lineageSupported === true}
      />

      {/* Active filter chips (records scope only) */}
      {!isNested && filters.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          {filters.map((c) => (
            <span
              key={c.col}
              className="inline-flex items-center gap-1 rounded-full border border-border bg-surface py-0.5 pl-2 pr-1 text-[11px] text-secondary"
            >
              <span className="max-w-[200px] truncate">{describeClause(c)}</span>
              <button
                onClick={() => setClauseFor(c.col, null)}
                className="rounded-full p-0.5 text-tertiary hover:bg-hover hover:text-ink"
                title={t('Remove filter')}
              >
                <XMarkIcon className="h-3 w-3" />
              </button>
            </span>
          ))}
          <button onClick={() => setFilters([])} className="text-[11px] font-medium text-tertiary hover:text-ink">
            {t('Clear all')}
          </button>
        </div>
      )}

      {/* Mixed-schema source chips: filter to one originating list key */}
      {sourceChips && (
        <div className="flex flex-wrap items-center gap-1.5">
          <button onClick={() => setSourceFilter(null)} className={shelfFilterChipClass(sourceFilter === null)}>
            {t('All {{count}}', { count: sourceChips.total })}
          </button>
          {sourceChips.entries.map(([key, n]) => (
            <button
              key={key || '::untagged'}
              onClick={() => setSourceFilter(sourceFilter === key ? null : key)}
              className={shelfFilterChipClass(sourceFilter === key)}
              title={t('Only records extracted into {{name}}', { name: key || t('other') })}
            >
              <span className="max-w-[160px] truncate">{key || t('other')}</span>
              <span className={shelfFilterCountClass(sourceFilter === key)}>{n}</span>
            </button>
          ))}
        </div>
      )}

      {/* Empty states */}
      {(loading || !tableEnabled) && !data ? (
        <RowsSkeleton rows={6} label={t('Loading data')} />
      ) : pristineEmpty ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-border bg-surface px-6 py-12 text-center">
          <TableCellsIcon className="h-6 w-6 text-tertiary" />
          <p className="mt-3 text-[13px] font-medium text-ink">{t('No extracted data yet')}</p>
          <p className="mt-1 max-w-sm text-[11px] text-tertiary">
            {t('When this workflow runs and extracts data, every run’s results land here as a sortable table.')}
          </p>
        </div>
      ) : !loading && viewTotal === 0 && hasFiltersOrSearch ? (
        <div className="rounded-xl border border-border bg-surface px-6 py-10 text-center">
          <p className="text-[13px] text-secondary">{t('No rows match your search or filters.')}</p>
          <button onClick={clearAll} className="mt-2 text-[12px] font-medium text-ink underline-offset-2 hover:underline">
            {t('Clear all')}
          </button>
        </div>
      ) : view === 'cards' ? (
        <div className={clsx('overflow-hidden rounded-xl border border-border bg-surface', maximized && CARD_SHELL_MAX)}>
          <div className={clsx('divide-y divide-border', maximized && 'min-h-0 flex-1 overflow-y-auto')}>
            {viewRows.map((row, ri) => (
              <RecordCard
                key={row.key}
                row={row}
                index={start + ri}
                columns={rankedColumns}
                roles={roles}
                workflowId={workflowId}
                showRunInfo={showRunInfo}
                showChange={showChangeUi}
                historyCtx={historyCtx}
                collections={isNested ? undefined : collections}
                onOpenCollection={isNested ? undefined : openCollection}
                onDelete={isNested ? undefined : askDeleteOne}
                selected={selected.has(row.key)}
                onSelect={handleRowSelect}
                onSelectMouseDown={handleRowMouseDown}
                onSelectMouseEnter={handleRowMouseEnter}
                dragging={dragMode !== null}
                isOpen={expanded === row.key}
                onToggle={toggleExpanded}
              />
            ))}
            {/* Records the previous snapshot had but this one doesn't (display-only) */}
            {!hasNext &&
              removedViewRows.map((row, ri) => (
                <RemovedCard key={row.key} row={row} index={ri + 1} columns={rankedColumns} roles={roles} />
              ))}
          </div>
          {footer}
        </div>
      ) : (
        <div className={clsx('overflow-hidden rounded-xl border border-border bg-surface', maximized && CARD_SHELL_MAX)}>
          <TableScroller bothAxes={maximized} className={clsx(maximized && 'min-h-0 flex-1')}>
            <table className="table-fixed border-collapse text-[12px]" style={{ width: totalWidth, minWidth: '100%' }}>
              <colgroup>
                <col style={{ width: W_GUTTER }} />
                {showChangeUi && <col style={{ width: W_DOT }} />}
                {metaCols.map((m) => (
                  <col key={m.key} style={{ width: m.width }} />
                ))}
                {visibleColumns.map((c) => (
                  <col key={c} style={{ width: widthFor(c) }} />
                ))}
                <col />
              </colgroup>
              <thead className="sticky top-0 z-10 bg-canvas/95 backdrop-blur supports-[backdrop-filter]:bg-canvas/80">
                <tr>
                  <th className="border-b border-border px-2 py-2 text-center align-middle">
                    <div className="flex justify-center">
                      <SelectBox
                        checked={allSelected}
                        indeterminate={someSelected}
                        onChange={toggleAll}
                        label={t('Select all')}
                      />
                    </div>
                  </th>
                  {showChangeUi && (
                    // Slim signal-dot column — not sortable (the change rank IS
                    // the lens's default order) and label-free (28px).
                    <th className="border-b border-border" aria-label={t('Change')} />
                  )}
                  {metaCols.map((m) => (
                    <HeaderCell
                      key={m.key}
                      col={m.key}
                      label={m.label}
                      filterable={m.filterable}
                      resizable={false}
                      showMenu={!isNested}
                    />
                  ))}
                  {visibleColumns.map((c) => (
                    <HeaderCell key={c} col={c} label={c} showMenu={!isNested} />
                  ))}
                  <th className="border-b border-border" />
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {viewRows.map((row) => {
                  const isOpen = expanded === row.key;
                  const isSel = selected.has(row.key);
                  const ss = statusStyle(row.status);
                  const lin = showChangeUi ? row.lineage : undefined;
                  const changedTops = new Set((lin?.changed_fields ?? []).map((p) => p.split('.')[0]));
                  // Noise cap: >50% of the visible cells changed → tinting is noise.
                  const noisy =
                    lin?.change === 'changed' &&
                    visibleColumns.filter((c) => changedTops.has(c)).length > visibleColumns.length / 2;
                  return (
                    <React.Fragment key={row.key}>
                      <tr
                        className={clsx(
                          'group transition-colors',
                          dragMode ? 'cursor-crosshair select-none' : 'cursor-pointer',
                          isSel ? 'bg-hover' : 'hover:bg-hover',
                          lin?.change === 'missing' && 'opacity-60',
                        )}
                        onClick={(e) => {
                          // A modifier click is a selection gesture; leave expand alone.
                          if (e.shiftKey || e.metaKey || e.ctrlKey) return;
                          setExpanded(isOpen ? null : row.key);
                        }}
                      >
                        <td className="px-2 py-2 text-center align-top">
                          <div className="flex justify-center">
                            <SelectBox
                              checked={isSel}
                              onChange={(e) => handleRowSelect(row.key, e)}
                              onMouseDown={(e) => handleRowMouseDown(row.key, e)}
                              onMouseEnter={() => handleRowMouseEnter(row.key)}
                              label={t('Select row')}
                            />
                          </div>
                        </td>
                        {showChangeUi && (
                          <td className="px-2 py-2 align-top">
                            {/* Dot only on signal rows; unchanged rows stay blank. */}
                            {lin && (
                              <ChangeDot
                                change={lin.change}
                                changedFields={lin.changed_fields}
                                noisy={noisy}
                                className="mt-1"
                              />
                            )}
                          </td>
                        )}
                        {metaCols.map((m) =>
                          m.key === 'run_at' ? (
                            <td
                              key={m.key}
                              title={row.run_at ? formatRelativeTime(row.run_at) : undefined}
                              className="overflow-hidden px-3 py-2 align-top whitespace-nowrap tabular-nums text-tertiary"
                            >
                              {row.run_at ? fmtRunDate(row.run_at) : '—'}
                            </td>
                          ) : m.key === 'status' ? (
                            <td key={m.key} className="overflow-hidden px-3 py-2 align-top whitespace-nowrap">
                              <span className="inline-flex items-center gap-1.5">
                                <span className={clsx('h-1.5 w-1.5 rounded-full', ss.dot)} />
                                <span className="truncate capitalize text-tertiary">{row.status || '—'}</span>
                              </span>
                            </td>
                          ) : m.key === 'first_seen_at' || m.key === 'last_seen_at' ? (
                            <td
                              key={m.key}
                              title={
                                m.key === 'first_seen_at' && data?.truncated
                                  ? t('within the last {{count}} runs', { count: data.scanned_runs })
                                  : undefined
                              }
                              className="overflow-hidden px-3 py-2 align-top whitespace-nowrap tabular-nums text-tertiary"
                            >
                              {(() => {
                                const at = m.key === 'first_seen_at' ? row.lineage?.first_seen_at : row.lineage?.last_seen_at;
                                return at ? fmtRunDate(at) : '—';
                              })()}
                            </td>
                          ) : m.key === 'versions' ? (
                            <td
                              key={m.key}
                              className="overflow-hidden px-3 py-2 align-top whitespace-nowrap tabular-nums text-tertiary"
                            >
                              {row.lineage?.versions ?? '—'}
                            </td>
                          ) : (
                            <td
                              key={m.key}
                              className="overflow-hidden px-3 py-2 align-top whitespace-nowrap tabular-nums text-tertiary"
                            >
                              #{row.run_id}
                            </td>
                          ),
                        )}
                        {visibleColumns.map((c) => (
                          <DataCell
                            key={c}
                            value={(row.fields || {})[c]}
                            changed={!noisy && lin?.change === 'changed' && changedTops.has(c)}
                            titleCell={c === roles.titleCol}
                            fallbackText={c === roles.titleCol ? titleFallbackFor(row.fields || {}) : undefined}
                          />
                        ))}
                        <td className="px-2 align-top">
                          <ChevronRightIcon
                            className={clsx(
                              'mt-2 h-3.5 w-3.5 text-tertiary transition-transform',
                              isOpen && 'rotate-90',
                            )}
                          />
                        </td>
                      </tr>
                      {isOpen && (
                        <tr className="bg-canvas/40">
                          {/* Padding lives INSIDE the Expand so the row grows from a
                              true 0 height — td padding would pop in un-animated. */}
                          <td
                            colSpan={2 + visibleColumns.length + metaCols.length + (showChangeUi ? 1 : 0)}
                            className="p-0"
                          >
                            {/* The cell is as wide as the TABLE; the record inside
                                has to be as wide as what you can SEE, or every
                                nested viewer (markdown, JSON, images) lays out
                                against ~1,600px and only its left third is ever on
                                screen. `sticky left-0` keeps the panel parked in
                                the visible column while the grid scrolls under it;
                                --grid-vw (published by TableScroller) is that
                                column's width, with 100% as the pre-measure
                                fallback. Keep this cell free of `overflow-hidden`
                                (unlike the data cells): it would make the cell its
                                own clipping container, which silently cancels the
                                sticky and leaves the panel parked off-screen left. */}
                            <div
                              className="sticky left-0 max-w-full"
                              style={{ width: 'var(--grid-vw, 100%)' }}
                            >
                              <Expand open appear className="px-4 py-3">
                                <RawRecord
                                  row={row}
                                  columns={rankedColumns}
                                  workflowId={workflowId}
                                  historyCtx={historyCtx}
                                  collections={isNested ? undefined : collections}
                                  onOpenCollection={isNested ? undefined : openCollection}
                                  onDelete={isNested ? undefined : () => askDeleteOne(row)}
                                />
                              </Expand>
                            </div>
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })}
                {/* Removed-records group (By date): display-only, dimmed */}
                {!hasNext &&
                  removedViewRows.map((row) => (
                    <tr key={row.key} className="opacity-60">
                      <td className="px-2 py-2" />
                      {showChangeUi && (
                        <td className="px-2 py-2 align-top">
                          <ChangeDot change="removed" className="mt-1" />
                        </td>
                      )}
                      {metaCols.map((m) => (
                        <td key={m.key} className="px-3 py-2 align-top text-tertiary/60">
                          —
                        </td>
                      ))}
                      {visibleColumns.map((c) => (
                        <DataCell
                          key={c}
                          value={(row.fields || {})[c]}
                          titleCell={c === roles.titleCol}
                          fallbackText={c === roles.titleCol ? titleFallbackFor(row.fields || {}) : undefined}
                        />
                      ))}
                      <td className="px-2 align-top" />
                    </tr>
                  ))}
              </tbody>
            </table>
          </TableScroller>
          {footer}
        </div>
      )}
    </>
  );

  if (!maximized) {
    return (
      <div className="min-w-0 space-y-3 outline-none" tabIndex={-1} onKeyDown={handleContainerKeyDown}>
        {body}
      </div>
    );
  }

  // Full screen. Portalled to <body> so no ancestor's `overflow-hidden`,
  // transform or stacking context can clip it — the grid lives several nested
  // scroll panes deep. z-40 keeps it UNDER this component's own modals (z-50)
  // and the column popovers (z-9999), which must still open on top of it.
  return createPortal(
    <div
      // `@container/stage`: every `@pair/stage:` / `@rail/stage:` class inside the
      // grid measures the page's content card — and this is portalled OUT of it,
      // so without re-declaring the container here every one of them would go
      // permanently false and full screen would render the NARROWEST layout of
      // all. Re-declaring it against the full window is also simply correct: the
      // overlay IS the stage now.
      className="@container/stage fixed inset-0 z-40 flex flex-col bg-canvas"
      role="dialog"
      aria-modal="true"
      aria-label={t('Extracted data')}
    >
      <div className="flex h-12 shrink-0 items-center gap-3 border-b border-border bg-chrome px-4 sm:px-6">
        <TableCellsIcon className="h-4 w-4 shrink-0 text-tertiary" />
        <span className="text-[13px] font-semibold text-ink">{t('Extracted data')}</span>
        <span className="hidden truncate text-[11px] text-tertiary @pair/stage:inline">
          {viewTotal.toLocaleString(uiLocale())} {isNested ? scope.split('.').pop() : t('records')}
        </span>
        <div className="flex-1" />
        <button
          onClick={() => setMaximized(false)}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-[12px] font-medium text-secondary transition-colors hover:text-ink"
        >
          <ArrowsPointingInIcon className="h-3.5 w-3.5" />
          {t('Exit full screen')}
        </button>
      </div>
      <div
        className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-4 py-3 outline-none sm:px-6"
        tabIndex={-1}
        onKeyDown={handleContainerKeyDown}
      >
        {body}
      </div>
    </div>,
    document.body,
  );
};

/**
 * One extracted record as a scannable list card: a prominent title (a name-like
 * field), an optional thumbnail, run provenance, and the remaining fields as
 * compact label/value pairs. Selectable; clicking the body opens the full
 * type-aware record — the same expanded view the table row uses.
 */
const RecordCard: React.FC<{
  row: ViewRow;
  index: number;
  columns: string[];
  roles: ColumnRoles;
  workflowId: number;
  showRunInfo: boolean;
  /** Lens has change tracking on: pill + changed-field promotion. */
  showChange?: boolean;
  historyCtx?: HistoryCtx | null;
  collections?: { path: string; count: number }[];
  onOpenCollection?: (path: string) => void;
  onDelete?: (row: ViewRow) => void;
  selected: boolean;
  onSelect: (key: string, e?: React.MouseEvent) => void;
  onSelectMouseDown?: (key: string, e: React.MouseEvent) => void;
  onSelectMouseEnter?: (key: string) => void;
  dragging?: boolean;
  isOpen: boolean;
  onToggle: (key: string) => void;
}> = React.memo(({
  row,
  index,
  columns,
  roles,
  workflowId,
  showRunInfo,
  showChange,
  historyCtx,
  collections,
  onOpenCollection,
  onDelete,
  selected,
  onSelect,
  onSelectMouseDown,
  onSelectMouseEnter,
  dragging,
  isOpen,
  onToggle,
}) => {
  const { t } = useTranslation();
  const fields = row.fields || {};
  const ss = statusStyle(row.status);
  const lineage = showChange ? row.lineage : undefined;
  const changedTops = new Set((lineage?.changed_fields ?? []).map((p) => p.split('.')[0]));

  const titleVal = roles.titleCol ? fields[roles.titleCol] : undefined;
  const title = previewText(titleVal);
  const imageSrc =
    roles.imageCol && classifyValue(fields[roles.imageCol]).kind === 'image' ? String(fields[roles.imageCol]) : null;

  const headerCols = new Set([roles.titleCol, roles.imageCol].filter(Boolean) as string[]);
  const entries = columns
    .filter((c) => !headerCols.has(c))
    .map((c) => [c, fields[c]] as const)
    .filter(([, v]) => v !== null && v !== undefined && v !== '');
  // Changed fields lead the visible slice (their old value shows in the
  // expanded History — collapsed cards render just the new value + the pill).
  const ordered =
    changedTops.size > 0
      ? [...entries].sort((a, b) => Number(changedTops.has(b[0])) - Number(changedTops.has(a[0])))
      : entries;
  const shown = ordered.slice(0, 6);
  const moreCount = ordered.length - shown.length;

  return (
    <div
      className={clsx(
        'transition-colors',
        dragging && 'select-none',
        selected && 'bg-hover',
        lineage?.change === 'missing' && 'opacity-60',
      )}
    >
      <div className="flex items-start gap-3 px-4 py-3">
        <div className="mt-0.5 flex items-center">
          <SelectBox
            checked={selected}
            onChange={(e) => onSelect(row.key, e)}
            onMouseDown={onSelectMouseDown ? (e) => onSelectMouseDown(row.key, e) : undefined}
            onMouseEnter={onSelectMouseEnter ? () => onSelectMouseEnter(row.key) : undefined}
            label={t('Select record')}
          />
        </div>
        <button
          onClick={(e) => {
            if (e.shiftKey || e.metaKey || e.ctrlKey) {
              onSelect(row.key, e);
              return;
            }
            onToggle(row.key);
          }}
          className="group flex min-w-0 flex-1 items-start gap-3 text-left"
        >
          {imageSrc && (
            <img src={imageSrc} alt="" loading="lazy" className="h-11 w-11 shrink-0 rounded-md border border-border object-cover" />
          )}
          <div className="min-w-0 flex-1">
            <div className="flex items-start justify-between gap-3">
              <span className="flex min-w-0 items-baseline gap-2">
                <span className="min-w-0 break-words text-[13px] font-semibold leading-snug text-ink line-clamp-2">
                  {title || t('Record {{n}}', { n: index })}
                </span>
                {lineage && lineage.change !== 'same' && <ChangePill change={lineage.change} className="shrink-0" />}
              </span>
              {showRunInfo && (
                <span className="flex shrink-0 items-center gap-1.5 whitespace-nowrap text-[10px] text-tertiary">
                  <span className={clsx('h-1.5 w-1.5 rounded-full', ss.dot)} />
                  <span className="tabular-nums">#{row.run_id}</span>
                  {row.run_at && <span>· {fmtRunDate(row.run_at)}</span>}
                </span>
              )}
            </div>

            {shown.length > 0 && (
              <div className="mt-1.5 grid grid-cols-1 gap-x-6 gap-y-1 @pair/stage:grid-cols-2 @wide/stage:grid-cols-3">
                {shown.map(([c, v]) => (
                  <div key={c} className="flex min-w-0 items-baseline gap-1.5 text-[12px]">
                    <span className="max-w-[45%] shrink-0 truncate text-[10px] font-semibold uppercase tracking-wide text-tertiary">
                      {c}
                    </span>
                    <span className="flex min-w-0 flex-1">
                      <InlineValue value={v} />
                    </span>
                  </div>
                ))}
              </div>
            )}
            {moreCount > 0 && (
              <div className="mt-1 text-[10px] text-tertiary">{t('+{{n}} more fields', { n: moreCount })}</div>
            )}
          </div>

          <ChevronRightIcon className={clsx('mt-0.5 h-4 w-4 shrink-0 text-tertiary transition-transform', isOpen && 'rotate-90')} />
        </button>
      </div>

      <Expand open={isOpen} mountOnEnter className="border-t border-border bg-canvas/40 px-4 py-3">
        <RawRecord
          row={row}
          columns={columns}
          workflowId={workflowId}
          historyCtx={historyCtx}
          collections={collections}
          onOpenCollection={onOpenCollection}
          onDelete={onDelete ? () => onDelete(row) : undefined}
        />
      </Expand>
    </div>
  );
});
RecordCard.displayName = 'RecordCard';

/** A dimmed, display-only card for a By-date removed record (no select/expand). */
const RemovedCard: React.FC<{
  row: ViewRow;
  index: number;
  columns: string[];
  roles: ColumnRoles;
}> = React.memo(({ row, index, columns, roles }) => {
  const { t } = useTranslation();
  const fields = row.fields || {};
  const title = previewText(roles.titleCol ? fields[roles.titleCol] : undefined);
  const headerCols = new Set([roles.titleCol, roles.imageCol].filter(Boolean) as string[]);
  const shown = columns
    .filter((c) => !headerCols.has(c))
    .map((c) => [c, fields[c]] as const)
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .slice(0, 6);
  return (
    <div className="flex items-start gap-3 px-4 py-3 opacity-60">
      <div className="w-4 shrink-0" />
      <div className="min-w-0 flex-1">
        <span className="flex min-w-0 items-baseline gap-2">
          <span className="min-w-0 break-words text-[13px] font-semibold leading-snug text-ink line-clamp-2">
            {title || t('Record {{n}}', { n: index })}
          </span>
          <ChangePill change="removed" className="shrink-0" />
        </span>
        {shown.length > 0 && (
          <div className="mt-1.5 grid grid-cols-1 gap-x-6 gap-y-1 @pair/stage:grid-cols-2 @wide/stage:grid-cols-3">
            {shown.map(([c, v]) => (
              <div key={c} className="flex min-w-0 items-baseline gap-1.5 text-[12px]">
                <span className="max-w-[45%] shrink-0 truncate text-[10px] font-semibold uppercase tracking-wide text-tertiary">
                  {c}
                </span>
                <span className="flex min-w-0 flex-1">
                  <InlineValue value={v} />
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
});
RemovedCard.displayName = 'RemovedCard';

/**
 * A compact, type-aware table cell: thumbnails for images, links for URLs, a
 * `{n}`/`[n]` chip for JSON, formatted numbers/booleans, and a one-line preview
 * for text/markdown. The full type-aware viewer lives in the expanded row.
 * The title column's cell is the row's anchor: 13px medium ink, and when its
 * own value is empty it shows `fallbackText` (the next non-empty ranked
 * column's value) rendered secondary instead of a dash.
 */
const DataCell: React.FC<{
  value: unknown;
  changed?: boolean;
  titleCell?: boolean;
  fallbackText?: string;
}> = React.memo(({ value, changed, titleCell, fallbackText }) => {
  const c = classifyValue(value);
  // Changed-cell treatment: soft amber tint + a dotted underline (the non-color
  // cue). Applied by the lineage lenses; capped upstream when most cells changed.
  const tdCls = (extra?: string) =>
    clsx('overflow-hidden px-3 py-2 align-top', changed && 'bg-amber-50/70', extra);
  const changedUnderline = changed
    ? 'underline decoration-amber-500/80 decoration-dotted decoration-[1.5px] underline-offset-[3px]'
    : undefined;

  if (c.kind === 'empty') {
    if (titleCell && fallbackText) {
      return (
        <td className={tdCls('text-secondary')}>
          <span className="block truncate" title={fallbackText}>
            {fallbackText}
          </span>
        </td>
      );
    }
    return <td className={tdCls('text-tertiary/50')}>—</td>;
  }
  if (c.kind === 'image') {
    const src = String(value);
    return (
      <td className={tdCls()}>
        <img src={src} alt="" loading="lazy" className="h-7 w-7 rounded border border-border object-cover" title={src} />
      </td>
    );
  }
  if (c.kind === 'json') {
    const v = c.json;
    const label = Array.isArray(v) ? `[${v.length}]` : `{${Object.keys(v as object).length}}`;
    return (
      <td className={tdCls()}>
        <code
          className={clsx('rounded bg-canvas px-1.5 py-0.5 text-[11px] text-secondary', changedUnderline)}
          title={previewText(v).slice(0, 240)}
        >
          {label}
        </code>
      </td>
    );
  }
  if (c.kind === 'url') {
    const s = String(value);
    return (
      <td className={tdCls()}>
        <a
          href={s}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(e) => e.stopPropagation()}
          className={clsx(
            'block truncate text-ink underline underline-offset-2',
            titleCell && 'text-[13px] font-medium',
            changed && 'decoration-amber-500/80 decoration-dotted decoration-[1.5px]',
          )}
          title={s}
        >
          {s}
        </a>
      </td>
    );
  }
  if (c.kind === 'number' || c.kind === 'boolean') {
    return (
      <td className={tdCls('tabular-nums text-ink')}>
        <span className={clsx('block truncate', titleCell && 'text-[13px] font-medium', changedUnderline)}>
          {String(value)}
        </span>
      </td>
    );
  }
  const text = previewText(value);
  return (
    <td className={tdCls('text-ink')}>
      <span
        className={clsx('block truncate', titleCell && 'text-[13px] font-medium', changedUnderline)}
        title={titleCell || text.length > 60 ? text : undefined}
      >
        {text}
      </span>
    </td>
  );
});
DataCell.displayName = 'DataCell';

/**
 * Expanded view of a single record. Each field renders through the type-aware
 * <ValueViewer>. A record-level toggle switches between the per-field view and
 * the whole record as raw JSON; quick actions copy the record or link to its run.
 */
const RawRecord: React.FC<{
  row: RecordLike;
  columns: string[];
  workflowId: number;
  /** When set (lineage lens) and the row has a uid, a History section is fetched on expand. */
  historyCtx?: HistoryCtx | null;
  collections?: { path: string; count: number }[];
  onOpenCollection?: (path: string) => void;
  onDelete?: () => void;
}> = ({ row, columns, workflowId, historyCtx, collections, onOpenCollection, onDelete }) => {
  const { t } = useTranslation();
  const [mode, setMode] = useState<'fields' | 'raw'>('fields');
  const fields = row.fields || {};
  const keys = [
    ...columns.filter((c) => c in fields),
    ...Object.keys(fields).filter((k) => !columns.includes(k)),
  ];

  const copyRecord = async (fmt: 'json' | 'md') => {
    const cols = unionColumns([fields], columns);
    const text = fmt === 'json' ? recordsToJson([fields]) : recordsToMarkdown([fields], cols);
    const ok = await copyToClipboard(text);
    if (ok) toast.success(t('Copied'));
    else toast.error(t('Copy failed'));
  };

  return (
    <div className="min-w-0 space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="inline-flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[11px] font-medium text-secondary">
          <span className={clsx('h-1.5 w-1.5 rounded-full', statusStyle(row.status).dot)} />
          {t('Run #{{id}}', { id: row.run_id })}
          {row.run_at && <span className="text-tertiary">· {formatRelativeTime(row.run_at)}</span>}
          {row.record_index > 0 && (
            <span className="text-tertiary">· {t('record {{i}}', { i: row.record_index + 1 })}</span>
          )}
          {/* Lineage facts live here (and in History) instead of grid columns. */}
          {row.lineage?.first_seen_at && (
            <span className="text-tertiary">
              · {t('First seen {{date}}', { date: fmtRunDate(row.lineage.first_seen_at) })}
            </span>
          )}
          {row.lineage && (
            <span className="tabular-nums text-tertiary">
              · {t('{{count}} versions', { count: row.lineage.versions })}
            </span>
          )}
        </span>
        <div className="flex items-center gap-3">
          <Popover
            width={200}
            trigger={
              <span className="inline-flex items-center gap-1 text-[11px] font-medium text-tertiary transition-colors hover:text-ink">
                <DocumentDuplicateIcon className="h-3.5 w-3.5" />
                {t('Copy')}
              </span>
            }
          >
            {(close) => (
              <div className="space-y-0.5">
                <MenuItem onClick={() => { copyRecord('json'); close(); }}>{t('Copy as JSON')}</MenuItem>
                <MenuItem onClick={() => { copyRecord('md'); close(); }}>{t('Copy as Markdown')}</MenuItem>
              </div>
            )}
          </Popover>
          <ModeToggle
            value={mode}
            onChange={setMode}
            options={[
              { id: 'fields', label: t('Fields') },
              { id: 'raw', label: t('Raw JSON') },
            ]}
          />
          <Link
            to={`/workflows/${workflowId}?tab=detail`}
            className="text-[11px] font-medium text-ink underline-offset-2 hover:underline"
          >
            {t('View run')}
          </Link>
          {onDelete && (
            <button
              onClick={onDelete}
              className="inline-flex items-center gap-1 text-[11px] font-medium text-red-600 transition-colors hover:text-red-700"
              title={t('Delete this record')}
            >
              <TrashIcon className="h-3.5 w-3.5" />
              {t('Delete')}
            </button>
          )}
        </div>
      </div>

      {mode === 'raw' ? (
        <pre className="max-h-80 overflow-auto rounded-lg border border-border bg-surface p-3 text-[11px] leading-relaxed text-ink">
          {JSON.stringify(fields, null, 2)}
        </pre>
      ) : keys.length === 0 ? (
        <p className="text-[11px] text-tertiary">{t('No fields.')}</p>
      ) : (
        <div className="space-y-2">
          {keys.map((k) => {
            const fieldCols = (collections || []).filter((c) => c.path === k || c.path.startsWith(`${k}.`));
            return (
              <div key={k} className="min-w-0 rounded-lg border border-border bg-surface px-3 py-2">
                <div className="mb-1 flex items-center justify-between gap-2">
                  <div className="min-w-0 break-words text-[10px] font-semibold uppercase tracking-wide text-tertiary">{k}</div>
                  {onOpenCollection &&
                    fieldCols.map((c) => (
                      <button
                        key={c.path}
                        onClick={() => onOpenCollection(c.path)}
                        className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border px-2 py-0.5 text-[11px] font-medium text-secondary transition-colors hover:border-zinc-400 hover:text-ink"
                        title={t('Open {{path}} as a selectable collection', { path: c.path })}
                      >
                        <RectangleStackIcon className="h-3 w-3" />
                        {t('Open as collection')}
                        <span className="tabular-nums text-tertiary">· {c.count}</span>
                      </button>
                    ))}
                </div>
                <ValueViewer value={fields[k]} />
              </div>
            );
          })}
        </div>
      )}

      {/* Per-record change history — fetched only when this record is expanded */}
      {historyCtx && row.lineage?.uid && (
        <RecordHistory
          workflowId={historyCtx.workflowId}
          recordUid={row.lineage.uid}
          keyFields={historyCtx.keyFields}
          truncated={historyCtx.truncated}
          scannedRuns={historyCtx.scannedRuns}
        />
      )}
    </div>
  );
};

export default ExtractedDataTable;
