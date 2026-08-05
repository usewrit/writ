import React from 'react';
import {
  PlusIcon,
  TrashIcon,
  CpuChipIcon,
  Cog6ToothIcon,
  CheckIcon,
  ChevronDownIcon,
  ExclamationTriangleIcon,
  UserGroupIcon,
  LockClosedIcon,
  CloudIcon,
  EyeIcon,
  LinkIcon,
  ComputerDesktopIcon,
  BellIcon,
  EnvelopeIcon,
  DevicePhoneMobileIcon,
  ChatBubbleBottomCenterTextIcon,
  PhoneIcon,
} from '@heroicons/react/24/outline';
import { ClipboardIcon, ClipboardDocumentCheckIcon } from '@heroicons/react/24/outline';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import { useFlowActions, useFlowState, useFlowStore, getAncestorChain } from '../FlowBuilderContext';
import { shallow } from 'zustand/shallow';
import { PersonaPicker } from '../../workflows/PersonaPicker';
import { FilePicker } from './FilePicker';
import { DataTablePicker } from './DataTablePicker';
import { FieldRef } from './FieldRef';
import { WizardBridge } from '../WizardBridge';
import { ContentMonitorPanel } from '../../wizard/panels/ContentMonitorPanel';
import { FlowBlock } from '../types';
import { NOTIFICATION_CHANNELS } from '../blockCatalog';
import { APP_PLATFORM } from '../../../api/aiAssist';
import { webhookTriggersApi, selectorsApi } from '../../../api/endpoints';
import { getNotificationPreferences, type NotificationPreferences } from '../../../api/notifications';
import { useQuery } from '../../../hooks/useQuery';
import { BlockPlaceholderHints } from './BlockPlaceholders';
import { saveFlowDraft } from '../flowDraft';
import type { WizardState } from '../../wizard/WizardContext';
import { Checkbox, NumberInput, Select } from '../../ui';
import { SchedulePicker } from '../../schedule/SchedulePicker';
import { scheduleFromBlockConfig, scheduleToBlockConfig } from '../../../utils/schedule';

interface BlockContentProps {
  block: FlowBlock;
}

/** True when a value carries a `{{...}}` placeholder bound to upstream data. */
const isBoundValue = (v: unknown): boolean => typeof v === 'string' && /\{\{.+?\}\}/.test(v);
/** Subtle "wired to an upstream output" treatment for a bound input's value. */
const boundInputClass = (v: unknown): string =>
  isBoundValue(v) ? ' font-mono border-l-2 border-l-ink/30' : '';

export const BlockContent: React.FC<BlockContentProps> = ({ block }) => {
  const { t } = useTranslation();
  const { updateBlockConfig } = useFlowActions();
  // Slice subscriptions: reference-data loads no longer re-render every block, and
  // this component stops depending on unrelated parts of the flow state.
  const blocks = useFlowState((s) => s.blocks, shallow);
  const workflows = useFlowState((s) => s.workflows, shallow);
  const availableSessions = useFlowState((s) => s.sessions, shallow);
  const availableRecipients = useFlowState((s) => s.recipients, shallow);
  const expandedAdvancedBlocks = useFlowState((s) => s.expandedAdvancedBlocks, shallow);
  const { dispatch } = useFlowActions();
  const [copiedWebhookToken, setCopiedWebhookToken] = React.useState<string | null>(null);

  const isFirstBlock = !block.parentId;

  const copyWebhookUrl = (token: string) => {
    navigator.clipboard.writeText(webhookTriggersApi.getWebhookUrl(token));
    setCopiedWebhookToken(token);
    toast.success(t('Copied webhook URL'));
    setTimeout(() => setCopiedWebhookToken(null), 2000);
  };

  // === EVENT BLOCKS ===
  if (block.type === 'event') {
    // Source block — full feature panel for the chosen source type
    if (isFirstBlock) {
      if (block.blockType === 'change_detected') {
        // Bound to an EXISTING monitor → compact config (which selector + guardrails).
        // The full browser recorder is ONLY for setting up a brand-new monitor's
        // selectors; showing it for an already-configured monitor is dead weight.
        if (block.config?.target_id) {
          return <ExistingMonitorSourceConfig block={block} />;
        }
        return <ContentMonitorEmbed block={block} />;
      }
      if (block.blockType === 'monitor_down' || block.blockType === 'monitor_stale' || block.blockType === 'monitor_recovered') {
        return <MonitorHealthSourceConfig block={block} />;
      }
      if (block.blockType === 'webhook_received') {
        return <WebhookReceivedConfig block={block} copiedWebhookToken={copiedWebhookToken} copyWebhookUrl={copyWebhookUrl} />;
      }
      if (block.blockType === 'ai_session_completed' || block.blockType === 'ai_session_started') {
        return <SessionFilterConfig block={block} sessions={availableSessions} updateBlockConfig={updateBlockConfig} />;
      }
      if (block.blockType === 'workflow_completed' || block.blockType === 'workflow_started') {
        return <WorkflowFilterConfig block={block} workflows={workflows} updateBlockConfig={updateBlockConfig} />;
      }
      if (block.blockType === 'scheduled') {
        return <ScheduledConfig block={block} updateBlockConfig={updateBlockConfig} />;
      }
      if (block.blockType === 'streaming_session_started' || block.blockType === 'streaming_session_ended') {
        return <StreamingSessionSourceConfig block={block} workflows={workflows} updateBlockConfig={updateBlockConfig} />;
      }
      if (block.blockType === 'data_extracted') {
        return <DataExtractedSourceConfig block={block} updateBlockConfig={updateBlockConfig} />;
      }
      if (block.blockType === 'file_uploaded') {
        return <FileUploadedSourceConfig block={block} updateBlockConfig={updateBlockConfig} />;
      }
      return null;
    }

    // Non-root event blocks (completion events)
    if (block.blockType === 'change_detected') {
      return <ContentChangedSelectorPicker block={block} blocks={blocks} updateBlockConfig={updateBlockConfig} />;
    }
    if (block.blockType === 'ai_session_completed' || block.blockType === 'ai_session_started') {
      return <CompletionEventAI block={block} blocks={blocks} sessions={availableSessions} updateBlockConfig={updateBlockConfig} />;
    }
    if (block.blockType === 'workflow_completed' || block.blockType === 'workflow_started') {
      return <CompletionEventWorkflow block={block} blocks={blocks} workflows={workflows} updateBlockConfig={updateBlockConfig} />;
    }
    if (block.blockType === 'streaming_session_started' || block.blockType === 'streaming_session_ended') {
      return <StreamingSessionSourceConfig block={block} workflows={workflows} updateBlockConfig={updateBlockConfig} />;
    }
    if (block.blockType === 'data_extracted') {
      return <DataExtractedSourceConfig block={block} updateBlockConfig={updateBlockConfig} />;
    }
    if (block.blockType === 'file_uploaded') {
      return <FileUploadedSourceConfig block={block} updateBlockConfig={updateBlockConfig} />;
    }
  }

  // === CONDITION BLOCKS ===
  if (block.type === 'condition') {
    return <ConditionConfig block={block} blocks={blocks} updateBlockConfig={updateBlockConfig} />;
  }

  // === ACTION BLOCKS ===
  if (block.type === 'action') {
    if (block.blockType === 'notification') {
      return (
        <NotificationConfig
          block={block}
          blocks={blocks}
          recipients={availableRecipients}
          updateBlockConfig={updateBlockConfig}
          expandedAdvancedBlocks={expandedAdvancedBlocks}
          dispatch={dispatch}
        />
      );
    }
    if (block.blockType === 'ai_session') {
      return <AISessionConfig block={block} blocks={blocks} sessions={availableSessions} updateBlockConfig={updateBlockConfig} />;
    }
    if (block.blockType === 'workflow') {
      return <WorkflowConfig block={block} blocks={blocks} workflows={workflows} updateBlockConfig={updateBlockConfig} />;
    }
    if (block.blockType === 'for_each') {
      return <ForEachConfig block={block} updateBlockConfig={updateBlockConfig} />;
    }
    if (block.blockType === 'return_data') {
      return <ReturnDataConfig blocks={blocks} workflows={workflows} />;
    }
    if (block.blockType === 'create_persona') {
      return <CreatePersonaConfig block={block} workflows={workflows} updateBlockConfig={updateBlockConfig} />;
    }
    if (block.blockType === 'save_data_to_file' || block.blockType === 'query_and_export') {
      return <DataExportConfig block={block} updateBlockConfig={updateBlockConfig} />;
    }
    if (block.blockType === 'append_to_data') {
      return <AppendToDataConfig block={block} updateBlockConfig={updateBlockConfig} />;
    }
    if (block.blockType === 'send_file') {
      return <SendFileConfig block={block} updateBlockConfig={updateBlockConfig} />;
    }
    if (block.blockType === 'start_streaming_session' || block.blockType === 'stop_streaming_session') {
      return <StreamingActionConfig block={block} workflows={workflows} updateBlockConfig={updateBlockConfig} />;
    }
  }

  return null;
};

// --- Sub-components ---

// Full ContentMonitorPanel embedded via WizardBridge for source blocks
function ContentMonitorEmbed({ block }: { block: FlowBlock }) {
  const { updateBlockConfig, dispatch } = useFlowActions();
  const name = useFlowState((s) => s.name);

  const initialConfig: Partial<WizardState['config']> = {
    name: name || '',
    url: block.config.url || '',
    selectors: block.config.wizardSelectors || [],
    checkPeriodMs: block.config.check_period_ms || 60000,
    requiresPlaywright: block.config.requires_playwright || false,
  };

  const handleConfigChange = React.useCallback((updates: Partial<WizardState['config']>) => {
    updateBlockConfig(block.id, {
      ...block.config,
      url: updates.url,
      wizardSelectors: updates.selectors,
      check_period_ms: updates.checkPeriodMs,
      requires_playwright: updates.requiresPlaywright,
    });
    if (updates.name && updates.name !== name) {
      dispatch({ type: 'SET_META', name: updates.name });
    }
  }, [block.id, block.config, updateBlockConfig, dispatch, name]);

  return (
    <>
      <WizardBridge mode="content_monitor" config={initialConfig} onConfigChange={handleConfigChange}>
        <div className="flow-source-panel" style={{ height: '560px' }}>
          <ContentMonitorPanel />
        </div>
      </WizardBridge>
      <GuardrailsSection block={block} />
    </>
  );
}

// Firing guardrails for the monitor source — persisted into trigger.conditions
// on save (see FlowBuilder.handleSave). Prevents "act on every change" flows
// (e.g. restock auto-buy) from re-firing on every poll.
function GuardrailsSection({ block }: { block: FlowBlock }) {
  const { t } = useTranslation();
  const { updateBlockConfig } = useFlowActions();
  const runOnce = Number(block.config.max_fires) === 1;
  const cooldown = block.config.cooldown_minutes ?? '';

  return (
    <div className="px-5 py-4 border-t border-border space-y-3">
      <div className="text-[10px] font-medium uppercase tracking-wider text-tertiary">{t('Guardrails')}</div>
      <Checkbox
        checked={runOnce}
        onChange={(e) => updateBlockConfig(block.id, { ...block.config, max_fires: e.target.checked ? 1 : undefined })}
        label={<>{t('Run once, then stop')} <span className="text-[11px] text-tertiary">{t('— act a single time, then auto-disable')}</span></>}
      />
      <div className="flex items-center gap-2">
        <label className="text-sm text-ink">{t('Cooldown')}</label>
        <NumberInput
          min={0}
          value={cooldown === '' ? null : Number(cooldown)}
          onChange={(v) => updateBlockConfig(block.id, { ...block.config, cooldown_minutes: v ?? undefined })}
          size="sm"
          className="w-24"
          placeholder="0"
        />
        <span className="text-[11px] text-tertiary">{t('minutes between fires')}</span>
      </div>
    </div>
  );
}

// Compact source config for a change_detected bound to an EXISTING monitor.
// No browser recorder — the monitor + its selectors already exist; the user only
// chooses which selector to watch and the firing guardrails.
function ExistingMonitorSourceConfig({ block }: { block: FlowBlock }) {
  const { t } = useTranslation();
  const { updateBlockConfig } = useFlowActions();
  const targets = useFlowState((s) => s.targets, shallow);
  const target = (targets || []).find((tg: any) => tg.id === block.config.target_id);
  // Selectors are stored WITH the target they were fetched for, so "still loading"
  // is derived during render instead of written by the effect (which would cost an
  // extra render pass and briefly show the previous target's list).
  const [loaded, setLoaded] = React.useState<{ targetId: any; rows: any[] } | null>(null);
  const fresh = loaded != null && loaded.targetId === block.config.target_id;
  const selectors = fresh ? loaded.rows : [];
  const loading = Boolean(block.config.target_id) && !fresh;

  React.useEffect(() => {
    const targetId = block.config.target_id;
    if (!targetId) return;
    let alive = true;
    selectorsApi.listForTarget(targetId, true)
      .then((data) => { if (alive) setLoaded({ targetId, rows: data || [] }); })
      .catch(() => { if (alive) setLoaded({ targetId, rows: [] }); });
    return () => { alive = false; };
  }, [block.config.target_id]);

  return (
    <div>
      <div className="px-5 py-4 space-y-3">
        <MonitorBoundHeader url={target?.url} label={t('Content monitor')} />
        <div>
          <label className="block text-xs text-secondary mb-1">{t('React when this changes')}</label>
          {loading ? (
            <p className="text-xs text-tertiary">{t('Loading selectors...')}</p>
          ) : (
            <Select<number>
              value={block.config.selector_id || 0}
              onChange={(v) => updateBlockConfig(block.id, { ...block.config, selector_id: v === 0 ? undefined : v })}
              placeholder={t('Any change on this page')}
              options={[
                { value: 0, label: t('Any change on this page') },
                ...selectors.map((s: any) => ({ value: Number(s.id), label: s.name || s.selector })),
              ]}
              size="sm"
              className="w-full"
            />
          )}
        </div>
      </div>
      <GuardrailsSection block={block} />
    </div>
  );
}

// Compact source config for a monitor-health event (down / stale / recovered).
// Health is whole-monitor, so there's no selector and no browser — just the bound
// monitor, a plain-language description of the event, and the firing guardrails.
function MonitorHealthSourceConfig({ block }: { block: FlowBlock }) {
  const { t } = useTranslation();
  const targets = useFlowState((s) => s.targets, shallow);
  const target = (targets || []).find((tg: any) => tg.id === block.config.target_id);
  const blurb: Record<string, string> = {
    monitor_down: t('Fires once when this monitor becomes unreachable (the check fails or the page can’t be reached).'),
    monitor_stale: t('Fires once when this monitor stops updating well past its expected check interval.'),
    monitor_recovered: t('Fires once when this monitor returns to healthy after being down or stale.'),
  };
  return (
    <div>
      <div className="px-5 py-4 space-y-3">
        <MonitorBoundHeader url={target?.url} label={t('Monitored page')} />
        <p className="text-xs text-secondary leading-relaxed">{blurb[block.blockType] || ''}</p>
        {!block.config.target_id && (
          <p className="text-xs text-amber-600">{t('No monitor bound — remove this and pick a monitor from the source picker.')}</p>
        )}
      </div>
      <GuardrailsSection block={block} />
    </div>
  );
}

// Shared header showing which monitor a source block is bound to.
function MonitorBoundHeader({ url, label }: { url?: string; label: string }) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-3 rounded-lg bg-hover/60 px-3 py-2">
      <EyeIcon className="h-4 w-4 text-secondary shrink-0" />
      <div className="min-w-0">
        <div className="text-sm font-medium text-ink truncate">{url || t('(monitor)')}</div>
        <div className="text-[11px] text-tertiary">{label}</div>
      </div>
    </div>
  );
}

// Selector picker for child change_detected blocks (non-source)
function ContentChangedSelectorPicker({ block, blocks, updateBlockConfig }: any) {
  const { t } = useTranslation();

  // Find the target_id from the source block or this block's config
  const rootSource = blocks.find((b: FlowBlock) => b.type === 'event' && !b.parentId);
  const targetId = block.config.target_id || rootSource?.config?.target_id;

  // Keyed by the target they were fetched for, so "still loading" is derived
  // during render rather than written by the effect (see the source-config
  // picker above — same shape).
  const [loaded, setLoaded] = React.useState<{ targetId: any; rows: any[] } | null>(null);
  const fresh = loaded != null && loaded.targetId === targetId;
  const selectors = fresh ? loaded.rows : [];
  const loading = Boolean(targetId) && !fresh;

  React.useEffect(() => {
    if (!targetId) return;
    let alive = true;
    selectorsApi.listForTarget(targetId, true)
      .then(data => { if (alive) setLoaded({ targetId, rows: data || [] }); })
      .catch(() => { if (alive) setLoaded({ targetId, rows: [] }); });
    return () => { alive = false; };
  }, [targetId]);

  if (!targetId) {
    return <p className="text-xs text-tertiary italic">{t('No target configured on the source block.')}</p>;
  }

  return (
    <div className="space-y-2">
      <label className="block text-xs text-secondary">{t('When this selector changes:')}</label>
      {loading ? (
        <p className="text-xs text-tertiary">{t('Loading selectors...')}</p>
      ) : selectors.length === 0 ? (
        <p className="text-xs text-tertiary italic">{t('No selectors found for this target. Add selectors in the source block.')}</p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          <button
            type="button"
            onClick={() => updateBlockConfig(block.id, { ...block.config, selector_id: null })}
            className={clsx(
              'px-3 py-1.5 rounded-lg text-xs font-medium border transition-all',
              !block.config.selector_id
                ? 'bg-hover text-ink border-border ring-1 ring-ink/20'
                : 'bg-canvas text-secondary border-border hover:border-secondary'
            )}
          >
            {t('Any selector')}
          </button>
          {selectors.map((s: any) => (
            <button
              key={s.id}
              type="button"
              onClick={() => updateBlockConfig(block.id, { ...block.config, selector_id: s.id })}
              className={clsx(
                'px-3 py-1.5 rounded-lg text-xs font-medium border transition-all',
                block.config.selector_id === s.id
                  ? 'bg-hover text-ink border-border ring-1 ring-ink/20'
                  : 'bg-canvas text-secondary border-border hover:border-secondary'
              )}
            >
              {s.name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function SessionFilterConfig({ block, sessions, updateBlockConfig }: any) {
  const { t } = useTranslation();
  const { dispatch } = useFlowActions();
  const blocks = useFlowState((s) => s.blocks, shallow);

  const currentEvent = block.blockType === 'ai_session_started' ? 'started'
    : block.config.status_condition === 'error' ? 'error'
    : 'completed';

  const handleEventChange = (eventKey: string) => {
    const newConfig = { ...block.config };
    delete newConfig.status_condition;
    let newBlockType: string;
    if (eventKey === 'started') {
      newBlockType = 'ai_session_started';
    } else if (eventKey === 'error') {
      newBlockType = 'ai_session_completed';
      newConfig.status_condition = 'error';
    } else {
      newBlockType = 'ai_session_completed';
    }
    const newBlocks = blocks.map(b =>
      b.id === block.id ? { ...b, blockType: newBlockType, config: newConfig } : b
    );
    dispatch({ type: 'SET_BLOCKS', blocks: newBlocks });
  };

  return (
    <div className="space-y-3 px-5 py-4">
      <div>
        <label className="block text-xs font-medium text-secondary mb-1.5">{t('Event')}</label>
        <div className="flex gap-1.5">
          {[
            { key: 'started', label: t('Started') },
            { key: 'completed', label: t('Completed') },
            { key: 'error', label: t('Error') },
          ].map(opt => (
            <button
              key={opt.key}
              type="button"
              onClick={() => handleEventChange(opt.key)}
              className={clsx(
                'px-3 py-1.5 rounded-lg text-xs font-medium transition-all border',
                currentEvent === opt.key
                  ? 'bg-zinc-900 text-white border-zinc-900'
                  : 'bg-white text-zinc-500 border-zinc-200 hover:border-zinc-300'
              )}
            >
              {t(opt.label)}
            </button>
          ))}
        </div>
      </div>
      <div>
        <label className="block text-xs font-medium text-zinc-500">{t('AI Session')}</label>
        <Select<number>
          value={block.config.ai_session_id || 0}
          onChange={v => updateBlockConfig(block.id, { ...block.config, ai_session_id: v === 0 ? null : v })}
          placeholder={t('Any AI session')}
          options={[
            { value: 0, label: t('Any AI session') },
            ...sessions.map((s: any) => ({ value: Number(s.id), label: s.name })),
          ]}
          className="w-full"
        />
      </div>
    </div>
  );
}

function WorkflowFilterConfig({ block, workflows, updateBlockConfig }: any) {
  const { t } = useTranslation();
  const { dispatch } = useFlowActions();
  const blocks = useFlowState((s) => s.blocks, shallow);

  const currentEvent = block.blockType === 'workflow_started' ? 'started'
    : block.config.status_condition === 'error' ? 'error'
    : 'completed';

  const handleEventChange = (eventKey: string) => {
    const newConfig = { ...block.config };
    delete newConfig.status_condition;
    let newBlockType: string;
    if (eventKey === 'started') {
      newBlockType = 'workflow_started';
    } else if (eventKey === 'error') {
      newBlockType = 'workflow_completed';
      newConfig.status_condition = 'error';
    } else {
      newBlockType = 'workflow_completed';
    }
    const newBlocks = blocks.map(b =>
      b.id === block.id ? { ...b, blockType: newBlockType, config: newConfig } : b
    );
    dispatch({ type: 'SET_BLOCKS', blocks: newBlocks });
  };

  return (
    <div className="space-y-3 px-5 py-4">
      <div>
        <label className="block text-xs font-medium text-secondary mb-1.5">{t('Event')}</label>
        <div className="flex gap-1.5">
          {[
            { key: 'started', label: t('Started') },
            { key: 'completed', label: t('Completed') },
            { key: 'error', label: t('Error') },
          ].map(opt => (
            <button
              key={opt.key}
              type="button"
              onClick={() => handleEventChange(opt.key)}
              className={clsx(
                'px-3 py-1.5 rounded-lg text-xs font-medium transition-all border',
                currentEvent === opt.key
                  ? 'bg-zinc-900 text-white border-zinc-900'
                  : 'bg-white text-zinc-500 border-zinc-200 hover:border-zinc-300'
              )}
            >
              {t(opt.label)}
            </button>
          ))}
        </div>
      </div>
      <div>
        <label className="block text-xs font-medium text-zinc-500">{t('Workflow')}</label>
        <Select<number>
          value={block.config.workflow_id || 0}
          onChange={v => updateBlockConfig(block.id, { ...block.config, workflow_id: v === 0 ? null : v })}
          placeholder={t('Any workflow')}
          options={[
            { value: 0, label: t('Any workflow') },
            ...workflows.map((w: any) => ({ value: Number(w.id), label: w.name })),
          ]}
          className="w-full"
        />
      </div>
    </div>
  );
}

function WebhookReceivedConfig({ block, copiedWebhookToken, copyWebhookUrl }: any) {
  const { t } = useTranslation();
  const token = block.config.webhook_trigger_token;
  const hasToken = !!token;
  const url = hasToken ? webhookTriggersApi.getWebhookUrl(token) : '';
  const waitUrl = hasToken ? `${url}?wait=true` : '';
  const [copiedField, setCopiedField] = React.useState<string | null>(null);

  const exampleCurl = hasToken
    ? `curl -X POST "${url}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"example": "value"}'`
    : '';

  const copyText = (text: string, field: string, label: string) => {
    navigator.clipboard.writeText(text);
    setCopiedField(field);
    toast.success(t('Copied {{label}}', { label }));
    setTimeout(() => setCopiedField((f) => (f === field ? null : f)), 2000);
  };

  return (
    <div className="space-y-4 px-5 py-4">
      {hasToken ? (
        <div className="space-y-4">
          {/* Plain-language intro */}
          <div className="px-4 py-3 bg-canvas rounded-lg border border-border">
            <p className="text-xs text-secondary leading-relaxed">
              {t('Send a')} <span className="font-semibold text-ink">POST</span> {t('request to this URL from your other tool (Zapier, your app, a script…) to start this automation. Anything you send as JSON becomes available to the steps below.')}
            </p>
          </div>

          {/* Webhook URL */}
          <div className="space-y-1.5">
            <div className="text-[10px] font-medium uppercase tracking-wider text-tertiary">{t('Your webhook URL')}</div>
            <div className="flex items-center gap-2">
              <code className="flex-1 text-xs text-ink bg-canvas px-3 py-2.5 rounded-lg border border-border truncate font-mono">
                {url}
              </code>
              <button type="button" onClick={() => copyWebhookUrl(token)} className="p-2 hover:bg-hover rounded-lg transition-colors shrink-0" title={t('Copy URL')}>
                {copiedWebhookToken === token ? (
                  <ClipboardDocumentCheckIcon className="h-4 w-4 text-ink" />
                ) : (
                  <ClipboardIcon className="h-4 w-4 text-tertiary" />
                )}
              </button>
            </div>
          </div>

          {/* Blocking mode (?wait=true) */}
          <div className="space-y-1.5">
            <div className="text-[10px] font-medium uppercase tracking-wider text-tertiary">{t('Wait for the result')}</div>
            <div className="flex items-center gap-2">
              <code className="flex-1 text-xs text-ink bg-canvas px-3 py-2.5 rounded-lg border border-border truncate font-mono">
                {waitUrl}
              </code>
              <button type="button" onClick={() => copyText(waitUrl, 'wait', t('URL'))} className="p-2 hover:bg-hover rounded-lg transition-colors shrink-0" title={t('Copy URL')}>
                {copiedField === 'wait' ? (
                  <ClipboardDocumentCheckIcon className="h-4 w-4 text-ink" />
                ) : (
                  <ClipboardIcon className="h-4 w-4 text-tertiary" />
                )}
              </button>
            </div>
            <p className="text-[10px] text-tertiary leading-relaxed">
              {t('Add')} <code className="bg-hover px-1 py-0.5 rounded text-secondary font-mono">?wait=true</code> {t('if you want the request to stay open until the automation finishes and return its result. Without it, the request returns immediately and the automation runs in the background.')}
            </p>
          </div>

          {/* Example request to test manually */}
          <div className="space-y-1.5">
            <div className="text-[10px] font-medium uppercase tracking-wider text-tertiary">{t('Try it — example request')}</div>
            <div className="relative">
              <pre className="text-[11px] text-ink bg-canvas px-3 py-2.5 pr-10 rounded-lg border border-border overflow-x-auto font-mono leading-relaxed whitespace-pre">{exampleCurl}</pre>
              <button type="button" onClick={() => copyText(exampleCurl, 'curl', t('command'))} className="absolute top-2 right-2 p-1.5 bg-surface/80 hover:bg-hover rounded-md border border-border transition-colors" title={t('Copy command')}>
                {copiedField === 'curl' ? (
                  <ClipboardDocumentCheckIcon className="h-3.5 w-3.5 text-ink" />
                ) : (
                  <ClipboardIcon className="h-3.5 w-3.5 text-tertiary" />
                )}
              </button>
            </div>
            <p className="text-[10px] text-tertiary leading-relaxed">
              {t('Paste this into a terminal to send a test call. Replace the JSON body with your own fields.')}
            </p>
          </div>

          {/* Signing / security note (secret is managed server-side, not shown here) */}
          <div className="flex items-start gap-2 px-3 py-2.5 bg-canvas rounded-lg border border-border">
            <Cog6ToothIcon className="h-3.5 w-3.5 text-tertiary mt-0.5 shrink-0" />
            <p className="text-[10px] text-secondary leading-relaxed">
              {t("This webhook has a signing secret for verifying that requests really came from your tool. The secret is managed here in the automation builder — set or rotate it in this block's settings.")}
            </p>
          </div>
        </div>
      ) : (
        <div className="px-4 py-3 bg-canvas rounded-lg border border-border">
          <p className="text-xs text-secondary leading-relaxed">
            {t('A unique webhook URL is generated as soon as you save this automation. You’ll then be able to send a POST request to it from any other tool to start this automation.')}
          </p>
        </div>
      )}

      <div className="text-[10px] text-tertiary leading-relaxed">
        {t('Anything you send is available as')} <code className="bg-hover px-1 py-0.5 rounded text-secondary font-mono">{'{{payload.field}}'}</code> {t('in the steps below.')}
      </div>

      {/* Subtle link to the central registry */}
      <a
        href="/developers/endpoints"
        className="inline-block text-[10px] text-tertiary hover:text-ink underline underline-offset-2 transition-colors"
      >
        {t('Manage all webhooks in Developers → Endpoints')}
      </a>
    </div>
  );
}

function CompletionEventAI({ block, blocks, sessions, updateBlockConfig }: any) {
  const { t } = useTranslation();
  const linkedBlockId = block.config.linked_to_block;
  const linkedBlock = linkedBlockId ? blocks.find((b: FlowBlock) => b.id === linkedBlockId) : null;
  const linkedSessionId = linkedBlock?.config?.session_ids?.[0];
  const linkedSession = linkedSessionId ? sessions.find((s: any) => s.id === linkedSessionId) : null;

  return (
    <div className="space-y-3 py-1">
      {linkedSession ? (
        <div className="flex items-center gap-2.5 text-sm text-ink bg-hover/80 px-4 py-2.5 rounded-lg border border-border">
          <CpuChipIcon className="h-4 w-4 shrink-0" />
          <span>{t('Linked to')} <strong>{linkedSession.name}</strong></span>
        </div>
      ) : (
        <div className="space-y-1.5">
          <label className="block text-xs font-medium text-secondary">{t('AI Session')}</label>
          <Select<number>
            value={block.config.ai_session_id || 0}
            onChange={v => updateBlockConfig(block.id, { ...block.config, ai_session_id: v === 0 ? null : v })}
            placeholder={t('Any AI session')}
            options={[
              { value: 0, label: t('Any AI session') },
              ...sessions.map((s: any) => ({ value: Number(s.id), label: s.name })),
            ]}
            className="w-full"
          />
        </div>
      )}
      {block.blockType === 'ai_session_completed' && (
        <StatusConditionPicker block={block} updateBlockConfig={updateBlockConfig} />
      )}
    </div>
  );
}

function CompletionEventWorkflow({ block, blocks, workflows, updateBlockConfig }: any) {
  const { t } = useTranslation();
  const linkedBlockId = block.config.linked_to_block;
  const linkedBlock = linkedBlockId ? blocks.find((b: FlowBlock) => b.id === linkedBlockId) : null;
  const linkedWorkflowId = linkedBlock?.config?.workflow_id;
  const linkedWorkflow = linkedWorkflowId ? workflows.find((w: any) => w.id === linkedWorkflowId) : null;

  return (
    <div className="space-y-3 py-1">
      {linkedWorkflow ? (
        <div className="flex items-center gap-2.5 text-sm text-ink bg-hover px-4 py-2.5 rounded-lg border border-border">
          <Cog6ToothIcon className="h-4 w-4 shrink-0" />
          <span>{t('Linked to')} <strong>{linkedWorkflow.name}</strong></span>
        </div>
      ) : (
        <div className="space-y-1.5">
          <label className="block text-xs font-medium text-secondary">{t('Workflow')}</label>
          <Select<number>
            value={block.config.workflow_id || 0}
            onChange={v => updateBlockConfig(block.id, { ...block.config, workflow_id: v === 0 ? null : v })}
            placeholder={t('Any workflow')}
            options={[
              { value: 0, label: t('Any workflow') },
              ...workflows.map((w: any) => ({ value: Number(w.id), label: w.name })),
            ]}
            className="w-full"
          />
        </div>
      )}
      {block.blockType === 'workflow_completed' && (
        <StatusConditionPicker block={block} updateBlockConfig={updateBlockConfig} />
      )}
    </div>
  );
}

// Precise recurrence editor for a `scheduled` event block. Emits the block config
// shape (mode/interval_ms/time/days/tz) via scheduleToBlockConfig and hydrates via
// scheduleFromBlockConfig. Interval mode floors to the daemon's 60s cadence.
function ScheduledConfig({ block, updateBlockConfig }: any) {
  const { t } = useTranslation();
  const value = scheduleFromBlockConfig(block.config);
  return (
    <div className="p-5 space-y-3">
      <label className="block text-xs font-medium text-secondary mb-1.5">{t('Runs on this schedule')}</label>
      <SchedulePicker
        value={value}
        onChange={(next) =>
          updateBlockConfig(block.id, { ...block.config, ...scheduleToBlockConfig(next) })
        }
      />
      <p className="text-xs text-tertiary">
        {t('The automation runs on this schedule. For intervals, the minimum effective cadence is 1 minute.')}
      </p>
    </div>
  );
}

// --- New surface blocks (streaming / data / files) ---

// Source config for streaming_session_started / _ended — pick the workflow whose
// live session the automation reacts to (blank = any workflow's session).
function StreamingSessionSourceConfig({ block, workflows, updateBlockConfig }: any) {
  const { t } = useTranslation();
  return (
    <div className="space-y-2 px-5 py-4">
      <label className="block text-xs font-medium text-secondary">{t('Streaming workflow')}</label>
      <Select<number>
        value={block.config.workflow_id || 0}
        onChange={(v) => updateBlockConfig(block.id, { ...block.config, workflow_id: v === 0 ? null : v })}
        aria-label={t('Streaming workflow')}
        placeholder={t('Any workflow')}
        options={[
          { value: 0, label: t('Any workflow') },
          ...workflows.map((w: any) => ({ value: Number(w.id), label: w.name })),
        ]}
        className="w-full"
      />
      <p className="text-[11px] text-tertiary">
        {t('Fires when a live streaming session for this workflow starts or ends.')}
      </p>
    </div>
  );
}

// Source config for data_extracted — pick the source workflow + a minimum row count.
function DataExtractedSourceConfig({ block, updateBlockConfig }: any) {
  const { t } = useTranslation();
  return (
    <div className="space-y-3 px-5 py-4">
      <DataTablePicker
        value={block.config.workflow_id ?? null}
        onChange={(id) => updateBlockConfig(block.id, { ...block.config, workflow_id: id })}
        label={t('When this workflow extracts data')}
      />
      <div>
        <label className="block text-xs font-medium text-secondary mb-1.5">{t('Minimum new rows')}</label>
        <NumberInput
          min={1}
          value={block.config.min_rows ?? null}
          onChange={(v) => updateBlockConfig(block.id, { ...block.config, min_rows: v ?? undefined })}
          placeholder="1"
          className="w-28"
        />
        <p className="text-[11px] text-tertiary mt-1">{t('Only fire when at least this many rows were produced.')}</p>
      </div>
    </div>
  );
}

// Source config for file_uploaded — optional source filter.
function FileUploadedSourceConfig({ block, updateBlockConfig }: any) {
  const { t } = useTranslation();
  return (
    <div className="space-y-2 px-5 py-4">
      <label className="block text-xs font-medium text-secondary">{t('File source')}</label>
      <Select
        value={block.config.source || ''}
        onChange={(v) => updateBlockConfig(block.id, { ...block.config, source: v || undefined })}
        aria-label={t('File source')}
        placeholder={t('Any source')}
        options={[
          { value: '', label: t('Any source') },
          { value: 'upload', label: t('Manual upload') },
          { value: 'api', label: t('File webhook (API)') },
          { value: 'workflow_output', label: t('Workflow output') },
        ]}
        className="w-full"
      />
      <p className="text-[11px] text-tertiary">
        {t('Fires when a new file arrives — from an upload or the file webhook.')}
      </p>
    </div>
  );
}

// Action config for save_data_to_file / query_and_export — source workflow +
// export format (+ optional row limit for query_and_export).
function DataExportConfig({ block, updateBlockConfig }: any) {
  const { t } = useTranslation();
  const isQuery = block.blockType === 'query_and_export';
  const format = block.config.format || 'csv';
  return (
    <div className="space-y-3">
      <DataTablePicker
        value={block.config.source_workflow_id ?? null}
        onChange={(id) => updateBlockConfig(block.id, { ...block.config, source_workflow_id: id })}
        label={t('Export data from workflow')}
      />
      <div>
        <label className="block text-xs font-medium text-zinc-500 mb-1.5">{t('Format')}</label>
        <div className="flex gap-1.5">
          {['csv', 'json'].map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => updateBlockConfig(block.id, { ...block.config, format: f })}
              className={clsx(
                'px-3 py-1.5 rounded-lg text-xs font-medium uppercase transition-all border',
                format === f ? 'bg-zinc-900 text-white border-zinc-900' : 'bg-white text-zinc-500 border-zinc-200 hover:border-zinc-300',
              )}
            >
              {f}
            </button>
          ))}
        </div>
      </div>
      {isQuery && (
        <div>
          <label className="block text-xs font-medium text-zinc-500 mb-1.5">{t('Row limit (optional)')}</label>
          <NumberInput
            min={1}
            value={block.config.limit ?? null}
            onChange={(v) => updateBlockConfig(block.id, { ...block.config, limit: v ?? undefined })}
            placeholder={t('All rows')}
            className="w-32"
          />
        </div>
      )}
      <p className="text-[11px] text-tertiary">
        {t('Exports a redacted file asset. It becomes available as')} <code className="text-ink font-mono">{'{{result.file_id}}'}</code>.
      </p>
    </div>
  );
}

// Action config for append_to_data — copy matching rows from one data table to another.
function AppendToDataConfig({ block, updateBlockConfig }: any) {
  const { t } = useTranslation();
  return (
    <div className="space-y-3">
      <DataTablePicker
        value={block.config.source_workflow_id ?? null}
        onChange={(id) => updateBlockConfig(block.id, { ...block.config, source_workflow_id: id })}
        label={t('Copy rows from')}
      />
      <DataTablePicker
        value={block.config.target_workflow_id ?? null}
        onChange={(id) => updateBlockConfig(block.id, { ...block.config, target_workflow_id: id })}
        label={t('Into')}
        allowClear
      />
      <div>
        <label className="block text-xs font-medium text-secondary mb-1.5">{t('Row limit (optional)')}</label>
        <NumberInput
          min={1}
          value={block.config.limit ?? null}
          onChange={(v) => updateBlockConfig(block.id, { ...block.config, limit: v ?? undefined })}
          placeholder={t('All rows')}
          className="w-32"
        />
      </div>
    </div>
  );
}

// Action config for send_file — pick a stored file + a delivery target (webhook URL).
function SendFileConfig({ block, updateBlockConfig }: any) {
  const { t } = useTranslation();
  return (
    <div className="space-y-3">
      <FilePicker
        value={block.config.file_id ?? null}
        onChange={(id) => updateBlockConfig(block.id, { ...block.config, file_id: id })}
        label={t('File to send')}
      />
      <div>
        <label className="block text-xs font-medium text-secondary mb-1.5">{t('Deliver to webhook URL')}</label>
        <input
          type="url"
          value={block.config.recipient_id || ''}
          onChange={(e) => updateBlockConfig(block.id, { ...block.config, recipient_type: 'webhook', recipient_id: e.target.value })}
          placeholder="https://example.com/hook"
          className="w-full px-3 py-2 rounded-lg border border-border bg-surface text-ink text-sm font-mono focus:outline-none focus:border-ink"
        />
        <p className="text-[11px] text-tertiary mt-1">
          {t('The daemon POSTs a file reference to this URL. Email delivery needs the cloud link.')}
        </p>
      </div>
    </div>
  );
}

// Action config for start_streaming_session / stop_streaming_session — pick the workflow.
function StreamingActionConfig({ block, workflows, updateBlockConfig }: any) {
  const { t } = useTranslation();
  const isStart = block.blockType === 'start_streaming_session';
  return (
    <div className="space-y-2">
      <label className="block text-xs font-medium text-secondary">{t('Workflow')}</label>
      <Select<number>
        value={block.config.workflow_id || undefined}
        onChange={(v) => updateBlockConfig(block.id, { ...block.config, workflow_id: v })}
        aria-label={t('Workflow')}
        placeholder={t('Select workflow...')}
        options={workflows.map((w: any) => ({ value: Number(w.id), label: w.name }))}
        className="w-full"
      />
      <p className="text-[11px] text-tertiary">
        {isStart
          ? t('Launches a live streaming session for this workflow.')
          : t('Ends the live streaming session for this workflow.')}
      </p>
    </div>
  );
}

function StatusConditionPicker({ block, updateBlockConfig }: any) {
  const { t } = useTranslation();
  return (
    <div className="space-y-2">
      <label className="block text-xs font-medium text-zinc-500">{t('Trigger when')}</label>
      <div className="flex gap-1.5">
        {[
          { value: 'any', label: t('Always'), active: 'bg-zinc-900 text-white border-zinc-900', inactive: 'bg-white text-zinc-500' },
          { value: 'success', label: t('Success'), active: 'bg-green-50 text-green-700 border-green-200', inactive: 'bg-white text-zinc-500' },
          { value: 'error', label: t('Error'), active: 'bg-red-50 text-red-700 border-red-200', inactive: 'bg-white text-zinc-500' },
        ].map(opt => (
          <button
            key={opt.value}
            type="button"
            onClick={() => updateBlockConfig(block.id, { ...block.config, status_condition: opt.value })}
            className={clsx(
              'px-3 py-1.5 rounded-lg text-xs font-medium transition-all border',
              (block.config.status_condition || 'any') === opt.value
                ? opt.active
                : `${opt.inactive} border-zinc-200 hover:border-zinc-300`
            )}
          >
            {t(opt.label)}
          </button>
        ))}
      </div>
    </div>
  );
}

function ConditionConfig({ block, blocks, updateBlockConfig }: any) {
  const { t } = useTranslation();
  const allWorkflows = useFlowState((s) => s.workflows, shallow);
  const allSessions = useFlowState((s) => s.sessions, shallow);
  const [isCustomField, setIsCustomField] = React.useState(block.config._isCustom || false);

  // Walk ancestors to find data available at this point in the flow
  const { flowOutputs, systemFields } = React.useMemo(() => {
    const flowOutputs: Array<{ field: string; label: string; source: string; color: string }> = [];
    const systemFields: Array<{ group: string; groupColor: string; items: Array<{ field: string; label: string; hint?: string }> }> = [];
    const ancestors = getAncestorChain(blocks, block.id);

    const hasChangeDetected = ancestors.some((b: FlowBlock) => b.blockType === 'change_detected');
    const hasWebhook = ancestors.some((b: FlowBlock) => b.blockType === 'webhook_received');

    if (hasChangeDetected) {
      const sourceBlock = ancestors.find((b: FlowBlock) => b.blockType === 'change_detected');
      const configSelectors = sourceBlock?.config?.selectors || [];
      for (const sel of configSelectors) {
        if (sel.name) {
          flowOutputs.push({ field: `extracted.${sel.name.toLowerCase().replace(/\s+/g, '_')}`, label: sel.name, source: t('Content Monitor'), color: 'blue' });
        }
      }

      systemFields.push({
        group: t('Content Change'),
        groupColor: 'text-ink',
        items: [
          { field: 'content', label: t('Full page content') },
          { field: 'diff_snippet', label: t('What changed (diff)') },
          { field: 'selector_name', label: t('Changed selector name') },
          { field: 'extracted.*', label: t('Any extracted field'), hint: t('type the key after extracted.') },
        ],
      });
    }

    if (hasWebhook) {
      systemFields.push({
        group: t('Webhook Payload'),
        groupColor: 'text-secondary',
        items: [
          { field: 'payload.*', label: t('Payload field'), hint: t('type the key after payload.') },
        ],
      });
    }

    const resolvedWfIds = new Set<number>();
    const resolvedSessIds = new Set<number>();
    let hasWorkflow = false;
    let hasAiSession = false;

    for (const ab of ancestors) {
      if (ab.blockType === 'workflow' || ab.blockType === 'workflow_completed' || ab.blockType === 'workflow_started') {
        hasWorkflow = true;
        const wid = ab.config?.workflow_id;
        if (wid && !resolvedWfIds.has(wid)) {
          resolvedWfIds.add(wid);
          const wf = allWorkflows.find((w: any) => w.id === wid);
          if (wf?.outputs?.length) {
            for (const o of wf.outputs) {
              flowOutputs.push({ field: `result.${o.key}`, label: o.label || o.key, source: wf.name, color: 'green' });
            }
          }
        }
      }

      if (ab.blockType === 'ai_session' || ab.blockType === 'ai_session_completed' || ab.blockType === 'ai_session_started') {
        hasAiSession = true;
        const sid = ab.config?.ai_session_id || ab.config?.session_ids?.[0];
        if (sid && !resolvedSessIds.has(sid)) {
          resolvedSessIds.add(sid);
          const sess = allSessions.find((s: any) => s.id === sid);
          if (sess?.outputs?.length) {
            for (const o of sess.outputs) {
              flowOutputs.push({ field: `ai_result.${o.key}`, label: o.label || o.key, source: sess.name, color: 'purple' });
            }
          }
        }
      }
    }

    if (hasWorkflow) {
      systemFields.push({
        group: t('Workflow'),
        groupColor: 'text-ink',
        items: [
          { field: 'success', label: t('Success (true/false)') },
          { field: 'status', label: t('Status') },
          { field: 'error', label: t('Error message') },
          { field: 'workflow_duration_seconds', label: t('Duration (seconds)') },
          { field: 'consecutive_failures', label: t('Failure streak') },
          { field: 'total_failure_count', label: t('Total failures') },
          { field: 'total_run_count', label: t('Total runs') },
          { field: 'failure_rate', label: t('Failure rate (%)') },
          { field: 'result.*', label: t('Any result field'), hint: t('type the key') },
        ],
      });
    }

    if (hasAiSession) {
      systemFields.push({
        group: t('AI Session'),
        groupColor: 'text-ink',
        items: [
          { field: 'success', label: t('Success (true/false)') },
          { field: 'status', label: t('Status') },
          { field: 'error', label: t('Error message') },
          { field: 'session_steps_taken', label: t('Steps taken') },
          { field: 'session_duration_seconds', label: t('Duration (seconds)') },
          { field: 'ai_result.*', label: t('Any AI result field'), hint: t('type the key') },
        ],
      });
    }

    return { flowOutputs, systemFields };
  }, [blocks, block.id, allWorkflows, allSessions, t]);

  const currentField = block.config.field || '';
  const isInputField = /^input\./i.test(currentField);

  const handleFieldSelect = (field: string) => {
    if (field.endsWith('.*')) {
      const prefix = field.replace('*', '');
      setIsCustomField(true);
      updateBlockConfig(block.id, { ...block.config, field: prefix, _isCustom: true });
    } else {
      setIsCustomField(false);
      updateBlockConfig(block.id, { ...block.config, field, _isCustom: false });
    }
  };

  const handleCustomToggle = () => {
    const next = !isCustomField;
    setIsCustomField(next);
    if (next) {
      updateBlockConfig(block.id, { ...block.config, field: '', _isCustom: true });
    } else {
      updateBlockConfig(block.id, { ...block.config, _isCustom: false });
    }
  };

  return (
    <div className="space-y-3">
      {/* Field selection */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <label className="text-xs font-medium text-secondary">{t('Field')}</label>
          <button type="button" onClick={handleCustomToggle}
            className={clsx('text-[10px] px-1.5 py-0.5 rounded transition-colors',
              isCustomField ? 'bg-ink text-white' : 'text-tertiary hover:text-ink bg-hover'
            )}
          >
            {isCustomField ? t('Pick from list') : t('Custom path')}
          </button>
        </div>

        {isCustomField ? (
          <div className="space-y-1">
            <div className="flex items-center gap-1.5">
              <input
                type="text"
                value={currentField}
                onChange={e => updateBlockConfig(block.id, { ...block.config, field: e.target.value })}
                placeholder={t('e.g., extracted.my_field, payload.user.email, result.data')}
                className={clsx(
                  'flex-1 px-2.5 py-2 bg-canvas border rounded-lg text-sm text-ink font-mono',
                  isInputField ? 'border-red-200' : 'border-border'
                )}
                autoFocus
              />
              <FieldRef
                blockId={block.id}
                onInsert={token => updateBlockConfig(block.id, { ...block.config, field: token.replace(/^\{\{|\}\}$/g, '') })}
              />
            </div>
            {isInputField && (
              <p className="text-[11px] text-red-800 flex items-center gap-1">
                <ExclamationTriangleIcon className="h-3 w-3" />
                {t('Input fields are not available in conditions. Use')} <code className="font-mono">result.*</code> {t('for output data.')}
              </p>
            )}
          </div>
        ) : (
          <>
            {!currentField && (flowOutputs.length > 0 || systemFields.length > 0) && (
              <div className="space-y-3">
                {/* Flow outputs — prominent section at top */}
                {flowOutputs.length > 0 && (
                  <div className="rounded-lg border border-border bg-canvas/50 p-2.5 space-y-2">
                    <div className="flex items-center gap-1.5">
                      <div className="w-1.5 h-1.5 rounded-full bg-hover" />
                      <span className="text-[10px] font-semibold uppercase tracking-wider text-secondary">{t('Flow output data')}</span>
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {flowOutputs.map((item, i) => {
                        const borderColor = item.color === 'green' ? 'border-border hover:border-border-strong'
                          : item.color === 'purple' ? 'border-border hover:border-border-strong'
                          : 'border-border hover:border-border-strong';
                        const bgColor = item.color === 'green' ? 'bg-hover'
                          : item.color === 'purple' ? 'bg-hover'
                          : 'bg-hover';
                        const codeColor = item.color === 'green' ? 'text-ink'
                          : item.color === 'purple' ? 'text-ink'
                          : 'text-ink';
                        return (
                          <button
                            key={i}
                            type="button"
                            onClick={() => handleFieldSelect(item.field)}
                            className={clsx('px-2.5 py-1.5 rounded-lg border text-xs font-medium transition-all', borderColor, bgColor)}
                            title={t('from {{source}}', { source: item.source })}
                          >
                            <code className={clsx('text-[11px] font-semibold', codeColor)}>{item.field}</code>
                            <span className="text-secondary ml-1.5">{t(item.label)}</span>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}

                {/* System / generic fields */}
                {systemFields.length > 0 && (
                  <div className="space-y-2">
                    {flowOutputs.length > 0 && (
                      <div className="text-[10px] text-tertiary uppercase tracking-wider font-medium">{t('System fields')}</div>
                    )}
                    {systemFields.map((group, gi) => (
                      <div key={gi}>
                        <div className={clsx('text-[10px] font-medium mb-1', group.groupColor)}>{t(group.group)}</div>
                        <div className="flex flex-wrap gap-1">
                          {group.items.map((item, ii) => (
                            <button
                              key={ii}
                              type="button"
                              onClick={() => handleFieldSelect(item.field)}
                              className="px-2 py-1 text-xs bg-canvas hover:bg-hover border border-border rounded-lg text-ink transition-colors"
                              title={item.hint && t(item.hint)}
                            >
                              <code className="text-[11px]">{item.field.endsWith('.*') ? item.field.replace('.*', '.') + '…' : item.field}</code>
                              <span className="text-tertiary ml-1">{t(item.label)}</span>
                            </button>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {currentField && (
              <div className="flex items-center gap-2">
                <div className="flex-1 px-2.5 py-2 bg-canvas border border-border rounded-lg text-sm font-mono text-ink">
                  {currentField}
                </div>
                <button type="button" onClick={() => updateBlockConfig(block.id, { ...block.config, field: '' })}
                  className="text-xs text-tertiary hover:text-ink px-2 py-1"
                >
                  {t('Change')}
                </button>
              </div>
            )}

            {flowOutputs.length === 0 && systemFields.length === 0 && (
              <p className="text-xs text-tertiary italic">{t('No data sources detected in this flow. Add a source block first, or use a custom field path.')}</p>
            )}
          </>
        )}
      </div>

      {/* Operator + Value */}
      {currentField && !isInputField && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <Select
              value={block.config.operator || 'contains'}
              onChange={v => updateBlockConfig(block.id, { ...block.config, operator: v })}
              size="sm"
              className="w-40"
              options={[
                { label: t('Text'), options: [
                  { value: 'contains', label: t('contains') },
                  { value: 'not_contains', label: t('does not contain') },
                  { value: 'equals', label: t('equals exactly') },
                  { value: 'not_equals', label: t('does not equal') },
                  { value: 'matches', label: t('matches regex') },
                ]},
                { label: t('Numbers'), options: [
                  { value: 'gt', label: t('greater than') },
                  { value: 'gte', label: t('at least') },
                  { value: 'lt', label: t('less than') },
                  { value: 'lte', label: t('at most') },
                ]},
                { label: t('Special'), options: [
                  { value: 'changed', label: t('has changed') },
                  { value: 'exists', label: t('exists (not empty)') },
                ]},
              ]}
            />
            {!['changed', 'exists'].includes(block.config.operator || '') && (
              <>
                <input
                  type="text"
                  value={block.config.value || ''}
                  onChange={e => updateBlockConfig(block.id, { ...block.config, value: e.target.value })}
                  placeholder={t('value')}
                  className="flex-1 px-2 py-1.5 bg-surface border border-border rounded-lg text-sm text-ink"
                />
                <FieldRef
                  blockId={block.id}
                  onInsert={token => updateBlockConfig(block.id, { ...block.config, value: `${block.config.value || ''}${token}` })}
                />
              </>
            )}
          </div>
        </div>
      )}

      {/* Live preview of the condition */}
      {currentField && !isInputField && (
        <div className="text-xs bg-canvas px-3 py-2 rounded-lg border border-border flex items-center gap-1.5">
          <span className="text-tertiary">{t('If')}</span>
          <code className="text-secondary font-medium">{currentField}</code>
          <span className="text-secondary">{block.config.operator || 'contains'}</span>
          {block.config.value && <code className="text-secondary">"{block.config.value}"</code>}
        </div>
      )}
    </div>
  );
}

function NotificationConfig({ block, blocks, recipients, updateBlockConfig, expandedAdvancedBlocks, dispatch }: any) {
  const { t } = useTranslation();
  const showAdvanced = expandedAdvancedBlocks.has(block.id);
  const hasChangeDetected = blocks.some((b: FlowBlock) => b.blockType === 'change_detected');
  const hasAiSession = blocks.some((b: FlowBlock) => b.blockType === 'ai_session' || b.blockType === 'ai_session_completed');
  const hasWorkflow = blocks.some((b: FlowBlock) => b.blockType === 'workflow' || b.blockType === 'workflow_completed');

  const CHANNEL_ICONS: Record<string, React.FC<{ className?: string }>> = {
    webhook: LinkIcon, desktop: ComputerDesktopIcon, in_app: BellIcon,
    email: EnvelopeIcon, pushover: DevicePhoneMobileIcon, twilio: ChatBubbleBottomCenterTextIcon,
    whatsapp: PhoneIcon, signal: LockClosedIcon,
  };
  const CHANNEL_LABELS: Record<string, string> = {
    webhook: t('Webhook'), desktop: t('Desktop notification'), in_app: t('In-app alert'),
    email: t('Email'), pushover: 'Pushover', twilio: t('SMS'), whatsapp: 'WhatsApp', signal: 'Signal',
  };
  // Which channels this deployment can ACTUALLY deliver on right now. A channel
  // whose provider was never set up drops the notification silently at send
  // time, so offering it here as a working choice builds an automation that
  // quietly does nothing. Read-only + silent: a failed probe must never block
  // editing the block, it just leaves every channel ungated (see below).
  const { data: notifPrefs } = useQuery<NotificationPreferences>(
    'notifications:preferences',
    () => getNotificationPreferences(),
    { silent: true, staleTime: 60_000 },
  );
  // Delivered by the coordinator itself — nothing to configure, never gated.
  const SELF_DELIVERED = new Set(['webhook', 'desktop', 'in_app']);
  // The block's channel id vs the notification catalog's. `twilio` and `sms` are
  // the same Twilio credentials under two names; every other id matches.
  const AVAILABILITY_KEY: Record<string, string> = { twilio: 'sms' };
  const availability = notifPrefs?.channel_availability;
  const isConfigured = (key: string): boolean => {
    if (SELF_DELIVERED.has(key)) return true;
    // Unknown (still loading, or the probe failed): do NOT gate. Showing every
    // channel disabled for a moment on open reads as "notifications are broken".
    if (!availability) return true;
    return !!availability[AVAILABILITY_KEY[key] ?? key];
  };

  // Drive the channel list from the catalog capability profile: local-capable
  // channels (webhook/desktop/in_app) are always enabled; cloud channels render
  // disabled on desktop with a "cloud" badge + link, enabled on the cloud app.
  const channels = NOTIFICATION_CHANNELS.map((c) => ({
    key: c.value,
    label: CHANNEL_LABELS[c.value] || c.value,
    icon: CHANNEL_ICONS[c.value] || BellIcon,
    enabled: c.platforms.includes(APP_PLATFORM),
    configured: isConfigured(c.value),
    cloudOnlyReason: c.cloudOnlyReason,
  }));

  const selectedChannels = (block.config.channels || []) as string[];
  const selectedRecipients = (block.config.recipients || []) as string[];

  // Which unconfigured channel the user just clicked — the chip is inert, so the
  // click's whole job is to explain WHY, and where to fix it.
  const [hintChannel, setHintChannel] = React.useState<string | null>(null);
  // Already-saved channels whose provider has since gone away (or was never set
  // up). Unlike the click-hint this is shown unprompted: the automation is
  // currently configured to deliver somewhere that cannot receive.
  const brokenSelected = selectedChannels.filter((c) => !isConfigured(c));

  return (
    <div className="space-y-3">
      <div>
        <div className="flex items-center justify-between mb-1">
          <label className="text-xs text-secondary">{t('Title (optional)')}</label>
          <FieldRef
            blockId={block.id}
            onInsert={token => updateBlockConfig(block.id, { ...block.config, title: `${block.config.title || ''}${token}` })}
          />
        </div>
        <input
          type="text"
          value={block.config.title || ''}
          onChange={e => updateBlockConfig(block.id, { ...block.config, title: e.target.value })}
          placeholder={t('Title (optional)')}
          className={`w-full px-2 py-1.5 bg-surface border border-border rounded text-sm text-ink${boundInputClass(block.config.title)}`}
        />
      </div>
      <div>
        <div className="flex items-center justify-end mb-1">
          <FieldRef
            blockId={block.id}
            onInsert={token => updateBlockConfig(block.id, { ...block.config, template: `${block.config.template || ''}${token}` })}
          />
        </div>
        <textarea
          value={block.config.template || ''}
          onChange={e => updateBlockConfig(block.id, { ...block.config, template: e.target.value })}
          placeholder={t('Message template with {{ph}}...', { ph: '{{placeholders}}' })}
          className="w-full px-2 py-1.5 bg-surface border border-border rounded text-sm text-ink resize-none"
          rows={2}
        />
      </div>
      <div className="text-[10px] text-tertiary flex flex-wrap gap-x-2 gap-y-1">
        <span className="text-secondary">{t('Placeholders:')}</span>
        <code className="text-ink">{'{{now_time}}'}</code>
        <code className="text-ink">{'{{target_name}}'}</code>
        {hasChangeDetected && <code className="text-ink">{'{{extracted.*}}'}</code>}
        {hasAiSession && <code className="text-ink">{'{{session_status}}'}</code>}
        {hasWorkflow && <code className="text-ink">{'{{workflow_status}}'}</code>}
        {hasWorkflow && <code className="text-ink">{'{{consecutive_failures}}'}</code>}
        {hasWorkflow && <code className="text-ink">{'{{failure_rate}}'}</code>}
      </div>

      <div>
        <label className="text-xs font-medium text-secondary mb-2 block">{t('Channels')}</label>
        <div className="flex flex-wrap gap-1.5">
          {channels.map(channel => {
            const isSelected = selectedChannels.includes(channel.key);
            if (!channel.enabled) {
              // Channel not available in this build: render disabled with a badge + link.
              return (
                <span
                  key={channel.key}
                  title={channel.cloudOnlyReason ? t(channel.cloudOnlyReason) : t('Available in the cloud')}
                  className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-border bg-canvas text-tertiary text-xs font-medium opacity-60 cursor-not-allowed"
                >
                  <channel.icon className="h-3.5 w-3.5 shrink-0" />
                  <span>{t(channel.label)}</span>
                  <Link
                    to="/cloud"
                    onClick={e => e.stopPropagation()}
                    className="flex items-center gap-0.5 px-1 py-0.5 rounded bg-hover text-[9px] uppercase tracking-wide text-secondary hover:text-ink pointer-events-auto"
                  >
                    <CloudIcon className="h-2.5 w-2.5" />
                    {t('cloud')}
                  </Link>
                </span>
              );
            }
            // Provider never configured: the chip stays visible (so the channel
            // is discoverable) but cannot be picked. Clicking it is the ONLY
            // affordance, and it explains where to set the provider up. An
            // ALREADY-SELECTED channel stays interactive even when unconfigured,
            // or the user could never remove it.
            if (!channel.configured && !isSelected) {
              return (
                <button
                  key={channel.key}
                  type="button"
                  aria-disabled
                  onClick={() => setHintChannel(h => (h === channel.key ? null : channel.key))}
                  title={t('Not configured — click to see how to enable it')}
                  className={clsx(
                    'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-dashed text-xs font-medium transition',
                    hintChannel === channel.key
                      ? 'border-secondary text-secondary bg-hover'
                      : 'border-border bg-canvas text-tertiary opacity-70 hover:opacity-100',
                  )}
                >
                  <channel.icon className="h-3.5 w-3.5 shrink-0" />
                  <span>{t(channel.label)}</span>
                  <Cog6ToothIcon className="h-3 w-3 shrink-0" />
                </button>
              );
            }
            return (
              <button
                key={channel.key}
                type="button"
                onClick={() => {
                  const newChannels = isSelected
                    ? selectedChannels.filter(c => c !== channel.key)
                    : [...selectedChannels, channel.key];
                  updateBlockConfig(block.id, { ...block.config, channels: newChannels });
                }}
                className={clsx(
                  'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border transition text-xs font-medium',
                  isSelected && !channel.configured
                    ? 'border-amber-500/40 bg-amber-500/10 text-ink'
                    : isSelected
                      ? 'border-ink/20 bg-ink/5 text-ink'
                      : 'border-border bg-canvas text-tertiary hover:border-secondary hover:text-secondary'
                )}
              >
                <channel.icon className="h-3.5 w-3.5 shrink-0" />
                <span>{t(channel.label)}</span>
                {isSelected && !channel.configured
                  ? <ExclamationTriangleIcon className="h-3 w-3 text-amber-600" />
                  : isSelected && <CheckIcon className="h-3 w-3" />}
              </button>
            );
          })}
        </div>

        {/* The click-hint for an unconfigured channel. */}
        {hintChannel && (
          <p className="mt-2 text-[11px] text-tertiary leading-relaxed">
            {t('{{channel}} has no provider set up yet, so it can’t deliver.', {
              channel: CHANNEL_LABELS[hintChannel] || hintChannel,
            })}{' '}
            <Link
              to="/settings?tab=notifications"
              className="text-ink underline decoration-border hover:decoration-secondary"
            >
              {t('Configure it in Settings → Notifications')}
            </Link>
          </p>
        )}

        {/* Saved-but-undeliverable channels, surfaced without needing a click. */}
        {brokenSelected.length > 0 && (
          <p className="mt-2 flex items-start gap-1.5 text-[11px] text-amber-600 leading-relaxed">
            <ExclamationTriangleIcon className="h-3.5 w-3.5 shrink-0 mt-px" />
            <span>
              {t('{{channels}} is selected but has no provider set up — this notification won’t be delivered there.', {
                channels: brokenSelected.map((c) => CHANNEL_LABELS[c] || c).join(', '),
              })}{' '}
              <Link
                to="/settings?tab=notifications"
                className="underline decoration-amber-600/40 hover:decoration-amber-600"
              >
                {t('Configure it in Settings → Notifications')}
              </Link>
            </span>
          </p>
        )}
      </div>

      {selectedChannels.includes('webhook') && (
        <div>
          <div className="flex items-center justify-between mb-1">
            <label className="block text-xs text-tertiary">{t('Webhook URL')}</label>
            <FieldRef
              blockId={block.id}
              onInsert={token => updateBlockConfig(block.id, { ...block.config, webhook_url: `${block.config.webhook_url || ''}${token}` })}
            />
          </div>
          <input
            type="url"
            value={block.config.webhook_url || ''}
            onChange={e => updateBlockConfig(block.id, { ...block.config, webhook_url: e.target.value })}
            placeholder="https://..."
            className={`w-full px-2 py-1.5 bg-surface border border-border rounded text-sm text-ink${boundInputClass(block.config.webhook_url)}`}
          />
        </div>
      )}

      {selectedChannels.some(c => ['pushover', 'email', 'twilio', 'whatsapp', 'signal', 'desktop', 'in_app'].includes(c)) && (
        <RecipientsSelector
          block={block}
          selectedChannels={selectedChannels}
          selectedRecipients={selectedRecipients}
          recipients={recipients}
          updateBlockConfig={updateBlockConfig}
        />
      )}

      <button
        type="button"
        onClick={() => dispatch({ type: 'TOGGLE_ADVANCED', blockId: block.id })}
        className="text-xs text-tertiary hover:text-ink flex items-center gap-1"
      >
        <ChevronDownIcon className={clsx('h-3 w-3 transition-transform', showAdvanced && 'rotate-180')} />
        {showAdvanced ? t('Hide advanced') : t('Show advanced')}
      </button>

      {showAdvanced && (
        <div className="space-y-3 p-2 bg-canvas rounded border border-border">
          {selectedChannels.includes('pushover') && (
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="block text-xs text-tertiary mb-1">{t('Priority')}</label>
                <Select<number>
                  value={block.config.priority ?? 0}
                  onChange={v => updateBlockConfig(block.id, { ...block.config, priority: v })}
                  size="sm"
                  className="w-full"
                  options={[
                    { value: -2, label: t('Silent') },
                    { value: -1, label: t('Quiet') },
                    { value: 0, label: t('Normal') },
                    { value: 1, label: t('High') },
                    { value: 2, label: t('Emergency') },
                  ]}
                />
              </div>
              <div>
                <label className="block text-xs text-tertiary mb-1">{t('Sound')}</label>
                <Select
                  value={block.config.sound || ''}
                  onChange={v => updateBlockConfig(block.id, { ...block.config, sound: v })}
                  size="sm"
                  className="w-full"
                  options={[
                    { value: '', label: t('Default') },
                    { value: 'cashregister', label: t('Cash Register') },
                    { value: 'magic', label: t('Magic') },
                    { value: 'siren', label: t('Siren') },
                    { value: 'none', label: t('None') },
                  ]}
                />
              </div>
            </div>
          )}
          {selectedChannels.includes('email') && (
            <div>
              <label className="block text-xs text-tertiary mb-1">{t('Email Subject')}</label>
              <input type="text" value={block.config.email_subject || ''} onChange={e => updateBlockConfig(block.id, { ...block.config, email_subject: e.target.value })} placeholder={t('e.g., [Writ] {{tn}} changed', { tn: '{{target_name}}' })} className="w-full px-2 py-1 bg-surface border border-border rounded text-xs text-ink" />
            </div>
          )}
        </div>
      )}

      <BlockPlaceholderHints block={block} blocks={blocks} />
    </div>
  );
}

function RecipientsSelector({ block, selectedChannels, selectedRecipients, recipients, updateBlockConfig }: any) {
  const { t } = useTranslation();
  const channelsWithRecipients = selectedChannels.filter((ch: string) =>
    recipients.some((r: any) => r.provider === ch)
  );
  const totalAvailable = recipients.filter((r: any) => selectedChannels.includes(r.provider)).length;

  return (
    <div className="space-y-2 p-2 bg-canvas rounded-lg border border-border">
      <div className="flex items-center justify-between">
        <label className="text-xs font-medium text-ink flex items-center gap-1.5">
          <UserGroupIcon className="h-4 w-4 text-secondary" />
          {t('Recipients')}
          {selectedRecipients.length > 0 && (
            <span className="px-1.5 py-0.5 rounded-full bg-hover text-ink text-[10px]">{t('{{n}} selected', { n: selectedRecipients.length })}</span>
          )}
        </label>
        <button type="button" onClick={() => {
          const all = recipients.filter((r: any) => selectedChannels.includes(r.provider)).map((r: any) => `${r.provider}:${r.id}`);
          updateBlockConfig(block.id, { ...block.config, recipients: all });
        }} className="text-[10px] text-ink">{t('Select All ({{n}})', { n: totalAvailable })}</button>
      </div>

      {channelsWithRecipients.length === 0 ? (
        <p className="text-[10px] text-tertiary italic py-2">{t('No recipients configured. Add them in Integrations.')}</p>
      ) : (
        <div className="space-y-2">
          {channelsWithRecipients.map((channel: string) => {
            const channelRecipients = recipients.filter((r: any) => r.provider === channel);
            return (
              <div key={channel} className="flex flex-wrap gap-1 pl-1">
                {channelRecipients.map((recipient: any) => {
                  const key = `${recipient.provider}:${recipient.id}`;
                  const isSelected = selectedRecipients.includes(key);
                  return (
                    <button key={key} type="button"
                      onClick={() => {
                        const next = isSelected ? selectedRecipients.filter((r: string) => r !== key) : [...selectedRecipients, key];
                        updateBlockConfig(block.id, { ...block.config, recipients: next });
                      }}
                      className={clsx(
                        'flex items-center gap-1.5 px-2 py-1 rounded border transition text-[11px]',
                        isSelected ? 'border-ink/20 bg-ink/5 text-ink' : 'border-border bg-surface text-tertiary hover:text-secondary'
                      )}
                    >
                      {isSelected && <CheckIcon className="h-3 w-3" />}
                      <span>{recipient.name}</span>
                    </button>
                  );
                })}
              </div>
            );
          })}
        </div>
      )}

      {selectedRecipients.length === 0 && totalAvailable > 0 && (
        <p className="text-[10px] text-secondary flex items-center gap-1 pt-1">
          <ExclamationTriangleIcon className="h-3 w-3" />
          {t('No recipients selected — will notify all enabled')}
        </p>
      )}
    </div>
  );
}

function AISessionConfig({ block, blocks, sessions: _sessions, updateBlockConfig }: any) {
  const { t } = useTranslation();
  // GOAL-shaped, not id-shaped. The cloud build picks a SAVED AI-session recipe by
  // id; self-host has no recipe table — its `ai_sessions` rows are RUN RECORDS and
  // `POST /ai-sessions/start` takes the goal inline. The old dropdown listed
  // `aiSessionsApi.listAll()`, which was hardcoded to `[]`, so it could never be
  // filled in: the block was addable and permanently unconfigurable.
  const hasChangeDetected = blocks.some((b: FlowBlock) => b.blockType === 'change_detected');
  const hasGoal = !!(block.config.goal || '').trim();

  return (
    <div className="space-y-3">
      {/* Disambiguation: this block is the AUTONOMOUS goal-runner, not Scribe chat. */}
      <p className="text-[11px] leading-relaxed text-tertiary">
        {t('An autonomous browser agent works toward a goal on its own, then saves its steps as a replayable workflow — not Scribe, the chat assistant.')}
      </p>

      <div>
        <div className="mb-1 flex items-center justify-between">
          <label className="block text-xs font-medium text-secondary">{t('Goal')}</label>
          <FieldRef
            blockId={block.id}
            onInsert={token => updateBlockConfig(block.id, { ...block.config, goal: `${block.config.goal || ''}${token}` })}
          />
        </div>
        <textarea
          value={block.config.goal || ''}
          onChange={e => updateBlockConfig(block.id, { ...block.config, goal: e.target.value })}
          placeholder={t('What should the agent accomplish? e.g. Find the cheapest plan and record the steps.')}
          className={`w-full px-3 py-2 bg-surface border border-border rounded-lg text-sm text-ink resize-none focus:border-border-strong outline-none transition-colors${boundInputClass(block.config.goal)}`}
          rows={2}
        />
      </div>

      <div>
        <label className="mb-1 block text-xs font-medium text-secondary">{t('Start URL (optional)')}</label>
        <input
          type="text"
          value={block.config.entry_url || ''}
          onChange={e => updateBlockConfig(block.id, { ...block.config, entry_url: e.target.value })}
          placeholder="https://..."
          className={`w-full px-3 py-2 bg-surface border border-border rounded-lg text-sm text-ink focus:border-border-strong outline-none transition-colors${boundInputClass(block.config.entry_url)}`}
        />
      </div>

      <div className="flex items-center gap-3">
        <div className="flex-1">
          <label className="mb-1 block text-xs font-medium text-secondary">{t('Max steps')}</label>
          <input
            type="number"
            min={1}
            value={block.config.max_steps ?? 20}
            onChange={e => updateBlockConfig(block.id, { ...block.config, max_steps: Number(e.target.value) || 20 })}
            className="w-full px-3 py-2 bg-surface border border-border rounded-lg text-sm text-ink focus:border-border-strong outline-none transition-colors"
          />
        </div>
        <label className="mt-4 flex flex-1 items-center gap-2 text-xs text-secondary">
          <input
            type="checkbox"
            checked={block.config.generate_workflow !== false}
            onChange={e => updateBlockConfig(block.id, { ...block.config, generate_workflow: e.target.checked })}
          />
          {t('Save what it did as a workflow')}
        </label>
      </div>

      <div className="flex items-center gap-1.5 text-[11px] text-tertiary">
        <CpuChipIcon className="h-3 w-3" />
        <span>{t('Runs on a connected fleet agent with local AI. It is picked automatically.')}</span>
      </div>

      {/* Context override */}
      {hasGoal && (
        <div>
          <div className="flex items-center justify-between mb-1">
            <label className="block text-xs font-medium text-secondary">{t('Context override')}</label>
            <FieldRef
              blockId={block.id}
              onInsert={token => updateBlockConfig(block.id, { ...block.config, user_context: `${block.config.user_context || ''}${token}` })}
            />
          </div>
          <textarea
            value={block.config.user_context || ''}
            onChange={e => updateBlockConfig(block.id, { ...block.config, user_context: e.target.value })}
            placeholder={hasChangeDetected ? t('e.g., Price changed to {{ph}}', { ph: '{{extracted.price}}' }) : t('Extra context for AI (optional)')}
            className={`w-full px-3 py-2 bg-surface border border-border rounded-lg text-sm text-ink resize-none focus:border-border-strong outline-none transition-colors${boundInputClass(block.config.user_context)}`}
            rows={2}
          />
        </div>
      )}

      {/* Form Data */}
      {hasGoal && (
        <div>
          <label className="block text-xs font-medium text-secondary mb-1">{t('Form Data')}</label>
          <div className="space-y-1">
            {Object.entries(block.config.form_data || {}).map(([key, value], i) => (
              <div key={i} className="flex gap-1">
                <input type="text" value={key} onChange={e => {
                  const d = { ...block.config.form_data }; delete d[key]; d[e.target.value] = value;
                  updateBlockConfig(block.id, { ...block.config, form_data: d });
                }} placeholder={t('Field')} className="flex-1 px-2 py-1 bg-surface border border-border rounded text-xs text-ink" />
                <input type="text" value={value as string} onChange={e => {
                  updateBlockConfig(block.id, { ...block.config, form_data: { ...block.config.form_data, [key]: e.target.value } });
                }} placeholder={t('Value or {{ph}}', { ph: '{{placeholder}}' })} className={`flex-1 px-2 py-1 bg-surface border border-border rounded text-xs text-ink${boundInputClass(value)}`} />
                <FieldRef
                  blockId={block.id}
                  onInsert={token => updateBlockConfig(block.id, { ...block.config, form_data: { ...block.config.form_data, [key]: `${(value as string) || ''}${token}` } })}
                />
                <button type="button" onClick={() => {
                  const d = { ...block.config.form_data }; delete d[key];
                  updateBlockConfig(block.id, { ...block.config, form_data: d });
                }} className="px-2 text-red-800 hover:text-red-800"><TrashIcon className="h-3 w-3" /></button>
              </div>
            ))}
            <button type="button" onClick={() => {
              const d = { ...block.config.form_data, [`field_${Object.keys(block.config.form_data || {}).length + 1}`]: '' };
              updateBlockConfig(block.id, { ...block.config, form_data: d });
            }} className="text-xs text-ink flex items-center gap-1">
              <PlusIcon className="h-3 w-3" /> {t('Add Field')}
            </button>
          </div>
        </div>
      )}

    </div>
  );
}

function WorkflowConfig({ block, blocks, workflows, updateBlockConfig }: any) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  // Non-reactive: the draft payload is read at click time, so this component
  // must not subscribe to the flow meta just to be able to save it.
  const store = useFlowStore();

  const selectedWorkflow = block.config.workflow_id
    ? workflows.find((w: any) => w.id === block.config.workflow_id)
    : null;

  const rootSource = blocks.find((b: FlowBlock) => b.type === 'event' && !b.parentId);
  const isWebhookFlow = rootSource?.blockType === 'webhook_received';
  const isContentFlow = rootSource?.blockType === 'change_detected';

  const placeholders: Array<{ key: string; label: string }> = selectedWorkflow?.placeholders || [];

  // Auto-prefill input_mapping
  const prevWorkflowRef = React.useRef<number | null>(null);
  React.useEffect(() => {
    if (!selectedWorkflow || selectedWorkflow.id === prevWorkflowRef.current) return;
    prevWorkflowRef.current = selectedWorkflow.id;
    if (placeholders.length === 0) return;
    if (block.config.input_mapping && Object.keys(block.config.input_mapping).length > 0) return;
    const prefilled: Record<string, string> = {};
    for (const ph of placeholders) {
      if (isWebhookFlow) prefilled[ph.key] = `{{payload.${ph.key}}}`;
      else if (isContentFlow) prefilled[ph.key] = `{{extracted.${ph.key}}}`;
    }
    if (Object.keys(prefilled).length > 0) {
      updateBlockConfig(block.id, { ...block.config, input_mapping: prefilled });
    }
  }, [selectedWorkflow, placeholders, isWebhookFlow, isContentFlow]);

  const inputMapping = block.config.input_mapping || {};
  const updateMapping = (key: string, value: string) => {
    updateBlockConfig(block.id, { ...block.config, input_mapping: { ...inputMapping, [key]: value } });
  };
  const placeholderHint = isWebhookFlow ? '{{payload.key}}' : isContentFlow ? '{{extracted.key}}' : '{{key}} or fixed value';

  // "Create a new workflow" hands off to the unified creation wizard instead of
  // embedding a recorder in the block: the wizard is the only surface that covers a
  // full workflow (steps, secure data, execution target, streaming). The automation
  // draft is stashed first so the round-trip doesn't drop unsaved work — see
  // flowDraft.ts and AutomationBuilderPage's resume path.
  const handleCreateNew = () => {
    saveFlowDraft({
      returnTo: location.pathname,
      pendingBlockId: block.id,
      pendingField: 'workflow_id',
      ...(({ flowId, name, description, enabled, blocks: allBlocks }) =>
        ({ flowId, name, description, enabled, blocks: allBlocks }))(store.getState().state),
    });
    navigate('/workflows/new?intent=automation_action');
  };

  return (
    <div className="space-y-3">
      <label className="block text-xs font-medium text-secondary">{t('Workflow')}</label>

      {workflows.length > 0 && (
        <>
          <Select<number>
            value={block.config.workflow_id || undefined}
            onChange={v => {
              const wid = v || null;
              const wf = workflows.find((w: any) => w.id === wid);
              updateBlockConfig(block.id, { ...block.config, workflow_id: wid, input_mapping: {}, workflow_has_login: !!wf?.has_login });
            }}
            placeholder={t('Select workflow...')}
            options={workflows.map((w: any) => ({
              value: Number(w.id),
              label: w.is_installed ? `${w.name} · ${t('Installed')}` : w.name,
            }))}
            className="w-full"
          />

          {/* Installed-recipe note — the consumer CAN automate their proxy, but it's read-only. */}
          {selectedWorkflow?.is_installed && (
            <div className="flex items-center gap-1.5 text-[11px] text-secondary bg-canvas border border-border/70 rounded-lg px-2.5 py-1.5">
              <LockClosedIcon className="w-3 h-3 shrink-0 text-tertiary" />
              <span>
                {t('Installed recipe · by {{name}}', { name: selectedWorkflow.creator_name || t('creator') })}
              </span>
            </div>
          )}

          {/* Run-as persona (optional per-action override) — only if the workflow logs in */}
          {block.config.workflow_id && (selectedWorkflow?.has_login || block.config.persona_id) && (
            <div className="pt-1">
              <PersonaPicker
                value={block.config.persona_id ?? null}
                domain={(() => { try { return selectedWorkflow?.entry_url ? new URL(selectedWorkflow.entry_url).hostname : undefined; } catch { return undefined; } })()}
                onChange={(pid) => updateBlockConfig(block.id, { ...block.config, persona_id: pid })}
                allowClear
                label={t('Run as persona (optional)')}
              />
            </div>
          )}

          {/* Placeholder data mapping */}
          {placeholders.length > 0 && (
            <div className="space-y-2 pt-1">
              <div className="flex items-center gap-1.5">
                <div className="w-1.5 h-1.5 rounded-full bg-hover" />
                <span className="text-[10px] font-medium uppercase tracking-wider text-tertiary">{t('Input data ({{n}})', { n: placeholders.length })}</span>
              </div>
              <p className="text-[11px] text-tertiary">
                {isWebhookFlow ? t('Pre-filled from webhook payload.') : isContentFlow ? t('Pre-filled from extracted data.') : t('Map flow data or type fixed values.')}
              </p>
              <div className="space-y-1.5">
                {placeholders.map(ph => (
                  <div key={ph.key} className="flex items-center gap-2">
                    <div className="w-28 shrink-0">
                      <code className="text-[11px] text-secondary font-mono truncate block">{ph.key}</code>
                      {ph.label !== ph.key && <div className="text-[10px] text-tertiary truncate">{ph.label}</div>}
                    </div>
                    <span className="text-tertiary text-xs">=</span>
                    <input type="text" value={inputMapping[ph.key] || ''} onChange={e => updateMapping(ph.key, e.target.value)}
                      placeholder={placeholderHint} className={`flex-1 px-2 py-1.5 bg-surface border border-border rounded text-xs text-ink font-mono placeholder:text-tertiary${isBoundValue(inputMapping[ph.key]) ? ' border-l-2 border-l-ink/30' : ''}`} />
                    <FieldRef
                      blockId={block.id}
                      onInsert={token => updateMapping(ph.key, `${inputMapping[ph.key] || ''}${token}`)}
                    />
                  </div>
                ))}
              </div>
            </div>
          )}

          {selectedWorkflow && placeholders.length === 0 && (
            <div className="text-[11px] text-tertiary">{t('No input data needed.')}</div>
          )}

          {/* Output keys */}
          {(selectedWorkflow?.outputs?.length > 0) && (
            <div className="space-y-1.5 pt-1">
              <div className="flex items-center gap-1.5">
                <div className="w-1.5 h-1.5 rounded-full bg-hover" />
                <span className="text-[10px] font-medium uppercase tracking-wider text-tertiary">{t('Output data ({{n}})', { n: selectedWorkflow.outputs.length })}</span>
              </div>
              <div className="flex flex-wrap gap-1">
                {selectedWorkflow.outputs.map((o: any) => (
                  <code key={o.key} className="text-[11px] px-2 py-1 bg-hover text-ink border border-border rounded font-mono">{o.key}</code>
                ))}
              </div>
              <p className="text-[10px] text-tertiary">
                {t('Available as')} <code className="text-ink font-mono">{'{{result.<key>}}'}</code> {t('in downstream blocks.')}
              </p>
            </div>
          )}
        </>
      )}

      {workflows.length === 0 && (
        <p className="text-[11px] text-tertiary">{t('No workflows yet')}</p>
      )}

      {/* Build a new one — leaves for the wizard and comes back with it selected. */}
      <button type="button" onClick={handleCreateNew}
        className="w-full px-4 py-3 border-2 border-dashed border-border rounded-lg hover:border-border-strong hover:bg-hover transition-all group text-center"
      >
        <Cog6ToothIcon className="h-5 w-5 text-tertiary group-hover:text-ink mx-auto mb-1 transition-colors" />
        <div className="text-sm font-medium text-secondary group-hover:text-ink">{t('Create a new workflow')}</div>
        <div className="text-[10px] text-tertiary">{t('Opens the workflow builder, then returns here')}</div>
      </button>

      <ErrorHandlingSection block={block} updateBlockConfig={updateBlockConfig} />
    </div>
  );
}

// Retry / on-error controls for a workflow action (config.retry / retry_backoff_ms / on_error).
// Interpreted identically by the cloud + desktop engines.
function ErrorHandlingSection({ block, updateBlockConfig }: any) {
  const { t } = useTranslation();
  const retry = block.config.retry ?? '';
  const stopOnError = block.config.on_error === 'stop';
  return (
    <div className="mt-3 pt-3 border-t border-border space-y-2">
      <div className="text-[10px] font-medium uppercase tracking-wider text-tertiary">{t('Error handling')}</div>
      <div className="flex items-center gap-2">
        <label className="text-xs text-secondary">{t('Retry on failure')}</label>
        <NumberInput
          min={0}
          value={retry === '' ? null : Number(retry)}
          onChange={(v) => updateBlockConfig(block.id, { ...block.config, retry: v ?? undefined })}
          size="sm"
          className="w-20"
          placeholder="0"
        />
        <span className="text-[11px] text-tertiary">{t('extra attempts')}</span>
      </div>
      <Checkbox
        checked={stopOnError}
        onChange={(e) => updateBlockConfig(block.id, { ...block.config, on_error: e.target.checked ? 'stop' : undefined })}
        label={<span className="text-xs text-secondary">{t('Stop the rest of this chain if it still fails')}</span>}
      />
    </div>
  );
}

// Editor for the `for_each` loop block: which upstream array to iterate. The blocks placed
// BELOW this one run once per item, with {{item}} / {{item_index}} available.
function ForEachConfig({ block, updateBlockConfig }: any) {
  const { t } = useTranslation();
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <label className="block text-xs text-secondary">{t('Repeat for each item in')}</label>
        <FieldRef
          blockId={block.id}
          onInsert={token => updateBlockConfig(block.id, { ...block.config, source: `${block.config.source || ''}${token}` })}
        />
      </div>
      <input
        type="text"
        value={block.config.source || ''}
        onChange={(e) => updateBlockConfig(block.id, { ...block.config, source: e.target.value })}
        placeholder="result.rows"
        className="w-full px-2 py-1.5 bg-surface border border-border rounded-lg text-sm text-ink focus:border-border-strong outline-none font-mono"
      />
      <p className="text-[11px] text-tertiary">{t('Dot-path to an upstream list (e.g. result.rows, extracted.items). Blocks added below run once per item — use {{item}} / {{item.field}} and {{item_index}}.')}</p>
      <div className="flex items-center gap-2 pt-1">
        <label className="text-xs text-secondary">{t('Max iterations')}</label>
        <NumberInput
          min={1}
          value={block.config.max_items ?? null}
          onChange={(v) => updateBlockConfig(block.id, { ...block.config, max_items: v ?? undefined })}
          placeholder="1000"
          size="sm"
          className="w-24"
        />
      </div>
    </div>
  );
}

function CreatePersonaConfig({ block, workflows, updateBlockConfig }: any) {
  const { t } = useTranslation();
  return (
    <div className="space-y-2">
      <p className="text-[11px] text-tertiary">
        {t('After the chosen workflow runs (e.g. an account-creation flow), save its login + session as a reusable persona.')}
      </p>
      <Select<number>
        value={block.config.workflow_id || 0}
        onChange={(v) => updateBlockConfig(block.id, { ...block.config, workflow_id: v === 0 ? null : v })}
        placeholder={t('Use the workflow that triggered this flow')}
        options={[
          { value: 0, label: t('Use the workflow that triggered this flow') },
          ...workflows.map((w: any) => ({ value: Number(w.id), label: w.name })),
        ]}
        className="w-full"
      />
      <input
        type="text"
        value={block.config.name || ''}
        onChange={(e) => updateBlockConfig(block.id, { ...block.config, name: e.target.value })}
        placeholder={t('Persona name (optional)')}
        className="w-full px-3 py-2 bg-surface border border-border rounded-lg text-sm text-ink focus:border-border-strong outline-none transition-colors"
      />
    </div>
  );
}

function ReturnDataConfig({ blocks, workflows }: { blocks: FlowBlock[]; workflows: any[] }) {
  const { t } = useTranslation();
  // Collect output keys from all workflow blocks in the flow
  const allOutputs = React.useMemo(() => {
    const outputs: Array<{ key: string; workflowName: string; stepType: string }> = [];
    for (const b of blocks) {
      if (b.blockType === 'workflow' && b.config?.workflow_id) {
        const wf = workflows.find((w: any) => w.id === b.config.workflow_id);
        if (wf?.outputs) {
          for (const o of wf.outputs) {
            outputs.push({ key: o.key, workflowName: wf.name, stepType: o.step_type });
          }
        }
      }
    }
    return outputs;
  }, [blocks, workflows]);

  return (
    <div className="space-y-3">
      <p className="text-xs text-secondary">
        {t('Data from completed workflows will be returned to the webhook caller as JSON.')}
      </p>
      {allOutputs.length > 0 ? (
        <div className="space-y-1.5">
          <div className="flex items-center gap-1.5">
            <div className="w-1.5 h-1.5 rounded-full bg-hover" />
            <span className="text-[10px] font-medium uppercase tracking-wider text-tertiary">
              {t('Returned keys ({{n}})', { n: allOutputs.length })}
            </span>
          </div>
          <div className="space-y-1">
            {allOutputs.map((o, i) => (
              <div key={i} className="flex items-center gap-2">
                <code className="text-[11px] px-2 py-1 bg-hover text-ink border border-border rounded font-mono">
                  {o.key}
                </code>
                <span className="text-[10px] text-tertiary">{t('from {{name}}', { name: o.workflowName })}</span>
              </div>
            ))}
          </div>
        </div>
      ) : (
        <p className="text-[11px] text-tertiary italic">
          {t('No output keys detected. Add a Run Workflow block with extract/API steps to see available data.')}
        </p>
      )}
    </div>
  );
}
