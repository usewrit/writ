import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import { useRequireAuth } from '../../hooks/useAuth';
import { FlowBuilderProvider } from '../../components/flows/FlowBuilderContext';
import { FlowBuilder } from '../../components/flows/FlowBuilder';
import { triggersApi, targetsApi, automationApi } from '../../api/endpoints';
import { TriggerRule } from '../../components/flows/types';
import { getPreset } from '../../components/flows/presets';
import { getTemplate } from '../../components/flows/templates';
import { TemplateWizard } from '../../components/flows/TemplateWizard';
import { takeFlowDraft } from '../../components/flows/flowDraft';
import { triggerMini } from '../../onboarding/miniTrigger';

function genBlockId(): string {
  return `block_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
}

// Back-compat: the old restock preset id now maps to the template registry.
const PRESET_ALIASES: Record<string, string> = {
  ecommerce_restock: 'restock_autobuy',
};

export const AutomationBuilderPage: React.FC = () => {
  useRequireAuth();
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [initialFlow, setInitialFlow] = useState<TriggerRule | undefined>();
  const [restoredDraft, setRestoredDraft] = useState(false);
  // Ref as well as state: the effect below must not re-fetch over a draft it
  // already restored (a second effect pass would find the draft consumed).
  const restoredRef = useRef(false);
  const [fetching, setFetching] = useState(true);

  const rawPreset = searchParams.get('preset') || '';
  const presetKey = PRESET_ALIASES[rawPreset] || rawPreset;
  const templateSpec = !id && presetKey ? getTemplate(presetKey) : undefined;

  // A template renders its own wizard and never fetches, so it is never loading.
  // Derived here instead of cleared from the effect below — that write only told
  // the next render what this expression already knows.
  const loading = !templateSpec && fetching;

  // First real automation creation → offer the contextual mini-tutorial
  // (suppressed if the user already did the full "?" walkthrough).
  useEffect(() => {
    if (!id && !templateSpec) triggerMini('automation');
  }, [id, templateSpec]);

  useEffect(() => {
    if (templateSpec) return;
    const load = async () => {
      try {
        // Returning from a block's "Create a new workflow" hand-off: rehydrate the
        // draft we stashed before leaving and bind the entity the wizard just made
        // onto the block that asked for it. Takes precedence over the id fetch —
        // the draft IS the newer state of that same automation.
        if (searchParams.get('resume') === '1' && !restoredRef.current) {
          const draft = takeFlowDraft();
          if (draft) {
            restoredRef.current = true;
            const newId = Number(searchParams.get('bindWorkflowId'));
            const blocks = newId
              ? draft.blocks.map((b) => b.id === draft.pendingBlockId
                ? { ...b, config: { ...b.config, [draft.pendingField]: newId, input_mapping: {} } }
                : b)
              : draft.blocks;
            setInitialFlow({
              id: draft.flowId ?? undefined,
              name: draft.name,
              description: draft.description,
              enabled: draft.enabled,
              blocks,
            } as any);
            setRestoredDraft(true);
            setFetching(false);
            return;
          }
        }
        if (restoredRef.current) { setFetching(false); return; }
        if (id) {
          const data = await triggersApi.get(parseInt(id));
          // Inject top-level webhook token into the webhook_received block config
          // so the builder can display the URL
          if (data.webhook_trigger_token && data.blocks) {
            for (const block of data.blocks) {
              if (block.blockType === 'webhook_received') {
                block.config = block.config || {};
                block.config.webhook_trigger_token = data.webhook_trigger_token;
                if (data.webhook_trigger_id) {
                  block.config.webhook_trigger_id = data.webhook_trigger_id;
                }
                break;
              }
            }
          }
          setInitialFlow(data as any);
        } else {
          const presetId = searchParams.get('preset');
          const source = searchParams.get('source');
          const checkId = searchParams.get('checkId');
          const workflowId = searchParams.get('workflowId');

          const preset = presetId ? getPreset(presetId) : undefined;
          if (preset) {
            const { name, description, blocks } = preset.build();
            setInitialFlow({ name, description, blocks } as any);
          } else if (source === 'check' && checkId) {
            const target = await targetsApi.getById(checkId);
            setInitialFlow({
              name: t('Automation for {{name}}', { name: new URL(target.url).hostname }),
              blocks: [{
                id: genBlockId(), type: 'event', blockType: 'change_detected',
                config: { target_id: Number(checkId) },
              }],
            } as any);
          } else if (source === 'workflow' && workflowId) {
            const workflow = await automationApi.getWorkflow(Number(workflowId));
            const event = searchParams.get('event');
            let blockType = 'workflow_completed';
            const config: any = { workflow_id: Number(workflowId) };
            if (event === 'started') blockType = 'workflow_started';
            else if (event === 'error') {
              blockType = 'workflow_completed';
              config.status_condition = 'error';
            }
            setInitialFlow({
              name: t('Automation for {{name}}', { name: workflow.name }),
              blocks: [{
                id: genBlockId(), type: 'event', blockType,
                config,
              }],
            } as any);
          }
        }
      } catch {
        navigate('/automations');
      } finally {
        setFetching(false);
      }
    };
    load();
  }, [id, navigate, searchParams, templateSpec, t]);

  if (templateSpec) {
    return (
      <>
        <TemplateWizard spec={templateSpec} onCancel={() => navigate('/automations')} />
      </>
    );
  }

  if (loading) {
    return (
      <>
        <div className="flex flex-col items-center justify-center h-full text-center">
          <div className="w-2 h-2 rounded-full bg-border-strong animate-pulse mb-3" />
          <p className="text-xs text-tertiary">{id ? t('Loading automation…') : t('Setting up the builder…')}</p>
        </div>
      </>
    );
  }

  return (
    <>
      <FlowBuilderProvider initialFlow={initialFlow} initialFlowDirty={restoredDraft}>
        <FlowBuilder
          onSave={() => navigate('/automations')}
          onCancel={() => navigate('/automations')}
        />
      </FlowBuilderProvider>
    </>
  );
};
