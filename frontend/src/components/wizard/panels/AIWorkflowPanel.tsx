import React from 'react';
import {
  SparklesIcon,
  EyeIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { useWizard } from '../WizardContext';
import { AI_COSTS, formatUsdShort } from '../../CreditGuard';
import { Input } from '../../ui/Input';
import { NumberInput, Checkbox } from '../../ui';
import { SecureDataFields, type VaultSecretOption } from '../../workflows/SecureDataFields';
import { PersonaPicker } from '../../workflows/PersonaPicker';
import { useQuery } from '../../../hooks/useQuery';
import { vaultApi } from '../../../api/endpoints';
import { Q } from '../../../stores/queryKeys';
import { StageBackdrop } from '../shared/StageBackdrop';

export const AIWorkflowPanel: React.FC = () => {
  const { t } = useTranslation();
  const { state, updateConfig } = useWizard();
  const { url, goal, siteDescription, availableData, maxSteps, useVision, aiMode, userContext, autoValidate, useWritAi, generateWorkflow, defaultPersonaId } = state.config;

  const { data: vaultSecrets } = useQuery<VaultSecretOption[]>(
    Q.vaultSecrets(),
    () => vaultApi.listSecrets() as Promise<VaultSecretOption[]>,
  );

  const domain = (() => {
    try { return url ? new URL(url).hostname : undefined; } catch { return undefined; }
  })();

  return (
    // ai_workflow has no live browser stage — render the goal form as a focused
    // glass card centered on the empty dot-grid stage (the AI dock floats below).
    <StageBackdrop className="flex justify-center">
      <div className="w-full max-w-2xl px-4 sm:px-6 py-8">
        <div className="rounded-2xl border border-ink/20 bg-surface/90 backdrop-blur-sm shadow-sm px-5 sm:px-6 py-6 space-y-5">
        <div className="flex items-center gap-3">
          <div className="p-2 rounded-lg bg-hover">
            <SparklesIcon className="w-5 h-5 text-secondary" />
          </div>
          <div>
            <h2 className="text-base font-semibold text-ink tracking-tight">{t('AI Workflow')}</h2>
            <p className="text-sm text-secondary">{t('Describe what you want and AI will navigate for you.')}</p>
          </div>
        </div>

        {/* Name and URL are now set in Step 1 (ModeSelectionStep) */}
        <div className="bg-hover rounded-lg px-4 py-2.5 text-sm">
          <span className="text-secondary">{t('Target:')}</span> <span className="text-ink font-medium">{url || t('Not set')}</span>
        </div>

        <div>
          <label className="block text-sm text-secondary mb-1.5">{t('Goal')}</label>
          <textarea value={goal} onChange={(e) => updateConfig({ goal: e.target.value })} placeholder={t('Describe what the AI should accomplish...')} rows={3} className="w-full px-4 py-2.5 text-sm text-ink bg-surface border border-border rounded-lg placeholder:text-tertiary focus:outline-none focus:ring-2 focus:ring-ink/5 focus:border-ink/30 resize-none" />
        </div>

        <Input label={t('Site Description')} value={siteDescription} onChange={(e) => updateConfig({ siteDescription: e.target.value })} placeholder={t('e.g., HR management system, requires login first')} />

        {/* Persona — the identity the AI signs in as (login creds + server-side 2FA). */}
        <div className="rounded-lg border border-ink/20 bg-surface p-4 space-y-1.5 shadow-sm">
          <PersonaPicker
            value={defaultPersonaId}
            domain={domain}
            onChange={(id) => updateConfig({ defaultPersonaId: id })}
            allowClear
          />
          <p className="text-[11px] text-tertiary">
            {t('The AI signs in as this identity (credentials + 2FA handled automatically). Leave empty to provide secrets below instead.')}
          </p>
        </div>

        <SecureDataFields
          fields={availableData}
          onChange={(fields) => updateConfig({ availableData: fields })}
          vaultSecrets={vaultSecrets ?? []}
        />

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm text-secondary mb-1.5">{t('Mode')}</label>
            <div className="flex gap-2">
              {(['standard', 'intelligent'] as const).map(m => (
                <button key={m} onClick={() => updateConfig({ aiMode: m })} className={clsx(
                  'flex-1 px-3 py-2.5 rounded-lg text-xs font-medium border transition-colors',
                  aiMode === m ? 'border-ink bg-ink text-white' : 'border-border text-secondary hover:border-ink/20',
                )}>{m === 'standard'
                  ? t('Standard · {{cost}}', { cost: formatUsdShort(AI_COSTS.standard) })
                  : t('Intelligent · {{cost}}', { cost: formatUsdShort(AI_COSTS.intelligent) })}</button>
              ))}
            </div>
            <p className="text-[11px] text-tertiary mt-1">{aiMode === 'standard' ? t('Batch actions, faster') : t('Verifies each action, more reliable')}</p>
          </div>
          <NumberInput label={t('Max Steps')} value={maxSteps} onChange={(v) => updateConfig({ maxSteps: v ?? 20 })} />
        </div>

        <div className="flex gap-6">
          <Checkbox
            checked={useVision}
            onChange={(e) => updateConfig({ useVision: e.target.checked })}
            label={
              <span className="flex items-center gap-2">
                <EyeIcon className="w-4 h-4 text-tertiary" />
                <span className="text-sm text-ink">{t('Use Vision')}</span>
              </span>
            }
          />
          <Checkbox
            checked={autoValidate}
            onChange={(e) => updateConfig({ autoValidate: e.target.checked })}
            label={t('Auto-validate')}
          />
        </div>

        <Checkbox
          checked={!!useWritAi}
          onChange={(e) => updateConfig({ useWritAi: e.target.checked })}
          label={
            <span className="text-sm text-ink">
              {t('Use Writ AI')} <span className="text-tertiary">{t('(cloud gateway)')}</span>
            </span>
          }
          description={t("Route this session's AI through the managed gateway instead of the agent's own API keys. Off = your agent's local keys (BYO).")}
        />

        <Checkbox
          checked={generateWorkflow}
          onChange={(e) => updateConfig({ generateWorkflow: e.target.checked })}
          label={<span className="text-sm text-ink">{t('Record a workflow')}</span>}
          description={t('When the AI finishes, save the steps it took as a reusable workflow.')}
        />

        {aiMode === 'intelligent' && (
          <div>
            <label className="block text-sm text-secondary mb-1.5">{t('User Context')}</label>
            <textarea value={userContext} onChange={(e) => updateConfig({ userContext: e.target.value })} placeholder={t('Additional context for intelligent mode...')} rows={2} className="w-full px-4 py-2.5 text-sm text-ink bg-surface border border-border rounded-lg placeholder:text-tertiary focus:outline-none focus:ring-2 focus:ring-ink/5 focus:border-ink/30 resize-none" />
          </div>
        )}

        <div>
          <label className="block text-sm text-secondary mb-1.5">{t('Description')}</label>
          <textarea value={state.config.description} onChange={(e) => updateConfig({ description: e.target.value })} placeholder={t('Notes about this workflow...')} rows={2} className="w-full px-4 py-2.5 text-sm text-ink bg-surface border border-border rounded-lg placeholder:text-tertiary focus:outline-none focus:ring-2 focus:ring-ink/5 focus:border-ink/30 resize-none" />
        </div>
        </div>
      </div>
    </StageBackdrop>
  );
};
