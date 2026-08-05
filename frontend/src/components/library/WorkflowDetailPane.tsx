import React, { useState, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import {
  PlayIcon,
  StopIcon,
  PencilSquareIcon,
  TrashIcon,
  ArrowTopRightOnSquareIcon,
  TableCellsIcon,
  GlobeAltIcon,
  ClockIcon,
  ArrowPathIcon,
  EyeIcon,
  EyeSlashIcon,
  KeyIcon,
  UserCircleIcon,
  LockClosedIcon,
  ListBulletIcon,
  CheckIcon,
  XMarkIcon,
  CursorArrowRaysIcon,
  DocumentDuplicateIcon,
  ExclamationTriangleIcon,
  RectangleStackIcon,
  BanknotesIcon,
  CpuChipIcon,
  CommandLineIcon,
  ClipboardDocumentIcon,
  CodeBracketIcon,
} from '@heroicons/react/24/outline';
import { SendToAgentModal } from '../fleet/SendToAgentModal';
import { useQuery } from '../../hooks/useQuery';
import { Q } from '../../stores/queryKeys';
import { automationApi } from '../../api/endpoints';
import { runsApi } from '../../api/runs';
import type { AutomationWorkflow, WorkflowStep } from '../../types/api';
import {
  formatPricePerRun,
  formatMicros,
  type WorkflowCostSummary,
  type MarketplaceCollection,
} from '../../api/marketplace';
import { formatRelativeTime, formatDate, cleanTitle } from '../../utils/format';
import { tintStyle, workflowTint } from '../../utils/tint';
import { TYPE_META, STATUS_DOT } from '../../pages/workflows/detail/meta';
import { statusStyle } from '../../utils/statusStyle';
import { RunWithTargetButton } from '../workflows/ExecutionTargetPicker';
import { InlineScheduleControl } from '../schedule/InlineScheduleControl';
import i18n from '../../i18n';
import { EmptyHero } from '../ui';

const RUNNING_STATES = ['running', 'pending', 'assigned', 'queued'];

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}

/** Short, human verb for a recorded step — keeps the steps preview scannable. */
const STEP_VERB: Record<string, string> = {
  navigate: 'Go to', navigated_to: 'Go to', open_tab: 'Open tab', wait_for_tab: 'Wait for tab',
  click: 'Click', hover: 'Hover', fill: 'Type', type: 'Type', press: 'Press',
  select: 'Select', check: 'Check', uncheck: 'Uncheck', upload: 'Upload',
  scroll: 'Scroll', scroll_into_view: 'Scroll to', screenshot: 'Screenshot',
  extract: 'Extract', evaluate: 'Run script', advanced_script: 'Run script', api_call: 'API call',
  assert: 'Assert', wait: 'Wait', wait_for_change: 'Wait for change', wait_for_download: 'Wait for download',
  captcha: 'Solve CAPTCHA', end_point: 'End',
  // `ai_continue` and `ai_navigate` are the two wire spellings of the one merged AI step,
  // so they read the same here. `codegen` is a script block, not an AI step.
  ai_continue: 'AI task', ai_navigate: 'AI task', ai_fill: 'AI fill', codegen: 'Script Block',
};

function stepLabel(step: WorkflowStep): string {
  const verb = STEP_VERB[step.type] ? i18n.t(STEP_VERB[step.type]) : step.type.replace(/_/g, ' ');
  const detail = step.url || step.value || step.selector || '';
  return detail ? `${verb} · ${detail}` : verb;
}

/** Single labelled fact (label left, value right) — the pane's quiet metadata row. */
const Fact: React.FC<{ icon: React.FC<any>; label: string; children: React.ReactNode }> = ({
  icon: Icon,
  label,
  children,
}) => (
  <div className="flex items-center gap-2 min-w-0">
    <Icon className="h-3.5 w-3.5 text-tertiary shrink-0" />
    <span className="text-[11px] text-tertiary shrink-0">{label}</span>
    <span className="ml-auto text-[12px] text-ink text-right truncate min-w-0">{children}</span>
  </div>
);

const SectionLabel: React.FC<{ children: React.ReactNode; right?: React.ReactNode }> = ({ children, right }) => (
  <div className="flex items-center justify-between mb-2">
    <span className="text-[10px] font-semibold uppercase tracking-wider text-secondary">{children}</span>
    {right}
  </div>
);

/**
 * ActionTile — one entry in the "Use this workflow" block. A shortcut into the tab
 * that already implements the capability (Connect = REST/MCP, Steps, Data), so the
 * value prop — this workflow is callable, schedulable, reusable — is one click away
 * instead of buried behind a generic "Open details".
 */
const ActionTile: React.FC<{
  icon: React.FC<any>;
  label: string;
  desc: string;
  onClick: () => void;
  disabled?: boolean;
}> = ({ icon: Icon, label, desc, onClick, disabled }) => (
  <button
    onClick={onClick}
    disabled={disabled}
    className={clsx(
      'flex items-center gap-2.5 rounded-xl border border-border bg-surface p-2.5 text-left transition-colors',
      'outline-none focus-visible:ring-2 focus-visible:ring-ink/30 active:scale-[0.99]',
      disabled ? 'opacity-50' : 'hover:bg-chrome',
    )}
  >
    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-chrome text-ink">
      <Icon className="h-4 w-4" />
    </span>
    <span className="min-w-0">
      <span className="block text-[12px] font-medium text-ink truncate">{label}</span>
      <span className="block text-[10.5px] text-tertiary truncate">{desc}</span>
    </span>
  </button>
);

interface WorkflowDetailPaneProps {
  /** Lightweight list row for the selected workflow (instant render). Null → empty state. */
  workflow: AutomationWorkflow | null;
  /** Per-workflow cost/gains summary (owner-only). Present ⇒ render the monetization section. */
  summary?: WorkflowCostSummary;
  /** Collections (folders) this workflow is a member of (owner-side management view). */
  memberCollections?: MarketplaceCollection[];
  /** Run / cancel dispatch is owned by the list (shared with row quick-run + the run modal). The
   *  optional `target` routes the run to a specific agent (auto/local/cloud); omitted = auto. */
  onRun: (w: any, target?: 'auto' | 'local' | 'cloud', agentId?: string) => void;
  onCancel: (w: any) => void;
  onDelete: (target: { id: number; name: string }) => void;
  /** Installed-proxy uninstall (marketplace op, distinct from owner delete). */
  onUninstall: (target: { id: number; name: string; slug: string | null; listingId: number | null }) => void;
  /** Resolved uninstall slug for the selected installed proxy (null otherwise). */
  uninstallSlug?: string | null;
  onDuplicate: (id: number) => void;
  /** Per-selection busy flags driven by the parent. */
  runningRow: boolean;
  cancellingRow: boolean;
  mutatingRow: boolean;
  /** Refresh the list query after an in-pane mutation (rename / toggle). */
  onChanged: () => void;
}

/**
 * Right pane of the workflows master–detail. Shows the selected workflow at a
 * glance — status, one-tap Run (with the cloud/local target picker), key facts, a
 * steps preview, recent runs and shortcuts to extracted data / the full editor —
 * without leaving the list. "Open details" jumps to the full detail/editor page
 * for steps editing, Connect and Data.
 *
 * Admin-only surfaces re-homed here (vs the desktop fork): marketplace earnings
 * (from the owner cost-summary) and the collections this workflow belongs to.
 *
 * The list payload omits the heavy `steps` blob, so the full workflow is
 * lazy-fetched here, sharing the Q.workflow(id) / Q.workflowTasks(id) caches with
 * the detail page.
 */
export const WorkflowDetailPane: React.FC<WorkflowDetailPaneProps> = ({
  workflow,
  summary,
  memberCollections,
  onRun,
  onCancel,
  onDelete,
  onUninstall,
  uninstallSlug,
  onDuplicate,
  runningRow,
  cancellingRow,
  mutatingRow,
  onChanged,
}) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const id = workflow?.id ?? null;

  const pollRunning = RUNNING_STATES.includes(workflow?.last_run_status || '');
  const poll = pollRunning ? 3000 : undefined;

  // Lazy-fetch the full workflow (steps/functions/config) for the selected row.
  const { data: full, refresh: refreshFull } = useQuery(
    id != null ? Q.workflow(id) : 'workflow:none',
    () => automationApi.getWorkflow(id as number),
    { enabled: id != null, staleTime: 15000, pollInterval: poll },
  );
  // Recent runs — the shared runs feed IS the execution history. Fail soft to []
  // so a feed hiccup never blanks the pane.
  const { data: runs } = useQuery(
    id != null ? Q.workflowTasks(id) : 'workflow:none:runs',
    () => runsApi.list({ run_type: 'workflow', entity_id: id as number, limit: 6 }).catch(() => []),
    { enabled: id != null, staleTime: 15000, pollInterval: poll },
  );

  // Richest object available — list row for instant paint, full when it lands.
  const w: any = full || workflow;

  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  const [savingName, setSavingName] = useState(false);
  const [toggling, setToggling] = useState(false);
  const [sendOpen, setSendOpen] = useState(false);

  const startRename = useCallback(() => {
    setRenameValue(w?.name || '');
    setRenaming(true);
  }, [w?.name]);

  const saveRename = useCallback(async () => {
    if (!id || !renameValue.trim() || savingName) { setRenaming(false); return; }
    setSavingName(true);
    try {
      await automationApi.updateWorkflow(id, { name: renameValue.trim() } as any);
      toast.success(t('Renamed'));
      setRenaming(false);
      refreshFull();
      onChanged();
    } catch {
      toast.error(t('Failed to rename'));
    } finally {
      setSavingName(false);
    }
  }, [id, renameValue, savingName, refreshFull, onChanged, t]);

  const handleToggle = useCallback(async () => {
    if (!w || toggling) return;
    setToggling(true);
    try {
      await automationApi.updateWorkflow(w.id, { is_active: !w.is_active });
      toast.success(w.is_active ? t('Deactivated') : t('Activated'));
      refreshFull();
      onChanged();
    } catch {
      toast.error(t('Failed to toggle'));
    } finally {
      setToggling(false);
    }
  }, [w, toggling, refreshFull, onChanged, t]);

  // ── Empty state ──────────────────────────────────────────────────────────
  if (!workflow) {
    return (
      <EmptyHero
        icon={CursorArrowRaysIcon}
        title={t('Select a workflow')}
        description={t('Pick one on the left to see its status, run it, and review recent results.')}
        className="flex-1"
      />
    );
  }

  const meta = TYPE_META[w.workflow_type] || { label: w.workflow_type, icon: CursorArrowRaysIcon };
  const TypeIcon = meta.icon;
  const isInstalled = !!w.is_installed;
  const needsAttach = isInstalled && w.installed_status === 'needs_attach';
  // Origin marker (cloud | mirrored | local:<agent>) from the merged list; the
  // P1/coordinator layer adds this — treat missing/unknown as plain cloud.
  const origin = (w as any).origin as string | undefined;
  const steps: WorkflowStep[] = full?.steps || [];
  const stepCount = full?.steps?.length ?? w.step_count ?? 0;

  // ── status line ──
  const lastStatus = w.last_run_status as string | undefined;
  const lastRunAt = w.last_run_at ?? null;
  const lastRunDurationMs = (w.last_run_duration_ms as number | null | undefined) ?? null;
  const isRunning = RUNNING_STATES.includes(lastStatus || '');
  const ss = statusStyle(lastStatus);
  const ranBefore = (w.usage_count ?? 0) > 0 || !!lastRunAt;
  let statusLabel = ranBefore ? t('Ran') : t('Never run');
  if (isRunning) statusLabel = t('Running…');
  else if (lastStatus === 'failed') {
    statusLabel = (w.consecutive_failures || 0) > 1
      ? t('Failing ×{{n}}', { n: w.consecutive_failures })
      : t('Last run failed');
  } else if (lastStatus === 'success' || lastStatus === 'completed') statusLabel = t('Succeeded');
  else if (lastStatus === 'cancelled' || lastStatus === 'timeout') statusLabel = t('Last run {{status}}', { status: lastStatus });

  const scheduleLabel = w.schedule_enabled && w.schedule_interval_ms
    ? (w.schedule_interval_ms >= 3600000
        ? t('Every {{n}}h', { n: w.schedule_interval_ms / 3600000 })
        : t('Every {{n}}m', { n: w.schedule_interval_ms / 60000 }))
    : t('Off');

  const hasData = w.last_run_has_extracted_data
    ?? (w.last_run_extracted_data && Object.keys(w.last_run_extracted_data).length > 0);

  const openEditor = (tab?: string) => navigate(`/workflows/${w.id}${tab ? `?tab=${tab}` : ''}`);

  const isStreaming = w.workflow_type === 'streaming';
  // The coordinator mounts the automation router under /api (routers/automation.py) —
  // NOT under /api/v1, where nothing but files/local-workflows live. Same URL the
  // Connect tab copies; keeping one source for it is what stops this pane from
  // advertising a 404 the way the cloud tree once did.
  // NB: `origin` above is the workflow's ORIGIN MARKER (cloud|local:…), not the page's.
  const callEndpoint = `${typeof window !== 'undefined' ? window.location.origin : ''}/api/automation/workflows/${w.id}/run`;
  const copyEndpoint = () => {
    navigator.clipboard.writeText(callEndpoint);
    toast.success(t('Endpoint copied'));
  };

  return (
    <div className="flex flex-1 flex-col min-h-0">
      {/* ── Header ── */}
      {/* The pane spans whatever the window leaves (can exceed 1100px on large
          displays); content self-caps to a readable column (mx-auto max-w-3xl)
          so facts/tiles/runs never stretch label-left value-right across the
          full pane. The chrome (border, surface) stays full-bleed. */}
      <div className="px-5 pt-5 pb-4 border-b border-border">
        <div className="mx-auto w-full max-w-3xl">
        <div className="flex items-start gap-3">
          <div
            style={tintStyle(isInstalled ? 'neutral' : workflowTint(w.workflow_type))}
            className="relative w-10 h-10 rounded-lg flex items-center justify-center shrink-0"
          >
            {isRunning ? (
              <>
                <div className="absolute inset-0 rounded-xl border-2 border-current/25 border-t-current animate-spin" />
                <TypeIcon className="h-5 w-5" />
              </>
            ) : (
              <TypeIcon className="h-5 w-5" />
            )}
            {isInstalled && (
              <span className="absolute -bottom-1 -right-1 w-4 h-4 rounded-full bg-ink text-surface flex items-center justify-center">
                <LockClosedIcon className="w-2.5 h-2.5" />
              </span>
            )}
          </div>

          <div className="flex-1 min-w-0">
            {renaming ? (
              <form onSubmit={(e) => { e.preventDefault(); saveRename(); }} className="flex items-center gap-1.5">
                <input
                  autoFocus
                  value={renameValue}
                  onChange={(e) => setRenameValue(e.target.value)}
                  onBlur={saveRename}
                  className="flex-1 min-w-0 text-[15px] font-semibold text-ink bg-canvas border border-border rounded-lg px-2 py-1 outline-none focus:ring-2 focus:ring-ink/10"
                />
                <button type="submit" disabled={savingName} className="p-1.5 rounded-lg text-ink hover:bg-chrome disabled:opacity-50">
                  <CheckIcon className="h-4 w-4" />
                </button>
                <button type="button" onClick={() => setRenaming(false)} className="p-1.5 rounded-lg text-tertiary hover:bg-chrome">
                  <XMarkIcon className="h-4 w-4" />
                </button>
              </form>
            ) : (
              <div className="flex items-center gap-2 min-w-0">
                <button
                  onClick={() => openEditor()}
                  className="text-[15px] font-semibold text-ink truncate hover:underline text-left"
                  title={w.name || undefined}
                >
                  {cleanTitle(w.name)}
                </button>
                <button
                  onClick={startRename}
                  className="p-1 rounded-md text-tertiary hover:text-ink hover:bg-chrome transition-colors shrink-0"
                  title={t('Rename')}
                >
                  <PencilSquareIcon className="h-3.5 w-3.5" />
                </button>
              </div>
            )}
            <div className="flex items-center gap-2 mt-1 flex-wrap">
              <span className="inline-flex items-center gap-1 text-[11px] text-tertiary">
                <TypeIcon className="w-3 h-3" />
                {t(meta.label)}
              </span>
              {summary?.is_monetized && !isInstalled && (
                <span
                  className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-success-bg text-success-fg"
                  title={t('Listed for sale on the marketplace')}
                >
                  <span className="w-1.5 h-1.5 rounded-full bg-success" />
                  {t('Selling')}
                </span>
              )}
              {isInstalled && (
                <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-chrome text-secondary">
                  <LockClosedIcon className="w-2.5 h-2.5" />
                  {w.creator_name ? t('Installed · {{name}}', { name: w.creator_name }) : t('Installed')}
                </span>
              )}
              {origin === 'mirrored' && (
                <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-chrome text-secondary">
                  {t('Mirrored')}
                </span>
              )}
              {origin?.startsWith('local:') && (
                <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-chrome text-secondary">
                  {t('On {{agent}} · local', { agent: origin.slice('local:'.length) })}
                </span>
              )}
              {needsAttach && (
                <span
                  className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-warning-bg text-warning-fg"
                  title={t('Attach your data before this workflow can run')}
                >
                  <ExclamationTriangleIcon className="w-2.5 h-2.5" />
                  {t('Needs your data')}
                </span>
              )}
              {/* Active toggle — quiet dot control (owner + installed alike). */}
              <button
                onClick={handleToggle}
                disabled={toggling}
                title={w.is_active ? t('Active — click to deactivate') : t('Inactive — click to activate')}
                className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-chrome text-secondary hover:text-ink transition-colors disabled:opacity-50"
              >
                <span className={clsx('w-1.5 h-1.5 rounded-full', w.is_active ? 'bg-ink' : 'bg-tertiary')} />
                {w.is_active ? t('Active') : t('Inactive')}
              </button>
            </div>
          </div>
        </div>

        {/* Status + primary actions. `flex-wrap` so the action buttons drop to a
            new line instead of overflowing the pane when it's narrow (the pane
            width tracks the window, not the viewport, so it can be ~520px). */}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 mt-4">
          <div className="flex items-center gap-2 min-w-0 flex-1">
            <span className={clsx('w-2.5 h-2.5 rounded-full shrink-0', isRunning ? 'bg-warning animate-status-pulse' : ss.dot)} />
            <span className="text-[13px] font-medium text-ink truncate">{statusLabel}</span>
            {lastRunAt && !isRunning && (
              <span className="text-[11px] text-tertiary shrink-0" title={formatDate(lastRunAt)}>
                {formatRelativeTime(lastRunAt)}
              </span>
            )}
            {lastRunDurationMs != null && !isRunning && (
              <span className="text-[11px] text-tertiary tabular-nums shrink-0">· {formatDuration(lastRunDurationMs)}</span>
            )}
          </div>

          {isRunning ? (
            <button
              onClick={() => onCancel(w)}
              disabled={cancellingRow}
              className="flex items-center gap-1.5 px-3.5 py-2 text-[13px] font-medium rounded-lg border border-border text-secondary hover:bg-chrome active:scale-[0.98] transition-all disabled:opacity-50 shrink-0"
            >
              <StopIcon className="h-4 w-4" />
              {cancellingRow ? t('Stopping…') : t('Stop')}
            </button>
          ) : needsAttach ? (
            <button
              onClick={() => openEditor()}
              className="flex items-center gap-1.5 px-4 py-2 text-[13px] font-medium bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 active:scale-[0.98] transition-all shrink-0"
            >
              <ExclamationTriangleIcon className="h-4 w-4" />
              {t('Attach data')}
            </button>
          ) : isInstalled ? (
            // Installed proxies run via the marketplace fast-run flow on the detail page.
            <button
              onClick={() => openEditor()}
              className="flex items-center gap-1.5 px-4 py-2 text-[13px] font-medium bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 active:scale-[0.98] transition-all shrink-0"
            >
              <PlayIcon className="h-4 w-4" />
              {t('Run')}
            </button>
          ) : (
            <RunWithTargetButton
              onRun={(target, agentId) => onRun(w, target, agentId)}
              loading={runningRow}
              size="md"
            />
          )}
        </div>

        {/* Secondary actions. View data / Duplicate / Send to agent moved into the
            "Use this workflow" grid in the body (where the cloud app has them), so
            this row keeps only the two that are about the pane itself: open the full
            page, and the destructive one. `flex-wrap` so it wraps instead of running
            off the edge of a narrow pane. */}
        <div className="flex flex-wrap items-center gap-1.5 mt-3">
          <button
            onClick={() => openEditor()}
            className="flex items-center gap-1.5 px-2.5 py-1.5 text-[12px] font-medium text-secondary border border-border rounded-lg hover:bg-chrome hover:text-ink transition-colors"
          >
            <ArrowTopRightOnSquareIcon className="h-3.5 w-3.5" />
            {t('Open details')}
          </button>
          {/* `ml-auto` (not a flex-1 spacer) keeps Delete pushed to the right on
              a full row, and lets it wrap cleanly instead of leaving a gap. */}
          <button
            onClick={() =>
              isInstalled
                ? onUninstall({ id: w.id, name: w.name, slug: uninstallSlug ?? null, listingId: w.source_listing_id ?? null })
                : onDelete({ id: w.id, name: w.name })
            }
            className="ml-auto flex items-center gap-1.5 px-2.5 py-1.5 text-[12px] font-medium text-danger rounded-lg hover:bg-danger-bg hover:text-danger-fg transition-colors"
            title={isInstalled ? t('Uninstall') : t('Delete')}
          >
            <TrashIcon className="h-3.5 w-3.5" />
            {isInstalled ? t('Uninstall') : t('Delete')}
          </button>
        </div>
        </div>
      </div>

      {/* ── Body (scrolls) ── */}
      <div className="flex-1 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden px-5 py-4">
        <div className="mx-auto w-full max-w-3xl space-y-6">
        {/* Facts */}
        <div>
          <SectionLabel>{t('Details')}</SectionLabel>
          <div className="space-y-2 rounded-xl border border-ink/20 bg-surface p-3">
            {w.entry_url && (
              <Fact icon={GlobeAltIcon} label={t('Entry')}>
                <span className="font-mono">{w.entry_url}</span>
              </Fact>
            )}
            <Fact icon={ListBulletIcon} label={t('Steps')}>{stepCount}</Fact>
            <Fact icon={ClockIcon} label={t('Schedule')}>{scheduleLabel}</Fact>
            {(w.has_login || w.default_persona_id) && (
              <Fact icon={UserCircleIcon} label={t('Persona')}>
                {w.default_persona_id ? t('Attached') : t('None')}
              </Fact>
            )}
            {w.timeout_ms != null && (
              <Fact icon={ClockIcon} label={t('Timeout')}>{w.timeout_ms / 1000}s</Fact>
            )}
            {w.retry_count != null && (
              <Fact icon={ArrowPathIcon} label={t('Retries')}>{w.retry_count}</Fact>
            )}
            <Fact icon={w.headless ? EyeSlashIcon : EyeIcon} label={t('Browser')}>
              {w.headless ? t('Headless') : t('Visible')}
            </Fact>
            {w.has_credentials && (
              <Fact icon={KeyIcon} label={t('Credentials')}>{t('Saved')}</Fact>
            )}
            {(w.usage_count ?? 0) > 0 && (
              <Fact icon={PlayIcon} label={t('Total runs')}>{w.usage_count}</Fact>
            )}
          </div>
        </div>

        {/* Use this workflow — turns the pane from a fact sheet into an action
            surface, matching the cloud app. Each tile is a shortcut into the tab
            that already implements the capability, so "this is callable
            infrastructure" is one click away rather than something you have to go
            hunting for behind "Open details". */}
        <div>
          <SectionLabel>{t('Use this workflow')}</SectionLabel>
          {/* auto-fit tracks the pane's real width, so tiles reflow 1→2→3 columns
              with the window instead of staying cramped at narrow widths. */}
          <div className="grid gap-2 [grid-template-columns:repeat(auto-fit,minmax(200px,1fr))]">
            {/* Call it — the headline. The body opens the Connect tab (endpoint,
                key, MCP); Copy grabs the run endpoint in one click. This is the
                COORDINATOR's real route (/api/automation/...) — the /api/v1 tree
                that once got advertised here is mounted nowhere and 404s. */}
            <div className="[grid-column:1/-1] flex items-center gap-2.5 rounded-xl border border-ink/20 bg-ink/[0.04] p-2.5">
              <button
                onClick={() => openEditor('connect')}
                className="group flex min-w-0 flex-1 items-center gap-2.5 rounded-lg text-left outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ink/30"
              >
                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-ink/15 text-ink">
                  <CommandLineIcon className="h-4 w-4" />
                </span>
                <span className="min-w-0">
                  <span className="block text-[12px] font-medium text-ink truncate group-hover:underline">{t('Call it')}</span>
                  <span className="block text-[10.5px] text-tertiary truncate" title={callEndpoint}>{t('REST · MCP')}</span>
                </span>
              </button>
              <button
                onClick={copyEndpoint}
                title={callEndpoint}
                aria-label={t('Copy endpoint URL')}
                className="flex shrink-0 items-center gap-1 rounded-lg border border-border bg-surface px-2 py-1 text-[11px] font-medium text-secondary hover:text-ink hover:border-border-strong outline-none focus-visible:ring-2 focus-visible:ring-ink/40 transition-colors"
              >
                <ClipboardDocumentIcon className="h-3.5 w-3.5" />
                {t('Copy')}
              </button>
            </div>
            {!isInstalled && (
              <ActionTile
                icon={isStreaming ? CodeBracketIcon : ListBulletIcon}
                label={isStreaming ? t('Edit script') : t('Edit steps')}
                desc={isStreaming ? t('Live session logic') : t('{{n}} recorded', { n: stepCount })}
                onClick={() => openEditor('steps')}
              />
            )}
            <ActionTile
              icon={TableCellsIcon}
              label={t('View data')}
              desc={hasData ? t('Extracted results') : t('Nothing yet')}
              onClick={() => openEditor('data')}
            />
            {!isInstalled && (
              <ActionTile
                icon={DocumentDuplicateIcon}
                label={t('Duplicate')}
                desc={mutatingRow ? t('Duplicating…') : t('Make a copy')}
                disabled={mutatingRow}
                onClick={() => { if (!mutatingRow) onDuplicate(w.id); }}
              />
            )}
            {/* Push the workflow (bundled with its secrets + persona) onto a
                connected agent so it can run there. Installed proxies aren't yours
                to send. */}
            {!isInstalled && (
              <ActionTile
                icon={CpuChipIcon}
                label={t('Send to agent')}
                desc={t('Run it on your machine')}
                onClick={() => setSendOpen(true)}
              />
            )}
          </div>
        </div>

        {/* Schedule — inline recurrence editor + one-click on/off (no navigation). */}
        {w.workflow_type !== 'streaming' && (
          <div>
            <SectionLabel>{t('Schedule')}</SectionLabel>
            <InlineScheduleControl workflow={w} refresh={refreshFull} onChanged={onChanged} />
          </div>
        )}

        {/* Marketplace earnings (admin-only, re-homed) — owner cost-summary. */}
        {summary && (
          <div>
            <SectionLabel
              right={
                <button onClick={() => openEditor('monetize')} className="text-[10px] text-tertiary hover:text-ink transition-colors">
                  {t('Manage')}
                </button>
              }
            >
              {t('Monetization')}
            </SectionLabel>
            {summary.is_monetized ? (
              <div className="space-y-2 rounded-xl border border-ink/20 bg-surface p-3">
                <Fact icon={BanknotesIcon} label={t('Price')}>{formatPricePerRun(summary.price_per_success_micros)}</Fact>
                {summary.earnings_total_micros > 0 && (
                  <Fact icon={BanknotesIcon} label={t('Earned')}>
                    <span className="tabular-nums">{formatMicros(summary.earnings_total_micros)}</span>
                    <span className="text-tertiary"> · {t('{{n}} runs', { n: summary.run_count })}</span>
                  </Fact>
                )}
                {summary.earnings_pending_micros > 0 && (
                  <Fact icon={ClockIcon} label={t('Pending')}>
                    <span className="tabular-nums">{formatMicros(summary.earnings_pending_micros)}</span>
                  </Fact>
                )}
              </div>
            ) : (
              <div className="flex items-center justify-between rounded-xl border border-ink/20 bg-surface p-3">
                <span className="text-[12px] text-tertiary">{t('Not listed for sale.')}</span>
                <button
                  onClick={() => openEditor('monetize')}
                  className="text-[11px] font-medium text-secondary hover:text-ink transition-colors"
                >
                  {t('Sell this')}
                </button>
              </div>
            )}
          </div>
        )}

        {/* Collections membership (admin-only, re-homed) — folders this API is in. */}
        {(memberCollections?.length ?? 0) > 0 && (
          <div>
            <SectionLabel
              right={
                <button onClick={() => navigate('/collections')} className="text-[10px] text-tertiary hover:text-ink transition-colors">
                  {t('Manage')}
                </button>
              }
            >
              {t('Collections')}
            </SectionLabel>
            <div className="flex flex-wrap gap-1.5">
              {memberCollections!.map((c) => (
                <button
                  key={c.id}
                  onClick={() => navigate('/collections')}
                  className="inline-flex items-center gap-1.5 px-2 py-1 rounded-lg border border-border bg-surface text-[11px] font-medium text-secondary hover:text-ink hover:border-border-strong transition-colors"
                >
                  <RectangleStackIcon className="h-3.5 w-3.5 text-tertiary" />
                  {cleanTitle(c.name)}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Steps preview */}
        {!isInstalled && (
          <div>
            <SectionLabel
              right={stepCount > 0 ? (
                <button onClick={() => openEditor('steps')} className="text-[10px] text-tertiary hover:text-ink transition-colors">
                  {t('Edit')}
                </button>
              ) : undefined}
            >
              {t('Steps')}
            </SectionLabel>
            {stepCount === 0 ? (
              <p className="text-[12px] text-tertiary">{t('No steps recorded yet.')}</p>
            ) : steps.length === 0 ? (
              <div className="h-12 rounded-lg bg-hover animate-pulse" />
            ) : (
              <ol className="rounded-xl border border-ink/20 bg-surface divide-y divide-border overflow-hidden">
                {steps.slice(0, 12).map((step, i) => (
                  <li key={step.id || i} className="flex items-center gap-2.5 px-3 py-2">
                    <span className="text-[10px] tabular-nums text-tertiary w-4 text-right shrink-0">{i + 1}</span>
                    <span className="text-[12px] text-ink truncate" title={stepLabel(step)}>{stepLabel(step)}</span>
                  </li>
                ))}
                {steps.length > 12 && (
                  <li className="px-3 py-2">
                    <button onClick={() => openEditor('steps')} className="text-[11px] text-tertiary hover:text-ink transition-colors">
                      {t('+{{n}} more steps', { n: steps.length - 12 })}
                    </button>
                  </li>
                )}
              </ol>
            )}
          </div>
        )}

        {/* Recent runs */}
        <div>
          <SectionLabel
            right={(runs || []).length > 0 ? (
              <button onClick={() => openEditor()} className="text-[10px] text-tertiary hover:text-ink transition-colors">
                {t('All runs')}
              </button>
            ) : undefined}
          >
            {t('Recent runs')}
          </SectionLabel>
          {(runs || []).length === 0 ? (
            <p className="text-[12px] text-tertiary">{t('No runs yet — run this workflow to see history.')}</p>
          ) : (
            <div className="rounded-xl border border-ink/20 bg-surface divide-y divide-border overflow-hidden">
              {(runs || []).map((run) => (
                <div key={run.id} className="flex items-center gap-3 px-3 py-2.5">
                  <span className={clsx('w-2 h-2 rounded-full shrink-0', STATUS_DOT[run.status] || 'bg-active')} />
                  <div className="min-w-0 flex-1">
                    <div className="text-[12px] font-medium text-ink capitalize">{run.status}</div>
                    {run.error && <div className="text-[11px] text-secondary truncate" title={run.error}>{run.error}</div>}
                  </div>
                  <div className="text-[11px] text-tertiary text-right whitespace-nowrap shrink-0">
                    {run.duration_ms != null && <span className="tabular-nums">{formatDuration(run.duration_ms)}</span>}
                    {run.started_at && <span className="ml-2">{formatRelativeTime(run.started_at)}</span>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
        </div>
      </div>

      <SendToAgentModal
        open={sendOpen}
        onClose={() => setSendOpen(false)}
        kind="workflow"
        entityId={w.id}
        entityName={w.name}
      />
    </div>
  );
};
