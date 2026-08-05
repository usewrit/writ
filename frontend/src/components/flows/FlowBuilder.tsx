import React from 'react';
import { ArrowLeftIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import { useFlowBuilder } from './FlowBuilderContext';
import { SourceBlockPicker } from './blocks/SourceBlockPicker';
import { AiAutomationBar } from './AiAutomationBar';
import { BlockNode } from './blocks/BlockNode';
import { triggersApi, webhookTriggersApi } from '../../api/endpoints';

interface FlowBuilderProps {
  onSave?: (id: number) => void;
  onCancel?: () => void;
  embedded?: boolean;
}

export const FlowBuilder: React.FC<FlowBuilderProps> = ({ onSave, onCancel, embedded = false }) => {
  const { t } = useTranslation();
  const { state, dispatch } = useFlowBuilder();
  const { blocks, name, enabled, flowId, saving, isDirty, loading } = state; // enabled used in save payload

  const hasBlocks = blocks.length > 0;

  const handleSave = async () => {
    if (!name.trim()) {
      toast.error(t('Give your automation a name'));
      return;
    }
    if (blocks.length === 0) {
      toast.error(t('Add at least one block'));
      return;
    }

    dispatch({ type: 'SET_SAVING', saving: true });

    try {
      const eventBlock = blocks.find(b => b.type === 'event' && !b.parentId);
      const actionBlocks = blocks.filter(b => b.type === 'action');
      const actions = actionBlocks.map(b => ({ type: b.blockType, config: b.config }));

      let webhookTriggerId: number | undefined;
      if (eventBlock?.blockType === 'webhook_received' && !eventBlock.config.webhook_trigger_token) {
        const wh = await webhookTriggersApi.create({
          name: `${name} webhook`,
          action: 'run_workflow',
        });
        webhookTriggerId = wh.id;
        dispatch({
          type: 'UPDATE_BLOCK_CONFIG',
          blockId: eventBlock.id,
          config: { ...eventBlock.config, webhook_trigger_token: wh.token, webhook_trigger_id: wh.id },
        });
      }

      // Firing guardrails live on the root event block; persist them into the
      // trigger's `conditions` so the backend engine enforces them
      // (cooldown / fire-limit). Always sent so clearing them round-trips.
      const guardrails: any = {};
      const cooldownMin = Number(eventBlock?.config?.cooldown_minutes);
      const maxFires = Number(eventBlock?.config?.max_fires);
      if (cooldownMin > 0) guardrails.schedule = { cooldown_minutes: cooldownMin };
      if (maxFires > 0) guardrails.max_fires = maxFires;

      const payload: any = {
        name,
        description: state.description || undefined,
        enabled,
        event_type: eventBlock?.blockType || 'change_detected',
        target_id: eventBlock?.config?.target_id || undefined,
        target_selector_id: eventBlock?.config?.selector_id || undefined,
        ai_session_id: eventBlock?.config?.ai_session_id || undefined,
        workflow_id: eventBlock?.config?.workflow_id || undefined,
        webhook_trigger_id: webhookTriggerId || eventBlock?.config?.webhook_trigger_id || undefined,
        actions,
        blocks,
        conditions: guardrails,
        priority: 0,
      };

      let savedId: number;
      if (flowId) {
        await triggersApi.update(flowId, payload);
        savedId = flowId;
        toast.success(t('Automation updated'));
      } else {
        const result = await triggersApi.create(payload);
        savedId = result.id;
        toast.success(t('Automation created'));
      }

      dispatch({ type: 'MARK_SAVED', flowId: savedId });
      onSave?.(savedId);
    } catch (err: any) {
      const msg = err?.response?.data?.detail || t('Failed to save automation');
      toast.error(msg);
      dispatch({ type: 'SET_SAVING', saving: false, error: msg });
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <div className="text-sm text-secondary">{t('Loading...')}</div>
      </div>
    );
  }

  return (
    <div className="relative flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center gap-3 h-12 px-4 sm:px-6 bg-chrome chrome-topbar border-b border-border shrink-0">
        {!embedded && onCancel && (
          <button type="button" onClick={onCancel} className="p-1 text-tertiary hover:text-ink transition-colors">
            <ArrowLeftIcon className="w-3.5 h-3.5" />
          </button>
        )}
        <input
          type="text"
          data-tour="flow-name"
          value={name}
          onChange={e => dispatch({ type: 'SET_META', name: e.target.value })}
          className="flex-1 max-w-xs px-2 py-1 bg-transparent text-[13px] font-semibold text-ink outline-none border-b border-transparent hover:border-border focus:border-ink/30 transition-colors"
          placeholder={t('Automation name...')}
        />
        <div className="flex-1" />
        {hasBlocks && (
          <button
            type="button"
            data-tour="flow-save"
            onClick={handleSave}
            disabled={saving || !isDirty}
            className={clsx(
              'px-3 py-1.5 text-[12px] font-medium rounded-lg transition-colors',
              saving || !isDirty
                ? 'bg-hover text-tertiary cursor-not-allowed'
                : 'bg-accent-strong text-accent-on hover:bg-accent-strong/90'
            )}
          >
            {saving ? t('Saving...') : flowId ? t('Update') : t('Save')}
          </button>
        )}
      </div>

      {/* Workspace canvas. The dot grid is a SIBLING of the scroller, not a child:
          an absolutely-positioned child inside an `overflow-auto` box is anchored
          to that box's padding box, so it can't ride the scrolled layer and the
          compositor has to repaint the whole gradient on the main thread every
          frame. That only bites once the content actually overflows, which is why
          the jank appeared exactly when a flow grew past one screen. */}
      <div data-tour="flow-canvas" className="relative flex-1 min-h-0">
        <div
          aria-hidden="true"
          className="absolute inset-0 pointer-events-none"
          style={{
            backgroundImage: 'radial-gradient(circle, rgb(var(--border)) 1px, transparent 1px)',
            backgroundSize: '24px 24px',
            opacity: 0.4,
          }}
        />

        <div className="absolute inset-0 overflow-auto">
          {/* pb clears the floating AI bar (pill at bottom-6 / progress strip at
              bottom-20) so the create-start choices are never hidden behind it. */}
          <div className="relative z-10 px-4 pt-8 pb-32">
          {!hasBlocks ? (
            <SourceBlockPicker />
          ) : (
            <div className="mx-auto">
              {/* Render the full block tree — source panel at full width, action cards centered */}
              {blocks.filter(b => !b.parentId).map(block => (
                <BlockNode key={block.id} block={block} />
              ))}
            </div>
          )}
        </div>
        </div>
      </div>

      {/* Floating AI author — the "describe it, AI builds it" input on the start
          step, which becomes a live progress strip during generation. */}
      {!embedded && <AiAutomationBar />}

    </div>
  );
};
