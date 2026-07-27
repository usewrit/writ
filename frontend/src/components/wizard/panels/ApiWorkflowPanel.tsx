import React, { useCallback, useState, useEffect, useRef } from 'react';
import {
  CommandLineIcon,
  SparklesIcon,
  ArrowPathIcon,
  CheckCircleIcon,
  SignalIcon,
  LockClosedIcon,
  ArrowRightIcon,
  DocumentTextIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { useWizard } from '../WizardContext';
import { BrowserRecorder } from '../../BrowserRecorder';
import { automationApi } from '../../../api/endpoints';
import { SecureDataFields, fieldsToFormData } from '../../workflows/SecureDataFields';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import i18n from '../../../i18n';

// ── Discovered flow types ───────────────────────────────────────────────────

interface DiscoveredFlow {
  id: string;
  name: string;
  type: 'auth' | 'navigation' | 'form_submit' | 'data_fetch' | 'file_upload' | 'api_call' | 'unknown';
  steps: Array<{
    action: string;
    description: string;
    status: 'pending' | 'running' | 'done' | 'error';
  }>;
  apiCalls: Array<{
    method: string;
    url: string;
    status?: number;
    hasAuth?: boolean;
  }>;
  status: 'discovering' | 'complete' | 'error';
}

// Monochrome — flow type is conveyed by icon + label; all share the neutral chip.
const FLOW_TYPE_META: Record<string, { icon: React.ElementType; label: string; color: string }> = {
  auth:        { icon: LockClosedIcon,     label: 'Authentication',  color: 'text-secondary bg-hover' },
  navigation:  { icon: ArrowRightIcon,     label: 'Navigation',      color: 'text-secondary bg-hover' },
  form_submit: { icon: DocumentTextIcon,   label: 'Form Submission', color: 'text-secondary bg-hover' },
  data_fetch:  { icon: SignalIcon,         label: 'Data Fetch',      color: 'text-secondary bg-hover' },
  file_upload: { icon: DocumentTextIcon,   label: 'File Upload',     color: 'text-secondary bg-hover' },
  api_call:    { icon: CommandLineIcon,    label: 'API Call',        color: 'text-secondary bg-hover' },
  unknown:     { icon: SignalIcon,         label: 'Flow',            color: 'text-secondary bg-hover' },
};

type SubMode = 'manual' | 'ai';

// ── Main Panel ──────────────────────────────────────────────────────────────

export const ApiWorkflowPanel: React.FC = () => {
  const { state, updateConfig } = useWizard();
  const { t } = useTranslation();
  // Manual API recording only — there is no autonomous AI-discovery flow in this
  // build. The panel is locked to 'manual' and the sub-mode toggle is hidden.
  const [subMode] = useState<SubMode>('manual');
  const [_aiSessionId, setAiSessionId] = useState<number | null>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [discoveredFlows, setDiscoveredFlows] = useState<DiscoveredFlow[]>([]);
  const [expandedFlow, setExpandedFlow] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState('');
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // AI config
  const [goal, setGoal] = useState(state.config.goal || t('Discover all API endpoints by navigating the site, logging in, and interacting with forms'));
  const [userContext, setUserContext] = useState(state.config.userContext || '');
  const [availableData, setAvailableData] = useState<Array<{key: string; value: string}>>(
    state.config.availableData.length > 0 ? state.config.availableData : []
  );

  // ── Manual mode callbacks ─────────────────────────────────────────────

  const handleSave = useCallback((steps: any[], name: string, credentials?: Record<string, string>, formData?: Record<string, string>) => {
    updateConfig({
      recordedSteps: steps,
      name: name || state.config.name,
      credentials: credentials || {},
      formData: formData || {},
      workflowType: 'api_recorded',
    });
  }, [updateConfig, state.config.name]);

  const handleSaveApi = useCallback((functions: Record<string, any>, name: string) => {
    updateConfig({
      apiFunctions: functions,
      name: name || state.config.name,
      workflowType: 'api_recorded',
    });
  }, [updateConfig, state.config.name]);

  // ── Poll session for discovered flows ─────────────────────────────────
  // Declared BEFORE startAIDiscovery, which calls it: reaching backwards past a
  // `const` is a temporal-dead-zone access, and the caller would keep whichever
  // binding existed when it was created rather than the current one
  // (`react-hooks/immutability`).

  const startPolling = useCallback((sessionId: number) => {
    if (pollRef.current) clearInterval(pollRef.current);

    pollRef.current = setInterval(async () => {
      try {
        const session = await automationApi.getAISession(sessionId);
        const status = session.status;
        const actionsTaken = session.actions_taken || session.steps_taken || 0;
        const progressMsg = session.progress_message || '';
        const currentUrl = session.current_url || '';

        // Build flows from ai_conversation (action history) and generated workflows
        const conversation = session.ai_conversation || [];
        const flows = groupConversationIntoFlows(conversation, session);
        setDiscoveredFlows(flows);

        if (status === 'running' || status === 'pending') {
          const parts = [t('AI exploring... {{count}} actions', { count: actionsTaken })];
          if (flows.length > 0) parts.push(t('{{count}} flows', { count: flows.length }));
          if (progressMsg) parts.push(progressMsg);
          if (currentUrl) parts.push(currentUrl.substring(0, 60));
          setStatusMessage(parts.join(' · '));
        } else if (status === 'completed') {
          setIsRunning(false);
          setStatusMessage(t('Discovery complete — {{flows}} flows, {{actions}} actions', { flows: flows.length, actions: actionsTaken }));
          if (pollRef.current) clearInterval(pollRef.current);

          // Fetch generated workflow steps if any
          if (session.generated_workflows.length > 0) {
            try {
              const wf = await automationApi.getWorkflow(session.generated_workflows[0].id);
              if (wf.steps && wf.steps.length > 0) {
                updateConfig({ recordedSteps: wf.steps, workflowType: 'api_recorded' });
              }
            } catch {}
          }
        } else if (status === 'failed' || status === 'cancelled') {
          setIsRunning(false);
          setStatusMessage(t('Discovery ended — {{count}} flows ({{reason}})', { count: flows.length, reason: session.error_message || status }));
          if (pollRef.current) clearInterval(pollRef.current);

          if (session.generated_workflows.length > 0) {
            try {
              const wf = await automationApi.getWorkflow(session.generated_workflows[0].id);
              if (wf.steps && wf.steps.length > 0) {
                updateConfig({ recordedSteps: wf.steps, workflowType: 'api_recorded' });
              }
            } catch {}
          }
        }
      } catch {
        // Silently retry
      }
    }, 2500);
  }, [updateConfig, t]);

  // ── AI mode: start discovery ──────────────────────────────────────────

  const startAIDiscovery = useCallback(async () => {
    if (!state.config.url) {
      toast.error(t('Enter a URL first'));
      return;
    }

    setIsRunning(true);
    setStatusMessage(t('Starting AI API discovery...'));
    setDiscoveredFlows([]);

    try {
      const formData = fieldsToFormData(availableData);

      // The coordinator's AI-session start accepts a slim payload; the richer cloud
      // fields (mode / user_context / form_data / auto_validate) have no coordinator
      // equivalent and are ignored. Cast so they don't fail the stricter
      // AISessionStartInput type on this legacy API-discovery path.
      const session = await automationApi.createAISession({
        name: state.config.name || t('API Discovery'),
        entry_url: state.config.url,
        goal,
        mode: 'api_discovery',
        user_context: userContext || undefined,
        form_data: Object.keys(formData).length > 0 ? formData : undefined,
        max_steps: 50,
        auto_validate: true,
      } as any);

      setAiSessionId(session.id);
      updateConfig({ aiSessionId: session.id } as any);
      setStatusMessage(t('AI is navigating the site...'));

      // Start polling session status
      startPolling(session.id);

    } catch (err: any) {
      setIsRunning(false);
      setStatusMessage('');
      toast.error(err.response?.data?.detail || t('Failed to start AI discovery'));
    }
  }, [state.config.url, state.config.name, goal, userContext, availableData, updateConfig, t, startPolling]);

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  // ── Render ────────────────────────────────────────────────────────────

  return (
    <div className="h-full flex flex-col">
      {/* Manual API recording only. */}
      {subMode === 'manual' ? (
        /* ── Manual Recording Mode ─────────────────────────────────────── */
        <div className="flex-1 min-h-0">
          <BrowserRecorder
              isOpen={true}
              embedded={true}
              apiMode={true}
              onClose={() => {}}
              onSave={handleSave}
              onSaveApi={handleSaveApi}
              onStepsChange={(steps) => { if (steps.length) updateConfig({ recordedSteps: steps as any[] }); }}
              onCredentialsChange={(credentials) => updateConfig({ credentials })}
              onFormDataChange={(formData) => updateConfig({ formData })}
              onPersonaCreated={(personaId) => updateConfig({ defaultPersonaId: personaId })}
            />
        </div>
      ) : (
        /* ── AI Discovery Mode ─────────────────────────────────────────── */
        <div className="flex-1 min-h-0 flex flex-col">
          {!isRunning && discoveredFlows.length === 0 ? (
            /* Configuration form */
            <div className="flex-1 overflow-y-auto">
              <div className="max-w-2xl mx-auto px-6 py-6 space-y-4">
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-lg bg-hover">
                    <CommandLineIcon className="w-5 h-5 text-secondary" />
                  </div>
                  <div>
                    <h2 className="text-base font-semibold text-ink tracking-tight">{t('AI API Discovery')}</h2>
                    <p className="text-sm text-secondary">{t('AI will navigate the site, discover flows, and capture API endpoints.')}</p>
                  </div>
                </div>

                <div className="bg-hover rounded-lg px-4 py-2.5 text-sm">
                  <span className="text-secondary">{t('Target:')}</span>{' '}
                  <span className="text-ink font-medium">{state.config.url || t('Not set')}</span>
                </div>

                <div>
                  <label className="block text-sm text-secondary mb-1.5">{t('Discovery Goal')}</label>
                  <textarea
                    value={goal}
                    onChange={e => setGoal(e.target.value)}
                    placeholder={t('Discover all API endpoints by navigating the site, logging in, and interacting with forms')}
                    rows={3}
                    className="w-full px-4 py-2.5 text-sm text-ink bg-surface border border-border rounded-lg placeholder:text-tertiary focus:outline-none focus:ring-2 focus:ring-ink/5 focus:border-ink/30 resize-none"
                  />
                </div>

                <div>
                  <label className="block text-sm text-secondary mb-1.5">{t('Site Context')}</label>
                  <input
                    type="text"
                    value={userContext}
                    onChange={e => setUserContext(e.target.value)}
                    placeholder={t('e.g., SaaS dashboard, requires login with test credentials')}
                    className="w-full px-4 py-2.5 text-sm text-ink bg-surface border border-border rounded-lg placeholder:text-tertiary focus:outline-none focus:ring-2 focus:ring-ink/5 focus:border-ink/30"
                  />
                </div>

                <SecureDataFields
                  fields={availableData}
                  onChange={setAvailableData}
                  label={t('Credentials / Form Data')}
                  description={t('Login credentials and form data the AI can use.')}
                />

                <button
                  onClick={startAIDiscovery}
                  className="w-full px-4 py-3 bg-accent-strong hover:bg-accent-strong/90 text-accent-on rounded-lg font-semibold shadow-sm flex items-center justify-center gap-2 transition-colors"
                >
                  <SparklesIcon className="w-5 h-5" />
                  {t('Start AI Discovery')}
                </button>
              </div>
            </div>
          ) : (
            /* Discovery in progress / results */
            <div className="flex-1 flex flex-col min-h-0">
              {/* Status bar */}
              <div className={clsx(
                'px-4 py-2.5 border-b border-border flex items-center gap-2',
                isRunning ? "bg-hover" : "bg-success/5"
              )}>
                {isRunning ? (
                  <ArrowPathIcon className="w-4 h-4 text-tertiary animate-spin" />
                ) : (
                  <CheckCircleIcon className="w-4 h-4 text-success" />
                )}
                <span className="text-sm text-ink flex-1">{statusMessage}</span>
                {isRunning && (
                  <button
                    onClick={() => {
                      if (pollRef.current) clearInterval(pollRef.current);
                      setIsRunning(false);
                      setStatusMessage(t('Stopped — {{count}} flows discovered', { count: discoveredFlows.length }));
                    }}
                    className="px-2 py-1 text-xs text-secondary hover:text-ink bg-hover hover:bg-surface rounded transition-colors"
                  >
                    {t('Stop')}
                  </button>
                )}
              </div>

              {/* Discovered flows list */}
              <div className="flex-1 overflow-y-auto divide-y divide-border">
                {discoveredFlows.length === 0 && isRunning && (
                  <div className="px-6 py-12 text-center text-secondary text-sm">
                    <ArrowPathIcon className="w-8 h-8 mx-auto mb-3 text-tertiary animate-spin" />
                    {t('AI is navigating the site and discovering flows...')}
                  </div>
                )}
                {discoveredFlows.map(flow => {
                  const meta = FLOW_TYPE_META[flow.type] || FLOW_TYPE_META.unknown;
                  const FlowIcon = meta.icon;
                  const isExpanded = expandedFlow === flow.id;

                  return (
                    <div key={flow.id}>
                      <div
                        className="px-4 py-3 flex items-center gap-3 cursor-pointer hover:bg-chrome transition-colors"
                        onClick={() => setExpandedFlow(isExpanded ? null : flow.id)}
                      >
                        <div className={clsx('p-1.5 rounded-md', meta.color)}>
                          <FlowIcon className="w-4 h-4" />
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="text-sm font-medium text-ink">{flow.name}</span>
                            {flow.status === 'discovering' && (
                              <ArrowPathIcon className="w-3 h-3 text-tertiary animate-spin" />
                            )}
                          </div>
                          <div className="text-xs text-tertiary">
                            {t('{{count}} steps', { count: flow.steps.length })}
                            {flow.apiCalls.length > 0 && (
                              <span> · {t('{{count}} API calls', { count: flow.apiCalls.length })}</span>
                            )}
                          </div>
                        </div>
                        <span className={clsx(
                          'text-[10px] px-2 py-0.5 rounded-full font-medium',
                          flow.status === 'complete' ? 'bg-success/10 text-success' :
                          flow.status === 'error' ? 'bg-danger/10 text-danger' :
                          'bg-hover text-secondary'
                        )}>
                          {flow.status === 'complete' ? t('Complete') : flow.status === 'error' ? t('Error') : t('Discovering')}
                        </span>
                      </div>

                      {isExpanded && (
                        <div className="px-4 pb-3 space-y-1.5">
                          {/* Steps */}
                          {flow.steps.map((step, i) => (
                            <div key={i} className="flex items-center gap-2 text-xs pl-3 border-l-2 border-border">
                              <span className={clsx(
                                'w-1.5 h-1.5 rounded-full shrink-0',
                                step.status === 'done' ? 'bg-success' :
                                step.status === 'running' ? 'bg-tertiary animate-pulse' :
                                step.status === 'error' ? 'bg-danger' : 'bg-border'
                              )} />
                              <span className="text-secondary font-mono">{step.action}</span>
                              <span className="text-tertiary truncate">{step.description}</span>
                            </div>
                          ))}
                          {/* API calls */}
                          {flow.apiCalls.length > 0 && (
                            <div className="mt-2 space-y-1">
                              <span className="text-[10px] text-tertiary font-medium uppercase tracking-wide">{t('API Calls')}</span>
                              {flow.apiCalls.map((call, i) => (
                                <div key={i} className="flex items-center gap-1.5 text-xs pl-3">
                                  <span className={clsx(
                                    'font-mono font-bold px-1 py-0.5 rounded text-[9px]',
                                    call.method === 'DELETE' ? 'bg-danger/10 text-danger' :
                                    'bg-hover text-secondary'
                                  )}>{call.method}</span>
                                  {call.status && (
                                    <span className={clsx(
                                      'text-[10px] font-mono',
                                      call.status < 300 ? 'text-success' : call.status < 400 ? 'text-warning' : 'text-danger'
                                    )}>{call.status}</span>
                                  )}
                                  <span className="text-secondary font-mono truncate">
                                    {(() => { try { return new URL(call.url).pathname; } catch { return call.url; } })()}
                                  </span>
                                  {call.hasAuth && <LockClosedIcon className="w-3 h-3 text-secondary shrink-0" />}
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>

              {/* Bottom: summary + save */}
              {!isRunning && discoveredFlows.length > 0 && (
                <div className="px-4 py-3 border-t border-border bg-surface space-y-2">
                  <div className="flex items-center gap-3 text-xs text-secondary">
                    <span>{t('{{count}} flows', { count: discoveredFlows.length })}</span>
                    <span>·</span>
                    <span>{t('{{count}} API calls', { count: discoveredFlows.reduce((a, f) => a + f.apiCalls.length, 0) })}</span>
                    <span>·</span>
                    <span>{t('{{count}} steps', { count: discoveredFlows.reduce((a, f) => a + f.steps.length, 0) })}</span>
                  </div>
                  <button
                    onClick={() => {
                      toast.success(t('Workflow saved to config — proceed to finalize'));
                    }}
                    disabled={state.config.recordedSteps.length === 0 && Object.keys(state.config.apiFunctions).length === 0}
                    className="w-full px-4 py-2.5 bg-accent-strong hover:bg-accent-strong/90 text-accent-on rounded-lg font-semibold shadow-sm flex items-center justify-center gap-2 transition-colors disabled:opacity-40"
                  >
                    <CheckCircleIcon className="w-5 h-5" />
                    {t('Use Discovered Flows')}
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Bottom status for manual mode */}
      {subMode === 'manual' && Object.keys(state.config.apiFunctions).length > 0 && (
        <div className="px-4 py-2 border-t border-border bg-surface text-xs text-secondary">
          {t('{{count}} API functions recorded', { count: Object.keys(state.config.apiFunctions).length })}
        </div>
      )}
    </div>
  );
};


// ── Flow grouping from ai_conversation ──────────────────────────────────────

function groupConversationIntoFlows(conversation: any[], session: any): DiscoveredFlow[] {
  // ai_conversation entries are action records: { action: { action, field_index, ... }, success, verification }
  // We also look at screenshots for URL changes
  const entries = conversation.filter(e => e.action);
  if (entries.length === 0) return [];

  const flows: DiscoveredFlow[] = [];
  let currentFlow: DiscoveredFlow | null = null;
  let flowCounter = 0;
  let lastUrl = session.entry_url || '';

  for (const entry of entries) {
    const act = entry.action || {};
    const actionType = act.action || '';
    const reason = act.reason || '';
    const verification = entry.verification || {};
    const verMsg = verification.message || '';
    const success = entry.success;

    // Detect URL changes from verification messages
    const urlMatch = verMsg.match(/https?:\/\/[^\s"')]+/);
    const newUrl = urlMatch ? urlMatch[0] : '';

    // Detect flow boundaries: URL change, navigate actions, tab switches
    const isNavBoundary = actionType === 'navigate' || actionType === 'wait_for_tab' || actionType === 'open_tab' || actionType === 'switch_tab' ||
                          (newUrl && newUrl !== lastUrl && !newUrl.includes(lastUrl));

    // Auth detection
    const isAuthAction = actionType === 'fill' && /password|email|username|login/i.test(reason + (act.data_key || ''));
    const isAuthSubmit = (actionType === 'click' || actionType === 'submit') && /login|sign.?in|auth/i.test(reason + verMsg);

    // Start new flow on boundary
    if (isNavBoundary || !currentFlow) {
      if (currentFlow && currentFlow.steps.length > 0) {
        currentFlow.status = 'complete';
        flows.push(currentFlow);
      }

      flowCounter++;
      currentFlow = {
        id: `flow-${flowCounter}`,
        name: detectFlowNameFromAction(act, newUrl || lastUrl, flowCounter),
        type: isAuthAction ? 'auth' : 'navigation',
        steps: [],
        apiCalls: [],
        status: 'discovering',
      };
    }

    if (newUrl) lastUrl = newUrl;

    if (currentFlow) {
      // Reclassify flow type
      if (isAuthAction || isAuthSubmit) currentFlow.type = 'auth';
      if (actionType === 'submit' && currentFlow.type === 'navigation') currentFlow.type = 'form_submit';
      if ((actionType === 'extract_data' || actionType === 'evaluate_js') && currentFlow.type === 'navigation') currentFlow.type = 'data_fetch';
      if (actionType === 'api_call') currentFlow.type = 'api_call';

      // Build step description
      let desc = '';
      if (actionType === 'fill') desc = i18n.t('Fill {{target}}', { target: act.data_key || act.value || '' });
      else if (actionType === 'click') desc = reason || (act.field_index !== undefined ? i18n.t('Click field {{index}}', { index: act.field_index }) : i18n.t('Click element'));
      else if (actionType === 'submit') desc = reason || i18n.t('Submit form');
      else if (actionType === 'evaluate_js') desc = i18n.t('JS: {{script}}', { script: (act.script || '').substring(0, 40) });
      else if (actionType === 'extract_data') desc = i18n.t('Extract → {{variable}}', { variable: act.variable || i18n.t('data') });
      else if (actionType === 'navigate') desc = i18n.t('Go to {{url}}', { url: (newUrl || '').substring(0, 40) });
      else if (actionType === 'press_key') desc = i18n.t('Press {{key}}', { key: act.key });
      else if (actionType === 'wait_for_tab') desc = i18n.t('Wait for tab opened by site');
      else if (actionType === 'open_tab') desc = newUrl ? i18n.t('Open new tab → {{url}}', { url: newUrl.substring(0, 40) }) : i18n.t('Open new tab');
      else if (actionType === 'switch_tab') desc = i18n.t('Switch to tab {{index}}', { index: act.tab_index });
      else desc = reason || actionType;

      currentFlow.steps.push({
        action: actionType,
        description: desc,
        status: success === false ? 'error' : 'done',
      });
    }
  }

  // Push last flow
  if (currentFlow && currentFlow.steps.length > 0) {
    currentFlow.status = session.status === 'running' ? 'discovering' : 'complete';
    flows.push(currentFlow);
  }

  // Polish: rename auth flows, deduplicate
  flows.forEach(f => {
    if (f.type === 'auth' && !/login|auth|sign/i.test(f.name)) f.name = i18n.t('Login Flow');
    if (f.type === 'form_submit' && f.name.startsWith('Flow ')) f.name = i18n.t('Form Submission');
    if (f.type === 'data_fetch' && f.name.startsWith('Flow ')) f.name = i18n.t('Data Extraction');
  });

  return flows;
}

function detectFlowNameFromAction(action: any, url: string, index: number): string {
  // Try to get a name from the URL path
  try {
    const path = new URL(url).pathname;
    const segments = path.split('/').filter(Boolean);
    if (segments.length > 0) {
      const last = segments[segments.length - 1]
        .replace(/[-_]/g, ' ')
        .replace(/\.[^.]+$/, '')
        .replace(/\b\w/g, c => c.toUpperCase());
      if (last.length > 2) return last;
    }
  } catch {}

  const reason = action.reason || '';
  if (reason) return reason.substring(0, 35);

  return `Flow ${index}`;
}
