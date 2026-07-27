import React, { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import {
  BellIcon,
  CursorArrowRaysIcon,
  SparklesIcon,
  LinkIcon,
  ChevronRightIcon,
  CheckIcon,
  ArrowRightIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import { triggersApi, automationApi, recipientsApi } from '../../api/endpoints';
import { buildNotifyOnChange, buildWorkflowOnChange, buildAISessionOnChange } from '../../utils/blockChainBuilder';
import { SectionHead } from '../common/SectionHead';
import { Checkbox, Select } from '../ui';

interface CheckFastActionsProps {
  targetId: number;
  targetUrl: string;
  triggers: any[];
  onCreated: () => void;
}

const CHANNELS = [
  { key: 'pushover', label: 'Pushover' },
  { key: 'email', label: 'Email' },
  { key: 'twilio', label: 'SMS' },
  { key: 'webhook', label: 'Webhook' },
];

export const CheckFastActions: React.FC<CheckFastActionsProps> = ({ targetId, targetUrl, triggers, onCreated }) => {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Data for pickers
  const [recipients, setRecipients] = useState<any[]>([]);
  const [workflows, setWorkflows] = useState<any[]>([]);
  const [sessions, setSessions] = useState<any[]>([]);

  useEffect(() => {
    recipientsApi.getAll().then(setRecipients).catch(() => {});
    automationApi.listWorkflows().then(setWorkflows).catch(() => {});
    automationApi.listAISessions({}).then(setSessions).catch(() => {});
  }, []);

  // Detect existing fast-actions from triggers
  const hasNotify = triggers.some(t => t.blocks?.some((b: any) => b.blockType === 'notification'));
  const hasWorkflow = triggers.some(t => t.blocks?.some((b: any) => b.blockType === 'workflow'));
  const hasAI = triggers.some(t => t.blocks?.some((b: any) => b.blockType === 'ai_session'));

  // Form state
  const [selectedChannels, setSelectedChannels] = useState<string[]>(['pushover']);
  const [selectedRecipients, setSelectedRecipients] = useState<string[]>([]);
  const [selectedWorkflowId, setSelectedWorkflowId] = useState<number | null>(null);
  const [selectedSessionId, setSelectedSessionId] = useState<number | null>(null);
  const [webhookUrl, setWebhookUrl] = useState('');

  const toggleChannel = (key: string) => {
    setSelectedChannels(prev => prev.includes(key) ? prev.filter(c => c !== key) : [...prev, key]);
  };

  const createTrigger = async (name: string, payload: any) => {
    setSaving(true);
    try {
      await triggersApi.create({ name, enabled: true, ...payload });
      toast.success(t('Automation created'));
      setExpanded(null);
      onCreated();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('Failed to create'));
    } finally {
      setSaving(false);
    }
  };

  const handleNotify = () => {
    const { payload } = buildNotifyOnChange(targetId, {
      channels: selectedChannels,
      recipients: selectedRecipients,
    });
    createTrigger(t('Notify on {{host}} change', { host: new URL(targetUrl).hostname }), payload);
  };

  const handleWorkflow = () => {
    if (!selectedWorkflowId) return;
    const wf = workflows.find(w => w.id === selectedWorkflowId);
    const { payload } = buildWorkflowOnChange(targetId, selectedWorkflowId, wf?.name || t('Workflow'));
    createTrigger(t('Run "{{name}}" on change', { name: wf?.name }), payload);
  };

  const handleAI = () => {
    if (!selectedSessionId) return;
    const sess = sessions.find(s => s.id === selectedSessionId);
    const { payload } = buildAISessionOnChange(targetId, selectedSessionId, sess?.name || t('AI Session'));
    createTrigger(t('Run AI "{{name}}" on change', { name: sess?.name }), payload);
  };

  const handleWebhook = async () => {
    if (!webhookUrl.trim()) return;
    setSaving(true);
    try {
      const { payload } = buildNotifyOnChange(targetId, {
        channels: ['webhook'],
        recipients: [],
        template: webhookUrl.trim(),
      });
      await triggersApi.create({ name: t('Webhook on {{host}} change', { host: new URL(targetUrl).hostname }), enabled: true, ...payload });
      toast.success(t('Webhook automation created'));
      setExpanded(null);
      onCreated();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('Failed to create'));
    } finally {
      setSaving(false);
    }
  };

  const actions = [
    {
      id: 'notify',
      icon: BellIcon,
      label: t('Notify me'),
      description: t('Send a notification when content changes'),
      active: hasNotify,
      form: (
        <div className="space-y-3">
          <div>
            <label className="text-[11px] font-medium text-secondary mb-1.5 block">{t('Channels')}</label>
            <div className="flex flex-wrap gap-1.5">
              {CHANNELS.map(ch => (
                <button key={ch.key} onClick={() => toggleChannel(ch.key)} className={clsx(
                  'px-2.5 py-1.5 rounded-lg text-xs font-medium border transition-all',
                  selectedChannels.includes(ch.key) ? 'border-ink bg-ink/5 text-ink' : 'border-border text-tertiary hover:text-secondary',
                )}>{t(ch.label)}</button>
              ))}
            </div>
          </div>
          {recipients.length > 0 && (
            <div>
              <label className="text-[11px] font-medium text-secondary mb-1.5 block">{t('Recipients')}</label>
              <div className="space-y-1">
                {recipients.filter(r => selectedChannels.includes(r.provider)).map((r: any) => (
                  <label key={r.id} className="flex items-center gap-2 text-xs cursor-pointer">
                    <Checkbox
                      size="sm"
                      checked={selectedRecipients.includes(`${r.provider}:${r.id}`)}
                      onChange={() => {
                        const key = `${r.provider}:${r.id}`;
                        setSelectedRecipients(prev => prev.includes(key) ? prev.filter(x => x !== key) : [...prev, key]);
                      }}
                    />
                    <span className="text-ink">{r.name}</span>
                    <span className="text-tertiary">{r.identifier_preview}</span>
                  </label>
                ))}
              </div>
            </div>
          )}
          <button onClick={handleNotify} disabled={saving || selectedChannels.length === 0} className={clsx(
            'w-full px-4 py-2.5 text-sm font-medium rounded-xl transition-colors',
            saving ? 'bg-hover text-tertiary' : 'bg-accent-strong text-accent-on hover:bg-accent-strong/90',
          )}>{saving ? t('Creating...') : t('Create')}</button>
        </div>
      ),
    },
    {
      id: 'workflow',
      icon: CursorArrowRaysIcon,
      label: t('Run a workflow'),
      description: t('Execute a workflow when content changes'),
      active: hasWorkflow,
      form: (
        <div className="space-y-3">
          <Select<string>
            value={selectedWorkflowId ? String(selectedWorkflowId) : ''}
            onChange={(v) => setSelectedWorkflowId(v ? Number(v) : null)}
            placeholder={t('Select a workflow...')}
            className="w-full"
            options={workflows.map(w => ({ value: String(w.id), label: w.name }))}
          />
          <button onClick={handleWorkflow} disabled={saving || !selectedWorkflowId} className={clsx(
            'w-full px-4 py-2.5 text-sm font-medium rounded-xl transition-colors',
            saving || !selectedWorkflowId ? 'bg-hover text-tertiary' : 'bg-accent-strong text-accent-on hover:bg-accent-strong/90',
          )}>{saving ? t('Creating...') : t('Create')}</button>
        </div>
      ),
    },
    {
      id: 'ai',
      icon: SparklesIcon,
      label: t('Run AI agent'),
      description: t('Start an AI session when content changes'),
      active: hasAI,
      form: (
        <div className="space-y-3">
          <Select<string>
            value={selectedSessionId ? String(selectedSessionId) : ''}
            onChange={(v) => setSelectedSessionId(v ? Number(v) : null)}
            placeholder={t('Select an AI session...')}
            className="w-full"
            options={sessions.map(s => ({ value: String(s.id), label: `${s.name} — ${s.mode}` }))}
          />
          <button onClick={handleAI} disabled={saving || !selectedSessionId} className={clsx(
            'w-full px-4 py-2.5 text-sm font-medium rounded-xl transition-colors',
            saving || !selectedSessionId ? 'bg-hover text-tertiary' : 'bg-accent-strong text-accent-on hover:bg-accent-strong/90',
          )}>{saving ? t('Creating...') : t('Create')}</button>
        </div>
      ),
    },
    {
      id: 'webhook',
      icon: LinkIcon,
      label: t('Call a webhook'),
      description: t('POST to a URL when content changes'),
      active: false,
      form: (
        <div className="space-y-3">
          <input type="text" value={webhookUrl} onChange={e => setWebhookUrl(e.target.value)}
            placeholder="https://your-app.com/webhook"
            className="w-full px-3 py-2.5 bg-surface border border-border rounded-xl text-sm text-ink focus:outline-none focus:border-ink/30 focus:ring-2 focus:ring-ink/5 placeholder:text-tertiary font-mono" />
          <button onClick={handleWebhook} disabled={saving || !webhookUrl.trim()} className={clsx(
            'w-full px-4 py-2.5 text-sm font-medium rounded-xl transition-colors',
            saving || !webhookUrl.trim() ? 'bg-hover text-tertiary' : 'bg-accent-strong text-accent-on hover:bg-accent-strong/90',
          )}>{saving ? t('Creating...') : t('Create')}</button>
        </div>
      ),
    },
  ];

  return (
    <div className="bg-surface border border-border rounded-xl overflow-hidden">
      <div className="px-4 sm:px-5 pt-4 pb-0.5 border-b border-border">
        <SectionHead title={t('When this monitor detects a change...')} description={t('Pick an action to run automatically on every detected change.')} />
      </div>

      <div className="divide-y divide-border">
        {actions.map(action => {
          const Icon = action.icon;
          const isExpanded = expanded === action.id;

          return (
            <div key={action.id}>
              <button
                onClick={() => setExpanded(isExpanded ? null : action.id)}
                className="w-full flex items-center gap-3 px-4 sm:px-5 py-3.5 text-left hover:bg-hover/30 transition-all duration-150"
              >
                <div className="w-7 h-7 bg-hover rounded-lg flex items-center justify-center flex-shrink-0">
                  <Icon className="w-4 h-4 text-ink" />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-ink">{t(action.label)}</p>
                  <p className="text-[11px] text-tertiary">{t(action.description)}</p>
                </div>
                {action.active ? (
                  <div className="w-5 h-5 bg-ink/5 rounded-full flex items-center justify-center">
                    <CheckIcon className="w-3 h-3 text-ink" />
                  </div>
                ) : (
                  <ChevronRightIcon className={clsx('w-4 h-4 text-tertiary transition-transform duration-200', isExpanded && 'rotate-90')} />
                )}
              </button>

              <div className={clsx(
                'grid transition-all duration-200',
                isExpanded ? 'grid-rows-[1fr] opacity-100' : 'grid-rows-[0fr] opacity-0',
              )}>
                <div className="overflow-hidden">
                  <div className="px-4 sm:px-5 pb-4 pt-1">
                    {action.form}
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <div className="px-4 sm:px-5 py-3 border-t border-border bg-canvas/50">
        <Link to={`/automations/new?source=check&checkId=${targetId}`} className="flex items-center gap-1.5 text-xs text-secondary hover:text-ink transition-colors">
          {t('Need more complex logic?')} <span className="font-medium text-ink">{t('Build in Automations')}</span> <ArrowRightIcon className="w-3 h-3" />
        </Link>
      </div>
    </div>
  );
};
