import React, { useState, useMemo, useCallback, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useQuery } from '../../hooks/useQuery';
import { useQueryCache } from '../../stores/queryCache';
import { Q } from '../../stores/queryKeys';
import { targetsApi } from '../../api/endpoints';
import { formatElapsedShort } from '../../utils/format';
import { Modal } from '../ui/Modal';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import {
  PlayIcon,
  PauseIcon,
  TrashIcon,
  CheckIcon,
  GlobeAltIcon,
  ExclamationTriangleIcon,
  EyeIcon,
  ArrowPathIcon,
} from '@heroicons/react/24/outline';
import { Stagger, Select } from '../ui';
import { ScrollArea } from '../ui/ScrollArea';
import { tintStyle } from '../../utils/tint';
import { MonitorDetailPane } from './MonitorDetailPane';
import { checksConfig } from '../toolrail/configs';
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

const PERIOD_LABELS: Record<number, string> = {
  10000: '10s', 30000: '30s', 60000: '1m', 300000: '5m',
  900000: '15m', 3600000: '1h', 86400000: '24h',
};

// ── field accessors (daemon serializes snake_case + integer booleans) ──
const isEnabled = (t: any) => t.enabled !== false && t.enabled !== 0;
const periodOf = (t: any): number => t.checkPeriodMs ?? t.check_period_ms ?? 0;
const stateOf = (t: any): string => (t.state ?? t.health_state ?? '').toLowerCase();
const changesOf = (t: any): number => t.changesCount ?? t.changes_count ?? 0;
const lastCheckedOf = (t: any) => t.lastCheckedAt ?? t.last_checked_at;
const lastChangeOf = (t: any) => t.lastChangeAt ?? t.last_change_at;
const checkTypeOf = (t: any) => t.checkType ?? t.check_type;

function hostOf(url?: string): string {
  if (!url) return '';
  try { return new URL(/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(url) ? url : 'https://' + url).hostname.replace(/^www\./, ''); } catch { return url; }
}
function pathOf(url?: string): string {
  if (!url) return '';
  try { const u = new URL(/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(url) ? url : 'https://' + url); return (u.pathname + u.search) || '/'; } catch { return ''; }
}

interface RowHealth { dot: string; label: string; className: string; attention: boolean; }
function rowHealth(t: any, tr: (k: string) => string): RowHealth {
  if (!isEnabled(t)) return { dot: 'bg-active', label: tr('Paused'), className: 'text-tertiary', attention: false };
  switch (stateOf(t)) {
    case 'down':
    case 'error': return { dot: 'bg-danger', label: tr('Down'), className: 'text-danger font-medium', attention: true };
    case 'stale': return { dot: 'bg-warning', label: tr('Stale'), className: 'text-warning font-medium', attention: true };
    case 'changed': return { dot: 'bg-warning', label: tr('Changed'), className: 'text-warning font-medium', attention: true };
    case 'up':
    case 'ok':
    case 'unchanged': return { dot: 'bg-success', label: tr('Healthy'), className: 'text-tertiary', attention: false };
    default: return { dot: 'bg-active', label: tr('Pending'), className: 'text-tertiary', attention: false };
  }
}

// ── Sort dimensions ──
type SortMode = 'attention' | 'recent_change' | 'checked' | 'cadence' | 'url' | 'changes';
const ts = (v: any): number => (v ? new Date(v).getTime() : 0);
function attentionRank(t: any): number {
  if (!isEnabled(t)) return 6;
  switch (stateOf(t)) {
    case 'down': case 'error': return 0;
    case 'stale': return 1;
    case 'changed': return 2;
    case 'up': case 'ok': case 'unchanged': return 4;
    default: return 3;
  }
}
function sortMonitors(list: any[], mode: SortMode): any[] {
  const arr = [...list];
  switch (mode) {
    case 'url': return arr.sort((a, b) => hostOf(a.url).localeCompare(hostOf(b.url)));
    case 'changes': return arr.sort((a, b) => changesOf(b) - changesOf(a));
    case 'checked': return arr.sort((a, b) => ts(lastCheckedOf(b)) - ts(lastCheckedOf(a)));
    case 'recent_change': return arr.sort((a, b) => ts(lastChangeOf(b)) - ts(lastChangeOf(a)));
    case 'cadence': return arr.sort((a, b) => (periodOf(a) || Infinity) - (periodOf(b) || Infinity));
    case 'attention':
    default:
      return arr.sort((a, b) => attentionRank(a) - attentionRank(b) || ts(lastChangeOf(b)) - ts(lastChangeOf(a)));
  }
}

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

interface MonitorRowProps {
  t: any;
  selected: boolean;
  checked: boolean;
  togglingRow: boolean;
  onSelect: (t: any) => void;
  onToggleCheck: () => void;
  onToggle: (t: any) => void;
}

const MonitorRow: React.FC<MonitorRowProps> = React.memo(({
  t, selected, checked, togglingRow, onSelect, onToggleCheck, onToggle,
}) => {
  const { t: tr } = useTranslation();
  const enabled = isEnabled(t);
  const health = rowHealth(t, tr);
  const periodMs = periodOf(t);
  const period = periodMs ? (PERIOD_LABELS[periodMs] || `${Math.round(periodMs / 1000)}s`) : null;
  const lastChange = lastChangeOf(t);

  return (
    <div
      onMouseDown={shelfRowMouseDown}
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      aria-label={t.url}
      onClick={() => onSelect(t)}
      onKeyDown={(e) => {
        if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) {
          e.preventDefault();
          onSelect(t);
        }
      }}
      className={shelfRowClass(selected)}
    >
      {selected && <ShelfAccentBar />}

      {/* Leading tile — globe with a health badge; flips to a checkbox on hover / when checked. */}
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); onToggleCheck(); }}
        aria-label={tr('Select monitor')}
        aria-pressed={checked}
        className="relative w-8 h-8 shrink-0 rounded-lg outline-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/20"
      >
        <span
          className={clsx(
            'absolute inset-0 flex items-center justify-center rounded-lg border transition-opacity',
            checked ? 'bg-ink border-ink text-surface opacity-100' : 'bg-surface border-border opacity-0 group-hover:opacity-100',
          )}
        >
          <CheckIcon className={clsx('h-3.5 w-3.5 transition-opacity', checked ? 'opacity-100' : 'opacity-0')} />
        </span>
        <span
          style={tintStyle('neutral')}
          className={clsx('absolute inset-0 flex items-center justify-center rounded-lg transition-opacity', checked ? 'opacity-0' : 'opacity-100 group-hover:opacity-0')}
        >
          <GlobeAltIcon className="h-4 w-4" />
        </span>
        {!checked && (
          <span className={clsx('absolute -bottom-1 -right-1 w-3.5 h-3.5 rounded-full border-2 border-surface transition-opacity group-hover:opacity-0', health.dot)}>
            {enabled && health.attention && (
              <span className={clsx('absolute inset-0 rounded-full opacity-60 animate-ping', health.dot)} />
            )}
          </span>
        )}
      </button>

      <div className="flex-1 min-w-0">
        <div className="text-[13px] font-medium text-ink truncate" title={t.url}>{hostOf(t.url)}</div>
        <div className="flex items-center gap-1 text-[11px] truncate mt-0.5">
          {period && <><span className="text-tertiary shrink-0">{period}</span><span className="text-tertiary/50 shrink-0">·</span></>}
          {health.attention && stateOf(t) === 'changed' && lastChange ? (
            <span className={clsx('shrink-0', health.className)}>{tr('changed {{ago}} ago', { ago: formatElapsedShort(lastChange) })}</span>
          ) : (
            <span className={clsx('shrink-0', health.className)}>{health.label}</span>
          )}
          <span className="text-tertiary/60 truncate min-w-0 flex-1 ml-1">{pathOf(t.url)}</span>
        </div>
      </div>

      {/* Pause / resume */}
      <button
        onClick={(e) => { e.stopPropagation(); onToggle(t); }}
        disabled={togglingRow}
        title={enabled ? tr('Pause') : tr('Resume')}
        aria-label={enabled ? tr('Pause') : tr('Resume')}
        className={clsx(
          'flex items-center justify-center w-7 h-7 rounded-lg transition-all active:scale-95 disabled:opacity-50 shrink-0',
          selected && !enabled ? 'bg-accent-strong text-accent-on hover:bg-accent-strong/90' : 'text-tertiary hover:bg-chrome hover:text-ink',
        )}
      >
        {togglingRow ? <ArrowPathIcon className="h-4 w-4 animate-spin" /> : enabled ? <PauseIcon className="h-4 w-4" /> : <PlayIcon className="h-4 w-4" />}
      </button>
    </div>
  );
});
MonitorRow.displayName = 'MonitorRow';

interface MonitorListProps {
  search: string;
  /** When set, the list renders its own search input at the top of the master column. */
  onSearchChange?: (v: string) => void;
  onItemClick?: (id: string) => void;
  filter?: (t: any) => boolean;
}

export const MonitorList: React.FC<MonitorListProps> = ({ search, onSearchChange, onItemClick, filter }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const isWide = useIsWide();

  const [pollInterval, setPollInterval] = useState<number | undefined>(15000);
  const { data: targets, loading, error, refresh } = useQuery(Q.targets('content'), () => targetsApi.getAll('content'), { pollInterval });

  // Poll only while at least one monitor is enabled — a paused list has nothing
  // live to refresh. Adjusted during render (guarded, so it converges in one
  // extra pass) rather than in an effect, which would commit the old cadence
  // first and only then tear the interval down.
  const wantPoll = (targets || []).some(isEnabled) ? 15000 : undefined;
  if (pollInterval !== wantPoll) setPollInterval(wantPoll);

  const [view, setView] = useState<string>(() => localStorage.getItem('writ.monView') || 'all');
  const [sortBy, setSortBy] = useState<SortMode>(() => {
    const v = localStorage.getItem('writ.monSortBy');
    return (['attention', 'recent_change', 'checked', 'cadence', 'url', 'changes'] as const).includes(v as any) ? (v as SortMode) : 'attention';
  });
  useEffect(() => { try { localStorage.setItem('writ.monView', view); } catch { /* noop */ } }, [view]);
  useEffect(() => { try { localStorage.setItem('writ.monSortBy', sortBy); } catch { /* noop */ } }, [sortBy]);

  // Content checks only; then search + external filter. Counts compute off this.
  const searched = useMemo(() => {
    const q = search.toLowerCase();
    return (targets || [])
      .filter((x: any) => checkTypeOf(x) === 'content')
      .filter((x: any) => !search || x.url?.toLowerCase().includes(q))
      .filter((x: any) => !filter || filter(x));
  }, [targets, search, filter]);

  const views = checksConfig.views;
  const viewCounts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const v of views) m[v.id] = searched.filter(v.predicate).length;
    return m;
  }, [searched, views]);

  const activeView = views.find((v) => v.id === view) || views[0];
  const monitorList = useMemo(
    () => sortMonitors(searched.filter(activeView.predicate), sortBy),
    [searched, activeView, sortBy],
  );

  const [pickedId, setSelectedId] = useState<string | null>(null);
  const [rawCheckedIds, setCheckedIds] = useState<Set<string>>(new Set());

  const visibleIds = useMemo(() => new Set(monitorList.map((x: any) => String(x.id))), [monitorList]);

  // The detail selection is DERIVED, not stored: the user's pick while the row
  // is still in the list, otherwise the first row. Computing it in an effect
  // would paint one frame with a dead selection before correcting it.
  const selectedId = useMemo(() => {
    if (!isWide) return pickedId;
    if (monitorList.length === 0) return null;
    return pickedId != null && visibleIds.has(pickedId) ? pickedId : String(monitorList[0].id);
  }, [pickedId, monitorList, visibleIds, isWide]);

  // Bulk checks are pruned ON READ — a row leaving the list (filter, search,
  // delete) drops out of the effective selection without a state write.
  const checkedIds = useMemo(
    () => new Set([...rawCheckedIds].filter((idv) => visibleIds.has(idv))),
    [rawCheckedIds, visibleIds],
  );

  const toggleCheck = useCallback((id: string) => {
    setCheckedIds((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });
  }, []);
  const clearChecks = useCallback(() => setCheckedIds(new Set()), []);

  const checkedMonitors = useMemo(
    () => monitorList.filter((x: any) => checkedIds.has(String(x.id))),
    [monitorList, checkedIds],
  );

  const handleSelect = useCallback((x: any) => {
    if (isWide) setSelectedId(String(x.id));
    else if (onItemClick) onItemClick(String(x.id));
    else navigate(`/checks/${x.id}`);
  }, [isWide, onItemClick, navigate]);

  const [togglingId, setTogglingId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; url: string } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);

  const handleToggle = useCallback(async (x: any) => {
    const idv = String(x.id);
    if (togglingId) return;
    setTogglingId(idv);
    try {
      await targetsApi.toggle(idv, !isEnabled(x));
      toast.success(isEnabled(x) ? t('Paused') : t('Resumed'));
      refresh();
    } catch {
      toast.error(t('Failed to toggle'));
    } finally {
      setTogglingId(null);
    }
  }, [togglingId, refresh, t]);

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await targetsApi.delete(deleteTarget.id);
      toast.success(t('Monitor deleted'));
      const qc = useQueryCache.getState();
      qc.invalidateMatching(`target:${deleteTarget.id}`);
      refresh();
      setDeleteTarget(null);
    } catch {
      toast.error(t('Failed to delete monitor'));
    } finally {
      setDeleting(false);
    }
  };

  const handleBulkToggle = useCallback(async (enable: boolean) => {
    const targetsToToggle = checkedMonitors.filter((x: any) => isEnabled(x) !== enable);
    if (!targetsToToggle.length) return;
    setBulkBusy(true);
    let ok = 0; let fail = 0;
    for (const x of targetsToToggle) {
      try { await targetsApi.toggle(String(x.id), enable); ok += 1; } catch { fail += 1; }
    }
    setBulkBusy(false);
    refresh();
    if (fail === 0) toast.success(enable ? t('Resumed {{n}}', { n: ok }) : t('Paused {{n}}', { n: ok }));
    else toast.error(t('{{ok}} updated · {{fail}} failed', { ok, fail }));
  }, [checkedMonitors, refresh, t]);

  const handleBulkDelete = useCallback(async () => {
    setBulkBusy(true);
    let ok = 0; let fail = 0;
    const qc = useQueryCache.getState();
    for (const x of checkedMonitors) {
      try { await targetsApi.delete(String(x.id)); qc.invalidateMatching(`target:${x.id}`); ok += 1; } catch { fail += 1; }
    }
    setBulkBusy(false);
    setBulkDeleteOpen(false);
    clearChecks();
    refresh();
    if (fail === 0) toast.success(t('Deleted {{n}}', { n: ok }));
    else toast.error(t('{{ok}} deleted · {{fail}} failed', { ok, fail }));
  }, [checkedMonitors, clearChecks, refresh, t]);

  if (loading && !targets) return <ShelfSkeleton withSearch={!!onSearchChange} label={t('Loading monitors')} />;
  if (!targets && error) return <ErrorState message={error} onRetry={refresh} />;
  const anyContent = (targets || []).some((x: any) => checkTypeOf(x) === 'content');
  if (!anyContent) return <EmptyState search={search} />;

  const selectedMonitor = monitorList.find((x: any) => String(x.id) === selectedId) || null;
  const allChecked = monitorList.length > 0 && checkedIds.size >= monitorList.length;

  return (
    <div className={SHELF_CONTAINER}>
      {/* ── Master list ── */}
      <div className={SHELF_LIST_COL}>
        {/* Filter + sort header */}
        <div className="shrink-0 border-b border-border px-3 py-2.5 space-y-2">
          {/* Search — lives with the list it filters, above the chips. */}
          {onSearchChange && <ShelfListSearch value={search} onChange={onSearchChange} ariaLabel={t('Search monitors')} />}
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
            <span className="text-[11px] text-tertiary">{t('{{n}} shown', { n: monitorList.length })}</span>
            <Select<SortMode>
              size="sm"
              value={sortBy}
              onChange={setSortBy}
              aria-label={t('Sort by')}
              wrapperClassName="w-40"
              options={[
                { value: 'attention', label: t('Needs attention') },
                { value: 'recent_change', label: t('Recent change') },
                { value: 'checked', label: t('Last checked') },
                { value: 'cadence', label: t('Tightest cadence') },
                { value: 'changes', label: t('Most changes') },
                { value: 'url', label: t('URL A–Z') },
              ]}
            />
          </div>
        </div>

        {/* Bulk action bar */}
        {checkedIds.size > 0 && (
          <div className="shrink-0 flex items-center justify-between gap-2 border-b border-border bg-chrome px-3 py-2 bulkbar-enter">
            <div className="flex items-center gap-2 text-[12px]">
              <button
                onClick={() => setCheckedIds(allChecked ? new Set() : new Set(monitorList.map((x: any) => String(x.id))))}
                className={clsx('flex h-4 w-4 items-center justify-center rounded border transition-colors', allChecked ? 'border-ink bg-ink text-surface' : 'border-border bg-surface')}
                aria-label={t('Select all')}
              >
                {allChecked && <CheckIcon className="h-3 w-3" />}
              </button>
              <span className="font-medium text-ink">{t('{{n}} selected', { n: checkedIds.size })}</span>
              <button onClick={clearChecks} className="text-tertiary hover:text-ink transition-colors">{t('Clear')}</button>
            </div>
            <div className="flex items-center gap-1">
              <button onClick={() => handleBulkToggle(true)} disabled={bulkBusy} title={t('Resume')}
                className="p-1.5 rounded-lg text-secondary hover:bg-surface hover:text-ink disabled:opacity-50 transition-colors">
                <PlayIcon className="h-3.5 w-3.5" />
              </button>
              <button onClick={() => handleBulkToggle(false)} disabled={bulkBusy} title={t('Pause')}
                className="p-1.5 rounded-lg text-secondary hover:bg-surface hover:text-ink disabled:opacity-50 transition-colors">
                <PauseIcon className="h-3.5 w-3.5" />
              </button>
              <button onClick={() => setBulkDeleteOpen(true)} disabled={bulkBusy} title={t('Delete')}
                className="p-1.5 rounded-lg text-danger hover:bg-danger-bg hover:text-danger-fg disabled:opacity-50 transition-colors">
                <TrashIcon className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        )}

        {/* Rows */}
        {monitorList.length === 0 ? (
          <div className="flex flex-1 flex-col items-center justify-center text-center px-6 py-10">
            <p className="text-[13px] font-medium text-ink">{t('Nothing here')}</p>
            <p className="text-[11px] text-secondary mt-1">{t('No monitors match this filter.')}</p>
          </div>
        ) : (
          <ScrollArea className="flex-1" viewportClassName="px-2 py-2" fade="chrome">
            <Stagger className="grid grid-cols-1 gap-0.5" staggerMs={24}>
              {monitorList.map((x: any) => (
                <MonitorRow
                  key={x.id}
                  t={x}
                  selected={isWide && selectedId === String(x.id)}
                  checked={checkedIds.has(String(x.id))}
                  togglingRow={togglingId === String(x.id)}
                  onSelect={handleSelect}
                  onToggleCheck={() => toggleCheck(String(x.id))}
                  onToggle={handleToggle}
                />
              ))}
            </Stagger>
          </ScrollArea>
        )}
      </div>

      {/* ── Detail pane (lg only) ── */}
      <div className={SHELF_DETAIL_COL}>
        <MonitorDetailPane
          target={selectedMonitor}
          onToggle={handleToggle}
          onDelete={setDeleteTarget}
          togglingRow={selectedMonitor ? togglingId === String(selectedMonitor.id) : false}
          onChanged={refresh}
        />
      </div>

      {/* ── Modals ── */}
      <Modal isOpen={!!deleteTarget} onClose={() => setDeleteTarget(null)} title={t('Delete Monitor')}>
        <p className="text-sm text-secondary mb-4">
          {t('Are you sure you want to delete the monitor for')} <strong className="text-ink break-all">{deleteTarget?.url}</strong>?
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

      <Modal isOpen={bulkDeleteOpen} onClose={() => (bulkBusy ? null : setBulkDeleteOpen(false))} title={t('Delete monitors')}>
        <p className="text-sm text-secondary mb-4">
          {t('Delete {{n}} monitor(s)? This cannot be undone.', { n: checkedMonitors.length })}
        </p>
        <div className="flex items-center justify-end gap-2">
          <button onClick={() => setBulkDeleteOpen(false)} disabled={bulkBusy}
            className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg hover:bg-chrome transition-colors disabled:opacity-50">
            {t('Cancel')}
          </button>
          <button onClick={handleBulkDelete} disabled={bulkBusy || checkedMonitors.length === 0}
            className="px-4 py-2 text-sm font-medium bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors disabled:opacity-50">
            {bulkBusy ? t('Deleting...') : t('Delete {{n}}', { n: checkedMonitors.length })}
          </button>
        </div>
      </Modal>
    </div>
  );
};

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-1 min-h-0 w-full flex-col items-center justify-center bg-surface text-center px-6">
      <div className="w-12 h-12 rounded-2xl bg-hover border border-border flex items-center justify-center mb-4">
        <ExclamationTriangleIcon className="h-6 w-6 text-tertiary" />
      </div>
      <p className="text-sm font-medium text-ink">{t("Couldn't load monitors")}</p>
      <p className="text-xs text-secondary mt-1">{message}</p>
      <button onClick={onRetry}
        className="mt-4 px-4 py-2 bg-accent-strong text-accent-on text-sm font-medium rounded-lg hover:bg-accent-strong/90 transition-colors">
        {t('Retry')}
      </button>
    </div>
  );
}

function EmptyState({ search }: { search: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <div className="flex flex-1 min-h-0 w-full flex-col items-center justify-center bg-surface text-center px-6">
      <div className="w-12 h-12 rounded-2xl bg-hover border border-border flex items-center justify-center mb-4">
        <EyeIcon className="h-6 w-6 text-tertiary" />
      </div>
      <p className="text-sm font-medium text-ink">
        {search ? t('No monitors match your search') : t('No monitors yet')}
      </p>
      <p className="text-xs text-secondary mt-1">
        {search ? t('Try a different search term') : t('Create your first monitor to watch a webpage for changes')}
      </p>
      {!search && (
        <button onClick={() => navigate('/checks/new')}
          className="mt-4 px-4 py-2 bg-accent-strong text-accent-on text-sm font-medium rounded-lg hover:bg-accent-strong/90 transition-colors">
          {t('New Monitor')}
        </button>
      )}
    </div>
  );
}
