import React, { useMemo, useState } from 'react';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import { automationApi } from '../api/endpoints';
import type { WorkflowStep } from '../types/api';
import { StepsEditor } from './steps/StepsEditor';
import { StreamingScriptEditor } from '../pages/workflows/detail/StreamingScriptEditor';
import { Checkbox, Select } from './ui';
import {
  ClockIcon,
  ArrowPathIcon,
  EyeIcon,
  EyeSlashIcon,
  KeyIcon,
  GlobeAltIcon,
  CheckCircleIcon,
  PuzzlePieceIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';

interface Workflow {
  id: number;
  name: string;
  description?: string;
  workflow_type: string;
  steps: WorkflowStep[];
  form_data?: Record<string, string>;
  credentials?: Record<string, string>;
  has_credentials?: boolean;
  entry_url?: string;
  exit_condition?: { type: string; value: string } | null;
  timeout_ms: number;
  retry_count: number;
  headless: boolean;
  is_active: boolean;
  schedule_enabled?: boolean;
  schedule_interval_ms?: number | null;
  /** Streaming engine/session config — holds the legacy advanced_script fallback. */
  streaming_config?: Record<string, any> | null;
  /** Callable step-group / extraction functions defined for this workflow. */
  functions?: Array<{
    name: string;
    type?: string;
    description?: string;
    step_range?: [number, number];
    step_indices?: number[];
  }> | null;
}

interface WorkflowDetailsProps {
  workflow: Workflow;
  onRun?: () => void;
  onEdit?: () => void;
  onUpdate?: () => void;
  /** Hide the inline timeout/retries/headless editor (the detail page hosts it in its Settings tab). */
  hideRunSettings?: boolean;
}

export const WorkflowDetails: React.FC<WorkflowDetailsProps> = ({ workflow, onUpdate, hideRunSettings }) => {
  const { t } = useTranslation();
  const [editingSettings, setEditingSettings] = useState(false);
  const [settings, setSettings] = useState({
    timeout_ms: workflow.timeout_ms,
    retry_count: workflow.retry_count,
    headless: workflow.headless,
  });
  const [savingSettings, setSavingSettings] = useState(false);

  // A streaming workflow's "advanced script" (the live-session message handler) is
  // persisted as an `advanced_script` step. Surface it in the Steps tab via its own
  // editor and keep it OUT of the generic step list — it isn't a sequential step.
  const isStreaming = workflow.workflow_type === 'streaming';
  const hasAdvancedScript = Array.isArray(workflow.steps) && workflow.steps.some(s => s?.type === 'advanced_script');
  const showScriptEditor = isStreaming || hasAdvancedScript;
  const visibleSteps = useMemo(() => {
    const arr = Array.isArray(workflow.steps) ? workflow.steps : [];
    return showScriptEditor ? arr.filter(s => s?.type !== 'advanced_script') : arr;
  }, [workflow.steps, showScriptEditor]);

  const handleSaveSettings = async () => {
    setSavingSettings(true);
    try {
      await automationApi.updateWorkflow(workflow.id, settings);
      toast.success(t('Settings saved'));
      setEditingSettings(false);
      onUpdate?.();
    } catch {
      toast.error(t('Failed to save'));
    } finally {
      setSavingSettings(false);
    }
  };

  const handleSaveSteps = async (steps: WorkflowStep[]) => {
    try {
      // The advanced_script step is edited via StreamingScriptEditor, not the generic
      // list — re-attach it (appended last) so saving the visible steps never drops it.
      const advanced = showScriptEditor && Array.isArray(workflow.steps)
        ? workflow.steps.filter(s => s?.type === 'advanced_script')
        : [];
      await automationApi.updateWorkflow(workflow.id, { steps: [...steps, ...advanced] } as any);
      toast.success(t('Steps saved'));
      onUpdate?.();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('Failed to save steps'));
      throw err; // keep the editor dirty so edits aren't lost
    }
  };

  // Persist the workflow's callable functions (step-groups). `functions` is the
  // full canonical surface — append/remove, then write the whole list back.
  const saveFunctions = async (functions: any[]) => {
    await automationApi.updateWorkflow(workflow.id, { functions } as any);
    onUpdate?.();
  };

  const handleCreateFunction = async (fn: any) => {
    await saveFunctions([...(workflow.functions || []), fn]);
  };

  const handleRemoveFunction = async (name: string) => {
    try {
      await saveFunctions((workflow.functions || []).filter(f => f.name !== name));
      toast.success(t('Function removed'));
    } catch {
      toast.error(t('Failed to save'));
    }
  };

  return (
    <div className="space-y-4">
      {/* Config pills row */}
      <div className="flex items-center gap-2 flex-wrap">
        {workflow.entry_url && (
          <div className="flex items-center gap-1.5 px-2.5 py-1 bg-canvas border border-border rounded-lg text-[11px]">
            <GlobeAltIcon className="h-3 w-3 text-tertiary" />
            <span className="text-ink font-mono truncate max-w-[200px]">{workflow.entry_url}</span>
          </div>
        )}
        <div className="flex items-center gap-1.5 px-2.5 py-1 bg-canvas border border-border rounded-lg text-[11px] text-secondary">
          <ClockIcon className="h-3 w-3 text-tertiary" />
          {workflow.timeout_ms / 1000}s
        </div>
        <div className="flex items-center gap-1.5 px-2.5 py-1 bg-canvas border border-border rounded-lg text-[11px] text-secondary">
          <ArrowPathIcon className="h-3 w-3 text-tertiary" />
          {t('{{n}} retries', { n: workflow.retry_count })}
        </div>
        <div className="flex items-center gap-1.5 px-2.5 py-1 bg-canvas border border-border rounded-lg text-[11px] text-secondary">
          {workflow.headless
            ? <><EyeSlashIcon className="h-3 w-3 text-tertiary" /> {t('Headless')}</>
            : <><EyeIcon className="h-3 w-3 text-tertiary" /> {t('Visible')}</>
          }
        </div>
        {workflow.has_credentials && (
          <div className="flex items-center gap-1.5 px-2.5 py-1 bg-canvas border border-border rounded-lg text-[11px] text-secondary">
            <KeyIcon className="h-3 w-3 text-tertiary" />
            {t('Credentials')}
          </div>
        )}
        {workflow.schedule_enabled && (
          <div className="flex items-center gap-1.5 px-2.5 py-1 bg-canvas border border-border rounded-lg text-[11px] text-secondary">
            <ClockIcon className="h-3 w-3 text-tertiary" />
            {t('Every {{interval}}', {
              interval: workflow.schedule_interval_ms && workflow.schedule_interval_ms >= 3600000
                ? `${workflow.schedule_interval_ms / 3600000}h`
                : workflow.schedule_interval_ms
                  ? `${workflow.schedule_interval_ms / 60000}m`
                  : '--',
            })}
          </div>
        )}
        {workflow.exit_condition && (
          <div className="flex items-center gap-1.5 px-2.5 py-1 bg-canvas border border-border rounded-lg text-[11px] text-secondary">
            <CheckCircleIcon className="h-3 w-3 text-tertiary" />
            {workflow.exit_condition.type === 'url_contains' && t('URL contains "{{value}}"', { value: workflow.exit_condition.value })}
            {workflow.exit_condition.type === 'element_exists' && t('Element: {{value}}', { value: workflow.exit_condition.value })}
          </div>
        )}
        {!hideRunSettings && (
          <button
            onClick={() => setEditingSettings(!editingSettings)}
            className="ml-auto text-[11px] text-tertiary hover:text-ink transition-colors"
          >
            {editingSettings ? t('Cancel') : t('Edit')}
          </button>
        )}
      </div>

      {/* Editable settings panel */}
      {!hideRunSettings && editingSettings && (
        <div className="flex items-center gap-4 p-3 bg-canvas border border-border rounded-xl">
          <label className="flex items-center gap-2 text-xs">
            <span className="text-secondary">{t('Timeout')}</span>
            <Select<number>
              size="sm"
              value={settings.timeout_ms}
              onChange={v => setSettings(s => ({ ...s, timeout_ms: v }))}
              options={[15000, 30000, 60000, 120000, 300000].map(v => ({ value: v, label: `${v / 1000}s` }))}
            />
          </label>
          <label className="flex items-center gap-2 text-xs">
            <span className="text-secondary">{t('Retries')}</span>
            <Select<number>
              size="sm"
              value={settings.retry_count}
              onChange={v => setSettings(s => ({ ...s, retry_count: v }))}
              options={[0, 1, 2, 3, 5].map(v => ({ value: v, label: String(v) }))}
            />
          </label>
          <Checkbox
            checked={settings.headless}
            onChange={e => setSettings(s => ({ ...s, headless: e.target.checked }))}
            label={t('Headless')}
            size="sm"
          />
          <button onClick={handleSaveSettings} disabled={savingSettings}
            className="ml-auto px-3 py-1 text-xs font-medium bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 disabled:opacity-50">
            {savingSettings ? t('Saving...') : t('Save')}
          </button>
        </div>
      )}

      {/* Callable functions (step-groups). Create new ones with "Group into function"
          in the steps editor below; full editing (inputs/outputs) lives in the Connect
          tab. This surfaces which steps each one spans, alongside the steps. */}
      {(workflow.functions || []).length > 0 && (
        <div className="rounded-xl border border-border bg-canvas p-3">
          <div className="flex items-center gap-1.5 mb-2">
            <PuzzlePieceIcon className="h-3.5 w-3.5 text-tertiary" />
            <span className="text-[11px] font-medium text-secondary uppercase tracking-wider">
              {t('Functions')}
            </span>
            <span className="text-[10px] text-tertiary">{t('edit in the Connect tab')}</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {(workflow.functions || []).map((fn, i) => {
              const range = fn.step_range && fn.step_range.length === 2
                ? t('steps {{from}}–{{to}}', { from: fn.step_range[0] + 1, to: fn.step_range[1] })
                : fn.step_indices && fn.step_indices.length
                  ? t('{{n}} steps', { n: fn.step_indices.length })
                  : null;
              return (
                <div key={`${fn.name}-${i}`}
                  className="group flex items-center gap-1.5 px-2.5 py-1 bg-surface border border-border rounded-lg text-[11px]"
                  title={fn.description || undefined}>
                  <span className="font-mono font-medium text-ink">{fn.name}</span>
                  {range && <span className="text-tertiary">{range}</span>}
                  <button
                    onClick={() => handleRemoveFunction(fn.name)}
                    className="text-tertiary hover:text-red-500 transition-colors opacity-0 group-hover:opacity-100"
                    title={t('Remove function')}
                  >
                    <XMarkIcon className="h-3 w-3" />
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Unified interactive steps editor */}
      <StepsEditor
        steps={visibleSteps}
        onSave={handleSaveSteps}
        existingFunctionNames={(workflow.functions || []).map(f => f.name)}
        onCreateFunction={handleCreateFunction}
      />

      {/* Streaming advanced script — the live-session message handler, editable here
          in the Steps tab (view + edit, with its typed callable-function declarations). */}
      {showScriptEditor && (
        <StreamingScriptEditor
          workflowId={workflow.id}
          workflow={workflow}
          streamingConfig={workflow.streaming_config}
          onUpdate={() => onUpdate?.()}
        />
      )}
    </div>
  );
};
