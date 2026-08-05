import React, { useState, useMemo, useCallback, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useQuery } from '../../hooks/useQuery';
import { useQueryCache } from '../../stores/queryCache';
import { Q } from '../../stores/queryKeys';
import { automationApi } from '../../api/endpoints';
import { formatElapsedShort, cleanTitle } from '../../utils/format';
import { Modal } from '../ui/Modal';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import {
  PlayIcon,
  StopIcon,
  TrashIcon,
  CheckIcon,
  MinusIcon,
  LockClosedIcon,
  ExclamationTriangleIcon,
  CursorArrowRaysIcon,
  SignalIcon,
  ArrowRightIcon,
  ComputerDesktopIcon,
  FolderIcon,
  FolderPlusIcon,
  ClockIcon,
} from '@heroicons/react/24/outline';
import { Stagger, Select, Button, EmptyHero, toastIcon } from '../ui';
import { ScrollArea } from '../ui/ScrollArea';
import { tintStyle, workflowTint } from '../../utils/tint';
import { TYPE_META } from '../../pages/workflows/detail/meta';
import { RunWorkflowModal, workflowNeedsInput } from '../workflows/RunWorkflowModal';
import { WorkflowDetailPane } from './WorkflowDetailPane';
import { workflowsConfig } from '../toolrail/configs';
import { apiErrorMessage, apiErrorDetail } from '../../api/client';
import {
  collectionsApi,
  marketplaceApi,
  type MarketplaceCollection,
  type WorkflowCostSummary,
  type InstalledWorkflow,
} from '../../api/marketplace';
import {
  SHELF_CONTAINER,
  SHELF_LIST_COL,
  SHELF_DETAIL_COL,
  shelfRowClass,
  shelfRowMouseDown,
  ShelfAccentBar,
  shelfFilterChipClass,
  shelfFilterCountClass,
  ShelfListSearch,
  ShelfSkeleton,
} from './shelf';

const RUNNING_STATES = ['running', 'pending', 'assigned', 'queued'];
const isRunningStatus = (w: any) => RUNNING_STATES.includes(w.last_run_status || '');

// ── Sort dimensions — the things you actually scan a runner list for. ──
type SortMode = 'recent' | 'attention' | 'runs' | 'name' | 'updated';

const ts = (v: any): number => (v ? new Date(v).getTime() : 0);

function sortWorkflows(list: any[], mode: SortMode): any[] {
  const arr = [...list];
  switch (mode) {
    case 'name':
      return arr.sort((a, b) => cleanTitle(a.name).localeCompare(cleanTitle(b.name)));
    case 'runs':
      return arr.sort((a, b) => (b.usage_count || 0) - (a.usage_count || 0));
    case 'updated':
      return arr.sort((a, b) => ts(b.updated_at) - ts(a.updated_at));
    case 'attention': {
      // Failing first, then running, then the rest by most-recent run.
      const rank = (w: any) => (w.last_run_status === 'failed' ? 0 : isRunningStatus(w) ? 1 : 2);
      return arr.sort((a, b) => rank(a) - rank(b) || ts(b.last_run_at) - ts(a.last_run_at));
    }
    case 'recent':
    default:
      return arr.sort((a, b) => ts(b.last_run_at) - ts(a.last_run_at));
  }
}

// Coarse "kind" buckets for the clean grouping. `manual` folds into recorded;
// unknown types sort last. Running rows are pulled out into a pinned "Live now"
// section ABOVE these, regardless of kind.
const KIND_ORDER = ['recorded', 'ai_navigate', 'api_recorded', 'scheduled', 'pre_check', 'on_change'];
const normKind = (w: any): string => {
  const ty = w.workflow_type || 'recorded';
  return ty === 'manual' ? 'recorded' : ty;
};

/** lg breakpoint (1024px) — master-detail two-pane only renders this wide. */
function useIsWide(): boolean {
  const [wide, setWide] = useState(() =>
    typeof window !== 'undefined' ? window.matchMedia('(min-width: 1024px)').matches : true,
  );
  useEffect(() => {
    const mq = window.matchMedia('(min-width: 1024px)');
    const onChange = (e: MediaQueryListEvent) => setWide(e.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);
  return wide;
}

/** A small tri-state selection checkbox (matches the Monitor / Data views). */
const SelectBox: React.FC<{
  checked: boolean;
  indeterminate?: boolean;
  onChange: () => void;
  label: string;
  className?: string;
}> = ({ checked, indeterminate, onChange, label, className }) => (
  <button
    type="button"
    role="checkbox"
    aria-checked={indeterminate ? 'mixed' : checked}
    aria-label={label}
    onClick={(e) => {
      e.stopPropagation();
      onChange();
    }}
    className={clsx(
      'flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors',
      checked || indeterminate ? 'border-ink bg-ink text-surface' : 'border-border bg-surface hover:border-ink/40',
      className,
    )}
  >
    {indeterminate ? <MinusIcon className="h-3 w-3" /> : checked ? <CheckIcon className="h-3 w-3" /> : null}
  </button>
);

interface WorkflowRowProps {
  w: any;
  selected: boolean;
  checked: boolean;
  isRunningRow: boolean;
  isCancellingRow: boolean;
  /** Owner cost-summary: monetized rows show a compact price/earned tag. */
  summary?: WorkflowCostSummary;
  /** Own, groupable workflow — enables the iOS-style drag-to-folder grip + drop target. */
  canCollect: boolean;
  isDropTarget: boolean;
  isDragging: boolean;
  onSelect: (w: any) => void;
  onToggleCheck: () => void;
  onRun: (w: any) => void;
  onCancel: (w: any) => void;
  onDragStartRow?: (id: number) => void;
  onDragEndRow?: () => void;
  onDragOverRow?: (id: number) => void;
  onDragLeaveRow?: () => void;
  onDropRow?: (id: number) => void;
}

/**
 * Compact master-list row. Memoized so a poll tick returning identical data (or
 * selection churn on OTHER rows) doesn't re-render every row.
 */
const WorkflowRow: React.FC<WorkflowRowProps> = React.memo(({
  w,
  selected,
  checked,
  isRunningRow,
  isCancellingRow,
  summary,
  canCollect,
  isDropTarget,
  isDragging,
  onSelect,
  onToggleCheck,
  onRun,
  onCancel,
  onDragStartRow,
  onDragEndRow,
  onDragOverRow,
  onDragLeaveRow,
  onDropRow,
}) => {
  const { t } = useTranslation();
  const meta = TYPE_META[w.workflow_type] || TYPE_META.recorded;
  const TypeIcon = meta.icon;
  const isRunning = isRunningStatus(w);
  const isInstalled = !!w.is_installed;
  const failed = w.last_run_status === 'failed';
  const monetized = !!summary?.is_monetized;
  // Origin marker (cloud | mirrored | local:<agent>) added by the P1/coordinator
  // merged list; treat missing/unknown as plain cloud (unbadged).
  const origin = (w as any).origin as string | undefined;

  return (
    <div
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      aria-label={cleanTitle(w.name)}
      onClick={() => onSelect(w)}
      onMouseDown={shelfRowMouseDown}
      onKeyDown={(e) => {
        if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) {
          e.preventDefault();
          onSelect(w);
        }
      }}
      // Own workflows are drop targets: drop one onto another → create a collection.
      onDragEnter={(e) => { if (canCollect && onDragStartRow) e.preventDefault(); }}
      onDragOver={(e) => {
        if (canCollect && onDragOverRow) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; onDragOverRow(w.id); }
      }}
      onDragLeave={() => onDragLeaveRow?.()}
      onDrop={(e) => { if (canCollect && onDropRow) { e.preventDefault(); onDropRow(w.id); } }}
      className={clsx(
        shelfRowClass(selected),
        isDropTarget && 'bg-chrome ring-2 ring-ink/30',
        isDragging && 'opacity-50',
      )}
    >
      {/* Selected accent bar. */}
      {selected && <ShelfAccentBar />}

      {/* Leading tile — the type icon, which flips to a multi-select checkbox on
          hover or when checked (so the row stays clean but bulk select is one click away). */}
      <button
        type="button"
        draggable={canCollect}
        onDragStart={(e) => {
          if (!canCollect) { e.preventDefault(); return; }
          e.stopPropagation();
          try { e.dataTransfer.setData('text/plain', String(w.id)); } catch { /* noop */ }
          e.dataTransfer.effectAllowed = 'move';
          onDragStartRow?.(w.id);
        }}
        onDragEnd={() => onDragEndRow?.()}
        onClick={(e) => { e.stopPropagation(); onToggleCheck(); }}
        aria-label={t('Select workflow')}
        aria-pressed={checked}
        className="relative w-8 h-8 shrink-0 rounded-lg outline-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/20"
        title={canCollect ? t('Drag onto another workflow or a folder to group them') : undefined}
      >
        {/* checkbox layer */}
        <span
          className={clsx(
            'absolute inset-0 flex items-center justify-center rounded-lg border transition-opacity',
            checked
              ? 'bg-ink border-ink text-surface opacity-100'
              : 'bg-surface border-border opacity-0 group-hover:opacity-100',
          )}
        >
          <CheckIcon className={clsx('h-3.5 w-3.5 transition-opacity', checked ? 'opacity-100' : 'opacity-0')} />
        </span>
        {/* icon layer */}
        <span
          style={tintStyle(isInstalled ? 'neutral' : workflowTint(w.workflow_type))}
          className={clsx(
            'absolute inset-0 flex items-center justify-center rounded-lg transition-opacity',
            checked ? 'opacity-0' : 'opacity-100 group-hover:opacity-0',
          )}
        >
          {isRunning && (
            <span className="absolute inset-0 rounded-lg border-2 border-current/25 border-t-current animate-spin" />
          )}
          <TypeIcon className="h-4 w-4" />
        </span>
        {isInstalled && !checked && (
          <span className="absolute -bottom-1 -right-1 w-3.5 h-3.5 rounded-full bg-accent-strong text-accent-on flex items-center justify-center transition-opacity group-hover:opacity-0">
            <LockClosedIcon className="w-2 h-2" />
          </span>
        )}
      </button>

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5 min-w-0">
          {/* Name: truncates at rest; on row hover the inner span marquees to
              reveal the trailing overflow. `container-type: inline-size` lets
              the keyframe translate by `100cqw - 100%`, which is 0px when the
              name already fits — so a short name never animates. `title` keeps
              the full text accessible as a tooltip. The wrapper takes `flex-1
              min-w-0` so the trailing `Selling` badge doesn't get shoved out. */}
          <div
            className="flex-1 min-w-0 text-[13px] font-medium text-ink overflow-hidden"
            style={{ containerType: 'inline-size' }}
            title={w.name || undefined}
          >
            <span className="inline-block whitespace-nowrap min-w-full group-hover:[animation:marquee-hover_8s_ease-in-out_infinite]">
              {cleanTitle(w.name)}
            </span>
          </div>
          {monetized && (
            <span className="shrink-0 text-[9px] font-semibold uppercase tracking-wide px-1 py-[1px] rounded bg-success-bg text-success-fg">
              {t('Selling')}
            </span>
          )}
          {origin === 'mirrored' && (
            <span className="shrink-0 text-[9px] font-semibold uppercase tracking-wide px-1 py-[1px] rounded bg-chrome text-secondary">
              {t('Mirrored')}
            </span>
          )}
          {origin?.startsWith('local:') && (
            <span
              className="shrink-0 text-[9px] font-semibold tracking-wide px-1 py-[1px] rounded bg-chrome text-secondary"
              title={t('Runs locally on {{agent}}', { agent: origin.slice('local:'.length) })}
            >
              {t('On {{agent}} · local', { agent: origin.slice('local:'.length) })}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1 text-[11px] truncate mt-0.5">
          <span className="text-tertiary truncate">{meta.label}</span>
          <span className="text-tertiary/50 shrink-0">·</span>
          {isRunning ? (
            <span className="text-warning font-medium shrink-0">{t('Running')}</span>
          ) : failed ? (
            <span className="text-danger font-medium shrink-0">{t('Failed')}</span>
          ) : !w.is_active ? (
            <span className="text-tertiary shrink-0">{t('Paused')}</span>
          ) : w.last_run_at ? (
            <span className="text-tertiary shrink-0">{t('{{ago}} ago', { ago: formatElapsedShort(w.last_run_at) })}</span>
          ) : (
            <span className="text-tertiary shrink-0">{t('Never run')}</span>
          )}
        </div>
      </div>

      {/* Quick run / stop — the one trailing control. */}
      {isRunning ? (
        <button
          onClick={(e) => { e.stopPropagation(); onCancel(w); }}
          disabled={isCancellingRow}
          title={t('Stop')}
          aria-label={t('Stop')}
          className="flex items-center justify-center w-7 h-7 rounded-lg border border-border text-secondary hover:bg-chrome transition-colors disabled:opacity-50 shrink-0"
        >
          <StopIcon className="h-4 w-4" />
        </button>
      ) : (
        <button
          onClick={(e) => { e.stopPropagation(); onRun(w); }}
          disabled={isRunningRow}
          title={t('Run')}
          aria-label={t('Run')}
          className={clsx(
            'flex items-center justify-center w-7 h-7 rounded-lg transition-all active:scale-95 disabled:opacity-50 shrink-0',
            selected
              ? 'bg-accent-strong text-accent-on hover:bg-accent-strong/90'
              : 'text-tertiary hover:bg-chrome hover:text-ink',
          )}
        >
          <PlayIcon className="h-4 w-4" />
        </button>
      )}
    </div>
  );
});
WorkflowRow.displayName = 'WorkflowRow';

interface WorkflowListProps {
  search: string;
  /** When set, the list renders its own search input at the top of the master column. */
  onSearchChange?: (v: string) => void;
  /** Optional override for opening a workflow (narrow screens); defaults to navigation. */
  onItemClick?: (id: number) => void;
  filter?: (w: any) => boolean;
  /** Per-workflow cost/gains map (owner-only ids). Drives the "Selling" tag + the
   *  monetization section in the detail pane. */
  costSummary?: Record<number, WorkflowCostSummary>;
  /** When the caller renders OTHER content, suppress the full-screen empty state. */
  suppressEmptyState?: boolean;
  /** When this (cloud, default) list is empty, advertise workflows on connected
   *  desktop devices + a way to switch to the Local Devices tab. */
  deviceWfCount?: number;
  onViewDevices?: () => void;
}

export const WorkflowList: React.FC<WorkflowListProps> = ({ search, onSearchChange, onItemClick, filter, costSummary, suppressEmptyState, deviceWfCount = 0, onViewDevices }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const isWide = useIsWide();

  const [pollInterval, setPollInterval] = useState<number | undefined>(undefined);
  const { data: workflows, loading, error, refresh } = useQuery(Q.workflows(), () => automationApi.listWorkflows(), { pollInterval });

  // Poll fast while any workflow run is in flight, stop when idle. Adjusted
  // during render (guarded, so it converges in one extra pass) rather than in an
  // effect, which would commit the stale cadence before tearing it down.
  const wantPoll = (workflows || []).some(isRunningStatus) ? 3000 : undefined;
  if (pollInterval !== wantPoll) setPollInterval(wantPoll);

  // Site crawls (Dragnet) are their own first-class Build surface now — the /crawls
  // shelf + dedicated /crawls/:id detail page — so they no longer appear as rows
  // here. The synthetic per-crawl aggregation workflow is still filtered out below.

  // ── Filter (view) + sort, persisted ───────────────────────────────────────
  const [view, setView] = useState<string>(() => localStorage.getItem('writ.wfView') || 'all');
  const [sortBy, setSortBy] = useState<SortMode>(() => {
    const v = localStorage.getItem('writ.wfSortBy');
    return (['recent', 'attention', 'runs', 'name', 'updated'] as const).includes(v as any) ? (v as SortMode) : 'recent';
  });
  useEffect(() => { try { localStorage.setItem('writ.wfView', view); } catch { /* noop */ } }, [view]);
  useEffect(() => { try { localStorage.setItem('writ.wfSortBy', sortBy); } catch { /* noop */ } }, [sortBy]);

  // Search + external filter applied first; view chips + counts compute off this.
  // Streaming workflows live on their own /streaming page — kept OUT of the master
  // list (surfaced via the pointer banner below).
  const { searched, streamingList } = useMemo(() => {
    const q = search.toLowerCase();
    const base = (workflows || [])
      // Soft-archived workflows ('Unpublish & remove') leave the library. The server
      // already excludes them; this is a defense-in-depth fallback.
      .filter((w: any) => !w.archived_at)
      // A crawl's synthetic aggregation workflow ('crawl') is represented by its
      // own crawl row + dedicated /crawls/:id page — never a generic workflow row.
      .filter((w: any) => w.workflow_type !== 'crawl')
      .filter((w: any) => !search || w.name?.toLowerCase().includes(q))
      .filter((w: any) => !filter || filter(w));
    return {
      searched: base.filter((w: any) => w.workflow_type !== 'streaming'),
      streamingList: base.filter((w: any) => w.workflow_type === 'streaming'),
    };
  }, [workflows, search, filter]);

  const views = workflowsConfig.views;
  const viewCounts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const v of views) m[v.id] = searched.filter(v.predicate).length;
    return m;
  }, [searched, views]);

  const activeView = views.find((v) => v.id === view) || views[0];
  const workflowList = useMemo(
    () => sortWorkflows(searched.filter(activeView.predicate), sortBy),
    [searched, activeView, sortBy],
  );

  // ── Sectioning: running pinned to a "Live now" group at the top, then a clean
  //    grouping by kind. Sort order is preserved WITHIN each section. ──
  const sections = useMemo(() => {
    const live: any[] = [];
    const byKind = new Map<string, any[]>();
    for (const w of workflowList) {
      if (isRunningStatus(w)) { live.push(w); continue; }
      const k = normKind(w);
      if (!byKind.has(k)) byKind.set(k, []);
      byKind.get(k)!.push(w);
    }
    const orderedKinds = [...byKind.keys()].sort((a, b) => {
      const ia = KIND_ORDER.indexOf(a); const ib = KIND_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });
    const secs: Array<{ key: string; label: string; items: any[]; live: boolean }> = [];
    if (live.length) secs.push({ key: '__live', label: t('Live now'), items: live, live: true });
    for (const k of orderedKinds) secs.push({ key: k, label: TYPE_META[k]?.label || k, items: byKind.get(k)!, live: false });
    return secs;
  }, [workflowList, t]);
  const flatOrdered = useMemo(() => sections.flatMap((s) => s.items), [sections]);
  const showHeaders = sections.length > 1;

  // ── Selection: one drives the detail pane, a set drives bulk actions ───────
  const [pickedId, setSelectedId] = useState<number | null>(null);
  const [rawCheckedIds, setCheckedIds] = useState<Set<number>>(new Set());

  const orderedIds = useMemo(() => new Set(flatOrdered.map((w: any) => w.id)), [flatOrdered]);
  const visibleIds = useMemo(() => new Set(workflowList.map((w: any) => w.id)), [workflowList]);

  // The detail selection is DERIVED, not stored: the user's pick while the row
  // is still in the list, otherwise the first row. Deriving it during render
  // avoids a frame painted against a workflow that just left the list.
  const selectedId = useMemo(() => {
    if (!isWide) return pickedId;
    if (flatOrdered.length === 0) return null;
    return pickedId != null && orderedIds.has(pickedId) ? pickedId : flatOrdered[0].id;
  }, [pickedId, flatOrdered, orderedIds, isWide]);

  // Bulk checks are pruned ON READ — a workflow leaving the list (filter,
  // search, delete) drops out of the effective selection with no state write.
  const checkedIds = useMemo(
    () => new Set([...rawCheckedIds].filter((idv) => visibleIds.has(idv))),
    [rawCheckedIds, visibleIds],
  );

  const toggleCheck = useCallback((id: number) => {
    setCheckedIds((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });
  }, []);
  const clearChecks = useCallback(() => setCheckedIds(new Set()), []);

  const checkedWorkflows = useMemo(
    () => workflowList.filter((w: any) => checkedIds.has(w.id)),
    [workflowList, checkedIds],
  );

  const handleSelect = useCallback((w: any) => {
    if (isWide) setSelectedId(w.id);
    else if (onItemClick) onItemClick(w.id);
    else navigate(`/workflows/${w.id}`);
  }, [isWide, onItemClick, navigate]);

  // ── Mutations (shared by rows + the detail pane) ───────────────────────────
  const [runningId, setRunningId] = useState<number | null>(null);
  const [cancellingId, setCancellingId] = useState<number | null>(null);
  const [mutatingId, setMutatingId] = useState<number | null>(null);
  const [runModalWorkflow, setRunModalWorkflow] = useState<any | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<{ id: number; name: string } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  // Surfaced when deleteWorkflow returns 409 (the workflow is on the marketplace
  // or has active installs). Offers "Unpublish & remove".
  const [unpublishRemoveTarget, setUnpublishRemoveTarget] = useState<{ id: number; name: string; installCount: number } | null>(null);
  const [unpublishRemoving, setUnpublishRemoving] = useState(false);
  // Installed-proxy uninstall is a marketplace op (by listing slug), distinct from delete.
  const [uninstallTarget, setUninstallTarget] = useState<{ id: number; name: string; slug: string | null; listingId: number | null } | null>(null);
  const [uninstalling, setUninstalling] = useState(false);

  const handleRun = useCallback(async (w: any, target?: 'auto' | 'local' | 'cloud', agentId?: string) => {
    // Route through the modal when the run needs input data.
    if (workflowNeedsInput(w)) {
      setRunModalWorkflow(w);
      return;
    }
    setRunningId(w.id);
    try {
      const result = await automationApi.runWorkflow(w.id, target, w.default_persona_id ?? undefined, agentId);
      const msg = result.message || t('Workflow "{{name}}" dispatched — Task #{{taskId}}', { name: w.name, taskId: result.task_id });
      if (result.queued) toast(msg, { icon: toastIcon(ClockIcon), duration: 6000 });
      else toast.success(msg, { duration: 4000 });
      refresh();
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      toast.error(typeof detail === 'string' ? detail : t('Failed to run workflow'));
    } finally {
      setRunningId(null);
    }
  }, [refresh, t]);

  const handleCancel = useCallback(async (w: any) => {
    if (!w.last_run_task_id) return;
    setCancellingId(w.id);
    try {
      await automationApi.cancelTask(w.last_run_task_id);
      toast.success(t('Task cancelled'));
      refresh();
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      toast.error(typeof detail === 'string' ? detail : t('Failed to cancel'));
    } finally {
      setCancellingId(null);
    }
  }, [refresh, t]);

  const handleDuplicate = useCallback(async (id: number) => {
    if (mutatingId) return;
    setMutatingId(id);
    try {
      await automationApi.duplicateWorkflow(id);
      toast.success(t('Workflow duplicated'));
      refresh();
    } catch {
      toast.error(t('Failed to duplicate'));
    } finally {
      setMutatingId(null);
    }
  }, [mutatingId, refresh, t]);

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await automationApi.deleteWorkflow(deleteTarget.id);
      toast.success(t('Workflow deleted'));
      const qc = useQueryCache.getState();
      qc.invalidate(Q.workflow(deleteTarget.id), Q.workflowTasks(deleteTarget.id));
      qc.invalidateMatching(`workflow:${deleteTarget.id}`);
      refresh();
      setDeleteTarget(null);
    } catch (err) {
      // 409 = the workflow is on the marketplace or has active installs. Never
      // cascade-delete buyers' installs — offer "Unpublish & remove" instead.
      const d = apiErrorDetail<{ code?: string; install_count?: number }>(err);
      if ((err as any)?.response?.status === 409 && d?.code === 'workflow_has_marketplace_dependents') {
        setUnpublishRemoveTarget({ id: deleteTarget.id, name: deleteTarget.name, installCount: d.install_count ?? 0 });
        setDeleteTarget(null);
      } else {
        toast.error(apiErrorMessage(err, t('Failed to delete workflow')));
      }
    } finally {
      setDeleting(false);
    }
  };

  const handleUnpublishRemove = async () => {
    if (!unpublishRemoveTarget) return;
    setUnpublishRemoving(true);
    try {
      await automationApi.unpublishAndRemove(unpublishRemoveTarget.id);
      toast.success(t('Removed from the marketplace and your library'));
      const qc = useQueryCache.getState();
      qc.invalidate(Q.workflow(unpublishRemoveTarget.id), Q.workflowTasks(unpublishRemoveTarget.id));
      qc.invalidateMatching(`workflow:${unpublishRemoveTarget.id}`);
      refresh();
      setUnpublishRemoveTarget(null);
    } catch (err) {
      toast.error(apiErrorMessage(err, t('Failed to remove workflow')));
    } finally {
      setUnpublishRemoving(false);
    }
  };

  const handleUninstall = async () => {
    if (!uninstallTarget) return;
    setUninstalling(true);
    try {
      let slug = uninstallTarget.slug;
      if (!slug && uninstallTarget.listingId != null) {
        const list = (await marketplaceApi.listInstalls()) as any[];
        slug = list.find((i) => i?.listing_id === uninstallTarget.listingId)?.slug || null;
      }
      if (!slug) {
        toast.error(t('Could not find this install to remove'));
        return;
      }
      await marketplaceApi.uninstall(slug);
      toast.success(t('Uninstalled'));
      const qc = useQueryCache.getState();
      qc.invalidate(Q.workflow(uninstallTarget.id), Q.workflowTasks(uninstallTarget.id), Q.marketplaceInstalls());
      qc.invalidateMatching('marketplace:install');
      refresh();
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to uninstall')));
    } finally {
      setUninstalling(false);
      setUninstallTarget(null);
    }
  };

  const handleBulkToggle = useCallback(async (active: boolean) => {
    const targets = checkedWorkflows.filter((w: any) => w.is_active !== active);
    if (!targets.length) return;
    setBulkBusy(true);
    let ok = 0; let fail = 0;
    for (const w of targets) {
      try { await automationApi.updateWorkflow(w.id, { is_active: active }); ok += 1; }
      catch { fail += 1; }
    }
    setBulkBusy(false);
    refresh();
    if (fail === 0) toast.success(active ? t('Activated {{n}}', { n: ok }) : t('Deactivated {{n}}', { n: ok }));
    else toast.error(t('{{ok}} updated · {{fail}} failed', { ok, fail }));
  }, [checkedWorkflows, refresh, t]);

  const handleBulkDelete = useCallback(async () => {
    const targets = checkedWorkflows.filter((w: any) => !w.is_installed);
    const skipped = checkedWorkflows.length - targets.length;
    setBulkBusy(true);
    let ok = 0; let fail = 0;
    const qc = useQueryCache.getState();
    for (const w of targets) {
      try {
        await automationApi.deleteWorkflow(w.id);
        qc.invalidate(Q.workflow(w.id), Q.workflowTasks(w.id));
        qc.invalidateMatching(`workflow:${w.id}`);
        ok += 1;
      } catch { fail += 1; }
    }
    setBulkBusy(false);
    setBulkDeleteOpen(false);
    clearChecks();
    refresh();
    const tail = skipped ? ` · ${t('{{n}} installed skipped', { n: skipped })}` : '';
    if (fail === 0) toast.success(t('Deleted {{n}}', { n: ok }) + tail);
    else toast.error(t('{{ok}} deleted · {{fail}} couldn’t be deleted', { ok, fail }));
  }, [checkedWorkflows, clearChecks, refresh, t]);

  // ── Installed-proxy join (uninstall slug + "needs your data" state) ────────
  const hasInstalled = useMemo(() => (workflows || []).some((w: any) => !!w.is_installed), [workflows]);
  const { data: installs } = useQuery<InstalledWorkflow[]>(
    Q.marketplaceInstalls(),
    () => marketplaceApi.listInstalls(),
    { enabled: hasInstalled, silent: true },
  );
  const slugByListingId = useMemo(() => {
    const m: Record<number, string> = {};
    for (const i of installs || []) {
      if (i?.listing_id != null && i?.slug) m[i.listing_id] = i.slug;
    }
    return m;
  }, [installs]);

  // ── iOS-style drag-to-folder (collections) — admin-only, preserved ─────────
  const { data: collectionsData, refresh: refreshCollections } = useQuery(
    Q.key('creator:collections:library'),
    () => collectionsApi.listMine(),
    { silent: true },
  );
  const collections: MarketplaceCollection[] = useMemo(() => {
    const seen = new Set<number>();
    const out: MarketplaceCollection[] = [];
    for (const c of collectionsData?.collections ?? []) {
      if (c.id == null || seen.has(c.id)) continue;
      seen.add(c.id);
      out.push(c);
    }
    return out;
  }, [collectionsData]);
  const [dragWfId, setDragWfId] = useState<number | null>(null);
  const [dropTargetId, setDropTargetId] = useState<number | null>(null);
  const [dropFolderId, setDropFolderId] = useState<number | null>(null);
  const [folderBusy, setFolderBusy] = useState(false);
  // SYNCHRONOUS guard: two drop events in the same tick would BOTH pass the state
  // check and each create a collection. A ref flips immediately.
  const folderOpRef = useRef(false);

  const handleDragStartRow = useCallback((id: number) => setDragWfId(id), []);
  const handleDragEndRow = useCallback(() => { setDragWfId(null); setDropTargetId(null); setDropFolderId(null); }, []);
  const handleDragOverRow = useCallback((id: number) => {
    if (dragWfId != null && dragWfId !== id) setDropTargetId(id);
  }, [dragWfId]);
  const handleDragLeaveRow = useCallback(() => setDropTargetId(null), []);

  const nameFor = useCallback((id: number | null) => {
    const w = (workflows || []).find((x: any) => x.id === id);
    return w ? cleanTitle(w.name) : t('this workflow');
  }, [workflows, t]);

  const handleDropRow = useCallback(async (targetId: number) => {
    const sourceId = dragWfId;
    setDropTargetId(null);
    if (sourceId == null || sourceId === targetId || folderBusy || folderOpRef.current) return;
    folderOpRef.current = true;
    setFolderBusy(true);
    let created: MarketplaceCollection | null = null;
    try {
      const a = nameFor(sourceId);
      const b = nameFor(targetId);
      const folderName = `${a} + ${b}`.slice(0, 60);
      created = await collectionsApi.create({ name: folderName, visibility: 'unlisted' });
      await collectionsApi.setItems(created.id as number, [
        { workflow_id: sourceId, sort_order: 0 },
        { workflow_id: targetId, sort_order: 1 },
      ]);
      await refreshCollections();
      toast.success(t('Folder created with 2 APIs — set pricing in Collections'));
    } catch (e: any) {
      if (created?.id != null) { try { await collectionsApi.remove(created.id); } catch { /* noop */ } }
      const code = e?.response?.data?.detail?.code;
      toast.error(
        code === 'member_not_published'
          ? t('Publish both workflows to the marketplace first, then group them.')
          : (e?.response?.data?.detail?.message || t('Could not create the folder')),
      );
    } finally {
      setFolderBusy(false);
      setDragWfId(null);
      folderOpRef.current = false;
    }
  }, [dragWfId, folderBusy, refreshCollections, nameFor, t]);

  const handleDropOnFolder = useCallback(async (collectionId: number) => {
    const sourceId = dragWfId;
    setDropFolderId(null);
    if (sourceId == null || folderBusy || folderOpRef.current) return;
    folderOpRef.current = true;
    const coll = collections.find((c) => c.id === collectionId);
    if (!coll) { folderOpRef.current = false; return; }
    const existing = (coll.items || []).map((it) => ({
      workflow_id: it.workflow_id,
      sort_order: it.sort_order,
      price_per_success_micros: it.price_per_success_micros ?? undefined,
    }));
    if (existing.some((it) => it.workflow_id === sourceId)) {
      toast(t('Already in {{name}}', { name: coll.name }));
      setDragWfId(null);
      folderOpRef.current = false;
      return;
    }
    setFolderBusy(true);
    try {
      await collectionsApi.setItems(collectionId, [
        ...existing,
        { workflow_id: sourceId, sort_order: existing.length },
      ]);
      await refreshCollections();
      toast.success(t('Added {{wf}} to {{name}}', { wf: nameFor(sourceId), name: coll.name }));
    } catch (e: any) {
      const code = e?.response?.data?.detail?.code;
      toast.error(
        code === 'member_not_published'
          ? t('Publish {{wf}} to the marketplace first.', { wf: nameFor(sourceId) })
          : (e?.response?.data?.detail?.message || t('Could not add to the folder')),
      );
    } finally {
      setFolderBusy(false);
      setDragWfId(null);
      folderOpRef.current = false;
    }
  }, [dragWfId, folderBusy, collections, refreshCollections, nameFor, t]);

  // Which collections the SELECTED workflow belongs to (detail-pane section).
  const memberCollectionsFor = useCallback((wfId: number | null) => {
    if (wfId == null) return [];
    return collections.filter((c) => (c.items || []).some((it) => it.workflow_id === wfId));
  }, [collections]);

  // ── Load / error / empty states ───────────────────────────────────────────
  if (loading && !workflows) return <ShelfSkeleton withSearch={!!onSearchChange} label={t('Loading workflows')} />;
  if (!workflows && error) return <ErrorState message={error} onRetry={refresh} />;
  if (workflowList.length === 0 && streamingList.length === 0) {
    if (suppressEmptyState) return null;
    return <EmptyState search={search} deviceWfCount={deviceWfCount} onViewDevices={onViewDevices} />;
  }

  const selectedWorkflow = workflowList.find((w: any) => w.id === selectedId) || null;
  const allChecked = workflowList.length > 0 && checkedIds.size >= workflowList.length;
  const someChecked = checkedIds.size > 0 && !allChecked;
  const selUninstallSlug = selectedWorkflow?.is_installed
    ? ((selectedWorkflow as any).source_listing_slug || slugByListingId[(selectedWorkflow as any).source_listing_id] || null)
    : null;

  return (
    <div className={SHELF_CONTAINER}>
      {/* ── Master list ── */}
      <div className={SHELF_LIST_COL}>
        {/* Filter + sort header */}
        <div className="shrink-0 border-b border-border px-3 py-2.5 space-y-2">
          {/* Search — lives with the list it filters, above the chips. */}
          {onSearchChange && <ShelfListSearch value={search} onChange={onSearchChange} ariaLabel={t('Search workflows')} />}
          <div className="flex flex-wrap items-center gap-1">
            {views.map((v) => {
              const Icon = v.icon;
              const active = v.id === view;
              const count = viewCounts[v.id] ?? 0;
              if (count === 0 && v.id !== 'all' && !active) return null;
              return (
                <button
                  key={v.id}
                  onClick={() => setView(v.id)}
                  aria-pressed={active}
                  className={shelfFilterChipClass(active)}
                >
                  <Icon className="w-3 h-3" />
                  {t(v.label)}
                  <span className={shelfFilterCountClass(active)}>{count}</span>
                </button>
              );
            })}
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11px] text-tertiary">{t('{{n}} shown', { n: workflowList.length })}</span>
            <Select<SortMode>
              size="sm"
              value={sortBy}
              onChange={setSortBy}
              aria-label={t('Sort by')}
              wrapperClassName="w-36"
              options={[
                { value: 'recent', label: t('Last run') },
                { value: 'attention', label: t('Needs attention') },
                { value: 'runs', label: t('Most runs') },
                { value: 'updated', label: t('Recently edited') },
                { value: 'name', label: t('Name A–Z') },
              ]}
            />
          </div>
        </div>

        {/* Bulk action bar */}
        {checkedIds.size > 0 && (
          <div className="shrink-0 flex items-center justify-between gap-2 border-b border-border bg-chrome px-3 py-2 bulkbar-enter">
            <div className="flex items-center gap-2 text-[12px]">
              <SelectBox
                checked={allChecked}
                indeterminate={someChecked}
                onChange={() =>
                  setCheckedIds(allChecked ? new Set() : new Set(workflowList.map((w: any) => w.id)))
                }
                label={t('Select all')}
              />
              <span className="font-medium text-ink">{t('{{n}} selected', { n: checkedIds.size })}</span>
              <button onClick={clearChecks} className="text-tertiary hover:text-ink transition-colors">{t('Clear')}</button>
            </div>
            <div className="flex items-center gap-1">
              <button onClick={() => handleBulkToggle(true)} disabled={bulkBusy} title={t('Activate')}
                className="p-1.5 rounded-lg text-secondary hover:bg-surface hover:text-ink disabled:opacity-50 transition-colors">
                <PlayIcon className="h-3.5 w-3.5" />
              </button>
              <button onClick={() => handleBulkToggle(false)} disabled={bulkBusy} title={t('Deactivate')}
                className="p-1.5 rounded-lg text-secondary hover:bg-surface hover:text-ink disabled:opacity-50 transition-colors">
                <StopIcon className="h-3.5 w-3.5" />
              </button>
              <button onClick={() => setBulkDeleteOpen(true)} disabled={bulkBusy} title={t('Delete')}
                className="p-1.5 rounded-lg text-danger hover:bg-danger-bg hover:text-danger-fg disabled:opacity-50 transition-colors">
                <TrashIcon className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        )}

        {/* Rows */}
        {workflowList.length === 0 ? (
          <div className="flex flex-1 flex-col items-center justify-center text-center px-6 py-10">
            <p className="text-[13px] font-medium text-ink">{t('Nothing here')}</p>
            <p className="text-[11px] text-secondary mt-1">{t('No workflows match this filter.')}</p>
          </div>
        ) : (
          <ScrollArea className="flex-1" viewportClassName="px-2 py-2" fade="chrome">
            {/* Streaming pointer banner + collections (folders) live above the rows. */}
            {streamingList.length > 0 && (
              <StreamingBanner count={streamingList.length} onOpen={() => navigate('/streaming')} />
            )}

            {(collections.length > 0 || dragWfId != null) && (
              <CollectionsStrip
                collections={collections}
                dragging={dragWfId != null}
                dropFolderId={dropFolderId}
                onManage={() => navigate('/collections')}
                onDragOverFolder={(id) => setDropFolderId(id)}
                onDragLeaveFolder={() => setDropFolderId(null)}
                onDropOnFolder={handleDropOnFolder}
              />
            )}

            {sections.map((sec, si) => (
              <div key={sec.key} className={si > 0 ? 'mt-3' : ''}>
                {showHeaders && (
                  <div className="flex items-center gap-1.5 px-2 pt-1 pb-1.5">
                    {sec.live ? (
                      <span className="relative flex h-1.5 w-1.5" aria-hidden="true">
                        <span className="absolute inline-flex h-full w-full rounded-full bg-warning opacity-60 animate-ping" />
                        <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-warning" />
                      </span>
                    ) : null}
                    <span className={clsx('text-[10px] font-semibold uppercase tracking-wider', sec.live ? 'text-ink' : 'text-tertiary')}>{sec.label}</span>
                    <span className="text-[10px] tabular-nums text-tertiary">{sec.items.length}</span>
                  </div>
                )}
                <Stagger className="grid gap-0.5" staggerMs={16}>
                  {sec.items.map((w: any) => (
                    <WorkflowRow
                      key={w.id}
                      w={w}
                      selected={isWide && selectedId === w.id}
                      checked={checkedIds.has(w.id)}
                      isRunningRow={runningId === w.id}
                      isCancellingRow={cancellingId === w.id}
                      summary={costSummary?.[w.id]}
                      canCollect={!w.is_installed}
                      isDropTarget={dropTargetId === w.id}
                      isDragging={dragWfId === w.id}
                      onSelect={handleSelect}
                      onToggleCheck={() => toggleCheck(w.id)}
                      onRun={handleRun}
                      onCancel={handleCancel}
                      onDragStartRow={handleDragStartRow}
                      onDragEndRow={handleDragEndRow}
                      onDragOverRow={handleDragOverRow}
                      onDragLeaveRow={handleDragLeaveRow}
                      onDropRow={handleDropRow}
                    />
                  ))}
                </Stagger>
              </div>
            ))}
          </ScrollArea>
        )}
      </div>

      {/* ── Detail pane (lg only) ── */}
      <div className={SHELF_DETAIL_COL}>
        <WorkflowDetailPane
          workflow={selectedWorkflow}
          summary={selectedWorkflow ? costSummary?.[selectedWorkflow.id] : undefined}
          memberCollections={memberCollectionsFor(selectedWorkflow?.id ?? null)}
          onRun={handleRun}
          onCancel={handleCancel}
          onDelete={setDeleteTarget}
          onUninstall={setUninstallTarget}
          uninstallSlug={selUninstallSlug}
          onDuplicate={handleDuplicate}
          runningRow={selectedWorkflow ? runningId === selectedWorkflow.id : false}
          cancellingRow={selectedWorkflow ? cancellingId === selectedWorkflow.id : false}
          mutatingRow={selectedWorkflow ? mutatingId === selectedWorkflow.id : false}
          onChanged={refresh}
        />
      </div>

      {/* ── Modals ── */}
      <Modal isOpen={!!deleteTarget} onClose={() => setDeleteTarget(null)} title={t('Delete Workflow')}>
        <p className="text-sm text-secondary mb-4">
          {t('Are you sure you want to delete')} <strong className="text-ink">{deleteTarget?.name}</strong>? {t('This action cannot be undone.')}
        </p>
        <div className="flex items-center justify-end gap-2">
          <button onClick={() => setDeleteTarget(null)}
            className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg hover:bg-chrome transition-colors">
            {t('Cancel')}
          </button>
          <button onClick={handleDelete} disabled={deleting}
            className="px-4 py-2 text-sm font-medium bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors disabled:opacity-50">
            {deleting ? t('Deleting...') : t('Delete')}
          </button>
        </div>
      </Modal>

      <Modal isOpen={!!unpublishRemoveTarget} onClose={() => setUnpublishRemoveTarget(null)} title={t('On the marketplace')}>
        <p className="text-sm text-secondary mb-1">
          {t('This workflow is on the marketplace ({{n}} active install(s)). Deleting it would strip buyers of the workflow they paid for.', { n: unpublishRemoveTarget?.installCount ?? 0 })}
        </p>
        <p className="text-xs text-tertiary mb-5">
          {t('Unpublish & remove takes it off the marketplace and out of your library — buyers keep running their installed copy on the version they have.')}
        </p>
        <div className="flex items-center justify-end gap-2">
          <button onClick={() => setUnpublishRemoveTarget(null)}
            className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg hover:bg-chrome transition-colors">
            {t('Cancel')}
          </button>
          <button onClick={handleUnpublishRemove} disabled={unpublishRemoving}
            className="px-4 py-2 text-sm font-medium bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 transition-colors disabled:opacity-50">
            {unpublishRemoving ? t('Removing…') : t('Unpublish & remove')}
          </button>
        </div>
      </Modal>

      <Modal isOpen={!!uninstallTarget} onClose={() => setUninstallTarget(null)} title={t('Uninstall Workflow')}>
        <p className="text-sm text-secondary mb-4">
          {t('Are you sure you want to uninstall')} <strong className="text-ink">{uninstallTarget?.name}</strong>? {t('Your attached data and bindings will be removed. You can reinstall it from the marketplace later.')}
        </p>
        <div className="flex items-center justify-end gap-2">
          <button onClick={() => setUninstallTarget(null)}
            className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg hover:bg-chrome transition-colors">
            {t('Cancel')}
          </button>
          <button onClick={handleUninstall} disabled={uninstalling}
            className="px-4 py-2 text-sm font-medium bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors disabled:opacity-50">
            {uninstalling ? t('Uninstalling…') : t('Uninstall')}
          </button>
        </div>
      </Modal>

      <Modal isOpen={bulkDeleteOpen} onClose={() => (bulkBusy ? null : setBulkDeleteOpen(false))} title={t('Delete workflows')}>
        {(() => {
          const owned = checkedWorkflows.filter((w: any) => !w.is_installed).length;
          const installed = checkedWorkflows.length - owned;
          return (
            <>
              <p className="text-sm text-secondary mb-1">
                {t('Delete {{n}} workflow(s)? This cannot be undone.', { n: owned })}
              </p>
              {installed > 0 && (
                <p className="text-xs text-tertiary mb-4">
                  {t('{{n}} installed workflow(s) in your selection will be skipped — uninstall those individually.', { n: installed })}
                </p>
              )}
              <div className="flex items-center justify-end gap-2 mt-4">
                <button onClick={() => setBulkDeleteOpen(false)} disabled={bulkBusy}
                  className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg hover:bg-chrome transition-colors disabled:opacity-50">
                  {t('Cancel')}
                </button>
                <button onClick={handleBulkDelete} disabled={bulkBusy || owned === 0}
                  className="px-4 py-2 text-sm font-medium bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors disabled:opacity-50">
                  {bulkBusy ? t('Deleting...') : t('Delete {{n}}', { n: owned })}
                </button>
              </div>
            </>
          );
        })()}
      </Modal>

      {runModalWorkflow && (
        <RunWorkflowModal
          workflow={runModalWorkflow}
          isOpen={!!runModalWorkflow}
          onClose={() => setRunModalWorkflow(null)}
          onDispatched={() => { setRunModalWorkflow(null); refresh(); }}
        />
      )}
    </div>
  );
};

/** Pointer to the dedicated Streaming page (streaming workflows aren't listed here). */
const StreamingBanner: React.FC<{ count: number; onOpen: () => void }> = ({ count, onOpen }) => {
  const { t } = useTranslation();
  return (
    <button
      onClick={onOpen}
      className="w-full mb-2 flex items-center justify-between gap-3 rounded-xl border border-border bg-surface px-3 py-2.5 text-left hover:bg-chrome transition-colors group"
    >
      <div className="flex items-center gap-2.5 min-w-0">
        <SignalIcon className="w-4 h-4 text-tertiary shrink-0" />
        <div className="min-w-0">
          <p className="text-[12px] font-medium text-ink">
            {count === 1 ? t('1 streaming workflow') : t('{{n}} streaming workflows', { n: count })}
          </p>
          <p className="text-[10px] text-tertiary">{t('Managed on the Streaming page.')}</p>
        </div>
      </div>
      <span className="inline-flex items-center gap-1 text-[11px] font-medium text-tertiary group-hover:text-ink shrink-0">
        {t('Open')}
        <ArrowRightIcon className="w-3 h-3" />
      </span>
    </button>
  );
};

/** Collections (folders) strip — drop a workflow's tile here to add it. */
const CollectionsStrip: React.FC<{
  collections: MarketplaceCollection[];
  dragging: boolean;
  dropFolderId: number | null;
  onManage: () => void;
  onDragOverFolder: (id: number) => void;
  onDragLeaveFolder: () => void;
  onDropOnFolder: (id: number) => void;
}> = ({ collections, dragging, dropFolderId, onManage, onDragOverFolder, onDragLeaveFolder, onDropOnFolder }) => {
  const { t } = useTranslation();
  return (
    <div className="mb-3 px-1">
      <div className="flex items-center gap-2 mb-1.5">
        <FolderIcon className="w-3.5 h-3.5 text-tertiary" />
        <h3 className="text-[10px] font-semibold uppercase tracking-wider text-tertiary">{t('Collections')}</h3>
        {collections.length > 0 && <span className="text-[10px] text-tertiary tabular-nums">{collections.length}</span>}
        <div className="flex-1" />
        <button onClick={onManage} className="text-[10px] font-medium text-tertiary hover:text-ink transition-colors">
          {t('Manage')}
        </button>
      </div>
      {collections.length > 0 ? (
        <div className="grid grid-cols-2 gap-2">
          {collections.map((c) => (
            <div
              key={c.id}
              onClick={onManage}
              onDragEnter={(e) => { if (dragging) e.preventDefault(); }}
              onDragOver={(e) => { if (dragging && c.id != null) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; onDragOverFolder(c.id); } }}
              onDragLeave={onDragLeaveFolder}
              onDrop={(e) => { e.preventDefault(); if (c.id != null) onDropOnFolder(c.id); }}
              className={clsx(
                'flex items-center gap-2 px-2.5 py-2 rounded-xl border bg-surface cursor-pointer transition-all',
                dropFolderId === c.id ? 'border-ink/50 ring-2 ring-ink/30' : 'border-border hover:border-ink/20',
              )}
            >
              <span className="w-7 h-7 rounded-lg bg-chrome flex items-center justify-center shrink-0 text-tertiary">
                <FolderIcon className="w-3.5 h-3.5" />
              </span>
              <div className="min-w-0">
                <p className="text-[11px] font-medium text-ink truncate">{cleanTitle(c.name)}</p>
                <p className="text-[9px] text-tertiary">{t('{{n}} APIs', { n: c.item_count ?? (c.items?.length ?? 0) })}</p>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="flex items-center gap-2 px-3 py-2.5 rounded-xl border border-dashed border-border text-[11px] text-tertiary">
          <FolderPlusIcon className="w-4 h-4 shrink-0" />
          {t('Drop this onto another workflow to create your first folder.')}
        </div>
      )}
    </div>
  );
};

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  const { t } = useTranslation();
  return (
    <EmptyHero
      icon={ExclamationTriangleIcon}
      title={t("Couldn't load workflows")}
      description={message}
      className="flex-1 min-h-0 bg-surface"
    >
      <Button onClick={onRetry} size="sm">{t('Retry')}</Button>
    </EmptyHero>
  );
}

function EmptyState({ search, deviceWfCount = 0, onViewDevices }: { search: string; deviceWfCount?: number; onViewDevices?: () => void }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <EmptyHero
      icon={CursorArrowRaysIcon}
      title={search ? t('No workflows match your search') : t('No workflows yet')}
      description={search ? t('Try a different search term') : t('Record a workflow to automate any website')}
      className="flex-1 min-h-0 bg-surface"
    >
      {!search && (
        <Button onClick={() => navigate('/workflows/new')} size="sm">
          {t('New Workflow')}
        </Button>
      )}
      {/* This (Recorded / cloud) list is the default; when it's empty but the user
          has workflows on connected desktop devices, point them there. */}
      {!search && onViewDevices && deviceWfCount > 0 && (
        <button
          type="button"
          onClick={onViewDevices}
          className="inline-flex items-center gap-2.5 rounded-xl border border-border bg-surface px-4 py-2.5 hover:border-ink/30 hover:bg-chrome/40 transition-colors"
        >
          <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-chrome shrink-0">
            <ComputerDesktopIcon className="h-4 w-4 text-secondary" />
          </span>
          <span className="text-left">
            <span className="block text-[12px] font-medium text-ink">{t('You have {{n}} workflows on connected devices', { n: deviceWfCount })}</span>
            <span className="block text-[11px] text-tertiary">{t('Open the Local Devices tab to run them')}</span>
          </span>
          <ArrowRightIcon className="h-4 w-4 text-tertiary shrink-0" />
        </button>
      )}
    </EmptyHero>
  );
}
