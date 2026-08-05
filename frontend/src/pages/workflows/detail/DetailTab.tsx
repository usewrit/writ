import React from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import {
  GlobeAltIcon,
  ClipboardDocumentIcon,
  WrenchScrewdriverIcon,
  PlayIcon,
  StopIcon,
  SignalIcon,
  ArrowRightIcon,
} from '@heroicons/react/24/outline';
import { automationApi } from '../../../api/endpoints';
import { formatRelativeTime } from '../../../utils/format';
import { Section } from './Section';
import { AuthPanel } from './AuthPanel';
import { Collapsible } from './Collapsible';
import { RunsTab } from './RunsTab';
import { DataWidget } from './DataWidget';
import { InputsIdentitySection, RunTuningSection } from './SettingsTab';
import { TierBadge, deriveProspectiveTier } from '../../../components/TierBadge';
import { WorkflowDetailTour, WorkflowDetailTourButton } from './tour/DetailTour';
import i18n from '../../../i18n';

interface DetailTabProps {
  workflow: any;
  tasks: any[];
  linkedTriggers: any[];
  isStreaming: boolean;
  activeSession?: any;
  recentSessions?: any[];
  busy: boolean;
  onStartSession: () => void;
  onEndSession: () => void;
  onNavigateTab: (tab: string) => void;
  onRefresh: () => void;
  /** True when this workflow already has a public marketplace listing. */
  hasPublicListing?: boolean;
  /** Owner-only marker — show the Earn & share widget (fast-links to Monetize). */
  isOwner?: boolean;
  /** Open the Advanced "How it runs" section on mount (deep-link from ?tab=settings). */
  defaultAdvancedOpen?: boolean;
}

const copyText = (text: string) => {
  navigator.clipboard.writeText(text);
  toast.success(i18n.t('Copied'));
};

/**
 * "What is it" — the workflow's identity, what it NEEDS to run (inputs/secrets/
 * persona, kept principal & inline), what it produced (Data widget), how it's
 * doing (run history), and — collapsed — how it runs (execution tuning).
 *
 * Invocation/wiring (schedule, REST/MCP, webhooks, automations) lives in Connect;
 * marketplace/earnings lives on the dedicated Monetize page (Earn widget links there).
 */
export const DetailTab: React.FC<DetailTabProps> = ({
  workflow, tasks, linkedTriggers, isStreaming, activeSession, recentSessions = [],
  busy, onStartSession, onEndSession, onNavigateTab, onRefresh,
  defaultAdvancedOpen = false,
}) => {
  const { t } = useTranslation();
  const sc = workflow.streaming_config || {};

  // Prospective cloud tier (auto-derived; shown, not chosen). A login/persona makes
  // runs sensitive → isolated on Writ Cloud. Only meaningful on the cloud venue.
  const wfSensitive = !!(workflow.has_login || workflow.default_persona_id);
  const wfTier = deriveProspectiveTier({ sensitive: wfSensitive, venueHint: workflow.execution_target || 'auto' });

  // ── streaming variant ──
  if (isStreaming) {
    const fnCount = (workflow.functions || []).length;
    const handlerCount = (sc.handlers || []).length;
    const fnLabel = fnCount === 1 ? t('{{n}} function', { n: fnCount }) : t('{{n}} functions', { n: fnCount });
    const handlerLabel = handlerCount === 1 ? t('{{n}} legacy handler', { n: handlerCount }) : t('{{n}} legacy handlers', { n: handlerCount });
    // All capability rows are configured in Connect now (steps in the Steps tab).
    const capabilities: { label: string; value: string; tab: string }[] = [
      { label: t('Setup steps'), value: String(sc.setup_steps_count || workflow.steps?.length || 0), tab: 'steps' },
      {
        label: t('Callable functions'),
        value: fnCount + handlerCount > 0 ? `${fnLabel}${handlerCount ? ` · ${handlerLabel}` : ''}` : t('None'),
        tab: 'connect',
      },
      { label: t('OpenAI-compatible endpoint'), value: sc.openai_compat?.enabled ? t('On') : t('Off'), tab: 'connect' },
      { label: t('Multiple conversations'), value: sc.multi_conversation ? t('On') : t('Off'), tab: 'connect' },
      { label: t('Advanced script'), value: sc.advanced_script?.enabled ? t('On') : t('Off'), tab: 'connect' },
    ];

    return (
      <div className="space-y-6">
        {/* Session */}
        <Section
          title={t('Session')}
          description={t('Streaming workflows run as long-lived browser sessions — start one, then call its handlers over the API.')}
          right={<WorkflowDetailTourButton />}
          anchor="wf-detail-session"
        >
          <div className="bg-surface border border-ink/20 shadow-sm rounded-xl overflow-hidden">
            <div className="flex items-center gap-3 px-4 py-3">
              <span className="relative flex w-2 h-2 shrink-0">
                {activeSession && <span className="absolute inline-flex w-full h-full rounded-full bg-emerald-500 opacity-60 animate-status-pulse" />}
                <span className={clsx('relative inline-flex w-2 h-2 rounded-full', activeSession ? 'bg-emerald-500' : 'bg-active')} />
              </span>
              <div className="flex-1 min-w-0">
                <p className="text-[13px] font-medium text-ink">{activeSession ? t('Session active') : t('No active session')}</p>
                {activeSession && (
                  <p className="text-[11px] text-emerald-600 mt-0.5">
                    <span className="capitalize">{activeSession.status}</span> · {t('{{n}} events', { n: activeSession.events_emitted ?? 0 })}
                  </p>
                )}
              </div>
              {activeSession ? (
                <div className="flex items-center gap-1.5 shrink-0">
                  <Link
                    to={`/streaming/${activeSession.session_key}`}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium text-ink border border-border rounded-lg hover:bg-chrome transition-colors"
                  >
                    <SignalIcon className="w-3.5 h-3.5" />
                    {t('Open')}
                  </Link>
                  <button
                    onClick={onEndSession}
                    disabled={busy}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium text-secondary border border-border rounded-lg hover:bg-chrome disabled:opacity-50 transition-colors"
                  >
                    <StopIcon className="w-3.5 h-3.5" />
                    {t('End')}
                  </button>
                </div>
              ) : (
                <button
                  onClick={onStartSession}
                  disabled={busy}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-semibold shadow-sm bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 disabled:opacity-50 transition-colors shrink-0"
                >
                  <PlayIcon className="w-3.5 h-3.5" />
                  {busy ? t('Starting…') : t('Start session')}
                </button>
              )}
            </div>
            {workflow.entry_url && (
              <div className="flex items-center gap-2.5 px-4 py-2.5 border-t border-border">
                <GlobeAltIcon className="w-4 h-4 text-tertiary shrink-0" />
                <span className="text-[11px] text-tertiary shrink-0">{t('Entry')}</span>
                <span className="text-xs text-ink font-mono truncate flex-1">{workflow.entry_url}</span>
                <button onClick={() => copyText(workflow.entry_url)} className="p-1 text-tertiary hover:text-ink transition-colors shrink-0">
                  <ClipboardDocumentIcon className="w-3.5 h-3.5" />
                </button>
              </div>
            )}
            {workflow.description && (
              <p className="px-4 py-2.5 border-t border-border text-[12px] text-secondary leading-relaxed">{workflow.description}</p>
            )}
          </div>
        </Section>

        {/* Auth (browserless HTTP lane sign-in + reusable session) */}
        <AuthPanel workflow={workflow} />

        {/* What it needs — input data + login identity (principal). */}
        <InputsIdentitySection workflow={workflow} linkedTriggers={linkedTriggers} isStreaming onRefresh={onRefresh} />

        {/* Capabilities */}
        <Section
          title={t('Capabilities')}
          description={t('What this workflow exposes and how it behaves — click a row to configure it.')}
          anchor="wf-detail-capabilities"
        >
          <div className="bg-surface border border-ink/20 shadow-sm rounded-xl overflow-hidden divide-y divide-border">
            {capabilities.map(cap => (
              <button
                key={cap.label}
                onClick={() => onNavigateTab(cap.tab)}
                className="w-full flex items-center justify-between gap-3 px-4 py-2.5 text-left hover:bg-chrome transition-colors"
              >
                <span className="text-[13px] text-ink">{t(cap.label)}</span>
                <span className="flex items-center gap-2 text-[12px] text-secondary shrink-0">
                  {cap.value}
                  <ArrowRightIcon className="w-3 h-3 text-tertiary" />
                </span>
              </button>
            ))}
          </div>
        </Section>

        {/* Recent sessions */}
        <Section title={t('Recent sessions')} description={t('Past and current sessions for this workflow.')} anchor="wf-detail-sessions">
          {recentSessions.length === 0 ? (
            <p className="text-[13px] text-tertiary py-2">{t('No sessions yet — start one to see it here.')}</p>
          ) : (
            <div className="bg-surface border border-ink/20 shadow-sm rounded-xl overflow-hidden divide-y divide-border">
              {recentSessions.slice(0, 5).map((s: any) => (
                <Link
                  key={s.session_key}
                  to={`/streaming/${s.session_key}`}
                  className="flex items-center gap-3 px-4 py-2.5 hover:bg-chrome transition-colors"
                >
                  <span className={clsx(
                    'w-2 h-2 rounded-full shrink-0',
                    ['starting', 'running'].includes(s.status) ? 'bg-emerald-500 animate-status-pulse' : s.status === 'failed' ? 'bg-red-500' : s.status === 'ended' ? 'bg-active' : 'bg-emerald-500',
                  )} />
                  <span className="text-[13px] text-ink capitalize">{s.status}</span>
                  <span className="text-[11px] text-tertiary">{t('{{n}} events', { n: s.events_emitted ?? 0 })}</span>
                  <span className="flex-1" />
                  {(s.started_at || s.created_at) && (
                    <span className="text-[11px] text-tertiary">{formatRelativeTime(s.started_at || s.created_at)}</span>
                  )}
                </Link>
              ))}
            </div>
          )}
        </Section>

        <WorkflowDetailTour />
      </div>
    );
  }

  // ── regular variant ──
  return (
    <div className="space-y-6">
      {/* About — description + entry + cloud tier. */}
      <Section
        title={t('About')}
        description={t('What this workflow does and where it starts.')}
        right={<WorkflowDetailTourButton />}
        anchor="wf-detail-about"
      >
        <div className="bg-surface border border-ink/20 shadow-sm rounded-xl overflow-hidden">
          <p className={clsx('px-4 py-3 text-[12px] leading-relaxed', workflow.description ? 'text-secondary' : 'text-tertiary italic')}>
            {workflow.description || t('No description yet — add one via Edit so collaborators (and AI agents) know what this does.')}
          </p>
          {wfTier && (
            <div className="flex items-center gap-2.5 px-4 py-2.5 border-t border-border">
              <span className="text-[11px] text-tertiary w-12 shrink-0">{t('On cloud')}</span>
              <TierBadge tier={wfTier} />
              <span className="text-[11px] text-tertiary">
                {wfTier === 'isolated'
                  ? t('Runs in a private, isolated browser because it signs in')
                  : t('Runs on a shared browser pool')}
              </span>
            </div>
          )}
          {workflow.entry_url && (
            <div className="flex items-center gap-2.5 px-4 py-2.5 border-t border-border">
              <span className="text-[11px] text-tertiary w-12 shrink-0">{t('Entry')}</span>
              <span className="text-xs text-ink font-mono truncate flex-1">{workflow.entry_url}</span>
              <button onClick={() => copyText(workflow.entry_url)} className="p-1 text-tertiary hover:text-ink transition-colors shrink-0">
                <ClipboardDocumentIcon className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </div>
      </Section>

      {/* AI Repair nudge */}
      {(workflow.consecutive_failures || 0) > 0 && !workflow.ai_repair_enabled && (
        <div className="flex items-center gap-3 px-4 py-2.5 bg-hover border border-border rounded-xl">
          <WrenchScrewdriverIcon className="w-4 h-4 text-ink flex-shrink-0" />
          <div className="flex-1 min-w-0">
            <p className="text-[13px] text-ink">{t('Failed {{n}}×', { n: workflow.consecutive_failures })}</p>
            <p className="text-[11px] text-tertiary">{t('Auto-repair broken steps')}</p>
          </div>
          <button onClick={async () => {
            try { await automationApi.updateWorkflow(workflow.id, { ai_repair_enabled: true }); toast.success(t('Enabled')); onRefresh(); } catch { toast.error(t('Failed')); }
          }} className="px-3 py-1.5 text-xs font-medium bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 transition-all">
            {t('Enable')}
          </button>
        </div>
      )}

      {/* What it NEEDS — input data (lockable secrets) + login identity. Principal. */}
      <InputsIdentitySection workflow={workflow} linkedTriggers={linkedTriggers} isStreaming={false} onRefresh={onRefresh} />

      {/* Output widget. */}
      <div className="grid grid-cols-1 gap-6">
        <div data-tour="wf-detail-data">
          <DataWidget workflow={workflow} onOpenData={() => onNavigateTab('data')} />
        </div>
      </div>

      {/* How it's doing — full run history (errors, per-run repair, repair log). */}
      <Section title={t('Run history')} description={t('Every execution — click a failed run for its full error.')} anchor="wf-detail-runs">
        <RunsTab workflow={workflow} tasks={tasks} bare />
      </Section>

      {/* How it runs — execution tuning. Collapsed unless deep-linked. */}
      <Collapsible
        title={t('Advanced — how it runs')}
        description={t('Browser behaviour per run, where it executes, and how it recovers when a step breaks.')}
        defaultOpen={defaultAdvancedOpen}
        anchor="wf-detail-advanced"
      >
        <RunTuningSection workflow={workflow} linkedTriggers={linkedTriggers} isStreaming={false} onRefresh={onRefresh} />
      </Collapsible>

      <WorkflowDetailTour />
    </div>
  );
};

export default DetailTab;
