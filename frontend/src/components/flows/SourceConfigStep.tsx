import React, { useState, useCallback } from 'react';
import { ArrowPathIcon } from '@heroicons/react/24/outline';
import { ClipboardIcon, ClipboardDocumentCheckIcon } from '@heroicons/react/24/outline';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import { useFlowBuilder } from './FlowBuilderContext';
import { WizardBridge } from './WizardBridge';
import { ContentMonitorPanel } from '../wizard/panels/ContentMonitorPanel';
import { webhookTriggersApi } from '../../api/endpoints';
import type { WizardState } from '../wizard/WizardContext';

interface SourceConfigStepProps {
  onDone: () => void;
}

export const SourceConfigStep: React.FC<SourceConfigStepProps> = ({ onDone }) => {
  const { t } = useTranslation();
  const { state } = useFlowBuilder();
  const sourceBlock = state.blocks.find(b => !b.parentId && b.type === 'event');

  if (!sourceBlock) return null;

  return (
    <div className="bg-surface border border-border rounded-xl overflow-hidden shadow-sm">
      <div className="px-5 py-3 border-b border-border bg-canvas">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="w-5 h-5 rounded-full bg-ink text-white flex items-center justify-center text-[10px] font-bold">2</div>
            <span className="text-sm font-medium text-ink">{t('Configure')}</span>
          </div>
          <button
            type="button"
            onClick={onDone}
            className="px-3 py-1.5 text-xs font-medium text-ink bg-hover hover:bg-active rounded-lg transition-colors"
          >
            {t('Continue →')}
          </button>
        </div>
      </div>

      <div>
        {sourceBlock.blockType === 'change_detected' && (
          <ContentMonitorEmbed sourceBlock={sourceBlock} onDone={onDone} />
        )}
        {sourceBlock.blockType === 'webhook_received' && (
          <div className="p-5">
            <WebhookConfig sourceBlock={sourceBlock} onDone={onDone} />
          </div>
        )}
      </div>
    </div>
  );
};

// --- Content Monitor: embeds the real ContentMonitorPanel via WizardBridge ---

function ContentMonitorEmbed({ sourceBlock }: { sourceBlock: any; onDone: () => void }) {
  const { t } = useTranslation();
  const { updateBlockConfig, dispatch, state } = useFlowBuilder();

  const initialConfig: Partial<WizardState['config']> = {
    name: state.name || t('Untitled Monitor'),
    url: sourceBlock.config.url || '',
    selectors: sourceBlock.config.selectors || [],
    checkPeriodMs: sourceBlock.config.check_period_ms || 60000,
    requiresPlaywright: sourceBlock.config.requires_playwright || false,
  };

  const handleConfigChange = useCallback((updates: Partial<WizardState['config']>) => {
    updateBlockConfig(sourceBlock.id, {
      ...sourceBlock.config,
      url: updates.url,
      selectors: updates.selectors,
      check_period_ms: updates.checkPeriodMs,
      requires_playwright: updates.requiresPlaywright,
    });
    if (updates.name && updates.name !== state.name) {
      dispatch({ type: 'SET_META', name: updates.name });
    }
  }, [sourceBlock.id, sourceBlock.config, updateBlockConfig, dispatch, state.name]);

  return (
    <WizardBridge mode="content_monitor" config={initialConfig} onConfigChange={handleConfigChange}>
      <div className="h-[600px] overflow-hidden">
        <ContentMonitorPanel />
      </div>
    </WizardBridge>
  );
}

// --- Webhook Config (simple, no bridge needed) ---

function WebhookConfig({ sourceBlock, onDone }: { sourceBlock: any; onDone: () => void }) {
  const { t } = useTranslation();
  const hasToken = !!sourceBlock.config.webhook_trigger_token;
  const [copiedToken, setCopiedToken] = useState<string | null>(null);

  const copyUrl = (token: string) => {
    navigator.clipboard.writeText(webhookTriggersApi.getWebhookUrl(token));
    setCopiedToken(token);
    toast.success(t('Copied'));
    setTimeout(() => setCopiedToken(null), 2000);
  };

  return (
    <div className="space-y-5">
      {hasToken ? (
        <div className="space-y-3">
          <div>
            <label className="block text-xs font-medium text-secondary mb-1.5">{t('Your webhook URL')}</label>
            <div className="flex items-center gap-2">
              <code className="flex-1 text-xs text-ink bg-canvas px-3 py-2.5 rounded-lg border border-border truncate">
                {webhookTriggersApi.getWebhookUrl(sourceBlock.config.webhook_trigger_token)}
              </code>
              <button type="button" onClick={() => copyUrl(sourceBlock.config.webhook_trigger_token)} className="p-2 hover:bg-hover rounded-lg transition-colors shrink-0">
                {copiedToken ? (
                  <ClipboardDocumentCheckIcon className="h-4 w-4 text-green-600" />
                ) : (
                  <ClipboardIcon className="h-4 w-4 text-secondary" />
                )}
              </button>
            </div>
          </div>
          <p className="text-[10px] text-tertiary">
            {t('POST JSON to this URL. Payload fields are available as')} <code className="bg-hover px-1 rounded">{'{{payload.field}}'}</code> {t('in actions.')}
          </p>
        </div>
      ) : (
        <div className="py-4 text-center">
          <div className="w-10 h-10 bg-amber-50 rounded-lg flex items-center justify-center mx-auto mb-3">
            <ArrowPathIcon className="h-5 w-5 text-amber-600" />
          </div>
          <p className="text-sm text-ink font-medium">{t('Webhook URL will be generated on save')}</p>
          <p className="text-xs text-secondary mt-1">{t('External systems will POST JSON to trigger this automation.')}</p>
        </div>
      )}

      <button
        type="button"
        onClick={onDone}
        className="w-full py-2.5 text-sm font-medium rounded-lg bg-accent-strong text-accent-on hover:bg-accent-strong/90 transition-colors"
      >
        {t('Continue — add actions')}
      </button>
    </div>
  );
}
