import React from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import { CodeBracketIcon, ChevronDownIcon } from '@heroicons/react/24/outline';
import { automationApi } from '../../../api/endpoints';
import type { WorkflowStep, WorkflowFunction } from '../../../types/api';
import { Section } from './Section';
import { Expand } from '../../../components/ui';

// Default scaffold — keeps the catch-all `message` handler so a workflow with NO
// declared functions still receives every action (backwards compatible). Users
// add named handlers via ps.on("name", …) (alias ps.fn) and declare them below.
const DEFAULT_SCRIPT = `ps.on("message", async ({ action, data, requestId }) => {
  // Catch-all: receives every action when no named handler matches.
  ps.respond(requestId, { ok: true });
});`;

const NAME_RE = /^[a-zA-Z_$][a-zA-Z0-9_$]*$/;

// The function row shape we edit. Inputs/outputs are kept as concrete objects
// (not the loose string-or-object union) so the editor controls stay typed.
interface DeclaredInput { name: string; type: string; description: string; required: boolean }
interface DeclaredOutput { name: string; type: string; description: string }
interface DeclaredFunction {
  name: string;
  description: string;
  input_variables: DeclaredInput[];
  output_fields: DeclaredOutput[];
}

/** Normalize a stored function (loose api shape) into the editor's row shape. */
function toDeclared(fn: WorkflowFunction): DeclaredFunction {
  return {
    name: fn.name || '',
    description: fn.description || '',
    input_variables: (fn.input_variables || []).map((v) =>
      typeof v === 'string'
        ? { name: v, type: 'string', description: '', required: true }
        : { name: v.name || '', type: v.type || 'string', description: v.description || '', required: v.required ?? true },
    ),
    output_fields: (fn.output_fields || []).map((f) => ({
      name: f.name || '', type: f.type || 'string', description: f.description || '',
    })),
  };
}

/** Read the authoritative advanced_script step (or fall back to streaming_config). */
function readAdvancedScript(workflow: any, streamingConfig: any): { code: string; functions: DeclaredFunction[] } {
  const steps: WorkflowStep[] = workflow?.steps || [];
  const stepEntry = steps.find((s) => s?.type === 'advanced_script');
  if (stepEntry?.config?.code) {
    const fns: WorkflowFunction[] = stepEntry.config.functions || [];
    return { code: stepEntry.config.code || '', functions: fns.map(toDeclared) };
  }
  const legacy = streamingConfig?.advanced_script;
  return {
    code: legacy?.code || '',
    functions: (legacy?.functions || []).map(toDeclared),
  };
}

export const StreamingScriptEditor: React.FC<{
  workflowId: number;
  workflow: any;
  streamingConfig: any;
  onUpdate: () => void;
}> = ({ workflowId, workflow, streamingConfig, onUpdate }) => {
  const { t } = useTranslation();

  const initial = React.useMemo(() => readAdvancedScript(workflow, streamingConfig), [workflow, streamingConfig]);
  const [editing, setEditing] = React.useState(false);
  const [code, setCode] = React.useState(initial.code);
  const [functions, setFunctions] = React.useState<DeclaredFunction[]>(initial.functions);
  const [expandedFn, setExpandedFn] = React.useState<number | null>(null);
  const [saving, setSaving] = React.useState(false);

  // Re-sync when the workflow reloads (e.g. after onUpdate). Only resets while
  // NOT actively editing so an in-progress edit isn't clobbered by a poll.
  // Adjusted DURING RENDER (React's derive-state-from-props escape hatch) instead
  // of from an effect, so a poll never paints the stale script for a frame. The
  // key goes null while editing, which is what makes leaving edit mode re-sync.
  const syncKey = editing ? null : initial;
  const [syncedTo, setSyncedTo] = React.useState<typeof initial | null>(syncKey);
  if (syncedTo !== syncKey) {
    setSyncedTo(syncKey);
    if (syncKey) {
      setCode(syncKey.code);
      setFunctions(syncKey.functions);
    }
  }

  const cancel = () => {
    setCode(initial.code);
    setFunctions(initial.functions);
    setExpandedFn(null);
    setEditing(false);
  };

  const updateFn = (idx: number, updates: Partial<DeclaredFunction>) =>
    setFunctions((prev) => prev.map((f, i) => (i === idx ? { ...f, ...updates } : f)));

  const addFn = () => {
    setFunctions((prev) => [...prev, { name: '', description: '', input_variables: [], output_fields: [] }]);
    setExpandedFn(functions.length);
  };

  const removeFn = (idx: number) => {
    setFunctions((prev) => prev.filter((_, i) => i !== idx));
    setExpandedFn(null);
  };

  const validateFunctions = (): WorkflowFunction[] | null => {
    const seen = new Set<string>();
    const out: WorkflowFunction[] = [];
    for (const fn of functions) {
      const name = fn.name.trim();
      if (!name) { toast.error(t('Each function needs a name.')); return null; }
      if (!NAME_RE.test(name)) {
        toast.error(t('Invalid function name: {{name}}. Use letters, numbers, _ or $ (not starting with a number).', { name }));
        return null;
      }
      if (seen.has(name)) { toast.error(t('Duplicate function name: {{name}}.', { name })); return null; }
      seen.add(name);
      out.push({
        name,
        type: 'script',
        description: fn.description.trim(),
        handler_event: name,
        input_variables: fn.input_variables
          .map((v) => ({ ...v, name: v.name.trim() }))
          .filter((v) => v.name),
        output_fields: fn.output_fields
          .map((f) => ({ ...f, name: f.name.trim() }))
          .filter((f) => f.name),
      });
    }
    return out;
  };

  const handleSave = async () => {
    const declared = validateFunctions();
    if (declared === null) return;
    const trimmed = code.trim();

    setSaving(true);
    try {
      // Write the authoritative advanced_script STEP into workflow.steps (the
      // backend `_sync_advanced_script_functions` lifts its `functions` into
      // workflow.functions → MCP / Managed-API / marketplace output-manifest).
      // Preserve every other step; replace the single advanced_script step.
      // The backend WorkflowStepCreate REQUIRES an `id`, so carry the existing
      // step's id (stable across re-saves) or mint one.
      const existing: WorkflowStep[] = workflow?.steps || [];
      const prevStep = existing.find((s) => s?.type === 'advanced_script');
      const others = existing.filter((s) => s?.type !== 'advanced_script');
      const stepId = prevStep?.id || `advscript-${(globalThis.crypto?.randomUUID?.() || `${Date.now()}`)}`.slice(0, 36);
      const stepConfig = { code: trimmed, persistent: true, functions: declared };
      const nextSteps: WorkflowStep[] = trimmed
        ? [...others, { id: stepId, type: 'advanced_script', enabled: true, config: stepConfig }]
        : others; // empty code removes the step entirely

      // Keep streaming_config.advanced_script in sync for back-compat with any
      // session path that still reads it (runtime fallback authoritative there).
      const nextConfig = {
        ...streamingConfig,
        advanced_script: {
          enabled: !!trimmed,
          code: trimmed,
          persistent: true,
          functions: declared,
        },
      };

      await automationApi.updateWorkflow(workflowId, {
        steps: nextSteps,
        streaming_config: nextConfig,
      } as any);
      toast.success(t('Script saved'));
      setEditing(false);
      setExpandedFn(null);
      onUpdate();
    } catch {
      toast.error(t('Failed to save'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Section
      title={t('Advanced script')}
      description={t('JavaScript injected into the live session page. Register handlers with ps.on("name", fn) (alias ps.fn). Declare them below to expose as callable functions to MCP / API.')}
      right={!editing ? (
        <button onClick={() => setEditing(true)} className="text-[11px] text-tertiary hover:text-ink transition-colors">
          {initial.code ? t('Edit') : t('Add script')}
        </button>
      ) : (
        <div className="flex items-center gap-2">
          <button onClick={cancel} className="text-[11px] text-tertiary hover:text-ink transition-colors">{t('Cancel')}</button>
          <button onClick={handleSave} disabled={saving} className="px-2.5 py-1 text-[11px] font-semibold shadow-sm bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 disabled:opacity-50 transition-colors">
            {saving ? t('Saving...') : t('Save')}
          </button>
        </div>
      )}
    >
      {editing ? (
        <div className="space-y-4">
          <textarea
            value={code}
            onChange={(e) => setCode(e.target.value)}
            rows={12}
            className="w-full px-4 py-3 bg-zinc-900 text-zinc-100 text-[11px] font-mono rounded-xl border border-zinc-700/50 resize-y leading-relaxed focus:outline-none focus:ring-1 focus:ring-zinc-600"
            spellCheck={false}
            placeholder={DEFAULT_SCRIPT}
          />

          {/* Callable functions declaration (Feature #2). */}
          <div className="bg-surface border border-ink/20 rounded-xl overflow-hidden shadow-sm">
            <div className="px-4 py-2.5 border-b border-border">
              <p className="text-[12px] font-medium text-ink">{t('Callable functions')}</p>
              <p className="text-[11px] text-tertiary mt-0.5">
                {t('Declare named ps.on(...) handlers as typed functions — they become callable via MCP / API and appear in the marketplace output manifest.')}
              </p>
            </div>

            {functions.length > 0 && (
              <div className="divide-y divide-border">
                {functions.map((fn, idx) => (
                  <div key={idx} className="px-4 py-2.5">
                    <div className="flex items-center gap-3 cursor-pointer" onClick={() => setExpandedFn(expandedFn === idx ? null : idx)}>
                      <div className="w-2 h-2 rounded-full shrink-0 bg-zinc-600" />
                      <div className="flex-1 min-w-0">
                        <span className="text-[12px] font-mono font-medium text-ink">{fn.name || t('unnamed')}</span>
                        {fn.description && <p className="text-[11px] text-tertiary mt-0.5 truncate">{fn.description}</p>}
                      </div>
                      <ChevronDownIcon className={clsx('w-3.5 h-3.5 text-tertiary transition-transform', expandedFn === idx && 'rotate-180')} />
                    </div>

                    <Expand open={expandedFn === idx} mountOnEnter className="mt-3 pl-5 space-y-2.5 pb-1">
                        <div className="flex items-center gap-2">
                          <span className="text-[11px] text-tertiary w-20">{t('Name')}</span>
                          <input
                            value={fn.name}
                            onChange={(e) => updateFn(idx, { name: e.target.value.replace(/[^a-zA-Z0-9_$]/g, '') })}
                            placeholder={t('function_name')}
                            className="flex-1 px-2.5 py-1 text-[11px] font-mono bg-hover border border-border rounded-lg focus:outline-none focus:ring-1 focus:ring-border"
                          />
                        </div>
                        <input
                          value={fn.description}
                          onChange={(e) => updateFn(idx, { description: e.target.value })}
                          placeholder={t('Description (shown to AI models)')}
                          className="w-full px-2.5 py-1.5 text-[11px] bg-hover border border-border rounded-lg focus:outline-none focus:ring-1 focus:ring-border"
                        />

                        {/* Inputs */}
                        <div>
                          <div className="flex items-center justify-between">
                            <span className="text-[10px] text-tertiary uppercase tracking-wider">{t('Inputs')}</span>
                            <button
                              onClick={() => updateFn(idx, { input_variables: [...fn.input_variables, { name: '', type: 'string', description: '', required: true }] })}
                              className="text-[10px] text-secondary hover:text-ink"
                            >
                              {t('+ Add')}
                            </button>
                          </div>
                          {fn.input_variables.map((v, vi) => (
                            <div key={vi} className="flex items-center gap-1.5 mt-1">
                              <input
                                value={v.name}
                                onChange={(e) => updateFn(idx, { input_variables: fn.input_variables.map((x, i) => i === vi ? { ...x, name: e.target.value } : x) })}
                                placeholder={t('name')}
                                className="w-24 px-2 py-0.5 text-[10px] font-mono bg-hover border border-border rounded focus:outline-none"
                              />
                              <input
                                value={v.description}
                                onChange={(e) => updateFn(idx, { input_variables: fn.input_variables.map((x, i) => i === vi ? { ...x, description: e.target.value } : x) })}
                                placeholder={t('description')}
                                className="flex-1 px-2 py-0.5 text-[10px] bg-hover border border-border rounded focus:outline-none"
                              />
                              <button
                                onClick={() => updateFn(idx, { input_variables: fn.input_variables.filter((_, i) => i !== vi) })}
                                className="text-[10px] text-tertiary hover:text-ink"
                                aria-label={t('Remove input')}
                              >
                                ×
                              </button>
                            </div>
                          ))}
                        </div>

                        {/* Outputs */}
                        <div>
                          <div className="flex items-center justify-between">
                            <span className="text-[10px] text-tertiary uppercase tracking-wider">{t('Outputs')}</span>
                            <button
                              onClick={() => updateFn(idx, { output_fields: [...fn.output_fields, { name: '', type: 'string', description: '' }] })}
                              className="text-[10px] text-secondary hover:text-ink"
                            >
                              {t('+ Add')}
                            </button>
                          </div>
                          {fn.output_fields.map((f, fi) => (
                            <div key={fi} className="flex items-center gap-1.5 mt-1">
                              <input
                                value={f.name}
                                onChange={(e) => updateFn(idx, { output_fields: fn.output_fields.map((x, i) => i === fi ? { ...x, name: e.target.value } : x) })}
                                placeholder={t('name')}
                                className="w-24 px-2 py-0.5 text-[10px] font-mono bg-hover border border-border rounded focus:outline-none"
                              />
                              <input
                                value={f.description}
                                onChange={(e) => updateFn(idx, { output_fields: fn.output_fields.map((x, i) => i === fi ? { ...x, description: e.target.value } : x) })}
                                placeholder={t('description')}
                                className="flex-1 px-2 py-0.5 text-[10px] bg-hover border border-border rounded focus:outline-none"
                              />
                              <button
                                onClick={() => updateFn(idx, { output_fields: fn.output_fields.filter((_, i) => i !== fi) })}
                                className="text-[10px] text-tertiary hover:text-ink"
                                aria-label={t('Remove output')}
                              >
                                ×
                              </button>
                            </div>
                          ))}
                        </div>

                        <button onClick={() => removeFn(idx)} className="text-[11px] text-tertiary hover:text-ink mt-1">
                          {t('Remove function')}
                        </button>
                    </Expand>
                  </div>
                ))}
              </div>
            )}

            <div className="px-4 py-2.5 border-t border-border">
              <button onClick={addFn} className="text-[11px] text-secondary hover:text-ink transition-colors">
                {t('+ Add function')}
              </button>
            </div>
          </div>
        </div>
      ) : initial.code ? (
        <div className="space-y-2.5">
          <pre
            className="px-4 py-3 bg-zinc-900 text-zinc-100 text-[11px] font-mono rounded-xl overflow-x-auto leading-relaxed max-h-64 overflow-y-auto border border-zinc-700/50 cursor-pointer hover:ring-1 hover:ring-zinc-600 transition-all"
            onClick={() => setEditing(true)}
          >
            {initial.code}
          </pre>
          {initial.functions.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {initial.functions.map((fn, i) => (
                <span key={i} className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-hover text-secondary">{fn.name}</span>
              ))}
            </div>
          )}
        </div>
      ) : (
        <button
          type="button"
          onClick={() => { setCode(DEFAULT_SCRIPT); setEditing(true); }}
          className="w-full flex items-center justify-between gap-3 rounded-xl border border-ink/20 shadow-sm bg-surface px-4 py-3.5 text-left hover:bg-chrome transition-colors"
        >
          <div className="flex items-start gap-3 min-w-0">
            <CodeBracketIcon className="w-4 h-4 text-tertiary shrink-0 mt-0.5" />
            <div className="min-w-0">
              <p className="text-[13px] font-medium text-ink">{t('No script configured')}</p>
              <p className="text-[13px] text-tertiary mt-0.5">{t('Add a JavaScript handler to respond to incoming messages.')}</p>
            </div>
          </div>
          <span className="text-[11px] text-tertiary shrink-0">{t('Add script')}</span>
        </button>
      )}
    </Section>
  );
};
