import React, { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import toast from 'react-hot-toast';
import {
  ClipboardDocumentIcon,
  ClipboardDocumentCheckIcon,
  ChevronRightIcon,
  PlayIcon,
  EyeIcon,
  ArrowsRightLeftIcon,
  SparklesIcon,
  CheckCircleIcon,
} from '@heroicons/react/24/outline';
import { webhookTriggersApi } from '../../../api/endpoints';
import { formatRelativeTime } from '../../../utils/format';
import { WorkflowFastActions } from '../../../components/workflows/WorkflowFastActions';
import { FunctionEditor } from './FunctionEditor';
import { StreamingSettings } from './StreamingSettings';
import { Section } from './Section';
import { Collapsible } from './Collapsible';
import { requestSurface } from '../../../onboarding/surfaceTrigger';
import i18n from '../../../i18n';

// Trigger-source identity for linked-automation rows (monochrome — the icon
// differentiates the source, matching the Automations list page).
const TRIGGER_SOURCE: Record<string, { label: string; icon: any }> = {
  change_detected: { label: 'Monitor', icon: EyeIcon },
  webhook_received: { label: 'Webhook', icon: ArrowsRightLeftIcon },
  ai_session_started: { label: 'AI Start', icon: SparklesIcon },
  ai_session_completed: { label: 'AI Done', icon: SparklesIcon },
  workflow_started: { label: 'Workflow', icon: PlayIcon },
  workflow_completed: { label: 'Workflow Done', icon: CheckCircleIcon },
};

function triggerActionSummary(blocks?: any[]): string {
  if (!blocks || blocks.length === 0) return '';
  return blocks
    .filter((b) => b.type === 'action')
    .map((a) => {
      if (a.blockType === 'notification') return i18n.t('Notify');
      if (a.blockType === 'ai_session') return i18n.t('AI Agent');
      if (a.blockType === 'workflow') return i18n.t('Workflow');
      if (a.blockType === 'return_data') return i18n.t('Return Data');
      return a.blockType;
    })
    .join(' → ');
}

interface ConnectTabProps {
  workflow: any;
  linkedTriggers: any[];
  isStreaming: boolean;
  onRefresh: () => void;
}

/**
 * "What to do after" — every way this workflow gets invoked or wired up:
 *   • Call it   — publish as REST / MCP / OpenAI, with callable functions.
 *   • Triggers  — run it on a schedule, an incoming webhook, or a monitor change.
 *   • Automations — react when it finishes (notify), plus everything linked to it.
 * Streaming engine/API tuning lives in a collapsed Advanced section.
 */
// One copyable endpoint row. At MODULE scope, not inside CallThisMini: a
// component declared in a render body is a new component *type* every render, so
// React remounts the row (and the copy button's transient checkmark) instead of
// updating it (`react-hooks/static-components`). The two values it used to close
// over travel as props — there are only two call sites.
const EndpointRow: React.FC<{ label: string; value: string; copied: boolean; onCopy: (value: string) => void }> = ({ label, value, copied, onCopy }) => {
  const { t } = useTranslation();
  return (
    <div>
      <div className="text-[12px] font-medium text-secondary mb-1">{label}</div>
      <div className="flex items-center gap-2">
        <code className="flex-1 text-[12px] font-mono text-ink bg-canvas border border-border rounded-lg px-3 py-2 truncate">{value}</code>
        <button
          onClick={() => onCopy(value)}
          className="p-2 text-tertiary hover:text-ink rounded-lg hover:bg-chrome transition-colors shrink-0"
          title={t('Copy')}
        >
          {copied ? <ClipboardDocumentCheckIcon className="w-4 h-4 text-emerald-500" /> : <ClipboardDocumentIcon className="w-4 h-4" />}
        </button>
      </div>
    </div>
  );
};

// Compact "call this over HTTP/MCP" panel. The coordinator serves REST at
// /api/v1 and MCP at /mcp on its own origin — authenticate with a scoped wlk_
// key from the API Keys tab. (The cloud managed-API/publish panel is gone.)
const CallThisMini: React.FC<{ workflowId: number }> = ({ workflowId }) => {
  const { t } = useTranslation();
  const [copied, setCopied] = useState<string | null>(null);
  const origin = typeof window !== 'undefined' ? window.location.origin : '';
  // The coordinator mounts the automation router under /api (routers/automation.py:
  // APIRouter(prefix="/automation")). This row used to advertise
  // /api/v1/workflows/{id}/run, which is mounted NOWHERE — only files and
  // local-workflows live under /api/v1 — so every copy-pasted call 404'd.
  const restUrl = `${origin}/api/automation/workflows/${workflowId}/run`;
  const mcpUrl = `${origin}/mcp`;
  // The copied value is the whole `POST …` line, so name it once.
  const restLine = `POST ${restUrl}`;

  const copy = (val: string) => {
    navigator.clipboard.writeText(val).then(
      () => { setCopied(val); setTimeout(() => setCopied(null), 1500); },
      () => toast.error(t('Could not copy')),
    );
  };

  return (
    <Section title={t('Call it')} description={t('Run this workflow over HTTP or MCP. Authenticate with a scoped key from the API Keys tab.')}>
      <div className="space-y-4">
        <EndpointRow label={t('REST')} value={restLine} copied={copied === restLine} onCopy={copy} />
        {/* The delivery choice, stated where the URL is copied. Without it a caller
            has to discover from an empty response that the run is fire-and-forget. */}
        <p className="text-[11px] text-tertiary leading-relaxed -mt-1">
          {t('Returns a task_id immediately. Add ?wait=true (and optionally &timeout=120) to block until the run finishes and get its result inline — past the timeout you get 504 with the task_id still valid, so collect it rather than re-running.')}
        </p>
        <EndpointRow label={t('MCP')} value={mcpUrl} copied={copied === mcpUrl} onCopy={copy} />
        <Link to="/developers" className="inline-flex items-center gap-1 text-[12px] text-ink underline decoration-zinc-300 hover:decoration-zinc-500 transition-colors">
          {t('Manage API keys & endpoints')}
          <ChevronRightIcon className="w-3 h-3" />
        </Link>
      </div>
    </Section>
  );
};

export const ConnectTab: React.FC<ConnectTabProps> = ({ workflow, linkedTriggers, isStreaming, onRefresh }) => {
  const { t } = useTranslation();
  const [copiedToken, setCopiedToken] = useState<string | null>(null);

  // First time a new user lands on a workflow's Connect surface, surface the
  // one-time "it's callable" hint (no-op once seen / when onboarding is off).
  useEffect(() => { requestSurface('callable'); }, []);

  // `wide`, not `split`: the SummaryRail eats 264px of the stage before this grid
  // gets any, so the sidebar third only earns its keep once the stage is ~1100.
  return (
    <div className="grid grid-cols-1 @wide/stage:grid-cols-3 gap-x-8 gap-y-6 items-start">
      {/* MAIN */}
      <div className="@wide/stage:col-span-2 min-w-0 space-y-6">
        {/* ── Call it — REST + MCP URLs (auth via a scoped API key) ── */}
        <div>
          {/* Zero-height marker at the panel top — the "it's callable" hint points here. */}
          <span data-surface="callable" aria-hidden="true" className="block h-0" />
          <CallThisMini workflowId={workflow.id} />
        </div>

        {/* Callable functions — each becomes an API operation and an MCP tool. */}
        <FunctionEditor workflowId={workflow.id} workflow={workflow} onUpdate={onRefresh} />

        {/* ── Triggers — ways it runs on its own (schedule is an invocation method,
            alongside the webhook + monitor change). Not applicable to streaming
            sessions, which are opened, not triggered. ── */}
        {!isStreaming && (
          <Section
            title={t('Triggers')}
            description={t('Run this workflow automatically — on a schedule, when a monitored page changes, or from an incoming webhook.')}
          >
            <WorkflowFastActions
              workflow={workflow}
              linkedTriggers={linkedTriggers}
              onCreated={onRefresh}
              actions={['schedule', 'onchange', 'api']}
              title={null}
              showMoreLink={false}
            />
          </Section>
        )}

        {/* ── Automations — react when it finishes + everything wired to it ── */}
        <Section
          title={t('Automations')}
          description={t('React when this workflow finishes, and see every trigger linked to it.')}
        >
          <div className="space-y-3">
            {linkedTriggers.length > 0 && (
              <div className="bg-surface border border-ink/20 rounded-xl overflow-hidden divide-y divide-border shadow-sm">
                {linkedTriggers.map((trigger: any) => {
                  const source = TRIGGER_SOURCE[trigger.event_type] || TRIGGER_SOURCE.change_detected;
                  const SourceIcon = source.icon;
                  const summary = triggerActionSummary(trigger.blocks);
                  const webhookToken =
                    trigger.webhook_trigger_token ||
                    trigger.blocks?.find((b: any) => b.blockType === 'webhook_received')?.config?.webhook_trigger_token;
                  return (
                    <Link
                      key={trigger.id}
                      to={`/automations/${trigger.id}`}
                      className="group flex items-center gap-3 px-4 py-3 hover:bg-chrome transition-colors"
                    >
                      <div className="w-7 h-7 rounded-lg bg-hover flex items-center justify-center shrink-0">
                        <SourceIcon className="w-3.5 h-3.5 text-secondary" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="text-[13px] font-medium text-ink truncate">{trigger.name || t('Untitled')}</span>
                          <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-hover text-secondary font-medium shrink-0">{t(source.label)}</span>
                        </div>
                        {summary && <div className="text-[11px] text-tertiary mt-0.5 truncate">→ {summary}</div>}
                      </div>
                      <div className="hidden @pair/stage:block text-right text-[11px] text-tertiary shrink-0">
                        <div>{t('{{n}} runs', { n: trigger.trigger_count || 0 })}</div>
                        {trigger.last_triggered_at && <div>{formatRelativeTime(trigger.last_triggered_at)}</div>}
                      </div>
                      {webhookToken && (
                        <button
                          onClick={(e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            navigator.clipboard.writeText(webhookTriggersApi.getWebhookUrl(webhookToken));
                            setCopiedToken(webhookToken);
                            toast.success(t('Copied webhook URL'));
                            setTimeout(() => setCopiedToken((c) => (c === webhookToken ? null : c)), 2000);
                          }}
                          title={t('Copy webhook URL')}
                          className="p-1.5 text-tertiary hover:text-ink hover:bg-chrome rounded-lg transition-colors shrink-0"
                        >
                          {copiedToken === webhookToken
                            ? <ClipboardDocumentCheckIcon className="w-3.5 h-3.5 text-ink" />
                            : <ClipboardDocumentIcon className="w-3.5 h-3.5" />}
                        </button>
                      )}
                      <div className={clsxDot(trigger.enabled)} title={trigger.enabled ? t('Enabled') : t('Paused')} />
                      <ChevronRightIcon className="w-3.5 h-3.5 text-tertiary group-hover:text-ink transition-colors shrink-0" />
                    </Link>
                  );
                })}
              </div>
            )}
            <WorkflowFastActions
              workflow={workflow}
              linkedTriggers={linkedTriggers}
              onCreated={onRefresh}
              actions={['notify']}
              title={null}
              showMoreLink={false}
            />
          </div>
        </Section>

        {/* ── Advanced — streaming API & engine settings (model, conversations,
            session). Collapsed by default. The advanced message script lives in
            the Steps tab. ── */}
        {isStreaming && (
          <Collapsible
            title={t('Advanced — API & engine')}
            description={t('Model details and capabilities, conversation behaviour, and session limits. The advanced message script lives in the Steps tab.')}
          >
            <StreamingSettings
              workflowId={workflow.id}
              workflow={workflow}
              streamingConfig={workflow.streaming_config || {}}
              onUpdate={onRefresh}
            />
          </Collapsible>
        )}
      </div>

      {/* SIDEBAR — how calling works */}
      <aside className="@wide/stage:col-span-1 min-w-0 space-y-6">
        <div>
          <h2 className="text-[13px] font-semibold text-ink mb-2">{t('How calling works')}</h2>
          <div className="rounded-xl border border-ink/20 bg-surface p-4 text-[12px] text-secondary leading-relaxed space-y-2 shadow-sm">
            <p>{t('Publish this workflow as a REST endpoint or an MCP tool, then call it from your own app, a script, or an AI agent using an API key.')}</p>
            <p className="text-tertiary">{t('Functions are its callable units — each becomes an API operation and an MCP tool.')}</p>
            <p className="text-tertiary">{t('Or have it run itself: a schedule, an incoming webhook, or a monitored page change can each trigger a run.')}</p>
          </div>
        </div>
      </aside>
    </div>
  );
};

// Tiny monochrome status dot for a linked automation row (enabled = ink, off = grey).
function clsxDot(enabled: boolean): string {
  return `w-2 h-2 rounded-full shrink-0 ${enabled ? 'bg-ink' : 'bg-active'}`;
}
