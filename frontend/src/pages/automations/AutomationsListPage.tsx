import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import i18n from '../../i18n';
import { useRequireAuth } from '../../hooks/useAuth';
import { useDocumentTitle } from '../../hooks/useDocumentTitle';
import { useQuery } from '../../hooks/useQuery';
import { Q } from '../../stores/queryKeys';
import { useNavigate } from 'react-router-dom';
import { triggersApi, webhookTriggersApi } from '../../api/endpoints';
import { formatRelativeTime, formatDate, cleanTitle } from '../../utils/format';
import { statusStyle } from '../../utils/statusStyle';
import { Modal } from '../../components/ui/Modal';
import { Stagger, SwapFade, Select } from '../../components/ui';
import { ScrollArea } from '../../components/ui/ScrollArea';
import { tintStyle, type Tint } from '../../utils/tint';
import {
  SHELF_CONTAINER,
  SHELF_LIST_COL,
  SHELF_DETAIL_COL,
  shelfRowClass,
  shelfRowMouseDown,
  ShelfAccentBar,
  shelfFilterChipClass,
  shelfFilterCountClass,
  SHELF_TOPBAR,
  ShelfListSearch,
  ShelfSkeleton,
} from '../../components/library/shelf';
import { automationsConfig } from '../../components/toolrail/configs';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import {
  PlusIcon,
  ArrowsRightLeftIcon,
  PlayIcon,
  PauseIcon,
  SparklesIcon,
  EyeIcon,
  BoltIcon,
  QuestionMarkCircleIcon,
  ExclamationTriangleIcon,
  CheckIcon,
  TrashIcon,
  ClipboardIcon,
  ClipboardDocumentCheckIcon,
  ArrowTopRightOnSquareIcon,
  ArrowPathIcon,
} from '@heroicons/react/24/outline';
import { FlowBlock } from '../../components/flows/types';
import { useTour } from '../../onboarding/TourProvider';

// Monochrome chrome, color-from-content: each trigger type gets a stable tint on
// its leading icon tile so an end user can scan the CAUSE at a glance, with a
// plain-language hint on hover. Label reads as "what sets this off".
const SOURCE_BADGES: Record<string, { label: string; icon: any; hint: string; tint: Tint }> = {
  change_detected: { label: 'Page change', icon: EyeIcon, hint: 'Runs when a monitored page changes', tint: 'blue' },
  webhook_received: { label: 'Webhook', icon: ArrowsRightLeftIcon, hint: 'Runs when an incoming request hits its URL', tint: 'violet' },
  ai_session_started: { label: 'AI session starts', icon: SparklesIcon, hint: 'Runs when an AI session begins', tint: 'teal' },
  ai_session_completed: { label: 'AI session ends', icon: SparklesIcon, hint: 'Runs when an AI session finishes', tint: 'teal' },
  workflow_started: { label: 'Workflow starts', icon: PlayIcon, hint: 'Runs when a workflow begins', tint: 'green' },
  workflow_completed: { label: 'Workflow ends', icon: PlayIcon, hint: 'Runs when a workflow finishes', tint: 'green' },
};
const sourceOf = (f: any) => SOURCE_BADGES[f.event_type] || SOURCE_BADGES.change_detected;

function actionSummary(blocks?: FlowBlock[]): string {
  if (!blocks || blocks.length === 0) return '';
  return blocks.filter((b) => b.type === 'action').map((a) => {
    if (a.blockType === 'notification') return i18n.t('Notify');
    if (a.blockType === 'ai_session') return i18n.t('AI Agent');
    if (a.blockType === 'workflow') return i18n.t('Workflow');
    if (a.blockType === 'return_data') return i18n.t('Return Data');
    return a.blockType;
  }).join(' → ');
}
const webhookTokenOf = (f: any): string | undefined =>
  f.webhook_trigger_token || f.blocks?.find((b: any) => b.blockType === 'webhook_received')?.config?.webhook_trigger_token;

const ts = (v: any): number => (v ? new Date(v).getTime() : 0);
type SortMode = 'recent' | 'most' | 'name' | 'created';
function sortFlows(list: any[], mode: SortMode): any[] {
  const arr = [...list];
  switch (mode) {
    case 'most': return arr.sort((a, b) => (b.trigger_count || 0) - (a.trigger_count || 0));
    case 'name': return arr.sort((a, b) => cleanTitle(a.name).localeCompare(cleanTitle(b.name)));
    case 'created': return arr.sort((a, b) => ts(b.created_at) - ts(a.created_at));
    case 'recent':
    default:
      return arr.sort((a, b) => ts(b.last_triggered_at) - ts(a.last_triggered_at));
  }
}

/** lg breakpoint (1024px) — the two-pane only renders this wide. */
function useIsWide(): boolean {
  const [wide, setWide] = useState(() => typeof window !== 'undefined' ? window.matchMedia('(min-width: 1024px)').matches : true);
  useEffect(() => {
    const mq = window.matchMedia('(min-width: 1024px)');
    const onChange = (e: MediaQueryListEvent) => setWide(e.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);
  return wide;
}

// ── Row ──
interface RowProps {
  f: any;
  selected: boolean;
  checked: boolean;
  toggling: boolean;
  onSelect: (f: any) => void;
  onToggleCheck: () => void;
  onToggle: (f: any) => void;
}
const AutomationRow: React.FC<RowProps> = React.memo(({ f, selected, checked, toggling, onSelect, onToggleCheck, onToggle }) => {
  const { t } = useTranslation();
  const source = sourceOf(f);
  const SourceIcon = source.icon;
  const summary = actionSummary(f.blocks);
  return (
    <div
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      aria-label={cleanTitle(f.name, t('Untitled'))}
      onClick={() => onSelect(f)}
      onMouseDown={shelfRowMouseDown}
      onKeyDown={(e) => { if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) { e.preventDefault(); onSelect(f); } }}
      className={shelfRowClass(selected)}
    >
      {selected && <ShelfAccentBar />}
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); onToggleCheck(); }}
        aria-label={t('Select automation')}
        aria-pressed={checked}
        className="relative w-8 h-8 shrink-0 rounded-lg outline-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/20"
      >
        <span className={clsx('absolute inset-0 flex items-center justify-center rounded-lg border transition-opacity', checked ? 'bg-ink border-ink text-surface opacity-100' : 'bg-surface border-border opacity-0 group-hover:opacity-100')}>
          <CheckIcon className={clsx('h-3.5 w-3.5 transition-opacity', checked ? 'opacity-100' : 'opacity-0')} />
        </span>
        <span style={tintStyle(source.tint)} className={clsx('absolute inset-0 flex items-center justify-center rounded-lg transition-opacity', checked ? 'opacity-0' : 'opacity-100 group-hover:opacity-0')}>
          <SourceIcon className="h-4 w-4" />
        </span>
        {!f.enabled && !checked && (
          <span className="absolute -bottom-1 -right-1 w-3.5 h-3.5 rounded-full border-2 border-surface bg-active transition-opacity group-hover:opacity-0" />
        )}
      </button>
      <div className="flex-1 min-w-0">
        {/* Name: clips at rest, marquees on row hover to reveal the overflow —
            same treatment as the WorkflowList rows (the row is a `group`). */}
        <div className="text-[13px] font-medium text-ink overflow-hidden" style={{ containerType: 'inline-size' }} title={f.name || undefined}>
          <span className="inline-block whitespace-nowrap min-w-full group-hover:[animation:marquee-hover_8s_ease-in-out_infinite]">
            {cleanTitle(f.name, t('Untitled'))}
          </span>
        </div>
        <div className="flex items-center gap-1 text-[11px] truncate mt-0.5">
          <span className="text-tertiary shrink-0">{t(source.label)}</span>
          {summary && <><span className="text-tertiary/50 shrink-0">→</span><span className="text-tertiary/80 truncate">{summary}</span></>}
          {!f.enabled && <><span className="text-tertiary/50 shrink-0">·</span><span className="text-tertiary shrink-0">{t('Paused')}</span></>}
        </div>
      </div>
      {/* Enable / pause — admin has no per-row Run, so the quick action flips the trigger. */}
      <button
        onClick={(e) => { e.stopPropagation(); onToggle(f); }}
        disabled={toggling}
        title={f.enabled ? t('Pause') : t('Enable')}
        aria-label={f.enabled ? t('Pause') : t('Enable')}
        className={clsx('flex items-center justify-center w-7 h-7 rounded-lg transition-all active:scale-95 disabled:opacity-50 shrink-0', selected && !f.enabled ? 'bg-accent-strong text-accent-on hover:bg-accent-strong/90' : 'text-tertiary hover:bg-chrome hover:text-ink')}
      >
        {toggling ? <ArrowPathIcon className="h-4 w-4 animate-spin" /> : f.enabled ? <PauseIcon className="h-4 w-4" /> : <PlayIcon className="h-4 w-4" />}
      </button>
    </div>
  );
});
AutomationRow.displayName = 'AutomationRow';

// ── Detail pane ──
const Fact: React.FC<{ icon: React.FC<any>; label: string; children: React.ReactNode }> = ({ icon: Icon, label, children }) => (
  <div className="flex items-center gap-2 min-w-0">
    <Icon className="h-3.5 w-3.5 text-tertiary shrink-0" />
    <span className="text-[11px] text-tertiary shrink-0">{label}</span>
    <span className="ml-auto text-[12px] text-ink text-right truncate min-w-0">{children}</span>
  </div>
);

interface DetailProps {
  f: any | null;
  toggling: boolean;
  onToggle: (f: any) => void;
  onDelete: (f: any) => void;
}
const AutomationDetailPane: React.FC<DetailProps> = ({ f, toggling, onToggle, onDelete }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [copied, setCopied] = useState(false);

  const { data: runs } = useQuery<any[]>(
    f ? `automation-runs:${f.id}` : 'automation-runs:none',
    () => triggersApi.getExecutions(f.id, 6).catch(() => []),
    { enabled: !!f, staleTime: 15000 },
  );

  if (!f) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center text-center px-6">
        <div className="w-12 h-12 rounded-2xl bg-hover border border-border flex items-center justify-center mb-4"><BoltIcon className="h-6 w-6 text-tertiary" /></div>
        <p className="text-sm font-medium text-ink">{t('Select an automation')}</p>
        <p className="text-xs text-secondary mt-1 max-w-xs">{t('Pick one on the left to see its trigger, actions, and recent runs.')}</p>
      </div>
    );
  }

  const source = sourceOf(f);
  const SourceIcon = source.icon;
  const summary = actionSummary(f.blocks);
  const token = webhookTokenOf(f);
  const runList = runs || [];

  const copyWebhook = () => {
    if (!token) return;
    navigator.clipboard.writeText(webhookTriggersApi.getWebhookUrl(token));
    setCopied(true);
    toast.success(t('Copied webhook URL'));
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="flex flex-1 flex-col min-w-0 min-h-0">
      <div className="px-5 pt-5 pb-4 border-b border-border">
        <div className="flex items-start gap-3">
          <div style={tintStyle(source.tint)} className="relative w-10 h-10 rounded-lg flex items-center justify-center shrink-0">
            <SourceIcon className="h-5 w-5" />
          </div>
          <div className="flex-1 min-w-0">
            <button onClick={() => navigate(`/automations/${f.id}`)} className="text-[15px] font-semibold text-ink truncate hover:underline text-left block max-w-full" title={f.name || undefined}>
              {cleanTitle(f.name, t('Untitled'))}
            </button>
            <div className="text-[11px] text-tertiary truncate mt-0.5" title={t(source.hint)}>{t(source.label)}{summary ? ` → ${summary}` : ''}</div>
          </div>
          <button
            onClick={() => onToggle(f)}
            disabled={toggling}
            title={f.enabled ? t('Active — click to pause') : t('Paused — click to enable')}
            className={clsx('flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-[11px] font-medium shrink-0 transition-colors disabled:opacity-50', f.enabled ? 'border-border text-ink hover:bg-chrome' : 'border-border text-tertiary hover:bg-chrome')}
          >
            <span className={clsx('w-2 h-2 rounded-full', f.enabled ? 'bg-success' : 'bg-active')} />
            {f.enabled ? t('Active') : t('Paused')}
          </button>
        </div>

        {/* Primary actions — admin has no run; the builder is the primary destination. */}
        <div className="flex items-center gap-1.5 mt-4">
          <button
            onClick={() => navigate(`/automations/${f.id}`)}
            className="flex items-center gap-1.5 px-4 py-2 text-[13px] font-medium bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 active:scale-[0.98] transition-all"
          >
            <ArrowTopRightOnSquareIcon className="h-4 w-4" />
            {t('Open builder')}
          </button>
          {token && (
            <button
              onClick={copyWebhook}
              className="flex items-center gap-1.5 px-2.5 py-2 text-[12px] font-medium text-secondary border border-border rounded-lg hover:bg-chrome hover:text-ink transition-colors"
            >
              {copied ? <ClipboardDocumentCheckIcon className="h-3.5 w-3.5" /> : <ClipboardIcon className="h-3.5 w-3.5" />}
              {copied ? t('Copied') : t('Copy URL')}
            </button>
          )}
          <div className="flex-1" />
          <button
            onClick={() => onDelete(f)}
            className="flex items-center gap-1.5 px-2.5 py-2 text-[12px] font-medium text-danger rounded-lg hover:bg-danger-bg hover:text-danger-fg transition-colors"
          >
            <TrashIcon className="h-3.5 w-3.5" />
            {t('Delete')}
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden px-5 py-4 space-y-6">
        <div>
          <div className="mb-2"><span className="text-[10px] font-semibold uppercase tracking-wider text-tertiary">{t('Details')}</span></div>
          <div className="space-y-2 rounded-xl border border-ink/20 shadow-sm bg-surface p-3">
            <Fact icon={SourceIcon} label={t('Trigger')}>{t(source.label)}</Fact>
            {summary && <Fact icon={BoltIcon} label={t('Actions')}>{summary}</Fact>}
            <Fact icon={PlayIcon} label={t('Total runs')}>{f.trigger_count || 0}</Fact>
            <Fact icon={BoltIcon} label={t('Last run')}>{f.last_triggered_at ? formatRelativeTime(f.last_triggered_at) : t('Never')}</Fact>
          </div>
        </div>

        <div>
          <div className="mb-2"><span className="text-[10px] font-semibold uppercase tracking-wider text-tertiary">{t('Recent runs')}</span></div>
          {runList.length === 0 ? (
            <p className="text-[12px] text-tertiary">{t('No runs yet — this automation runs when its trigger fires.')}</p>
          ) : (
            <div className="rounded-xl border border-ink/20 shadow-sm bg-surface divide-y divide-border overflow-hidden">
              {runList.map((r: any) => (
                <div key={r.id} className="flex items-center justify-between gap-3 px-3 py-2.5">
                  <div className="min-w-0 flex items-center gap-2">
                    <span className={clsx('w-2 h-2 rounded-full shrink-0', statusStyle(r.status).dot)} />
                    <span className="text-[12px] font-medium text-ink capitalize truncate">{r.status}</span>
                    {r.error_message && <span className="text-[11px] text-secondary truncate">· {r.error_message}</span>}
                  </div>
                  {r.triggered_at && <span className="text-[11px] text-tertiary shrink-0" title={formatDate(r.triggered_at)}>{formatRelativeTime(r.triggered_at)}</span>}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export const AutomationsListPage: React.FC = () => {
  useRequireAuth();
  const { t } = useTranslation();
  useDocumentTitle(t('Automations'));
  const navigate = useNavigate();
  const { startFullTutorial } = useTour();
  const isWide = useIsWide();

  const [search, setSearch] = useState('');
  const [pollInterval, setPollInterval] = useState<number | undefined>(undefined);
  const { data: flows, loading, error, refresh } = useQuery(Q.triggers(), () => triggersApi.listAll(), { pollInterval });

  const allFlows = flows || [];
  const initialLoading = loading && !flows;
  const loadFailed = !flows && !initialLoading && Boolean(error);

  // Poll only while at least one automation is enabled (live trigger counts can
  // change); when everything is disabled there's nothing live to refresh.
  // Adjusted during render (guarded) rather than in an effect — `allFlows` is a
  // fresh array every render, so the effect form re-ran on every single pass.
  const wantPoll = allFlows.some((f: any) => f.enabled) ? 10000 : undefined;
  if (pollInterval !== wantPoll) setPollInterval(wantPoll);

  const [view, setView] = useState<string>(() => localStorage.getItem('writ.autoView') || 'all');
  const [sortBy, setSortBy] = useState<SortMode>(() => {
    const v = localStorage.getItem('writ.autoSort');
    return (['recent', 'most', 'name', 'created'] as const).includes(v as any) ? (v as SortMode) : 'recent';
  });
  useEffect(() => { try { localStorage.setItem('writ.autoView', view); } catch { /* noop */ } }, [view]);
  useEffect(() => { try { localStorage.setItem('writ.autoSort', sortBy); } catch { /* noop */ } }, [sortBy]);

  const searched = useMemo(() => {
    const q = search.trim().toLowerCase();
    return allFlows.filter((f: any) => !q || f.name?.toLowerCase().includes(q));
  }, [allFlows, search]);

  const views = automationsConfig.views;
  const viewCounts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const v of views) m[v.id] = searched.filter(v.predicate).length;
    return m;
  }, [searched, views]);
  const activeView = views.find((v) => v.id === view) || views[0];
  const flowList = useMemo(() => sortFlows(searched.filter(activeView.predicate), sortBy), [searched, activeView, sortBy]);

  const [pickedId, setSelectedId] = useState<number | null>(null);
  const [rawCheckedIds, setCheckedIds] = useState<Set<number>>(new Set());
  const [togglingId, setTogglingId] = useState<number | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<any | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);

  const visibleIds = useMemo(() => new Set(flowList.map((f: any) => f.id)), [flowList]);

  // Detail selection is DERIVED, not stored: the user's pick while that row is
  // still listed, otherwise the first row. An effect would paint one frame
  // against an automation the current filter no longer shows.
  const selectedId = useMemo(() => {
    if (!isWide) return pickedId;
    if (flowList.length === 0) return null;
    return pickedId != null && visibleIds.has(pickedId) ? pickedId : flowList[0].id;
  }, [pickedId, flowList, visibleIds, isWide]);

  // Bulk checks pruned ON READ — a row leaving the list (filter, search, delete)
  // drops out of the effective selection with no state write.
  const checkedIds = useMemo(
    () => new Set([...rawCheckedIds].filter((id) => visibleIds.has(id))),
    [rawCheckedIds, visibleIds],
  );

  const toggleCheck = useCallback((id: number) => setCheckedIds((prev) => { const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n; }), []);
  const clearChecks = useCallback(() => setCheckedIds(new Set()), []);
  const checkedFlows = useMemo(() => flowList.filter((f: any) => checkedIds.has(f.id)), [flowList, checkedIds]);

  const handleSelect = useCallback((f: any) => { if (isWide) setSelectedId(f.id); else navigate(`/automations/${f.id}`); }, [isWide, navigate]);

  // Admin toggle FLIPS server-side — call toggle(id) with no bool.
  const handleToggle = useCallback(async (f: any) => {
    if (togglingId) return;
    setTogglingId(f.id);
    try {
      await triggersApi.toggle(f.id);
      toast.success(f.enabled ? t('Paused') : t('Enabled'));
      refresh();
    } catch { toast.error(t('Failed to toggle')); }
    finally { setTogglingId(null); }
  }, [togglingId, refresh, t]);

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await triggersApi.delete(deleteTarget.id);
      toast.success(t('Automation deleted'));
      refresh();
      setDeleteTarget(null);
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('Failed to delete automation'));
    } finally { setDeleting(false); }
  };

  // Bulk enable/pause: admin toggle flips, so only call it for items whose state
  // actually needs changing to reach the target.
  const handleBulkToggle = useCallback(async (enable: boolean) => {
    const targets = checkedFlows.filter((f: any) => !!f.enabled !== enable);
    if (!targets.length) return;
    setBulkBusy(true);
    let ok = 0; let fail = 0;
    for (const f of targets) { try { await triggersApi.toggle(f.id); ok += 1; } catch { fail += 1; } }
    setBulkBusy(false);
    refresh();
    if (fail === 0) toast.success(enable ? t('Enabled {{n}}', { n: ok }) : t('Paused {{n}}', { n: ok }));
    else toast.error(t('{{ok}} updated · {{fail}} failed', { ok, fail }));
  }, [checkedFlows, refresh, t]);

  const handleBulkDelete = useCallback(async () => {
    setBulkBusy(true);
    let ok = 0; let fail = 0;
    for (const f of checkedFlows) { try { await triggersApi.delete(f.id); ok += 1; } catch { fail += 1; } }
    setBulkBusy(false);
    setBulkDeleteOpen(false);
    clearChecks();
    refresh();
    if (fail === 0) toast.success(t('Deleted {{n}}', { n: ok }));
    else toast.error(t('{{ok}} deleted · {{fail}} failed', { ok, fail }));
  }, [checkedFlows, clearChecks, refresh, t]);

  const activeCount = allFlows.filter((f: any) => f.enabled).length;
  const totalRuns = allFlows.reduce((s: number, f: any) => s + (f.trigger_count || 0), 0);
  const selected = flowList.find((f: any) => f.id === selectedId) || null;
  const allChecked = flowList.length > 0 && checkedIds.size >= flowList.length;

  return (
    <div className="flex flex-col h-full">
      {/* Compact inline toolbar (admin idiom — no PageHeader portal). */}
      <div className={SHELF_TOPBAR}>
        <BoltIcon className="w-4 h-4 text-secondary shrink-0" />
        <span className="text-[13px] font-semibold text-ink shrink-0">{t('Automations')}</span>
        {allFlows.length > 0 && (
          <span className="hidden @pair/stage:inline text-[11px] text-secondary ml-1">{t('{{active}} active · {{runs}} runs', { active: activeCount, runs: totalRuns })}</span>
        )}
        <div className="flex-1" />
        <button type="button" title={t('Show me how this works')} aria-label={t('Show me how this works')} onClick={() => startFullTutorial('automation')}
          className="p-1.5 text-secondary hover:text-ink rounded-lg hover:bg-surface/60 transition-colors shrink-0">
          <QuestionMarkCircleIcon className="w-4 h-4" />
        </button>
        <button data-tour="automations-new" onClick={() => navigate('/automations/new')}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-accent-strong text-accent-on text-[12px] font-semibold rounded-lg shadow-sm hover:bg-accent-strong/90 transition-colors shrink-0">
          <PlusIcon className="w-3.5 h-3.5" />{t('New')}
        </button>
      </div>

      <div className="flex flex-1 overflow-hidden">
        {initialLoading ? (
          <ShelfSkeleton withSearch label={t('Loading automations')} />
        ) : loadFailed ? (
          <div className="flex flex-1 flex-col items-center justify-center text-center px-6">
            <div className="w-12 h-12 rounded-2xl bg-hover border border-border flex items-center justify-center mb-4"><ExclamationTriangleIcon className="h-6 w-6 text-tertiary" /></div>
            <p className="text-sm font-medium text-ink">{t("Couldn't load automations")}</p>
            <p className="text-xs text-secondary mt-1">{error}</p>
            <button onClick={() => refresh()} className="mt-4 px-4 py-2 bg-accent-strong text-accent-on text-sm font-semibold rounded-lg shadow-sm hover:bg-accent-strong/90 transition-colors">{t('Retry')}</button>
          </div>
        ) : allFlows.length === 0 ? (
          <div className="flex flex-1 flex-col items-center justify-center text-center px-6">
            <div className="w-12 h-12 rounded-2xl bg-hover border border-border flex items-center justify-center mb-4"><BoltIcon className="h-6 w-6 text-tertiary" /></div>
            <p className="text-sm font-medium text-ink">{t('No automations yet')}</p>
            <p className="text-xs text-secondary mt-1 max-w-sm">{t('Automations are created automatically when you set up actions on monitors and workflows. You can also build complex pipelines here.')}</p>
            <button onClick={() => navigate('/automations/new')} className="mt-4 px-4 py-2 bg-accent-strong text-accent-on text-sm font-semibold rounded-lg shadow-sm hover:bg-accent-strong/90 transition-colors">{t('New Automation')}</button>
          </div>
        ) : (
          <div className={SHELF_CONTAINER}>
            {/* ── Master list ── */}
            <div className={SHELF_LIST_COL}>
              <div className="shrink-0 border-b border-border px-3 py-2.5 space-y-2">
                {/* Search — lives with the list it filters, above the chips. */}
                <ShelfListSearch value={search} onChange={setSearch} ariaLabel={t('Search automations')} />
                <div className="flex flex-wrap items-center gap-1">
                  {views.map((v) => {
                    const Icon = v.icon; const active = v.id === view; const count = viewCounts[v.id] ?? 0;
                    if (count === 0 && v.id !== 'all' && !active) return null;
                    return (
                      <button key={v.id} onClick={() => setView(v.id)} aria-pressed={active}
                        className={shelfFilterChipClass(active)}>
                        <Icon className="w-3 h-3" />{t(v.label)}
                        <span className={shelfFilterCountClass(active)}>{count}</span>
                      </button>
                    );
                  })}
                </div>
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[11px] text-tertiary">{t('{{n}} shown', { n: flowList.length })}</span>
                  <Select<SortMode> size="sm" value={sortBy} onChange={setSortBy} aria-label={t('Sort by')} wrapperClassName="w-36"
                    options={[{ value: 'recent', label: t('Recently triggered') }, { value: 'most', label: t('Most triggered') }, { value: 'name', label: t('Name A–Z') }, { value: 'created', label: t('Newest') }]} />
                </div>
              </div>

              {checkedIds.size > 0 && (
                <div className="shrink-0 flex items-center justify-between gap-2 border-b border-border bg-chrome px-3 py-2 bulkbar-enter">
                  <div className="flex items-center gap-2 text-[12px]">
                    <button
                      onClick={() => setCheckedIds(allChecked ? new Set() : new Set(flowList.map((f: any) => f.id)))}
                      className={clsx('flex h-4 w-4 items-center justify-center rounded border transition-colors', allChecked ? 'border-ink bg-ink text-surface' : 'border-border bg-surface')}
                      aria-label={t('Select all')}
                    >
                      {allChecked && <CheckIcon className="h-3 w-3" />}
                    </button>
                    <span className="font-medium text-ink">{t('{{n}} selected', { n: checkedIds.size })}</span>
                    <button onClick={clearChecks} className="text-tertiary hover:text-ink transition-colors">{t('Clear')}</button>
                  </div>
                  <div className="flex items-center gap-1">
                    <button onClick={() => handleBulkToggle(true)} disabled={bulkBusy} title={t('Enable')} className="p-1.5 rounded-lg text-secondary hover:bg-surface hover:text-ink disabled:opacity-50 transition-colors"><PlayIcon className="h-3.5 w-3.5" /></button>
                    <button onClick={() => handleBulkToggle(false)} disabled={bulkBusy} title={t('Pause')} className="p-1.5 rounded-lg text-secondary hover:bg-surface hover:text-ink disabled:opacity-50 transition-colors"><PauseIcon className="h-3.5 w-3.5" /></button>
                    <button onClick={() => setBulkDeleteOpen(true)} disabled={bulkBusy} title={t('Delete')} className="p-1.5 rounded-lg text-danger hover:bg-danger-bg hover:text-danger-fg disabled:opacity-50 transition-colors"><TrashIcon className="h-3.5 w-3.5" /></button>
                  </div>
                </div>
              )}

              {flowList.length === 0 ? (
                <div className="flex flex-1 flex-col items-center justify-center text-center px-6 py-10">
                  <p className="text-[13px] font-medium text-ink">{t('Nothing here')}</p>
                  <p className="text-[11px] text-secondary mt-1">{t('No automations match this filter.')}</p>
                </div>
              ) : (
                <ScrollArea className="flex-1" viewportClassName="px-2 py-2" fade="chrome">
                  <Stagger className="grid gap-0.5" staggerMs={20}>
                    {flowList.map((f: any) => (
                      <AutomationRow
                        key={f.id}
                        f={f}
                        selected={isWide && selectedId === f.id}
                        checked={checkedIds.has(f.id)}
                        toggling={togglingId === f.id}
                        onSelect={handleSelect}
                        onToggleCheck={() => toggleCheck(f.id)}
                        onToggle={handleToggle}
                      />
                    ))}
                  </Stagger>
                </ScrollArea>
              )}
            </div>

            {/* ── Detail pane (lg only) ── */}
            <div className={SHELF_DETAIL_COL}>
              <SwapFade swapKey={selected?.id ?? 'none'} className="flex flex-1 flex-col min-w-0 min-h-0">
                <AutomationDetailPane
                  f={selected}
                  toggling={selected ? togglingId === selected.id : false}
                  onToggle={handleToggle}
                  onDelete={setDeleteTarget}
                />
              </SwapFade>
            </div>
          </div>
        )}
      </div>

      {/* Modals */}
      <Modal isOpen={!!deleteTarget} onClose={() => setDeleteTarget(null)} title={t('Delete automation')}>
        <p className="text-sm text-secondary mb-4">
          {t('Are you sure you want to delete')} <strong className="text-ink">{cleanTitle(deleteTarget?.name, t('Untitled'))}</strong>? {t('This action cannot be undone.')}
        </p>
        <div className="flex items-center justify-end gap-2">
          <button onClick={() => setDeleteTarget(null)} className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg hover:bg-chrome transition-colors">{t('Cancel')}</button>
          <button onClick={handleDelete} disabled={deleting} className="px-4 py-2 text-sm font-medium bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors disabled:opacity-50">{deleting ? t('Deleting...') : t('Delete')}</button>
        </div>
      </Modal>

      <Modal isOpen={bulkDeleteOpen} onClose={() => (bulkBusy ? null : setBulkDeleteOpen(false))} title={t('Delete automations')}>
        <p className="text-sm text-secondary mb-4">{t('Delete {{n}} automation(s)? This cannot be undone.', { n: checkedFlows.length })}</p>
        <div className="flex items-center justify-end gap-2">
          <button onClick={() => setBulkDeleteOpen(false)} disabled={bulkBusy} className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg hover:bg-chrome transition-colors disabled:opacity-50">{t('Cancel')}</button>
          <button onClick={handleBulkDelete} disabled={bulkBusy || checkedFlows.length === 0} className="px-4 py-2 text-sm font-medium bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors disabled:opacity-50">{bulkBusy ? t('Deleting...') : t('Delete {{n}}', { n: checkedFlows.length })}</button>
        </div>
      </Modal>
    </div>
  );
};

export default AutomationsListPage;
