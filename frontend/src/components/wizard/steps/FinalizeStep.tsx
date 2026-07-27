import React, { useCallback, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ArrowRightIcon,
  KeyIcon,
  BeakerIcon,
  ChevronRightIcon,
  CheckCircleIcon,
  XCircleIcon,
  ArrowPathIcon,
  ClipboardDocumentIcon,
  CheckIcon,
  EyeIcon,
  CursorArrowRaysIcon,
  SparklesIcon,
  DocumentTextIcon,
  CommandLineIcon,
  SignalIcon,
  GlobeAltIcon,
  UserCircleIcon,
  CpuChipIcon,
  ClockIcon,
  Squares2X2Icon,
  ListBulletIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import { useWizard } from '../WizardContext';
import { FleetCapacityHint } from '../../checks/FleetCapacityHint';
import { ExecutionTargetPicker } from '../../workflows/ExecutionTargetPicker';
import { PersonaPicker } from '../../workflows/PersonaPicker';
import { TierBadge, deriveProspectiveTier } from '../../TierBadge';
import { workflowHasLogin } from '../../../utils/persona';
import { automationApi, selectorsApi, userRecorderApi } from '../../../api/endpoints';
import { mcpPublishApi } from '../../../api/publish';
import client from '../../../api/client';
import { useQuery } from '../../../hooks/useQuery';
import { Q } from '../../../stores/queryKeys';
import { useCredits, AI_COSTS } from '../../CreditGuard';
import { StageBackdrop } from '../shared/StageBackdrop';
import { SendToAgentModal } from '../../fleet/SendToAgentModal';
import { StepsEditor } from '../../steps/StepsEditor';
import { Select, Switch } from '../../ui';
import { SchedulePicker } from '../../schedule/SchedulePicker';
import { ScheduleValue, defaultSchedule, scheduleToPayload } from '../../../utils/schedule';

// Monitor check cadence presets (mirrors the vocabulary shown while picking targets).
const CHECK_INTERVAL_PRESETS = [
  { label: '10s', ms: 10000 },
  { label: '30s', ms: 30000 },
  { label: '1m', ms: 60000 },
  { label: '5m', ms: 300000 },
  { label: '15m', ms: 900000 },
  { label: '1h', ms: 3600000 },
  { label: '24h', ms: 86400000 },
];

// ── Collapsible Section ────────────────────────────────────────────────────

const Section: React.FC<{
  title: string;
  icon: React.FC<any>;
  badge?: string;
  defaultOpen?: boolean;
  tour?: string;
  children: React.ReactNode;
}> = ({ title, icon: Icon, badge, defaultOpen = false, tour, children }) => {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div data-tour={tour} className="border border-ink/20 rounded-lg bg-surface shadow-sm overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-chrome transition-colors"
      >
        <Icon className="w-4 h-4 text-secondary flex-shrink-0" />
        <span className="text-sm font-medium text-ink flex-1">{title}</span>
        {badge && (
          <span className="px-2 py-0.5 text-[10px] font-medium border border-border text-secondary rounded-full">{badge}</span>
        )}
        <ChevronRightIcon className={clsx('w-3.5 h-3.5 text-tertiary transition-transform', open && 'rotate-90')} />
      </button>
      {open && <div className="border-t border-border">{children}</div>}
    </div>
  );
};

// ── Mode label helper ──────────────────────────────────────────────────────

const MODE_META: Record<string, { icon: React.FC<any>; label: string }> = {
  content_monitor: { icon: EyeIcon, label: 'Content Monitor' },
  manual_workflow: { icon: CursorArrowRaysIcon, label: 'Workflow' },
  ai_workflow: { icon: SparklesIcon, label: 'AI Session' },
  extract_scrape: { icon: DocumentTextIcon, label: 'Extraction' },
  api_workflow: { icon: CommandLineIcon, label: 'API Workflow' },
  streaming_workflow: { icon: SignalIcon, label: 'Streaming Session' },
};

const formatDuration = (ms: number): string => {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60000)}m ${((ms % 60000) / 1000).toFixed(0)}s`;
};

// ── Main Component ─────────────────────────────────────────────────────────

export const FinalizeStep: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { state, updateTestResults, updateConfig, dispatch } = useWizard();
  const creditCtx = useCredits();
  // BYO AI: if the run targets a local agent with its own AI keys, AI is free —
  // no pre-flight charge (the backend routes the AI to the agent's keys).
  const { data: agentData } = useQuery(Q.userAgents(), () => userRecorderApi.getAgents());
  const selectedAgent = (agentData?.agents || []).find(
    (a: any) => a.agent_id === state.config.executionTarget && a.status === 'online',
  );
  const byoAi = !!selectedAgent?.ai_provider;
  const meta = state.mode ? MODE_META[state.mode] : null;
  const MetaIcon = meta?.icon || GlobeAltIcon;

  // ── Outcome flags ──────────────────────────────────────────────────────────
  const isMonitor = state.mode === 'content_monitor';
  const isAiSession = state.mode === 'ai_workflow';
  const isStreaming = state.mode === 'streaming_workflow';
  // Everything that isn't a monitor or an autonomous AI session is a callable
  // workflow (recorded one-shot or streaming live session) — a workflow IS the API.
  const isCallableWorkflow = !isMonitor && !isAiSession;

  // ── Ids (populated on advance — the entity already exists here) ─────────────
  const workflowId = state.createdIds.workflowId;
  const targetId = state.createdIds.targetId;
  const aiSessionId = state.createdIds.aiSessionId;
  const baseUrl = typeof window !== 'undefined' ? window.location.origin : '';
  const scopedUrl = workflowId ? `${baseUrl}/api/v1/workflows/${workflowId}/runs` : null;

  // ── Execution tier (auto-derived; shown, not chosen) ───────────────────────
  const sensitive =
    workflowHasLogin(state.config.recordedSteps, state.config.formData) ||
    state.config.defaultPersonaId != null;
  const execTarget = state.config.executionTarget;
  const ownVenue = execTarget === 'local' || (!!selectedAgent && execTarget !== 'cloud');
  const tier = ownVenue
    ? null
    : deriveProspectiveTier({ sensitive, venueHint: execTarget === 'cloud' ? 'cloud' : 'auto' });

  const [copiedUrl, setCopiedUrl] = useState<string | null>(null);
  const [sendAgentOpen, setSendAgentOpen] = useState(false);

  const copyUrl = (text: string) => {
    navigator.clipboard.writeText(text);
    setCopiedUrl(text);
    toast.success(t('Copied'));
    setTimeout(() => setCopiedUrl(null), 2000);
  };

  // Automations are built in the flow builder, not inline here. This routes to it
  // with the first (trigger) block pre-wired to this entity. The workflow/monitor
  // is already saved, so leaving the wizard loses nothing.
  const goAutomation = useCallback(() => {
    if (isMonitor && targetId) {
      navigate(`/automations/new?source=check&checkId=${targetId}`);
    } else if (workflowId) {
      navigate(`/automations/new?source=workflow&workflowId=${workflowId}&event=completed`);
    }
  }, [isMonitor, targetId, workflowId, navigate]);

  const automationCard = (workflowId || targetId) ? (
    <button
      onClick={goAutomation}
      className="w-full flex items-center gap-3 p-4 rounded-lg border border-ink/20 bg-surface shadow-sm hover:border-ink/30 text-left transition-colors"
    >
      <Squares2X2Icon className="w-4 h-4 text-secondary flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <span className="text-sm text-ink">
          {isMonitor ? t('Run an automation when it changes') : t('Run an automation when it completes')}
        </span>
        <p className="text-[11px] text-tertiary">
          {t('Opens the automation builder, pre-wired to this {{kind}}.', { kind: isMonitor ? t('monitor') : t('workflow') })}
        </p>
      </div>
      <ArrowRightIcon className="w-4 h-4 text-tertiary flex-shrink-0" />
    </button>
  ) : null;

  // ── Immediate exposure actions (bind to the real, already-created id) ───────

  const [keyBusy, setKeyBusy] = useState(false);
  const createKey = useCallback(async () => {
    if (!workflowId || keyBusy) return;
    setKeyBusy(true);
    try {
      const resp = await client.post('/auth/api-keys', {
        label: state.config.name ? `${state.config.name} key` : 'api-key',
        role: 'client',
        workflow_id: workflowId,
      });
      dispatch({ type: 'UPDATE_CREATED_IDS', updates: { apiKeyId: resp.data.id, apiKeyValue: resp.data.key } });
      toast.success(t('API key created'));
    } catch {
      toast.error(t('Failed to create API key'));
    } finally {
      setKeyBusy(false);
    }
  }, [workflowId, keyBusy, state.config.name, dispatch, t]);

  const [mcpBusy, setMcpBusy] = useState(false);
  const [mcpExposed, setMcpExposed] = useState(false);
  const exposeMcp = useCallback(async () => {
    if (!workflowId || mcpBusy || mcpExposed) return;
    setMcpBusy(true);
    try {
      const wf = await automationApi.getWorkflow(workflowId);
      await mcpPublishApi.expose(wf);
      setMcpExposed(true);
      toast.success(t('Exposed as an MCP tool'));
    } catch {
      toast.error(t('Failed to expose as an MCP tool'));
    } finally {
      setMcpBusy(false);
    }
  }, [workflowId, mcpBusy, mcpExposed, t]);

  // ── Schedule (applied immediately to the created entity) ───────────────────
  const [scheduleOn, setScheduleOn] = useState(false);
  const applySchedule = useCallback(async (next: ScheduleValue) => {
    dispatch({
      type: 'UPDATE_EXPOSE',
      updates: {
        workflowSchedule: next,
        ...(next.kind === 'interval' ? { scheduleIntervalMs: next.intervalMs } : {}),
      },
    });
    try {
      const payload = {
        schedule_enabled: true,
        schedule_interval_ms: next.intervalMs,
        ...scheduleToPayload(next),
      };
      if (workflowId) await automationApi.updateWorkflow(workflowId, payload as any);
      else if (aiSessionId) await automationApi.updateAISession(aiSessionId, payload as any);
    } catch {
      toast.error(t('Failed to set the schedule'));
    }
  }, [dispatch, workflowId, aiSessionId, t]);

  // ── Test runner (works now — the entity already exists) ────────────────────
  const runTest = useCallback(async () => {
    // Pre-flight credit confirmation for AI sessions (only these spend credits).
    // BYO agents run AI on their own keys → free, so no confirmation/charge.
    if (isAiSession && creditCtx && !byoAi) {
      const costs = creditCtx.credits.aiCosts ?? AI_COSTS;
      const cost = (costs as any)[state.config.aiMode] ?? costs.standard ?? AI_COSTS.standard;
      const ok = await creditCtx.confirmSpend(cost, t('Run AI session'));
      if (!ok) return;
    }
    updateTestResults({ status: 'running', error: null, durationMs: null, extractedData: null, selectorResults: [], screenshots: [] });
    const start = Date.now();
    try {
      if (isMonitor) {
        if (!targetId) { updateTestResults({ status: 'failed', error: t('Something went wrong — try again.') }); return; }
        const results: any[] = [];
        for (const sel of state.config.selectors) {
          try {
            const resp = await selectorsApi.test(targetId, parseInt(sel.id) || 0);
            results.push({ selectorId: sel.id, name: sel.name, matched: !!resp?.content, content: resp?.content || t('No match') });
          } catch { results.push({ selectorId: sel.id, name: sel.name, matched: false, content: t('Failed') }); }
        }
        updateTestResults({ status: results.every(r => r.matched) ? 'success' : 'failed', durationMs: Date.now() - start, selectorResults: results });
      } else {
        if (!workflowId) { updateTestResults({ status: 'failed', error: t('Something went wrong — try again.') }); return; }
        const result = await automationApi.dispatchAndWait(workflowId, undefined, 120);
        updateTestResults({
          status: result.status === 'success' ? 'success' : 'failed',
          durationMs: result.duration_ms || (Date.now() - start),
          extractedData: result.result_data || null,
          error: result.error_message || null,
          screenshots: result.screenshots || [],
        });
      }
    } catch (err: any) {
      updateTestResults({ status: 'failed', durationMs: Date.now() - start, error: err.message || t('Test failed') });
    }
  }, [state, updateTestResults, creditCtx, t, isMonitor, isAiSession, byoAi, targetId, workflowId]);

  const configSummary = () => {
    if (isMonitor) return t('{{n}} selectors', { n: state.config.selectors.length });
    if (state.mode === 'manual_workflow') return t('{{n}} steps', { n: state.config.recordedSteps.length });
    if (isAiSession) return state.config.goal?.substring(0, 60) || t('AI session');
    if (isStreaming) {
      const parts = [];
      if (state.config.setupStepsCount) parts.push(t('{{n}} setup steps', { n: state.config.setupStepsCount }));
      if (state.config.advancedScriptEnabled) parts.push(t('script'));
      if (state.config.streamingHandlers?.length) parts.push(t('{{n}} handlers', { n: state.config.streamingHandlers.length }));
      return parts.join(' · ') || t('streaming');
    }
    return '';
  };

  return (
    // Finalize = a slide-up glass SHEET floating over the dimmed-but-visible stage.
    // Top-aligned so tall content scrolls INSIDE the viewport (bottom-anchoring
    // clipped the top). The entity already exists (create-on-advance), so every
    // action here binds to a real id — no generic connector matrix, just the few
    // things that make sense for this outcome.
    <StageBackdrop dimmed className="flex justify-center items-start">
      <div className="w-full max-w-2xl px-4 sm:px-6 pt-6 pb-8 animate-wizard-slide-up">
        <div className="rounded-2xl border border-ink/20 bg-surface/95 backdrop-blur-md shadow-xl px-5 sm:px-6 py-6 space-y-4">
          <div className="mx-auto mb-1 h-1 w-10 rounded-full bg-border" />

          {/* Summary card */}
          <div data-tour="finalize-summary" className="flex items-center gap-3 p-4 rounded-lg border border-ink/20 bg-surface shadow-sm">
            <div className="p-2 rounded-lg bg-hover">
              <MetaIcon className="w-5 h-5 text-secondary" />
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-ink flex items-center gap-1.5">
                {state.config.name || t('Untitled')}
                <CheckCircleIcon className="w-3.5 h-3.5 text-success-fg" />
              </p>
              <div className="flex items-center gap-2 mt-0.5 text-xs text-tertiary">
                <span>{meta?.label && t(meta.label)} · {t('Saved')}</span>
                {state.config.url && (
                  <>
                    <span className="text-border">|</span>
                    <span className="truncate">{state.config.url}</span>
                  </>
                )}
              </div>
            </div>
            <span className="text-xs text-secondary border border-border px-2 py-0.5 rounded-full">{configSummary()}</span>
          </div>

          {/* ═══ MONITOR ═══════════════════════════════════════════════════════ */}

          {isMonitor && (
            <Section title={t('Check settings')} icon={ClockIcon} defaultOpen>
              <div className="p-4 space-y-4">
                <div>
                  <p className="text-xs text-secondary mb-1.5">{t('How often to check')}</p>
                  <SchedulePicker
                    value={{
                      kind: state.config.scheduleKind,
                      intervalMs: state.config.checkPeriodMs ?? 60000,
                      time: state.config.scheduleTime,
                      days: state.config.scheduleDays,
                      tz: state.config.scheduleTz,
                    }}
                    onChange={(next: ScheduleValue) =>
                      dispatch({
                        type: 'UPDATE_CONFIG',
                        updates: {
                          scheduleKind: next.kind,
                          checkPeriodMs: next.intervalMs,
                          scheduleTime: next.time,
                          scheduleDays: next.days,
                          scheduleTz: next.tz,
                        },
                      })
                    }
                    intervalPresets={CHECK_INTERVAL_PRESETS}
                    intervalExtra={<FleetCapacityHint checkPeriodMs={state.config.checkPeriodMs} />}
                  />
                </div>

                <div className="flex items-end justify-between gap-3 flex-wrap">
                  <div className="flex-1 min-w-[8rem]">
                    <p className="text-xs text-secondary mb-1.5">{t('Region')}</p>
                    <Select
                      value={state.config.preferredRegion || ''}
                      onChange={(v) => dispatch({ type: 'UPDATE_CONFIG', updates: { preferredRegion: v || null } })}
                      options={[
                        { value: '', label: t('Any region') },
                        { value: 'us-east', label: t('US East') },
                        { value: 'us-west', label: t('US West') },
                        { value: 'eu-west', label: t('EU West') },
                        { value: 'eu-central', label: t('EU Central') },
                        { value: 'ap-southeast', label: t('AP SE') },
                      ]}
                    />
                  </div>
                  <label className="flex items-center gap-2 cursor-pointer select-none pb-2">
                    <Switch
                      size="sm"
                      checked={state.config.requiresPlaywright}
                      onChange={() => dispatch({ type: 'UPDATE_CONFIG', updates: { requiresPlaywright: !state.config.requiresPlaywright } })}
                    />
                    <span className="text-xs text-secondary">{t('Render JS')}</span>
                  </label>
                </div>
                <p className="text-[11px] text-tertiary">
                  {t('Render JS runs the page in a real browser before checking — needed for JS-rendered content and visual zones.')}
                </p>
              </div>
            </Section>
          )}

          {/* Monitor → run an automation WHEN IT CHANGES (redirect to the builder). */}
          {isMonitor && automationCard}

          {/* ═══ CALLABLE WORKFLOW ═════════════════════════════════════════════ */}

          {isCallableWorkflow && (
            <>
              <Section title={t("It's callable")} icon={CommandLineIcon} defaultOpen tour="finalize-callable">
                <div className="p-4 space-y-3">
                  {scopedUrl && (
                    <div>
                      <p className="text-xs text-secondary mb-1.5">{t('Endpoint')}</p>
                      <div className="flex items-center gap-2">
                        <code className="flex-1 text-[11px] font-mono text-secondary bg-canvas border border-border rounded px-3 py-1.5 truncate">
                          POST {scopedUrl}
                        </code>
                        <button onClick={() => copyUrl(scopedUrl)} className="p-1.5 text-tertiary hover:text-ink rounded-lg hover:bg-hover">
                          {copiedUrl === scopedUrl ? <CheckIcon className="w-3.5 h-3.5 text-ink" /> : <ClipboardDocumentIcon className="w-3.5 h-3.5" />}
                        </button>
                      </div>
                    </div>
                  )}

                  {state.createdIds.apiKeyValue ? (
                    <div className="p-3 bg-success-bg border border-success rounded-lg">
                      <p className="text-xs font-medium text-success-fg mb-1">{t('Key created — save it now:')}</p>
                      <code className="text-[11px] font-mono text-success-fg break-all">{state.createdIds.apiKeyValue}</code>
                    </div>
                  ) : (
                    <button
                      onClick={createKey}
                      disabled={keyBusy}
                      className="w-full flex items-center gap-3 p-3 rounded-lg border border-border hover:border-ink/20 text-left transition-colors disabled:opacity-60"
                    >
                      <KeyIcon className="w-4 h-4 text-tertiary" />
                      <div className="min-w-0">
                        <span className="text-sm text-ink">{keyBusy ? t('Creating…') : t('Create an API key')}</span>
                        <p className="text-[11px] text-tertiary">{t('A scoped client key linked to this workflow.')}</p>
                      </div>
                    </button>
                  )}

                  <button
                    onClick={exposeMcp}
                    disabled={mcpBusy || mcpExposed}
                    className={clsx(
                      'w-full flex items-center gap-3 p-3 rounded-lg border text-left transition-colors',
                      mcpExposed ? 'border-ink bg-hover' : 'border-border hover:border-ink/20',
                    )}
                  >
                    <CpuChipIcon className={clsx('w-4 h-4', mcpExposed ? 'text-ink' : 'text-tertiary')} />
                    <div className="min-w-0 flex-1">
                      <span className="text-sm text-ink">{mcpExposed ? t('Exposed as an MCP tool') : mcpBusy ? t('Exposing…') : t('Expose as an MCP tool')}</span>
                      <p className="text-[11px] text-tertiary">{t('Make it callable by AI agents. Manage it in Developers → Endpoints.')}</p>
                    </div>
                    {mcpExposed && <CheckIcon className="w-3.5 h-3.5 text-ink flex-shrink-0" />}
                  </button>
                </div>
              </Section>

              {/* Automation redirect card. */}
              {automationCard}

              {/* Run on a schedule — applied immediately. */}
              <Section title={t('Run on a schedule')} icon={ClockIcon} tour="finalize-schedule">
                <div className="p-4 space-y-3">
                  <label className="flex items-center justify-between gap-3">
                    <span className="text-xs text-secondary">{t('Run this automatically on an interval.')}</span>
                    <Switch size="sm" checked={scheduleOn} onChange={() => setScheduleOn((v) => !v)} />
                  </label>
                  {scheduleOn && (
                    <SchedulePicker
                      value={state.expose.workflowSchedule ?? defaultSchedule(state.expose.scheduleIntervalMs)}
                      onChange={applySchedule}
                    />
                  )}
                </div>
              </Section>

              {/* Steps — secondary. Collapsed; the recorder is the primary editor. */}
              {(state.mode === 'manual_workflow' || isStreaming) && state.config.recordedSteps.length > 0 && (
                <Section title={t('Steps')} icon={ListBulletIcon} badge={t('{{n}}', { n: state.config.recordedSteps.length })}>
                  <div className="p-2">
                    <p className="text-[11px] text-tertiary px-2 pt-1 pb-2">
                      {t('Quick edits — reorder, rename, or remove. For anything visual, edit in the recorder.')}
                    </p>
                    <StepsEditor
                      steps={state.config.recordedSteps}
                      onSave={async (steps) => {
                        updateConfig({ recordedSteps: steps });
                        if (workflowId) await automationApi.updateWorkflow(workflowId, { steps });
                        toast.success(t('Steps updated'));
                      }}
                    />
                  </div>
                </Section>
              )}
            </>
          )}

          {/* ═══ AI SESSION ════════════════════════════════════════════════════ */}

          {isAiSession && (
            <Section title={t('Run on a schedule')} icon={ClockIcon} defaultOpen>
              <div className="p-4 space-y-3">
                <p className="text-xs text-secondary">{t('Hand the AI its goal now, or run it automatically on an interval.')}</p>
                <label className="flex items-center justify-between gap-3">
                  <span className="text-xs text-secondary">{t('Run on a schedule')}</span>
                  <Switch size="sm" checked={scheduleOn} onChange={() => setScheduleOn((v) => !v)} />
                </label>
                {scheduleOn && (
                  <SchedulePicker
                    value={state.expose.workflowSchedule ?? defaultSchedule(state.expose.scheduleIntervalMs)}
                    onChange={applySchedule}
                  />
                )}
              </div>
            </Section>
          )}

          {/* ═══ SHARED — persona + advanced + test ════════════════════════════ */}

          {(isAiSession
            || (isCallableWorkflow && workflowHasLogin(state.config.recordedSteps, state.config.formData))) && (
            <Section title={t('Persona')} icon={UserCircleIcon} badge={t('Optional')} defaultOpen={isAiSession}>
              <div className="p-4 space-y-2">
                <PersonaPicker
                  value={state.config.defaultPersonaId}
                  domain={(() => { try { return state.config.url ? new URL(state.config.url).hostname : undefined; } catch { return undefined; } })()}
                  onChange={(id) => dispatch({ type: 'UPDATE_CONFIG', updates: { defaultPersonaId: id } })}
                  allowClear
                />
                {isAiSession && (
                  <p className="text-[11px] text-tertiary">
                    {t('The AI signs in as this identity (credentials + 2FA handled automatically). Leave empty to provide secrets manually.')}
                  </p>
                )}
              </div>
            </Section>
          )}

          {isCallableWorkflow && (
            <Section title={t('Advanced settings')} icon={GlobeAltIcon}>
              <div className="p-4 space-y-3">
                <div>
                  <p className="text-xs text-secondary mb-1.5">{t('Run on')}</p>
                  <ExecutionTargetPicker
                    value={state.config.executionTarget as 'auto' | 'local' | 'cloud'}
                    onChange={(value) => dispatch({ type: 'UPDATE_CONFIG', updates: { executionTarget: value } })}
                  />
                  {tier && (
                    <div className="mt-2 flex items-start gap-2.5 rounded-lg border border-border bg-hover/40 px-3 py-2.5">
                      <TierBadge tier={tier} />
                      <p className="text-[11px] text-secondary leading-relaxed">
                        {tier === 'isolated'
                          ? t('This workflow signs in / uses credentials, so on Writ Cloud it runs ISOLATED — a fresh sandboxed browser process, destroyed after each run. Stronger isolation; small surcharge. (Runs on your own agent are unaffected.)')
                          : t('No credentials are injected, so on Writ Cloud this runs on the shared pool (warm browser, fresh context per run).')}
                      </p>
                    </div>
                  )}
                </div>

                {isStreaming && (
                  <label className="flex items-center justify-between gap-3 px-1">
                    <div>
                      <span className="text-sm text-ink">{t('Multiple conversations')}</span>
                      <p className="text-[11px] text-tertiary">{t('Run separate conversations in their own tabs. When off, every message reuses the same tab.')}</p>
                    </div>
                    <Switch
                      size="sm"
                      checked={state.config.multiConversation}
                      onChange={() => dispatch({ type: 'UPDATE_CONFIG', updates: { multiConversation: !state.config.multiConversation } })}
                    />
                  </label>
                )}

                {workflowId != null && (
                  <button
                    type="button"
                    onClick={() => setSendAgentOpen(true)}
                    className="flex w-full items-start gap-3 rounded-lg border border-border p-3 text-left transition-colors hover:border-ink/20"
                  >
                    <CpuChipIcon className="w-4 h-4 mt-0.5 flex-shrink-0 text-tertiary" />
                    <div className="min-w-0">
                      <span className="text-sm text-ink">{t('Save to a fleet agent')}</span>
                      <p className="text-[11px] text-tertiary leading-snug">{t('Copy it to one of your connected agents to run locally.')}</p>
                    </div>
                  </button>
                )}
              </div>
            </Section>
          )}

          {/* Test — verify it works, right here. */}
          <Section title={t('Test')} icon={BeakerIcon} badge={state.testResults.status !== 'idle' ? state.testResults.status : undefined} tour="finalize-test">
            <div className="p-4 space-y-3">
              <p className="text-xs text-secondary">
                {isMonitor ? t('Run the check once to confirm the selectors match.') : t('Run it once to confirm everything works.')}
              </p>
              <div className="flex items-center gap-3">
                <button
                  onClick={runTest}
                  disabled={state.testResults.status === 'running'}
                  className={clsx(
                    'flex items-center gap-2 px-4 py-2 text-sm font-semibold rounded-lg transition-all',
                    state.testResults.status === 'running'
                      ? 'bg-hover text-secondary cursor-not-allowed'
                      : 'bg-accent-strong text-accent-on shadow-sm hover:opacity-80',
                  )}
                >
                  {state.testResults.status === 'running' ? <ArrowPathIcon className="w-4 h-4 animate-spin" /> : <BeakerIcon className="w-4 h-4" />}
                  {state.testResults.status === 'running' ? t('Running...') : t('Run Test')}
                </button>

                {state.testResults.status === 'success' && (
                  <span className="flex items-center gap-1 text-sm text-success-fg">
                    <CheckCircleIcon className="w-4 h-4" /> {t('Passed')}
                    {state.testResults.durationMs && <span className="text-tertiary ml-1">({formatDuration(state.testResults.durationMs)})</span>}
                  </span>
                )}
                {state.testResults.status === 'failed' && (
                  <span className="flex items-center gap-1 text-sm text-danger">
                    <XCircleIcon className="w-4 h-4" /> {t('Failed')}
                  </span>
                )}
              </div>

              {state.testResults.error && (
                <p className="text-xs text-danger bg-danger-bg border border-danger rounded-lg p-2">{state.testResults.error}</p>
              )}

              {state.testResults.extractedData && Object.keys(state.testResults.extractedData).length > 0 && (
                <div>
                  <p className="text-xs text-secondary mb-1">{t('Extracted data:')}</p>
                  <pre className="text-[11px] font-mono text-secondary bg-canvas border border-border rounded-lg p-2 overflow-auto max-h-32">
                    {JSON.stringify(state.testResults.extractedData, null, 2)}
                  </pre>
                </div>
              )}

              {state.testResults.selectorResults.length > 0 && (
                <div className="space-y-1">
                  {state.testResults.selectorResults.map((r: any, i: number) => (
                    <div key={i} className={clsx('flex items-center gap-2 text-xs p-2 rounded-lg border', r.matched ? 'bg-success-bg border-success text-success-fg' : 'bg-danger-bg border-danger text-danger-fg')}>
                      {r.matched ? <CheckCircleIcon className="w-3.5 h-3.5" /> : <XCircleIcon className="w-3.5 h-3.5" />}
                      <span className="font-medium">{r.name || t('Selector {{n}}', { n: i + 1 })}</span>
                      {r.content && <span className="text-tertiary truncate ml-auto">{r.content.substring(0, 60)}</span>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </Section>
        </div>
      </div>

      {workflowId != null && (
        <SendToAgentModal
          open={sendAgentOpen}
          onClose={() => setSendAgentOpen(false)}
          kind="workflow"
          entityId={workflowId}
          entityName={state.config.name || t('Untitled')}
        />
      )}
    </StageBackdrop>
  );
};
