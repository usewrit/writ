import React, { useState, useMemo, useCallback } from 'react';
import { useRequireAuth } from '../hooks/useAuth';
import { useDocumentTitle } from '../hooks/useDocumentTitle';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '../hooks/useQuery';
import { Q } from '../stores/queryKeys';
import { runsApi, Run, RunType } from '../api/runs';
import { useLiveRuns, mergeOptimistic } from '../stores/liveRuns';
import { formatDate, formatRelativeTime } from '../utils/format';
import { statusStyle } from '../utils/statusStyle';
import {
  QueueListIcon,
  ExclamationTriangleIcon,
  ChevronDownIcon,
  ArrowUpRightIcon,
  XMarkIcon,
  ClipboardDocumentIcon,
  TableCellsIcon,
  MagnifyingGlassIcon,
} from '@heroicons/react/24/outline';
import { EyeIcon } from '@heroicons/react/24/solid';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import { AIPreviewModal } from '../components/AIPreviewModal';
import { TierBadge, tierLabel } from '../components/TierBadge';
import { formatUsd } from '../components/CreditGuard';
import { ToolRail } from '../components/toolrail/ToolRail';
import { runsConfig } from '../components/toolrail/configs';
import { Expand } from '../components/ui';
import { RowsSkeleton } from '../components/library/shelf';

// Recent-runs window loaded for the client-side (Insights-rail) filtering pattern.
const LOAD_LIMIT = 200;

const RunSearch: React.FC<{ value: string; onChange: (value: string) => void }> = ({ value, onChange }) => {
  const { t } = useTranslation();
  return <label className="relative hidden w-56 @pair/stage:block">
    <MagnifyingGlassIcon className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-tertiary" />
    <input value={value} onChange={(e) => onChange(e.target.value)} placeholder={t('Search workflow or run')} aria-label={t('Search workflow or run')}
      className="h-8 w-full rounded-lg border border-border bg-surface pl-8 pr-3 text-[12px] text-ink placeholder:text-tertiary focus:border-ink/30 focus:outline-none" />
  </label>;
};

const TYPE_LABELS: Record<RunType, string> = {
  workflow: 'Workflow',
  check: 'Monitor',
  ai_session: 'AI Session',
  automation: 'Automation',
  crawl: 'Crawl',
};

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}

// Capitalize a raw status token for display ("success" → "Success").
const statusLabel = (s: string): string => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);

interface RunRowProps {
  run: Run;
  expanded: boolean;
  isCancelling: boolean;
  onToggle: (id: string) => void;
  onOpenDetail: (e: React.MouseEvent, run: Run) => void;
  onViewData: (e: React.MouseEvent, run: Run) => void;
  onWatch: (watch: { sessionId: string; name: string }) => void;
  onCancel: (e: React.MouseEvent, run: Run) => void;
}

/**
 * Memoized run row. Extracted so a 15s poll tick returning identical data (or
 * expanding a different row) doesn't re-render every row in the feed.
 */
const RunRow: React.FC<RunRowProps> = React.memo(({ run, expanded, isCancelling, onToggle, onOpenDetail, onViewData, onWatch, onCancel }) => {
  const { t } = useTranslation();
  return (
    <div>
      <div
        onClick={() => onToggle(run.id)}
        className="flex items-center gap-3 px-4 py-2.5 hover:bg-chrome/60 cursor-pointer transition-colors"
      >
        <span
          className={clsx(
            'inline-flex justify-center w-[72px] px-1.5 py-0.5 rounded text-[10px] font-medium shrink-0',
            statusStyle(run.status).pill,
          )}
        >
          {statusLabel(run.status)}
        </span>
        <span className="hidden @rail/stage:inline-flex justify-center w-[84px] px-1.5 py-0.5 bg-zinc-100 text-secondary rounded-full text-[10px] shrink-0">
          {TYPE_LABELS[run.run_type]}
        </span>

        <div className="flex-1 min-w-0">
          {run.detail_url_hint ? (
            <span
              onClick={(e) => onOpenDetail(e, run)}
              className="text-[13px] font-medium text-ink truncate block hover:underline"
              title={run.entity_name || undefined}
            >
              {run.entity_name || '—'}
            </span>
          ) : (
            <span className="text-[13px] font-medium text-ink truncate block">{run.entity_name || '—'}</span>
          )}
          {run.status === 'failed' && run.error && (
            <p className="text-[11px] text-secondary truncate mt-0.5">{run.error}</p>
          )}
        </div>

        {/* ── Metadata columns — every slot is a FIXED reserved width and is
            always rendered (empty when N/A) so nothing shifts row-to-row. ── */}
        <span className="hidden @pair/stage:inline w-[96px] text-right text-[11px] text-tertiary shrink-0" title={formatDate(run.started_at)}>
          {formatRelativeTime(run.started_at)}
        </span>
        <span className="hidden @rail/stage:inline w-[60px] text-right font-mono text-[11px] text-secondary shrink-0">
          {run.duration_ms != null ? fmtDuration(run.duration_ms) : '—'}
        </span>
        {/* Price charged for this run (money → emerald). Empty (not em-dash) for
            unbilled runs, but the width is reserved so columns stay aligned. */}
        <span
          className="hidden @rail/stage:inline w-[60px] text-right text-[11px] font-medium text-emerald-600 tabular-nums shrink-0"
          title={run.cost_micros ? t('Charged for this run') : undefined}
        >
          {run.cost_micros != null && run.cost_micros > 0 ? formatUsd(run.cost_micros) : ''}
        </span>
        {/* Execution tier — reserved for all rows; the badge only renders for
            workflow runs (placeholder keeps the slot aligned otherwise). */}
        <span className="hidden @split/stage:inline-flex w-[72px] justify-end shrink-0">
          {run.run_type === 'workflow' && <TierBadge tier={run.execution_tier} placeholder />}
        </span>
        {/* Inline actions — reserved fixed slot so the chevron never shifts. */}
        <span className="w-[84px] flex justify-end shrink-0">
          {run.run_type === 'ai_session' && run.status === 'running' && run.entity_id != null ? (
            <button
              onClick={(e) => { e.stopPropagation(); onWatch({ sessionId: `ai-${run.entity_id}`, name: run.entity_name || t('AI Session') }); }}
              className="inline-flex items-center gap-1 px-2 py-1 text-[11px] font-medium bg-zinc-900 text-white rounded-md hover:bg-zinc-800 transition-colors"
              title={t('Watch this run live')}
            >
              <EyeIcon className="w-3 h-3" />
              {t('Watch')}
            </button>
          ) : runsApi.isCancellable(run) ? (
            <button
              onClick={(e) => onCancel(e, run)}
              disabled={isCancelling}
              className="inline-flex items-center gap-1 px-2 py-1 text-[11px] font-medium text-secondary bg-white border border-zinc-200 rounded-md hover:text-ink hover:border-zinc-300 disabled:opacity-40 transition-colors"
              title={run.status === 'running' ? t('Abort this run') : t('Cancel this run')}
            >
              <XMarkIcon className="w-3 h-3" />
              {run.status === 'running' ? t('Abort') : t('Cancel')}
            </button>
          ) : null}
        </span>
        <ChevronDownIcon className={clsx('w-3.5 h-3.5 text-tertiary shrink-0 transition-transform', expanded && 'rotate-180')} />
      </div>

      {/* Expanded detail — a designed panel, not a flat key/value grid */}
      <Expand open={expanded} mountOnEnter className="px-4 pb-4 pt-2 bg-hover/40">
          <div className="bg-surface border border-ink/20 rounded-xl shadow-sm p-4 space-y-3">
            {/* Facts — a readable inline row */}
            <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-[12px]">
              <span className="text-tertiary">{t('Run')}<span className="text-ink font-mono ml-1.5">{run.id}</span></span>
              {run.trigger_source && <span className="text-tertiary">{t('Trigger')}<span className="text-ink ml-1.5">{run.trigger_source}</span></span>}
              <span className="text-tertiary">{t('Started')}<span className="text-ink ml-1.5">{formatDate(run.started_at)}</span></span>
              {run.finished_at && <span className="text-tertiary">{t('Finished')}<span className="text-ink ml-1.5">{formatDate(run.finished_at)}</span></span>}
              {run.duration_ms != null && <span className="text-tertiary">{t('Duration')}<span className="text-ink font-mono ml-1.5">{fmtDuration(run.duration_ms)}</span></span>}
              {run.cost_micros != null && run.cost_micros > 0 && (
                <span className="text-tertiary">{t('Price')}<span className="text-emerald-600 font-semibold ml-1.5 tabular-nums">{formatUsd(run.cost_micros)}</span></span>
              )}
              {run.run_type === 'workflow' && run.execution_tier && (
                <span className="inline-flex items-center gap-1.5">
                  <TierBadge tier={run.execution_tier} />
                  <span className="text-tertiary">{tierLabel(run.execution_tier, t)}</span>
                </span>
              )}
            </div>

            {run.error && (
              <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2.5">
                <div className="flex items-center gap-1.5 text-[11px] font-semibold text-red-700 mb-1">
                  <ExclamationTriangleIcon className="w-3.5 h-3.5" /> {t('Error')}
                </div>
                <pre className="text-[12px] font-mono text-red-900/80 whitespace-pre-wrap break-all">{run.error}</pre>
              </div>
            )}

            {/* Actions */}
            <div className="flex items-center gap-2 pt-0.5">
              {run.detail_url_hint && (
                <button
                  onClick={(e) => onOpenDetail(e, run)}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-accent-strong text-accent-on text-[12px] font-medium rounded-lg hover:bg-accent-strong/90 transition-colors"
                >
                  {t('Open {{type}}', { type: (TYPE_LABELS[run.run_type] || run.run_type).toLowerCase() })}
                  <ArrowUpRightIcon className="w-3 h-3" />
                </button>
              )}
              {/* Jump straight to this workflow's extracted data (buyer-owns-data
                  for installed runs). Only shown when a data view exists. */}
              {run.data_url_hint && (
                <button
                  onClick={(e) => onViewData(e, run)}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-white border border-zinc-200 text-[12px] font-medium text-secondary rounded-lg hover:text-ink hover:border-zinc-300 transition-colors"
                >
                  <TableCellsIcon className="w-3.5 h-3.5" /> {t('View data')}
                </button>
              )}
              <button
                onClick={(e) => { e.stopPropagation(); navigator.clipboard.writeText(String(run.id)); toast.success(t('Run ID copied')); }}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-white border border-zinc-200 text-[12px] font-medium text-secondary rounded-lg hover:text-ink hover:border-zinc-300 transition-colors"
              >
                <ClipboardDocumentIcon className="w-3.5 h-3.5" /> {t('Copy ID')}
              </button>
            </div>
          </div>
      </Expand>
    </div>
  );
});
RunRow.displayName = 'RunRow';

export const RunsPage: React.FC = () => {
  const { t } = useTranslation();
  useRequireAuth();
  useDocumentTitle(t('Runs'));
  const navigate = useNavigate();

  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  // Client-side filter predicate driven by the contextual Insights rail (same
  // pattern as Workflows / Monitors / Automations).
  const [railFilter, setRailFilter] = useState<((r: Run) => boolean) | undefined>(undefined);
  const [search, setSearch] = useState('');
  const handleFilter = useCallback((p: (r: Run) => boolean) => setRailFilter(() => p), []);
  // Live spectator for a running AI-session run. The spectate WS session id is
  // "ai-<AIWorkflowSession id>" (== run.entity_id for ai_session runs).
  const [watchRun, setWatchRun] = useState<{ sessionId: string; name: string } | null>(null);

  const [pollInterval, setPollInterval] = useState<number | undefined>(undefined);
  // Load a recent window and filter client-side via the rail (matching the other
  // list pages). The feed is newest-first; LOAD_LIMIT bounds the window.
  const { data, loading, error, refresh } = useQuery<Run[]>(
    Q.key('runs:recent'),
    () => runsApi.list({ limit: LOAD_LIMIT }),
    { pollInterval },
  );

  // Re-render + merge when an optimistic launch is added/removed.
  const optimistic = useLiveRuns((s) => s.optimistic);
  // Show just-launched runs instantly (optimistic), then let the server feed
  // reconcile — drop optimistic rows the feed now reports.
  React.useEffect(() => {
    if (data) useLiveRuns.getState().reconcile(data.map((r) => r.id));
  }, [data]);
  const allRuns = useMemo(() => mergeOptimistic(data || []), [data, optimistic]);
  const runs = useMemo(() => {
    const q = search.trim().toLowerCase();
    return allRuns.filter((run) => (!railFilter || railFilter(run)) && (!q ||
      [run.entity_name, run.id, run.run_type, run.trigger_source, run.status]
        .some((value) => String(value ?? '').toLowerCase().includes(q))));
  }, [allRuns, railFilter, search]);
  const failedCount = useMemo(() => runs.filter((r) => r.status === 'failed').length, [runs]);
  const truncated = allRuns.length >= LOAD_LIMIT;

  // Poll fast while something is in-flight so a finish reflects near-instantly
  // even if the realtime push is missed; otherwise keep a slow baseline poll
  // (the run-events stream + optimistic insert cover starts while idle).
  // Adjusted during render (guarded) rather than in an effect: `allRuns` changes
  // on every optimistic insert, and the effect form committed the stale cadence
  // before swapping it.
  const wantPoll = allRuns.some((r) => r.status === 'running' || r.status === 'pending') ? 2000 : 45000;
  if (pollInterval !== wantPoll) setPollInterval(wantPoll);

  const initialLoading = loading && !data;
  const loadFailed = !data && !initialLoading && Boolean(error);
  const filtered = !!railFilter || !!search.trim();

  const openDetail = useCallback((e: React.MouseEvent, run: Run) => {
    e.stopPropagation();
    if (run.detail_url_hint) navigate(run.detail_url_hint);
  }, [navigate]);

  // Jump to the workflow's extracted-data view (the backend supplies the full
  // hint, e.g. "/workflows/{id}?tab=data"; null for runs with no data view).
  const viewData = useCallback((e: React.MouseEvent, run: Run) => {
    e.stopPropagation();
    if (run.data_url_hint) navigate(run.data_url_hint);
  }, [navigate]);

  const handleCancel = useCallback(async (e: React.MouseEvent, run: Run) => {
    e.stopPropagation();
    if (cancellingId) return;
    setCancellingId(run.id);
    try {
      await runsApi.cancel(run);
      toast.success(t('Run cancelled'));
      refresh();
    } catch (err: any) {
      toast.error(err.response?.data?.detail || t('Failed to cancel run'));
    } finally {
      setCancellingId(null);
    }
  }, [cancellingId, refresh, t]);

  const toggleExpanded = useCallback((id: string) => {
    setExpandedId((prev) => (prev === id ? null : id));
  }, []);

  return (
    <>
      <div className="flex flex-col h-full">
        {/* Compact toolbar — matches the other list pages */}
        <div className="flex items-center gap-3 h-12 px-4 sm:px-6 bg-chrome chrome-topbar border-b border-border shrink-0">
          <QueueListIcon className="w-4 h-4 text-secondary shrink-0" />
          <span className="text-[13px] font-semibold text-ink shrink-0">{t('Runs')}</span>
          {allRuns.length > 0 && (
            <span className="hidden @pair/stage:inline text-[11px] text-secondary ml-1 shrink-0">
              {t('{{n}} runs · {{failed}} failed', { n: runs.length, failed: failedCount })}
            </span>
          )}
          <span className="hidden @rail/stage:inline text-[11px] text-secondary ml-1 truncate">{t('Every execution across the platform, newest first.')}</span>
          <div className="flex-1" />
          <RunSearch value={search} onChange={setSearch} />
        </div>

        {/* Body — dot-grid canvas + contextual Insights rail */}
        <div className="flex flex-1 overflow-hidden">
          <div className="flex-1 relative overflow-auto">
            <div
              className="absolute inset-0 pointer-events-none"
              style={{
                backgroundImage: 'radial-gradient(circle, #d4d4d8 1px, transparent 1px)',
                backgroundSize: '24px 24px',
                opacity: 0.12,
              }}
            />
            <div className="relative z-10 py-4 px-4 sm:px-6">
              {/* Loading — carded row skeletons, not a 50vh tile */}
              {initialLoading && (
                <RowsSkeleton rows={8} label={t('Loading runs…')} />
              )}

              {/* Error — compact action row */}
              {loadFailed && (
                <div className="flex flex-col @pair/stage:flex-row @pair/stage:items-center @pair/stage:justify-between gap-3 rounded-xl border border-ink/20 bg-surface shadow-sm px-4 py-3.5">
                  <div className="flex items-start gap-3 min-w-0">
                    <ExclamationTriangleIcon className="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
                    <div className="min-w-0">
                      <p className="text-[13px] font-medium text-ink">{t("Couldn't load runs")}</p>
                      {error && <p className="text-xs text-secondary mt-0.5 break-all">{error}</p>}
                    </div>
                  </div>
                  <button
                    onClick={() => refresh()}
                    className="px-3 py-1.5 bg-accent-strong text-accent-on text-[12px] font-semibold rounded-lg shadow-sm hover:bg-accent-strong/90 transition-colors shrink-0"
                  >
                    {t('Retry')}
                  </button>
                </div>
              )}

              {/* Empty — compact row when filtered, single muted line otherwise */}
              {!initialLoading && !loadFailed && runs.length === 0 && (
                filtered ? (
                  <div className="flex flex-col @pair/stage:flex-row @pair/stage:items-center @pair/stage:justify-between gap-3 rounded-xl border border-ink/20 bg-surface shadow-sm px-4 py-3.5">
                    <div className="flex items-start gap-3 min-w-0">
                      <QueueListIcon className="w-5 h-5 text-tertiary shrink-0 mt-0.5" />
                      <div className="min-w-0">
                        <p className="text-[13px] font-medium text-ink">{t('No runs match your filters')}</p>
                        <p className="text-xs text-secondary mt-0.5">{t('Clear filters in the Insights panel to see everything.')}</p>
                      </div>
                    </div>
                  </div>
                ) : (
                  <p className="text-[13px] text-tertiary py-6">{t('No runs yet — runs from workflows, monitors, AI sessions and automations appear here.')}</p>
                )
              )}

              {/* Runs feed */}
              {runs.length > 0 && (
                <div className="bg-surface border border-ink/20 rounded-xl shadow-sm overflow-hidden divide-y divide-border">
                  <div className="hidden @rail/stage:flex items-center gap-3 bg-chrome/70 px-4 py-2 text-[10px] font-semibold uppercase tracking-[0.08em] text-tertiary">
                    <span className="w-[72px] shrink-0">{t('Status')}</span>
                    <span className="w-[84px] shrink-0 text-center">{t('Type')}</span>
                    <span className="min-w-0 flex-1">{t('Workflow or task')}</span>
                    <span className="w-[96px] shrink-0 text-right">{t('Started')}</span>
                    <span className="w-[60px] shrink-0 text-right">{t('Duration')}</span>
                    <span className="w-[60px] shrink-0 text-right">{t('Cost')}</span>
                    <span className="w-[172px] shrink-0" />
                  </div>
                  {runs.map((run) => (
                    <RunRow
                      key={run.id}
                      run={run}
                      expanded={expandedId === run.id}
                      isCancelling={cancellingId === run.id}
                      onToggle={toggleExpanded}
                      onOpenDetail={openDetail}
                      onViewData={viewData}
                      onWatch={setWatchRun}
                      onCancel={handleCancel}
                    />
                  ))}
                </div>
              )}

              {/* Window note — the feed shows the most recent runs, not all history */}
              {truncated && runs.length > 0 && (
                <p className="text-[11px] text-tertiary mt-3 text-center">
                  {t('Showing the latest {{n}} runs.', { n: LOAD_LIMIT })}
                </p>
              )}
            </div>
          </div>

          <ToolRail config={runsConfig} items={allRuns} onFilterChange={handleFilter as any} />
        </div>
      </div>

      {watchRun && (
        <AIPreviewModal
          isOpen={true}
          onClose={() => setWatchRun(null)}
          sessionId={watchRun.sessionId}
          sessionName={watchRun.name}
        />
      )}
    </>
  );
};
