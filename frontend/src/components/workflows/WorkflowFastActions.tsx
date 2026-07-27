import React, { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import {
  LinkIcon,
  ClockIcon,
  EyeIcon,
  BellIcon,
  WrenchScrewdriverIcon,
  ComputerDesktopIcon,
  ChevronRightIcon,
  CheckIcon,
  ArrowRightIcon,
  ClipboardIcon,
  ClipboardDocumentCheckIcon,
  ServerStackIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import { triggersApi, automationApi, recipientsApi, webhookTriggersApi, targetsApi } from '../../api/endpoints';
import { buildExposeAsAPI, buildNotifyOnWorkflowComplete, buildWorkflowOnChange } from '../../utils/blockChainBuilder';
import { Select } from '../ui';
import { SchedulePicker } from '../schedule/SchedulePicker';
import { scheduleFromApi, scheduleToPayload, scheduleError, ScheduleValue } from '../../utils/schedule';

export type FastActionId = 'mcp' | 'api' | 'schedule' | 'onchange' | 'notify' | 'airepair' | 'execution_target';

interface WorkflowFastActionsProps {
  workflow: any;
  linkedTriggers: any[];
  onCreated: () => void;
  onOpenMcp?: () => void;
  /** Subset of action rows to render (default: all). Lets pages place rows per tab. */
  actions?: FastActionId[];
  /** Section heading. Pass null to render the bare card (page supplies its own header). */
  title?: string | null;
  /** Show the "Build in Automations" footer link (default true). */
  showMoreLink?: boolean;
}

const CHANNELS = [
  { key: 'pushover', label: 'Pushover' },
  { key: 'email', label: 'Email' },
  { key: 'webhook', label: 'Webhook' },
];

export const WorkflowFastActions: React.FC<WorkflowFastActionsProps> = ({ workflow, linkedTriggers, onCreated, onOpenMcp, actions: visibleActions, title, showMoreLink = true }) => {
  const { t } = useTranslation();
  const titleText = title === undefined ? t('Actions') : title;
  const [expanded, setExpanded] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Data for pickers
  const [targets, setTargets] = useState<any[]>([]);
  const [, setRecipients] = useState<any[]>([]);

  useEffect(() => {
    targetsApi.getAll('content').then(setTargets).catch(() => {});
    recipientsApi.getAll().then(setRecipients).catch(() => {});
  }, []);

  // Detect existing
  const existingApiTrigger = linkedTriggers.find(t => t.event_type === 'webhook_received');
  const hasAPI = !!existingApiTrigger;
  const hasSchedule = workflow.schedule_enabled;
  const hasOnChange = linkedTriggers.some(t => t.event_type === 'change_detected');
  const hasNotify = linkedTriggers.some(t => t.event_type === 'workflow_completed' && t.blocks?.some((b: any) => b.blockType === 'notification'));

  // API expose state
  const [apiCreated, setApiCreated] = useState<{ url: string; token: string } | null>(
    hasAPI && existingApiTrigger?.webhook_trigger_token
      ? { url: webhookTriggersApi.getWebhookUrl(existingApiTrigger.webhook_trigger_token), token: existingApiTrigger.webhook_trigger_token }
      : null
  );
  const [copied, setCopied] = useState<string | null>(null);

  // Schedule state — full recurrence value (interval | daily | weekly), seeded
  // from the persisted workflow (falls back to the legacy interval).
  const [schedule, setSchedule] = useState<ScheduleValue>(() =>
    scheduleFromApi(workflow, workflow.schedule_interval_ms || 3600000),
  );

  // On change state
  const [selectedTargetId, setSelectedTargetId] = useState<string | null>(null);

  // Notify state
  const [selectedChannels, setSelectedChannels] = useState<string[]>(['pushover']);

  const copyToClipboard = (text: string, label: string) => {
    navigator.clipboard.writeText(text);
    setCopied(label);
    toast.success(t('Copied'));
    setTimeout(() => setCopied(null), 2000);
  };

  const handleExposeAsAPI = async () => {
    setSaving(true);
    try {
      // Create automation with webhook_received source — backend auto-creates webhook endpoint
      const { payload } = buildExposeAsAPI(workflow.id, 0, '');
      const result = await triggersApi.create({ name: t('API: {{name}}', { name: workflow.name }), enabled: true, ...payload });

      // Backend returns the auto-generated webhook token
      if (result.webhook_trigger_token) {
        const url = webhookTriggersApi.getWebhookUrl(result.webhook_trigger_token);
        setApiCreated({ url, token: result.webhook_trigger_token });
      }
      toast.success(t('API endpoint created'));
      onCreated();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('Failed to create'));
    } finally {
      setSaving(false);
    }
  };

  const handleSchedule = async () => {
    if (scheduleError(schedule)) return;
    setSaving(true);
    try {
      await automationApi.updateWorkflow(workflow.id, {
        schedule_enabled: true,
        // Interval kind carries the interval column; daily/weekly carry the
        // structured recurrence fields (interval column kept for back-compat).
        schedule_interval_ms: schedule.intervalMs,
        ...scheduleToPayload(schedule),
      });
      toast.success(t('Schedule enabled'));
      setExpanded(null);
      onCreated();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('Failed'));
    } finally {
      setSaving(false);
    }
  };

  const handleOnChange = async () => {
    if (!selectedTargetId) return;
    setSaving(true);
    try {
      const target = targets.find(t => t.id === selectedTargetId);
      const { payload } = buildWorkflowOnChange(Number(selectedTargetId), workflow.id, workflow.name);
      await triggersApi.create({ name: t('Run "{{name}}" when {{page}} changes', { name: workflow.name, page: target?.url || t('page') }), enabled: true, ...payload });
      toast.success(t('Automation created'));
      setExpanded(null);
      onCreated();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('Failed'));
    } finally {
      setSaving(false);
    }
  };

  const handleNotify = async () => {
    setSaving(true);
    try {
      const { payload } = buildNotifyOnWorkflowComplete(workflow.id, { channels: selectedChannels, recipients: [] });
      await triggersApi.create({ name: t('Notify on "{{name}}" completion', { name: workflow.name }), enabled: true, ...payload });
      toast.success(t('Notification automation created'));
      setExpanded(null);
      onCreated();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('Failed'));
    } finally {
      setSaving(false);
    }
  };

  // Build cURL example
  const formDataKeys = Object.keys(workflow.form_data || {});
  const curlBody = formDataKeys.length > 0
    ? formDataKeys.reduce((acc: any, key: string) => ({ ...acc, [key]: '...' }), {})
    : {};

  const actions = [
    {
      id: 'mcp',
      icon: ServerStackIcon,
      label: t('Expose as MCP Server'),
      description: t('Let AI agents call this workflow via the Model Context Protocol'),
      active: false,
      form: null,
      onClick: onOpenMcp,
    },
    {
      id: 'api',
      icon: LinkIcon,
      label: t('Expose as API'),
      description: t('Make this workflow callable via HTTP endpoint'),
      active: hasAPI || !!apiCreated,
      form: apiCreated ? (
        <div className="space-y-3">
          <div>
            <label className="text-[11px] font-medium text-secondary mb-1 block">{t('Endpoint URL')}</label>
            <div className="flex items-center gap-2">
              <code className="flex-1 px-3 py-2 bg-canvas border border-border rounded-lg text-xs font-mono text-ink truncate">{apiCreated.url}</code>
              <button onClick={() => copyToClipboard(apiCreated.url, 'url')} className="p-2 hover:bg-hover rounded-lg transition-colors">
                {copied === 'url' ? <ClipboardDocumentCheckIcon className="w-4 h-4 text-ink" /> : <ClipboardIcon className="w-4 h-4 text-tertiary" />}
              </button>
            </div>
          </div>
          {formDataKeys.length > 0 && (
            <div>
              <label className="text-[11px] font-medium text-secondary mb-1 block">{t('Input Parameters')}</label>
              <div className="space-y-1">
                {formDataKeys.map(key => (
                  <div key={key} className="flex items-center gap-2 text-xs">
                    <code className="px-1.5 py-0.5 bg-canvas rounded font-mono text-ink">{key}</code>
                    <span className="text-tertiary">{t('string')}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          <div>
            <label className="text-[11px] font-medium text-secondary mb-1 block">{t('cURL Example')}</label>
            <div className="relative">
              <pre className="px-3 py-2.5 bg-canvas border border-border text-ink rounded-xl text-[11px] font-mono overflow-x-auto leading-relaxed">
{`curl -X POST \\
  ${apiCreated.url} \\
  -H "Content-Type: application/json"${formDataKeys.length > 0 ? ` \\
  -d '${JSON.stringify(curlBody)}'` : ''}`}
              </pre>
              <button onClick={() => copyToClipboard(`curl -X POST ${apiCreated.url} -H "Content-Type: application/json"${formDataKeys.length > 0 ? ` -d '${JSON.stringify(curlBody)}'` : ''}`, 'curl')}
                className="absolute top-2 right-2 p-1 hover:bg-hover rounded transition-colors">
                {copied === 'curl' ? <ClipboardDocumentCheckIcon className="w-3.5 h-3.5 text-ink" /> : <ClipboardIcon className="w-3.5 h-3.5 text-tertiary" />}
              </button>
            </div>
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          {formDataKeys.length > 0 && (
            <div>
              <label className="text-[11px] font-medium text-secondary mb-1 block">{t('This workflow accepts input')}</label>
              <div className="flex flex-wrap gap-1.5">
                {formDataKeys.map(key => (
                  <code key={key} className="px-2 py-1 bg-canvas border border-border rounded-lg text-[11px] font-mono text-ink">{key}</code>
                ))}
              </div>
            </div>
          )}
          <button onClick={handleExposeAsAPI} disabled={saving} className={clsx(
            'w-full px-4 py-2.5 text-sm font-medium rounded-xl transition-colors',
            saving ? 'bg-hover text-tertiary' : 'bg-accent-strong text-accent-on hover:bg-accent-strong/90',
          )}>{saving ? t('Creating...') : t('Create API Endpoint')}</button>
        </div>
      ),
    },
    {
      id: 'schedule',
      icon: ClockIcon,
      label: t('Schedule this'),
      description: hasSchedule ? t('On a recurring schedule') : t('Run on a recurring schedule'),
      active: hasSchedule,
      form: (
        <div className="space-y-3">
          <SchedulePicker value={schedule} onChange={setSchedule} />
          <button onClick={handleSchedule} disabled={saving || !!scheduleError(schedule)} className={clsx(
            'w-full px-4 py-2.5 text-sm font-medium rounded-xl transition-colors',
            saving || scheduleError(schedule) ? 'bg-hover text-tertiary' : 'bg-accent-strong text-accent-on hover:bg-accent-strong/90',
          )}>{saving ? t('Saving...') : hasSchedule ? t('Update Schedule') : t('Enable Schedule')}</button>
        </div>
      ),
    },
    {
      id: 'onchange',
      icon: EyeIcon,
      label: t('Run when a page changes'),
      description: t('Trigger this workflow when a monitor detects a change'),
      active: hasOnChange,
      form: (
        <div className="space-y-3">
          <Select
            value={selectedTargetId || ''}
            onChange={v => setSelectedTargetId(v || null)}
            placeholder={t('Select a monitor...')}
            options={targets.map(tg => ({ value: String(tg.id), label: tg.url }))}
          />
          <button onClick={handleOnChange} disabled={saving || !selectedTargetId} className={clsx(
            'w-full px-4 py-2.5 text-sm font-medium rounded-xl transition-colors',
            saving || !selectedTargetId ? 'bg-hover text-tertiary' : 'bg-accent-strong text-accent-on hover:bg-accent-strong/90',
          )}>{saving ? t('Creating...') : t('Create')}</button>
        </div>
      ),
    },
    {
      id: 'notify',
      icon: BellIcon,
      label: t('Notify on completion'),
      description: t('Get notified when this workflow finishes'),
      active: hasNotify,
      form: (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-1.5">
            {CHANNELS.map(ch => (
              <button key={ch.key} onClick={() => setSelectedChannels(prev => prev.includes(ch.key) ? prev.filter(c => c !== ch.key) : [...prev, ch.key])} className={clsx(
                'px-2.5 py-1.5 rounded-lg text-xs font-medium border transition-all',
                selectedChannels.includes(ch.key) ? 'border-ink bg-ink/5 text-ink' : 'border-border text-tertiary hover:text-secondary',
              )}>{t(ch.label)}</button>
            ))}
          </div>
          <button onClick={handleNotify} disabled={saving || selectedChannels.length === 0} className={clsx(
            'w-full px-4 py-2.5 text-sm font-medium rounded-xl transition-colors',
            saving ? 'bg-hover text-tertiary' : 'bg-accent-strong text-accent-on hover:bg-accent-strong/90',
          )}>{saving ? t('Creating...') : t('Create')}</button>
        </div>
      ),
    },
    {
      id: 'airepair',
      icon: WrenchScrewdriverIcon,
      label: t('AI Auto-Repair'),
      description: workflow.ai_repair_enabled
        ? (workflow.repair_count ? t('Enabled · {{n}} repairs', { n: workflow.repair_count }) : t('Enabled'))
        : t('Automatically fix broken steps with AI'),
      active: !!workflow.ai_repair_enabled,
      form: (
        <div className="space-y-3">
          <p className="text-xs text-secondary">
            {t('When this workflow fails, AI will attempt to find the correct element or navigate the new flow.')}
          </p>
          <div className="flex items-center justify-between px-3 py-2.5 bg-canvas rounded-xl">
            <span className="text-sm text-ink">{t('Auto-repair on failure')}</span>
          </div>
          <button
            onClick={async () => {
              setSaving(true);
              try {
                await automationApi.updateWorkflow(workflow.id, {
                  ai_repair_enabled: !workflow.ai_repair_enabled,
                });
                toast.success(workflow.ai_repair_enabled ? t('Auto-repair disabled') : t('Auto-repair enabled'));
                setExpanded(null);
                onCreated();
              } catch (err: any) {
                toast.error(err?.response?.data?.detail || t('Failed'));
              } finally {
                setSaving(false);
              }
            }}
            disabled={saving}
            className={clsx(
              'w-full px-4 py-2.5 text-sm font-medium rounded-xl transition-colors',
              saving ? 'bg-hover text-tertiary' : 'bg-accent-strong text-accent-on hover:bg-accent-strong/90',
            )}
          >
            {saving ? t('Saving...') : workflow.ai_repair_enabled ? t('Disable Auto-Repair') : t('Enable Auto-Repair')}
          </button>
        </div>
      ),
    },
    {
      id: 'execution_target',
      icon: ComputerDesktopIcon,
      label: t('Default Execution Target'),
      description: workflow.execution_target === 'local' ? t('Local Agent') : workflow.execution_target === 'cloud' ? t('Writ Cloud') : t('Auto (prefer local)'),
      active: workflow.execution_target && workflow.execution_target !== 'auto',
      form: (
        <div className="space-y-3">
          <p className="text-xs text-secondary">
            {t('Choose where this workflow runs by default. You can still override when running manually.')}
          </p>
          {(['auto', 'local', 'cloud'] as const).map((target) => {
            const labels: Record<string, { title: string; desc: string }> = {
              auto: { title: t('Auto'), desc: t('Prefer local agent, fallback to cloud') },
              local: { title: t('Local Agent'), desc: t('Only run on your connected agent') },
              cloud: { title: t('Writ Cloud'), desc: t('Only run on Writ infrastructure') },
            };
            const isSelected = (workflow.execution_target || 'auto') === target;
            return (
              <button
                key={target}
                onClick={async () => {
                  setSaving(true);
                  try {
                    await automationApi.updateWorkflow(workflow.id, { execution_target: target });
                    toast.success(t('Default target set to {{title}}', { title: labels[target].title }));
                    setExpanded(null);
                    onCreated();
                  } catch (err: any) {
                    toast.error(err?.response?.data?.detail || t('Failed'));
                  } finally {
                    setSaving(false);
                  }
                }}
                disabled={saving}
                className={clsx(
                  'w-full flex items-center gap-3 px-3 py-2.5 rounded-xl border transition-all text-left',
                  isSelected ? 'border-ink bg-ink text-white' : 'border-border hover:border-secondary',
                )}
              >
                <div className="flex-1">
                  <div className={clsx('text-sm font-medium', isSelected ? 'text-white' : 'text-ink')}>{labels[target].title}</div>
                  <div className={clsx('text-xs', isSelected ? 'text-white/70' : 'text-secondary')}>{labels[target].desc}</div>
                </div>
                {isSelected && <CheckIcon className="w-4 h-4" />}
              </button>
            );
          })}
        </div>
      ),
    },
  ];

  const shownActions = visibleActions ? actions.filter(a => visibleActions.includes(a.id as FastActionId)) : actions;

  return (
    <div>
      {titleText && <h2 className="text-xs font-semibold text-secondary uppercase tracking-wider mb-2">{titleText}</h2>}

      <div className="bg-surface border border-border rounded-xl overflow-hidden divide-y divide-border">
        {shownActions.map(action => {
          const Icon = action.icon;
          const isExpanded = expanded === action.id;

          return (
            <div key={action.id}>
              <button
                onClick={() => (action as any).onClick ? (action as any).onClick() : setExpanded(isExpanded ? null : action.id)}
                className="w-full flex items-center gap-3 px-4 py-2.5 text-left hover:bg-hover transition-colors"
              >
                <Icon className="w-4 h-4 text-tertiary shrink-0" />
                <div className="flex-1 min-w-0">
                  <span className="text-[13px] text-ink">{t(action.label)}</span>
                  <span className="text-[11px] text-tertiary ml-2 hidden @rail/stage:inline">{t(action.description)}</span>
                </div>
                {action.active ? (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-hover text-secondary font-medium">{t('Active')}</span>
                ) : (
                  <ChevronRightIcon className={clsx('w-3.5 h-3.5 text-tertiary transition-transform duration-150', isExpanded && 'rotate-90')} />
                )}
              </button>

              <div className={clsx(
                'grid transition-all duration-200',
                isExpanded ? 'grid-rows-[1fr] opacity-100' : 'grid-rows-[0fr] opacity-0',
              )}>
                <div className="overflow-hidden">
                  <div className="px-4 pb-3 pt-1 ml-7">
                    {action.form}
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {showMoreLink && (
        <div className="mt-2.5">
          <Link to={`/automations/new?source=workflow&workflowId=${workflow.id}`} className="flex items-center gap-1.5 text-[11px] text-tertiary hover:text-ink transition-colors">
            {t('Need more?')} <span className="font-medium text-ink">{t('Build in Automations')}</span> <ArrowRightIcon className="w-3 h-3" />
          </Link>
        </div>
      )}
    </div>
  );
};
