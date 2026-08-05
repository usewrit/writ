import React, { useState } from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import {
  ChevronDoubleRightIcon,
  ChevronDoubleLeftIcon,
  PlayIcon,
  StopIcon,
} from '@heroicons/react/24/outline';
import { useQuery } from '../../../hooks/useQuery';
import { Q } from '../../../stores/queryKeys';
import { mcpPublishApi } from '../../../api/publish';
import type { McpEndpoint } from '../../../api/endpoints';
import { formatRelativeTime } from '../../../utils/format';
import { TYPE_META } from './meta';

const COLLAPSE_KEY = 'wfDetailRailCollapsed';

// ── visual idiom mirrors the list pages' ToolRail ──
const Section: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => (
  <div className="px-3 py-3 border-b border-border last:border-b-0">
    <div className="mb-2 px-1">
      <span className="uppercase tracking-wider text-[10px] font-semibold text-tertiary">{title}</span>
    </div>
    {children}
  </div>
);

// Restrained semantic status dot (see utils/statusStyle.ts): failed=red,
// in-progress=amber pulse, ok=green.
const Dot: React.FC<{ ok?: boolean | null }> = ({ ok }) => (
  <span
    className={clsx(
      'w-1.5 h-1.5 rounded-full shrink-0',
      ok === false ? 'bg-red-500' : ok === null ? 'bg-amber-500 animate-status-pulse' : 'bg-green-500',
    )}
  />
);

const FactRow: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <div className="flex items-center justify-between gap-2 px-1 py-1">
    <span className="text-[11px] text-tertiary shrink-0">{label}</span>
    <div className="text-[11px] text-ink text-right min-w-0">{children}</div>
  </div>
);

interface SummaryRailProps {
  workflow: any;
  tasks: any[];
  linkedTriggers: any[];
  isStreaming: boolean;
  activeSession?: any;
  recentSessions?: any[];
  isRunning: boolean;
  busy: boolean;
  onRun: () => void;
  onStop: () => void;
  onNavigateTab: (tab: string) => void;
}

/**
 * At-a-glance rail for the workflow detail page. Read-mostly: status, key
 * facts, where this workflow is published, and recent activity. All data comes
 * from queries the page already runs (plus the shared publish caches).
 */
export const SummaryRail: React.FC<SummaryRailProps> = ({
  workflow, tasks, linkedTriggers, isStreaming, activeSession,
  isRunning, busy, onRun, onStop, onNavigateTab,
}) => {
  const { t } = useTranslation();
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(COLLAPSE_KEY) === '1');
  const toggleCollapsed = () => {
    setCollapsed(c => {
      localStorage.setItem(COLLAPSE_KEY, c ? '0' : '1');
      return !c;
    });
  };

  // Shared caches — same MCP-endpoints key the Connect tab uses, so no extra request.
  const { data: mcpEndpoints } = useQuery<McpEndpoint[]>(Q.key('mcp-endpoints'), mcpPublishApi.list, { silent: true });
  const mcpPublished = !!mcpPublishApi.findForWorkflow(mcpEndpoints, workflow.id);
  const oaiEnabled = !!workflow.streaming_config?.openai_compat?.enabled;

  const typeMeta = TYPE_META[workflow.workflow_type] || { label: workflow.workflow_type };

  // ── status summary ──
  // Prefer the workflow's denormalized last-run fields, but fall back to the
  // most recent task. Otherwise a workflow with successful runs (listed right
  // below in Recent runs) still reads "Never run" whenever last_run_status
  // wasn't backfilled — a confusing contradiction.
  const lastTask = !isStreaming ? (tasks || [])[0] : undefined;
  const lastStatus = workflow.last_run_status || lastTask?.status;
  const lastRunAt = workflow.last_run_at || lastTask?.created_at;

  let statusDot: boolean | null = true;
  let statusLabel = t('Never run');
  let statusDetail: string | undefined;
  if (isStreaming) {
    if (activeSession) {
      statusDot = null;
      statusLabel = t('Session active');
      statusDetail = activeSession.events_emitted != null ? t('{{n}} events', { n: activeSession.events_emitted }) : undefined;
    } else {
      statusDot = true;
      statusLabel = t('No active session');
    }
  } else if (isRunning) {
    statusDot = null;
    statusLabel = t('Running…');
  } else if (lastStatus === 'failed') {
    statusDot = false;
    statusLabel = (workflow.consecutive_failures || 0) > 1 ? t('Failing ×{{n}}', { n: workflow.consecutive_failures }) : t('Last run failed');
  } else if (lastStatus === 'success' || lastStatus === 'completed') {
    statusLabel = t('Last run succeeded');
  } else if (lastStatus === 'cancelled' || lastStatus === 'skipped') {
    statusDot = null;
    statusLabel = t('Last run {{status}}', { status: lastStatus });
  } else if (!lastStatus) {
    // genuinely never run — keep the neutral label but don't imply success
    statusLabel = t('Never run');
  }
  if (!isStreaming && lastRunAt) {
    statusDetail = formatRelativeTime(lastRunAt);
  }

  const scheduleLabel = workflow.schedule_enabled && workflow.schedule_interval_ms
    ? (workflow.schedule_interval_ms >= 3600000
        ? t('Every {{n}}h', { n: workflow.schedule_interval_ms / 3600000 })
        : t('Every {{n}}m', { n: workflow.schedule_interval_ms / 60000 }))
    : t('Off');


  return (
    <aside
      data-tour="wf-detail-rail"
      className={clsx(
        'hidden @rail/stage:flex flex-col shrink-0 border-l border-border bg-surface transition-[width] duration-200',
        collapsed ? 'w-[44px]' : 'w-[264px]',
      )}
    >
      {collapsed ? (
        <button
          onClick={toggleCollapsed}
          className="flex flex-col items-center gap-2 pt-3 text-tertiary hover:text-ink transition-colors"
          title={t('Expand summary')}
        >
          <ChevronDoubleLeftIcon className="w-4 h-4" />
          <Dot ok={statusDot} />
        </button>
      ) : (
        <>
          <div className="flex items-center justify-between h-11 px-3 border-b border-border shrink-0">
            <span className="text-[12px] font-semibold text-ink">{t('At a glance')}</span>
            <button onClick={toggleCollapsed} className="p-1 text-tertiary hover:text-ink transition-colors" title={t('Collapse')}>
              <ChevronDoubleRightIcon className="w-4 h-4" />
            </button>
          </div>

          <div className="flex-1 overflow-y-auto">
            {/* Status */}
            <Section title={t('Status')}>
              <div className="flex items-center gap-2 px-1">
                <Dot ok={statusDot} />
                <span className="text-[13px] font-medium text-ink truncate">{statusLabel}</span>
                {statusDetail && <span className="text-[11px] text-tertiary ml-auto shrink-0">{statusDetail}</span>}
              </div>
              {/* Streaming session controls (Start/Open/End) live in the header
                  toolbar and the Overview "Session" card — intentionally not
                  duplicated here. The rail keeps only the regular run/stop. */}
              {!isStreaming && (
                <div className="mt-2.5 px-1">
                  {isRunning ? (
                    <button
                      onClick={onStop}
                      disabled={busy}
                      className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-[11px] font-medium text-secondary border border-border/80 rounded-lg hover:bg-chrome disabled:opacity-50 transition-colors"
                    >
                      <StopIcon className="w-3 h-3" />
                      {t('Stop run')}
                    </button>
                  ) : (
                    <button
                      onClick={onRun}
                      disabled={busy}
                      className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-[11px] font-semibold shadow-sm bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 disabled:opacity-50 transition-colors"
                    >
                      <PlayIcon className="w-3 h-3" />
                      {t('Run now')}
                    </button>
                  )}
                </div>
              )}
            </Section>

            {/* Details — quick facts not shown in the main column (Type/Entry
                live in the About card, so they're not repeated here). */}
            <Section title={t('Details')}>
              <FactRow label={t('Type')}>{t(typeMeta.label)}</FactRow>
              {!isStreaming && <FactRow label={t('Schedule')}>{scheduleLabel}</FactRow>}
              {!isStreaming && (workflow.has_login || workflow.default_persona_id) && (
                <FactRow label={t('Persona')}>{workflow.default_persona_id ? t('Attached') : t('None')}</FactRow>
              )}
              <FactRow label={t('Steps')}>{isStreaming ? (workflow.streaming_config?.setup_steps_count || workflow.steps?.length || 0) : (workflow.steps?.length || 0)}</FactRow>
              <FactRow label={t('Automations')}>{linkedTriggers.length}</FactRow>
            </Section>

            {/* Exposure */}
            <Section title={t('Available as')}>
              <div className="space-y-0.5">
                {[
                  ...(!isStreaming ? [{ id: 'rest', label: t('REST endpoint'), on: true }] : []),
                  { id: 'mcp', label: t('MCP tool'), on: mcpPublished },
                  ...(isStreaming ? [{ id: 'oai', label: t('OpenAI endpoint'), on: oaiEnabled }] : []),
                ].map(e => (
                  <button
                    key={e.id}
                    onClick={() => onNavigateTab('connect')}
                    className="w-full flex items-center gap-2 px-1 py-1 rounded-md hover:bg-chrome transition-colors"
                  >
                    <span className={clsx('w-1.5 h-1.5 rounded-full shrink-0', e.on ? 'bg-ink' : 'border border-border')} />
                    <span className="text-[12px] text-ink flex-1 text-left">{e.label}</span>
                    <span className="text-[10px] text-tertiary">{e.on ? t('On') : t('Off')}</span>
                  </button>
                ))}
              </div>
            </Section>

          </div>
        </>
      )}
    </aside>
  );
};
