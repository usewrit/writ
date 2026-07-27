import React from 'react';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import { automationApi, personasApi } from '../../../api/endpoints';
import { PersonaPicker } from '../../../components/workflows/PersonaPicker';
import { RunSettingsCard } from '../../../components/workflows/RunSettingsCard';
import { WorkflowFastActions } from '../../../components/workflows/WorkflowFastActions';
import { Section } from './Section';
import { InputDataCard } from './InputDataCard';
import { StreamingSettings } from './StreamingSettings';
import { StreamingScriptEditor } from './StreamingScriptEditor';
import { TierBadge, deriveProspectiveTier } from '../../../components/TierBadge';

interface SectionProps {
  workflow: any;
  linkedTriggers: any[];
  isStreaming: boolean;
  onRefresh: () => void;
}

/**
 * Principal "what this workflow NEEDS to run": its input data (lockable secrets)
 * and the login identity it signs in with. These are prerequisites — part of
 * what the workflow IS — so they stay inline on the Detail page, never hidden
 * behind Advanced.
 */
export const InputsIdentitySection: React.FC<SectionProps> = ({ workflow, isStreaming, onRefresh }) => {
  const { t } = useTranslation();
  // Prospective cloud tier (auto-derived). A login/persona makes runs sensitive →
  // isolated on Writ Cloud; shown next to the Persona control so the owner sees
  // why attaching a persona affects how the run is isolated.
  const wfTier = deriveProspectiveTier({
    sensitive: !!(workflow.has_login || workflow.default_persona_id),
    venueHint: workflow.execution_target || 'auto',
  });

  return (
    <div className="space-y-6">
      {/* Input data — values injected at run time; lock a field to store it encrypted. */}
      <Section
        title={t('Input data')}
        description={isStreaming
          ? t('Values available to the setup steps — lock a field to store it encrypted.')
          : t('Values injected when the workflow runs — lock a field to store it encrypted.')}
        anchor="wf-detail-inputs"
      >
        <InputDataCard workflow={workflow} onSaved={onRefresh} />
      </Section>

      {/* Persona — the login identity. Shown when the workflow signs in or already
          has one attached; this is what it NEEDS to reach an authenticated page. */}
      {(workflow.has_login || workflow.default_persona_id) && (
        <Section
          title={t('Persona')}
          description={t('The login identity this workflow signs in with — attached personas are used by default on every run.')}
          right={wfTier === 'isolated' ? <TierBadge tier="isolated" /> : undefined}
          anchor="wf-detail-persona"
        >
          <div className="bg-surface border border-ink/20 shadow-sm rounded-xl p-4">
            <PersonaPicker
              value={workflow.default_persona_id ?? null}
              domain={(() => { try { return workflow.entry_url ? new URL(workflow.entry_url).hostname : undefined; } catch { return undefined; } })()}
              onChange={async (pid) => {
                try {
                  await automationApi.updateWorkflow(workflow.id, { default_persona_id: pid });
                  toast.success(pid ? t('Persona attached — runs use it by default') : t('Persona cleared'));
                  onRefresh();
                } catch { toast.error(t('Failed to update persona')); }
              }}
              allowClear
            />
            {!workflow.default_persona_id && (
              <button
                onClick={async () => {
                  try {
                    const p = await personasApi.createFromWorkflow(workflow.id);
                    toast.success(t('Saved login as persona "{{name}}"', { name: p.name }));
                    onRefresh();
                  } catch (e: any) {
                    toast.error(e?.response?.data?.detail || t('Failed to save persona'));
                  }
                }}
                className="mt-2 text-xs text-secondary hover:text-ink underline decoration-dotted"
              >
                {t("Save this workflow's login as a persona →")}
              </button>
            )}
          </div>
        </Section>
      )}
    </div>
  );
};

/**
 * Advanced "how it runs" tuning: browser behaviour per run, plus when it runs on
 * its own and what happens when it breaks. For streaming, the engine/script
 * config. These are knobs, not prerequisites — they live in the Detail page's
 * collapsed Advanced section.
 */
export const RunTuningSection: React.FC<SectionProps> = ({ workflow, linkedTriggers, isStreaming, onRefresh }) => {
  const { t } = useTranslation();

  if (isStreaming) {
    return (
      <div className="space-y-6">
        <StreamingSettings
          workflowId={workflow.id}
          workflow={workflow}
          streamingConfig={workflow.streaming_config || {}}
          onUpdate={onRefresh}
        />
        <StreamingScriptEditor
          workflowId={workflow.id}
          workflow={workflow}
          streamingConfig={workflow.streaming_config || {}}
          onUpdate={onRefresh}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Execution — browser behaviour per run. */}
      <Section title={t('Execution')} description={t('Browser behaviour for each run.')}>
        <RunSettingsCard workflow={workflow} onUpdate={onRefresh} />
      </Section>

      {/* Venue & reliability — WHERE it runs and how it recovers. Scheduling is an
          invocation method and lives in the Connect tab, not here. */}
      <Section
        title={t('Venue & reliability')}
        description={t('Where this workflow runs, and how it recovers when a step breaks.')}
      >
        <WorkflowFastActions
          workflow={workflow}
          linkedTriggers={linkedTriggers}
          onCreated={onRefresh}
          actions={['airepair', 'execution_target']}
          title={null}
          showMoreLink={false}
        />
      </Section>
    </div>
  );
};
