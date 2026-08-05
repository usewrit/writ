import React from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import i18n from '../../../i18n';
import { ChevronDownIcon, PlusIcon, SparklesIcon } from '@heroicons/react/24/outline';
import { automationApi } from '../../../api/endpoints';
import { NumberInput } from '../../../components/ui';
import { Section } from './Section';

export interface WorkflowFunction {
  name: string;
  type: 'steps' | 'script' | 'extraction';
  description: string;
  step_range?: [number, number];
  input_variables?: { name: string; type: string; description: string; required: boolean }[];
  output_fields?: { name: string; type: string; description: string; selector?: string }[];
  handler_event?: string;
  selector?: string;
  js_expression?: string;
}

/**
 * The workflow's OWN streaming/legacy handlers (streaming_config) that exist but are
 * not yet exposed as callable functions → function entries. The Connect tab AUTO-LISTS
 * these on load so the creator never has to click "Import as functions" to see the
 * full callable surface; the (always-required) Save then persists them as MCP tools /
 * API operations. Pure — derived only from the workflow's own config.
 */
function detectImportableFunctions(workflow: any, existing: WorkflowFunction[]): WorkflowFunction[] {
  if (workflow.workflow_type !== 'streaming') return [];
  const sc = workflow.streaming_config || {};
  const have = new Set(existing.map((f) => f.name));
  const out: WorkflowFunction[] = [];
  for (const h of (sc.handlers || [])) {
    if (have.has(h.name)) continue;
    out.push({
      name: h.name,
      type: 'steps',
      description: i18n.t('Handler: {{name}}', { name: h.name }),
      step_range: h.step_range,
      input_variables: (h.input_variables || []).map((v: string) => ({
        name: v, type: 'string', description: '', required: true,
      })),
      output_fields: (h.extract_fields || []).map((f: string) => ({
        name: f, type: 'string', description: '',
      })),
    });
  }
  if ((sc.openai_compat?.enabled || sc.advanced_script?.enabled) && !have.has('chat')) {
    out.push({
      name: 'chat',
      type: 'script',
      description: i18n.t('Send a message to the AI chat session'),
      handler_event: 'message',
      input_variables: [{ name: 'message', type: 'string', description: i18n.t('The message to send'), required: true }],
      output_fields: [{ name: 'content', type: 'string', description: i18n.t('AI response') }],
    });
  }
  return out;
}

/**
 * Callable units DETECTED in the workflow's own steps — extraction steps,
 * evaluate-JS steps, and the typed functions declared by an advanced-script step.
 * These are surfaced as one-click "expose" suggestions so the creator can see the
 * full functional surface of the workflow without hand-authoring each function.
 * Pure — derived only from `workflow.steps`. Deduped by name.
 */
function detectStepCandidates(workflow: any): WorkflowFunction[] {
  const steps: any[] = workflow.steps || [];
  const out: WorkflowFunction[] = [];
  steps.forEach((s: any, i: number) => {
    const c = s.config || {};
    if (s.type === 'advanced_script') {
      // The advanced script step declares its own typed functions.
      for (const fn of (c.functions || [])) {
        if (fn?.name) out.push({ ...fn, type: fn.type || 'script' });
      }
    } else if (s.type === 'extract') {
      const name = c.variable || c.output_key || `extract_${i + 1}`;
      out.push({
        name,
        type: 'extraction',
        description: s.description || i18n.t('Extract “{{name}}” from the page', { name }),
        selector: c.selector || s.selector || '',
        output_fields: [{ name, type: 'string', description: '' }],
      });
    } else if (s.type === 'evaluate') {
      const name = c.variable || c.output_key || `evaluate_${i + 1}`;
      out.push({
        name,
        type: 'script',
        description: s.description || i18n.t('Run a JavaScript snippet and return its result'),
        js_expression: c.script || s.script || '',
      });
    } else if (s.type === 'api_call') {
      // A GET api_call is a read — it returns data, so it's a callable data function (parity with
      // extract/evaluate). A POST/PUT/etc. mutates and isn't surfaced as a data getter. Backed by a
      // step-group spanning the workflow's start THROUGH this call, so invoking it replays the auth
      // prelude (navigate + login_post) and then the fetch — live, authenticated data every call.
      const method = String(c.method || 'GET').toUpperCase();
      if (method === 'GET') {
        const name = c.variable || c.output_key || `api_${i + 1}`;
        out.push({
          name,
          type: 'steps',
          description: s.description || i18n.t('Call the API and return its data'),
          step_range: [0, i + 1],
          output_fields: [{ name, type: 'string', description: '' }],
        });
      }
    }
  });
  const seen = new Set<string>();
  return out.filter((f) => !!f.name && !seen.has(f.name) && (seen.add(f.name), true));
}

export const FunctionEditor: React.FC<{ workflowId: number; workflow: any; onUpdate: () => void }> = ({ workflowId, workflow, onUpdate }) => {
  const { t } = useTranslation();
  // Auto-list present-but-unlisted handlers on load (no manual "Import" click). Seed
  // the list with the workflow's functions PLUS its detected handlers, and start
  // dirty when anything was auto-detected so the Save affordance surfaces (Save is the
  // conscious action that persists them — and is what always triggered any re-review).
  const [functions, setFunctions] = React.useState<WorkflowFunction[]>(() => {
    const base: WorkflowFunction[] = workflow.functions || [];
    return [...base, ...detectImportableFunctions(workflow, base)];
  });
  const [editingIdx, setEditingIdx] = React.useState<number | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [showAdd, setShowAdd] = React.useState(false);
  const [dirty, setDirty] = React.useState<boolean>(
    () => detectImportableFunctions(workflow, workflow.functions || []).length > 0,
  );

  const steps = workflow.steps || [];

  // Callable units found in the steps that aren't exposed yet → one-click expose.
  const stepCandidates = React.useMemo(() => detectStepCandidates(workflow), [workflow]);
  const exposedNames = new Set(functions.map((f) => f.name));
  const suggestions = stepCandidates.filter((c) => !exposedNames.has(c.name));

  const exposeSuggestion = (fn: WorkflowFunction) => {
    setFunctions((prev) => (prev.some((f) => f.name === fn.name) ? prev : [...prev, fn]));
    setDirty(true);
  };

  // New function defaults
  const [newFn, setNewFn] = React.useState<WorkflowFunction>({
    name: '', type: 'steps', description: '',
    step_range: [0, steps.length],
    input_variables: [], output_fields: [],
  });

  const handleSave = async () => {
    setSaving(true);
    try {
      await automationApi.updateWorkflow(workflowId, { functions } as any);
      toast.success(t('Functions saved'));
      setDirty(false);
      onUpdate();
    } catch { toast.error(t('Failed to save')); }
    finally { setSaving(false); }
  };

  const addFunction = () => {
    if (!newFn.name) { toast.error(t('Name required')); return; }
    if (functions.some(f => f.name === newFn.name)) { toast.error(t('Name already used')); return; }
    setFunctions([...functions, { ...newFn }]);
    setNewFn({ name: '', type: 'steps', description: '', step_range: [0, steps.length], input_variables: [], output_fields: [] });
    setShowAdd(false);
    setDirty(true);
  };

  const updateFunction = (idx: number, updates: Partial<WorkflowFunction>) => {
    setFunctions(functions.map((f, i) => i === idx ? { ...f, ...updates } : f));
    setDirty(true);
  };

  const TYPE_LABELS: Record<string, string> = { steps: t('Step Group'), script: t('Script'), extraction: t('Extraction') };

  return (
    <Section
      title={t('Functions')}
      description={t('The callable units of this workflow — each one becomes an MCP tool and an API operation.')}
      right={dirty ? (
        <button onClick={handleSave} disabled={saving} className="px-2.5 py-1 text-[11px] font-semibold shadow-sm bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 disabled:opacity-50">
          {saving ? t('Saving...') : t('Save')}
        </button>
      ) : undefined}
    >
      <div className="bg-surface border border-ink/20 rounded-xl overflow-hidden shadow-sm">
        {/* Function list */}
        {functions.length > 0 && (
          <div className="divide-y divide-border">
            {functions.map((fn, idx) => (
              <div key={idx} className="px-4 py-2.5">
                <div className="flex items-center gap-3 cursor-pointer" onClick={() => setEditingIdx(editingIdx === idx ? null : idx)}>
                  <div className={clsx(
                    'w-2 h-2 rounded-full shrink-0',
                    fn.type === 'steps' ? 'bg-tertiary' : fn.type === 'script' ? 'bg-zinc-600' : 'bg-active',
                  )} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-[12px] font-mono font-medium text-ink">{fn.name}</span>
                      <span className="text-[10px] text-tertiary px-1.5 py-0.5 bg-hover rounded">{TYPE_LABELS[fn.type]}</span>
                    </div>
                    {fn.description && <p className="text-[11px] text-tertiary mt-0.5 truncate">{fn.description}</p>}
                  </div>
                  <ChevronDownIcon className={clsx('w-3.5 h-3.5 text-tertiary transition-transform', editingIdx === idx && 'rotate-180')} />
                </div>

                {/* Expanded edit form */}
                {editingIdx === idx && (
                  <div className="mt-3 pl-5 space-y-2.5 pb-1">
                    <input
                      value={fn.description} onChange={e => updateFunction(idx, { description: e.target.value })}
                      placeholder={t('Description (shown to AI models)')}
                      className="w-full px-2.5 py-1.5 text-[11px] bg-hover border border-border rounded-lg focus:outline-none focus:ring-1 focus:ring-border"
                    />

                    {fn.type === 'steps' && (
                      <div className="flex items-center gap-2">
                        <span className="text-[11px] text-tertiary w-20">{t('Steps')}</span>
                        <NumberInput
                          value={fn.step_range?.[0] ?? 0}
                          min={0}
                          max={steps.length}
                          size="sm"
                          hideSteppers
                          onChange={v => updateFunction(idx, { step_range: [v ?? 0, fn.step_range?.[1] ?? steps.length] })}
                          wrapperClassName="w-16"
                          className="font-mono text-center"
                        />
                        <span className="text-[11px] text-tertiary">{t('to')}</span>
                        <NumberInput
                          value={fn.step_range?.[1] ?? steps.length}
                          min={0}
                          max={steps.length}
                          size="sm"
                          hideSteppers
                          onChange={v => updateFunction(idx, { step_range: [fn.step_range?.[0] ?? 0, v ?? 0] })}
                          wrapperClassName="w-16"
                          className="font-mono text-center"
                        />
                      </div>
                    )}

                    {fn.type === 'extraction' && (
                      <>
                        <div className="flex items-center gap-2">
                          <span className="text-[11px] text-tertiary w-20">{t('Selector')}</span>
                          <input value={fn.selector || ''} onChange={e => updateFunction(idx, { selector: e.target.value })}
                            placeholder=".product-price" className="flex-1 px-2.5 py-1 text-[11px] font-mono bg-hover border border-border rounded-lg focus:outline-none" />
                        </div>
                        <div className="flex items-start gap-2">
                          <span className="text-[11px] text-tertiary w-20 pt-1">{t('JS expr')}</span>
                          <textarea value={fn.js_expression || ''} onChange={e => updateFunction(idx, { js_expression: e.target.value })}
                            placeholder="document.querySelector('.price')?.textContent" rows={2}
                            className="flex-1 px-2.5 py-1.5 text-[11px] font-mono bg-hover border border-border rounded-lg focus:outline-none resize-none" />
                        </div>
                      </>
                    )}

                    {fn.type === 'script' && (
                      <div className="flex items-center gap-2">
                        <span className="text-[11px] text-tertiary w-20">{t('Event')}</span>
                        <input value={fn.handler_event || 'message'} onChange={e => updateFunction(idx, { handler_event: e.target.value })}
                          className="w-40 px-2.5 py-1 text-[11px] font-mono bg-hover border border-border rounded-lg focus:outline-none" />
                      </div>
                    )}

                    {/* Input variables */}
                    <div>
                      <div className="flex items-center justify-between">
                        <span className="text-[10px] text-tertiary uppercase tracking-wider">{t('Inputs')}</span>
                        <button onClick={() => updateFunction(idx, {
                          input_variables: [...(fn.input_variables || []), { name: '', type: 'string', description: '', required: true }],
                        })} className="text-[10px] text-secondary hover:text-ink">{t('+ Add')}</button>
                      </div>
                      {(fn.input_variables || []).map((v, vi) => (
                        <div key={vi} className="flex items-center gap-1.5 mt-1">
                          <input value={v.name} onChange={e => {
                            const vars = [...(fn.input_variables || [])];
                            vars[vi] = { ...v, name: e.target.value };
                            updateFunction(idx, { input_variables: vars });
                          }} placeholder={t('name')} className="w-24 px-2 py-0.5 text-[10px] font-mono bg-hover border border-border rounded focus:outline-none" />
                          <input value={v.description} onChange={e => {
                            const vars = [...(fn.input_variables || [])];
                            vars[vi] = { ...v, description: e.target.value };
                            updateFunction(idx, { input_variables: vars });
                          }} placeholder={t('description')} className="flex-1 px-2 py-0.5 text-[10px] bg-hover border border-border rounded focus:outline-none" />
                          <button onClick={() => {
                            const vars = (fn.input_variables || []).filter((_, i) => i !== vi);
                            updateFunction(idx, { input_variables: vars });
                          }} className="text-[10px] text-tertiary hover:text-red-500">×</button>
                        </div>
                      ))}
                    </div>

                    {/* Output fields */}
                    <div>
                      <div className="flex items-center justify-between">
                        <span className="text-[10px] text-tertiary uppercase tracking-wider">{t('Outputs')}</span>
                        <button onClick={() => updateFunction(idx, {
                          output_fields: [...(fn.output_fields || []), { name: '', type: 'string', description: '' }],
                        })} className="text-[10px] text-secondary hover:text-ink">{t('+ Add')}</button>
                      </div>
                      {(fn.output_fields || []).map((f, fi) => (
                        <div key={fi} className="flex items-center gap-1.5 mt-1">
                          <input value={f.name} onChange={e => {
                            const fields = [...(fn.output_fields || [])];
                            fields[fi] = { ...f, name: e.target.value };
                            updateFunction(idx, { output_fields: fields });
                          }} placeholder={t('name')} className="w-24 px-2 py-0.5 text-[10px] font-mono bg-hover border border-border rounded focus:outline-none" />
                          <input value={f.description} onChange={e => {
                            const fields = [...(fn.output_fields || [])];
                            fields[fi] = { ...f, description: e.target.value };
                            updateFunction(idx, { output_fields: fields });
                          }} placeholder={t('description')} className="flex-1 px-2 py-0.5 text-[10px] bg-hover border border-border rounded focus:outline-none" />
                          <button onClick={() => {
                            const fields = (fn.output_fields || []).filter((_, i) => i !== fi);
                            updateFunction(idx, { output_fields: fields });
                          }} className="text-[10px] text-tertiary hover:text-red-500">×</button>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {/* Empty state — only when nothing is exposed AND nothing was detected. */}
        {functions.length === 0 && suggestions.length === 0 && !showAdd && (
          <div className="px-4 py-6 text-center">
            <p className="text-[12px] text-tertiary mb-2">{t('No functions defined')}</p>
            <p className="text-[11px] text-tertiary mb-3">
              {t('Functions make parts of this workflow callable — as MCP tools, API endpoints, or from other workflows.')}
            </p>
          </div>
        )}

        {/* Detected-in-steps — extraction / evaluate-JS / advanced-script units found
            in this workflow's steps that aren't exposed yet. One click exposes one as
            a callable function (then Save persists it). Streaming/legacy handlers are
            already auto-listed above via detectImportableFunctions. */}
        {suggestions.length > 0 && (
          <div className="px-4 py-3 border-t border-border">
            <div className="flex items-center gap-1.5 mb-2">
              <SparklesIcon className="w-3.5 h-3.5 text-tertiary" />
              <span className="text-[10px] font-semibold text-tertiary uppercase tracking-wider">{t('Detected in steps')}</span>
              <span className="text-[10px] text-tertiary">· {t('expose as callable functions')}</span>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {suggestions.map((c, i) => (
                <button
                  key={`${c.name}-${i}`}
                  onClick={() => exposeSuggestion(c)}
                  title={c.description}
                  className="group inline-flex items-center gap-1.5 px-2.5 py-1 bg-hover border border-border rounded-lg text-[11px] hover:border-ink/40 transition-colors"
                >
                  <span className="font-mono font-medium text-ink">{c.name}</span>
                  <span className="text-[10px] text-tertiary">{TYPE_LABELS[c.type]}</span>
                  <PlusIcon className="w-3 h-3 text-tertiary group-hover:text-ink transition-colors" />
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Add function form */}
        {showAdd && (
          <div className="px-4 py-3 border-t border-border space-y-2.5">
            <div className="flex items-center gap-2">
              <input value={newFn.name} onChange={e => setNewFn({ ...newFn, name: e.target.value.replace(/[^a-zA-Z0-9_]/g, '') })}
                placeholder={t('function_name')} className="w-40 px-2.5 py-1.5 text-[12px] font-mono bg-hover border border-border rounded-lg focus:outline-none focus:ring-1 focus:ring-border" />
              <div className="flex gap-1">
                {(['steps', 'script', 'extraction'] as const).map(ty => (
                  <button key={ty} onClick={() => setNewFn({ ...newFn, type: ty })}
                    className={clsx('px-2.5 py-1 text-[10px] rounded-lg border transition-all',
                      newFn.type === ty ? 'bg-ink text-white border-ink' : 'bg-hover text-secondary border-border hover:text-ink')}>
                    {TYPE_LABELS[ty]}
                  </button>
                ))}
              </div>
            </div>
            <input value={newFn.description} onChange={e => setNewFn({ ...newFn, description: e.target.value })}
              placeholder={t('Description')} className="w-full px-2.5 py-1.5 text-[11px] bg-hover border border-border rounded-lg focus:outline-none" />
            <div className="flex items-center gap-2 justify-end">
              <button onClick={() => setShowAdd(false)} className="px-3 py-1 text-[11px] text-secondary hover:text-ink rounded-lg hover:bg-chrome">{t('Cancel')}</button>
              <button onClick={addFunction} className="px-3 py-1 text-[11px] font-semibold shadow-sm bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90">{t('Add')}</button>
            </div>
          </div>
        )}

        {/* Add button */}
        {!showAdd && (
          <div className="px-4 py-2.5 border-t border-border">
            <button onClick={() => setShowAdd(true)} className="text-[11px] text-secondary hover:text-ink transition-colors">
              {t('+ Add function')}
            </button>
          </div>
        )}
      </div>
    </Section>
  );
};
