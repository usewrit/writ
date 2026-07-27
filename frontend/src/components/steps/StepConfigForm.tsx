import React, { useState, useRef, useLayoutEffect } from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import type { WorkflowStep } from '../../types/api';
import { EyeIcon, EyeSlashIcon, DocumentIcon, XMarkIcon, CursorArrowRaysIcon } from '@heroicons/react/24/outline';
import { FilePicker, PickedFile } from '../FilePicker';
import { Checkbox, NumberInput as UiNumberInput, Select as UiSelect } from '../ui';

/**
 * Per-type step configuration form — the single edit surface for a step's fields.
 * Renders only what the given step type needs, with quick-pick presets where useful.
 */

interface StepConfigFormProps {
  step: WorkflowStep;
  onUpdate: (updates: Partial<WorkflowStep>) => void;
  /**
   * When provided (live recorder only), selector fields show a "Pick" button. Calling
   * it hands the recorder an `apply` callback; the recorder lets the user click an
   * element on the live page and calls `apply(selector)` with the result. Absent in the
   * workflow editor, where there is no live page — selector fields fall back to text.
   */
  onPickSelector?: (apply: (selector: string) => void) => void;
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-3">
      <label className="text-xs text-secondary shrink-0 w-[88px] pt-2">{label}</label>
      <div className="flex-1 min-w-0">
        {children}
        {hint && <p className="text-[10px] text-tertiary mt-1">{hint}</p>}
      </div>
    </div>
  );
}

const inputClass = 'w-full px-3 py-1.5 text-sm bg-canvas border border-border rounded-lg outline-none focus:border-ink/30 focus:ring-2 focus:ring-ink/5 transition-colors';

function TextInput({ value, onChange, placeholder, mono, type = 'text' }: {
  value: string; onChange: (v: string) => void; placeholder?: string; mono?: boolean; type?: string;
}) {
  return (
    <input
      type={type}
      value={value}
      onChange={e => onChange(e.target.value)}
      placeholder={placeholder}
      className={clsx(inputClass, mono && 'font-mono text-xs')}
    />
  );
}

function TextArea({ value, onChange, placeholder, rows = 3, mono = true }: {
  value: string; onChange: (v: string) => void; placeholder?: string; rows?: number; mono?: boolean;
}) {
  return (
    <textarea
      value={value}
      onChange={e => onChange(e.target.value)}
      placeholder={placeholder}
      rows={rows}
      spellCheck={false}
      className={clsx(inputClass, 'resize-y', mono ? 'font-mono text-xs leading-relaxed' : 'text-sm')}
    />
  );
}

/**
 * Selector field with an optional "Pick" button. With `onPick`, the user can click an
 * element on the live page to fill the selector; without it, it's a plain mono input.
 */
function SelectorInput({ value, onChange, placeholder, onPick }: {
  value: string; onChange: (v: string) => void; placeholder?: string;
  onPick?: (apply: (selector: string) => void) => void;
}) {
  const { t } = useTranslation();
  if (!onPick) {
    return <TextInput value={value} onChange={onChange} placeholder={placeholder} mono />;
  }
  return (
    <div className="flex items-center gap-1.5">
      <input
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        className={clsx(inputClass, 'font-mono text-xs flex-1')}
      />
      <button
        type="button"
        onClick={() => onPick(onChange)}
        title={t('Pick an element from the page')}
        className="shrink-0 inline-flex items-center gap-1 px-2 py-1.5 rounded-lg border border-border text-secondary hover:text-ink hover:border-ink/30 transition-colors"
      >
        <CursorArrowRaysIcon className="w-3.5 h-3.5" />
        <span className="text-[11px] font-medium">{t('Pick')}</span>
      </button>
    </div>
  );
}

function SelectInput({ value, onChange, options }: {
  value: string; onChange: (v: string) => void; options: { value: string; label: string }[];
}) {
  return (
    <UiSelect value={value} onChange={onChange} options={options} size="sm" />
  );
}

/**
 * A tiny key/value rows editor for a `Record<string,string>` config field (api_call headers and
 * response_extractions). Placeholders like {{secret:...}} / {{extracted:...}} are allowed in values.
 */
function KVRows({ value, onChange, keyPlaceholder, valPlaceholder }: {
  value: Record<string, string> | undefined;
  onChange: (v: Record<string, string>) => void;
  keyPlaceholder: string;
  valPlaceholder: string;
}) {
  const { t } = useTranslation();
  const entries = Object.entries(value || {});
  const setEntry = (idx: number, k: string, v: string) => {
    const next = entries.slice();
    next[idx] = [k, v];
    onChange(Object.fromEntries(next.filter(([kk]) => kk !== '')));
  };
  const removeEntry = (idx: number) => {
    const next = entries.slice();
    next.splice(idx, 1);
    onChange(Object.fromEntries(next));
  };
  const addEntry = () => onChange({ ...(value || {}), '': '' });
  return (
    <div className="space-y-1.5">
      {entries.map(([k, v], idx) => (
        <div key={idx} className="flex items-center gap-1.5">
          <input
            value={k}
            onChange={e => setEntry(idx, e.target.value, v)}
            placeholder={keyPlaceholder}
            className={clsx(inputClass, 'font-mono text-xs flex-1')}
          />
          <input
            value={v}
            onChange={e => setEntry(idx, k, e.target.value)}
            placeholder={valPlaceholder}
            className={clsx(inputClass, 'font-mono text-xs flex-1')}
          />
          <button
            type="button"
            onClick={() => removeEntry(idx)}
            title={t('Remove')}
            className="shrink-0 p-1.5 rounded-lg border border-border text-secondary hover:text-ink hover:border-ink/30 transition-colors"
          >
            <XMarkIcon className="w-3.5 h-3.5" />
          </button>
        </div>
      ))}
      <button
        type="button"
        onClick={addEntry}
        className="text-[11px] font-medium text-secondary hover:text-ink transition-colors"
      >
        + {t('Add row')}
      </button>
    </div>
  );
}

function NumberInput({ value, onChange, min, max, step }: {
  value: number; onChange: (v: number) => void; min?: number; max?: number; step?: number;
}) {
  return (
    <UiNumberInput
      value={value}
      onChange={v => onChange(v ?? 0)}
      min={min} max={max} step={step}
      size="sm"
      className="w-32"
    />
  );
}

function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <Checkbox
      checked={checked}
      onChange={e => onChange(e.target.checked)}
      label={<span className="text-secondary text-xs">{label}</span>}
    />
  );
}

/** Quick-pick chips that write a value into a field. */
function Presets({ options, current, onPick, format }: {
  options: (string | number)[]; current?: string | number; onPick: (v: string | number) => void; format?: (v: string | number) => string;
}) {
  return (
    <div className="flex flex-wrap gap-1 mt-1.5">
      {options.map(o => (
        <button
          key={String(o)}
          type="button"
          onClick={() => onPick(o)}
          className={clsx(
            'px-2 py-0.5 text-[10px] font-medium rounded-full border transition-colors',
            String(current) === String(o)
              ? 'bg-ink text-white border-ink'
              : 'border-zinc-200 text-tertiary hover:text-ink hover:border-ink/30',
          )}
        >
          {format ? format(o) : String(o)}
        </button>
      ))}
    </div>
  );
}

function SensitiveValueInput({ value, onChange, masked }: { value: string; onChange: (v: string) => void; masked: boolean }) {
  const { t } = useTranslation();
  const [reveal, setReveal] = useState(false);
  return (
    <div className="relative">
      <input
        type={masked && !reveal ? 'password' : 'text'}
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={t('Value or {{ph}}', { ph: '{{placeholder}}' })}
        className={clsx(inputClass, 'font-mono text-xs pr-8')}
      />
      {masked && (
        <button type="button" onClick={() => setReveal(r => !r)} className="absolute right-2 top-1/2 -translate-y-1/2 text-tertiary hover:text-ink">
          {reveal ? <EyeSlashIcon className="w-3.5 h-3.5" /> : <EyeIcon className="w-3.5 h-3.5" />}
        </button>
      )}
    </div>
  );
}

/**
 * Links a stored file to a step. Shows the bound filename (or a "browse" prompt)
 * and opens the shared FilePicker, which itself enforces ownership/quota via the
 * backend. Only the {file_id, filename} the picker returns is kept locally.
 */
function FileLink({ fileId, filename, onPick, onClear }: {
  fileId?: string; filename?: string; onPick: (f: PickedFile) => void; onClear: () => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <div className="space-y-1.5">
      {fileId ? (
        <div className="flex items-center gap-2 px-3 py-2 bg-canvas border border-border rounded-lg">
          <DocumentIcon className="w-4 h-4 text-tertiary shrink-0" />
          <span className="flex-1 min-w-0 truncate text-sm text-ink" title={filename || fileId}>
            {filename || fileId}
          </span>
          <button type="button" onClick={() => setOpen(true)} className="text-[11px] font-medium text-secondary hover:text-ink">
            {t('Change')}
          </button>
          <button type="button" onClick={onClear} className="text-tertiary hover:text-ink" title={t('Clear')}>
            <XMarkIcon className="w-3.5 h-3.5" />
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="w-full px-3 py-2 text-sm text-secondary bg-canvas border border-dashed border-border rounded-lg hover:border-ink/30 hover:text-ink transition-colors text-left"
        >
          {t('Choose a stored file…')}
        </button>
      )}
      <FilePicker
        isOpen={open}
        onClose={() => setOpen(false)}
        onSelect={onPick}
        selectedId={fileId ?? null}
        title={t('Link a file to this step')}
      />
    </div>
  );
}

/**
 * CodeEditor — a dependency-free code field for the script/JSON steps: a synced
 * line-number gutter, auto-grow up to a cap (then internal scroll), Tab/Shift-Tab to
 * indent, Enter to keep indentation, and a JSON "Format" action. Controlled value, so
 * manual edits (Tab/Enter) restore the caret after React re-renders.
 */
function CodeEditor({ value: rawValue, onChange, language = 'javascript', placeholder, minRows = 4, maxRows = 24 }: {
  value: unknown; onChange: (v: string) => void; language?: 'javascript' | 'json'; placeholder?: string; minRows?: number; maxRows?: number;
}) {
  const { t } = useTranslation();
  const taRef = useRef<HTMLTextAreaElement>(null);
  const gutterRef = useRef<HTMLDivElement>(null);
  const pendingSel = useRef<[number, number] | null>(null);
  const [jsonError, setJsonError] = useState<string | null>(null);

  // Coerce whatever's handed to us to a string — a workflow saved with an
  // API-call `body` that arrived as parsed JSON (an object/array) would otherwise
  // crash the first render at `value.split('\n')`. Objects/arrays open as
  // pretty-printed JSON so they're immediately editable; anything else falls
  // through String() (rare, harmless). The parent gets a plain string back on
  // the first edit, so the row heals on next save.
  const value: string =
    typeof rawValue === 'string' ? rawValue
    : rawValue == null ? ''
    : (typeof rawValue === 'object')
      ? (() => { try { return JSON.stringify(rawValue, null, 2); } catch { return String(rawValue); } })()
      : String(rawValue);

  // Re-apply the caret after a programmatic edit (Tab/Enter) re-renders the textarea.
  useLayoutEffect(() => {
    if (pendingSel.current && taRef.current) {
      const [s, e] = pendingSel.current;
      taRef.current.focus();
      taRef.current.setSelectionRange(s, e);
      pendingSel.current = null;
    }
  });

  const lines = value ? value.split('\n').length : 1;
  const displayRows = Math.min(Math.max(minRows, lines), maxRows);
  const gutterCount = Math.max(lines, displayRows);

  const syncScroll = () => {
    if (gutterRef.current && taRef.current) gutterRef.current.scrollTop = taRef.current.scrollTop;
  };

  const commit = (next: string, caretStart: number, caretEnd: number) => {
    pendingSel.current = [caretStart, caretEnd];
    setJsonError(null);
    onChange(next);
  };

  const onKeyDown = (ev: React.KeyboardEvent<HTMLTextAreaElement>) => {
    const ta = taRef.current;
    if (!ta) return;
    const s = ta.selectionStart, e = ta.selectionEnd;
    const indent = '  ';

    if (ev.key === 'Tab') {
      ev.preventDefault();
      const lineStart = value.lastIndexOf('\n', s - 1) + 1;
      if (ev.shiftKey) {
        // Outdent every line touched by the selection.
        const block = value.slice(lineStart, e);
        const dedented = block.replace(/^ {1,2}/gm, '');
        const firstLineCut = block.length - block.replace(/^ {1,2}/, '').length;
        commit(value.slice(0, lineStart) + dedented + value.slice(e), Math.max(lineStart, s - firstLineCut), lineStart + dedented.length);
      } else if (s !== e && value.slice(s, e).includes('\n')) {
        // Indent every line in a multi-line selection.
        const block = value.slice(lineStart, e);
        const indented = block.replace(/^/gm, indent);
        commit(value.slice(0, lineStart) + indented + value.slice(e), s + indent.length, e + (indented.length - block.length));
      } else {
        // Insert an indent at the caret.
        commit(value.slice(0, s) + indent + value.slice(e), s + indent.length, s + indent.length);
      }
      return;
    }

    if (ev.key === 'Enter' && s === e) {
      const lineStart = value.lastIndexOf('\n', s - 1) + 1;
      const curLine = value.slice(lineStart, s);
      const lead = curLine.match(/^[ \t]*/)?.[0] || '';
      const extra = /[{([:]\s*$/.test(curLine) ? indent : '';
      if (lead || extra) {
        ev.preventDefault();
        const ins = '\n' + lead + extra;
        commit(value.slice(0, s) + ins + value.slice(e), s + ins.length, s + ins.length);
      }
    }
  };

  const formatJson = () => {
    try {
      onChange(JSON.stringify(JSON.parse(value), null, 2));
      setJsonError(null);
    } catch (err: any) {
      setJsonError(err?.message || t('Invalid JSON'));
    }
  };

  return (
    <div className="rounded-lg border border-border bg-canvas overflow-hidden transition-colors focus-within:border-ink/30 focus-within:ring-2 focus-within:ring-ink/5">
      <div className="flex items-center justify-between gap-2 px-2.5 py-1 border-b border-border bg-hover/40">
        <span className="text-[10px] font-mono font-semibold uppercase tracking-wider text-tertiary">{language === 'json' ? 'JSON' : 'JavaScript'}</span>
        <div className="flex items-center gap-2.5 min-w-0">
          {jsonError && <span className="text-[10px] font-medium text-red-500 truncate" title={jsonError}>{jsonError}</span>}
          {language === 'json' && (
            <button type="button" onMouseDown={e => e.preventDefault()} onClick={formatJson} className="text-[10px] font-medium text-secondary hover:text-ink shrink-0">
              {t('Format')}
            </button>
          )}
          <span className="text-[10px] tabular-nums text-tertiary shrink-0">{t('{{n}} ln', { n: lines })}</span>
        </div>
      </div>
      <div className="flex">
        <div ref={gutterRef} aria-hidden className="shrink-0 select-none overflow-hidden border-r border-border bg-hover/30 py-2 pl-2.5 pr-2 text-right font-mono text-xs leading-5 text-tertiary/70">
          {Array.from({ length: gutterCount }, (_, i) => <div key={i}>{i + 1}</div>)}
        </div>
        <textarea
          ref={taRef}
          value={value}
          onChange={e => { setJsonError(null); onChange(e.target.value); }}
          onKeyDown={onKeyDown}
          onScroll={syncScroll}
          rows={displayRows}
          wrap="off"
          spellCheck={false}
          placeholder={placeholder}
          className="flex-1 min-w-0 resize-none bg-transparent py-2 px-3 font-mono text-xs leading-5 text-ink outline-none overflow-auto placeholder:text-tertiary/70"
        />
      </div>
    </div>
  );
}

/**
 * KeyCaptureInput — focus the box and press any key (with modifiers) to record it in
 * Playwright key syntax (e.g. "Control+a", "ArrowDown"). Preset chips cover the common
 * keys without needing focus. Captures Tab/Escape too (so they're recorded, not eaten
 * by focus/close), which is why it stops propagation while focused.
 */
function KeyCaptureInput({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const { t } = useTranslation();
  const [capturing, setCapturing] = useState(false);

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (['Control', 'Alt', 'Shift', 'Meta'].includes(e.key)) return; // wait for the real key
    e.preventDefault();
    e.stopPropagation();
    const mods: string[] = [];
    if (e.ctrlKey) mods.push('Control');
    if (e.altKey) mods.push('Alt');
    if (e.shiftKey) mods.push('Shift');
    if (e.metaKey) mods.push('Meta');
    const main = e.key === ' ' ? 'Space' : e.key;
    onChange([...mods, main].join('+'));
  };

  return (
    <div className="space-y-2">
      <div
        role="textbox"
        tabIndex={0}
        aria-label={t('Key to press')}
        onKeyDown={onKeyDown}
        onFocus={() => setCapturing(true)}
        onBlur={() => setCapturing(false)}
        className={clsx(
          'w-full px-3 py-2 rounded-lg border text-sm cursor-text outline-none flex items-center gap-2 transition-colors',
          capturing ? 'border-ink/40 ring-2 ring-ink/10 bg-canvas' : 'border-border bg-canvas hover:border-ink/30',
        )}
      >
        {value ? (
          <kbd className="px-2 py-0.5 text-xs font-mono font-semibold text-ink bg-hover border border-border rounded">{value}</kbd>
        ) : (
          <span className="text-tertiary">{capturing ? t('Press any key…') : t('Click here, then press a key')}</span>
        )}
        {value && (
          <button
            type="button"
            onMouseDown={e => { e.preventDefault(); onChange(''); }}
            className="ml-auto text-tertiary hover:text-ink"
            title={t('Clear')}
          >
            <XMarkIcon className="w-3.5 h-3.5" />
          </button>
        )}
      </div>
      <Presets
        options={['Enter', 'Tab', 'Escape', 'ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Space', 'Backspace', 'Delete']}
        current={value}
        onPick={v => onChange(String(v))}
      />
    </div>
  );
}

export const StepConfigForm: React.FC<StepConfigFormProps> = ({ step, onUpdate, onPickSelector }) => {
  const { t } = useTranslation();
  const c = step.config || {};
  const o = step.options || {};

  const updateConfig = (key: string, value: any) => onUpdate({ config: { ...c, [key]: value } });
  const updateOptions = (key: string, value: any) => onUpdate({ options: { ...o, [key]: value } });

  // Recorded steps often carry selector/value/url inside config rather than at the
  // top level — read with fallback, and write to both so the executor sees the edit
  // regardless of which location it reads.
  const readField = (key: 'selector' | 'value' | 'url'): string => (step as any)[key] ?? c[key] ?? '';
  const writeField = (key: 'selector' | 'value' | 'url') => (v: string) => {
    const updates: Partial<WorkflowStep> = { [key]: v };
    if (c[key] !== undefined) updates.config = { ...c, [key]: v };
    onUpdate(updates);
  };

  switch (step.type) {
    case 'navigate':
      return (
        <Field label={t('URL')}>
          <TextInput value={readField('url')} onChange={writeField('url')} placeholder="https://example.com" mono />
        </Field>
      );

    case 'navigated_to':
      return (
        <Field label={t('Expected URL')} hint={t('The URL the page should land on after the previous click.')}>
          <TextInput value={readField('url')} onChange={writeField('url')} placeholder="https://example.com/after-click" mono />
        </Field>
      );

    case 'click':
      return (
        <>
          <Field label={t('Selector')}>
            <SelectorInput value={readField('selector')} onChange={writeField('selector')} placeholder="button.submit, #login-btn" onPick={onPickSelector} />
          </Field>
          <Field label={t('Label')} hint={t('Visible text of the target — used as a fallback when the selector breaks.')}>
            <TextInput value={o.label || o.tag || ''} onChange={v => updateOptions('label', v)} placeholder={t('Button text')} />
          </Field>
        </>
      );

    case 'fill':
    case 'type': {
      const sensitive = !!(o.is_sensitive || c.is_sensitive);
      return (
        <>
          <Field label={t('Selector')}>
            <SelectorInput value={readField('selector')} onChange={writeField('selector')} placeholder="input#email, [name='username']" onPick={onPickSelector} />
          </Field>
          <Field label={t('Value')} hint={t('Use {{a}} for input data or {{b}} for credentials.', { a: '{{field}}', b: '{{secret:name}}' })}>
            <SensitiveValueInput value={readField('value')} onChange={writeField('value')} masked={sensitive} />
          </Field>
          <Field label={t('Field name')}>
            <TextInput value={o.field_name || o.label || ''} onChange={v => updateOptions('field_name', v)} placeholder={t('email, password…')} />
          </Field>
          <div className="flex items-center gap-5 pl-[100px]">
            <Toggle checked={sensitive} onChange={v => updateOptions('is_sensitive', v)} label={t('Sensitive')} />
            <Toggle checked={!!o.via_keyboard} onChange={v => updateOptions('via_keyboard', v)} label={t('Type via keyboard')} />
          </div>
        </>
      );
    }

    case 'select':
      return (
        <>
          <Field label={t('Selector')}>
            <SelectorInput value={readField('selector')} onChange={writeField('selector')} placeholder="select#country" onPick={onPickSelector} />
          </Field>
          <Field label={t('Option')}>
            <TextInput value={readField('value')} onChange={writeField('value')} placeholder={t('Option value or visible text')} />
          </Field>
          <div className="pl-[100px]">
            <Toggle checked={!!o.from_custom_dropdown} onChange={v => updateOptions('from_custom_dropdown', v)} label={t('Custom dropdown (not a native <select>)')} />
          </div>
        </>
      );

    case 'check':
    case 'uncheck':
      return (
        <>
          <Field label={t('Selector')}>
            <SelectorInput value={readField('selector')} onChange={writeField('selector')} placeholder="input[type=checkbox]" onPick={onPickSelector} />
          </Field>
          <Field label={t('Label')}>
            <TextInput value={o.label || ''} onChange={v => updateOptions('label', v)} placeholder={t('Checkbox label')} />
          </Field>
        </>
      );

    case 'press': {
      // Recorded press steps keep the key in config.key — mirror writes there too.
      const setKey = (v: string) => {
        const updates: Partial<WorkflowStep> = { value: v };
        if (c.key !== undefined) updates.config = { ...c, key: v };
        onUpdate(updates);
      };
      return (
        <Field label={t('Key')} hint={t('Click the box and press the key to record it — modifiers (Ctrl/Alt/Shift/Cmd) included.')}>
          <KeyCaptureInput value={step.value || c.key || ''} onChange={setKey} />
        </Field>
      );
    }

    case 'wait': {
      // Same precedence as the run-time summary (config.value first); write both
      // keys so every executor variant picks up the edit.
      const duration = c.value ?? c.duration ?? o.duration ?? 1000;
      const setDuration = (v: number) => onUpdate({ config: { ...c, duration: v, value: v } });
      return (
        <Field label={t('Duration')}>
          <NumberInput value={duration} onChange={setDuration} min={100} step={100} />
          <Presets
            options={[500, 1000, 2000, 5000, 10000]}
            current={duration}
            onPick={v => setDuration(Number(v))}
            format={v => Number(v) >= 1000 ? `${Number(v) / 1000}s` : `${v}ms`}
          />
        </Field>
      );
    }

    case 'scroll':
      return (
        <>
          <Field label={t('Container')} hint={t('Leave empty to scroll the whole page.')}>
            <TextInput value={readField('selector')} onChange={writeField('selector')} placeholder=".results-list" mono />
          </Field>
          <div className="pl-[100px]">
            <Toggle checked={!!o.scrollToBottom} onChange={v => updateOptions('scrollToBottom', v)} label={t('Scroll to bottom')} />
          </div>
        </>
      );

    case 'scroll_into_view':
      return (
        <Field label={t('Element')}>
          <SelectorInput value={readField('selector')} onChange={writeField('selector')} placeholder={t('Element selector')} onPick={onPickSelector} />
        </Field>
      );

    case 'hover':
      return (
        <Field label={t('Element')}>
          <SelectorInput value={readField('selector')} onChange={writeField('selector')} placeholder={t('Element to hover')} onPick={onPickSelector} />
        </Field>
      );

    case 'screenshot':
      return (
        <Field label={t('Name')}>
          <TextInput value={c.name || ''} onChange={v => updateConfig('name', v)} placeholder="screenshot-name" />
        </Field>
      );

    case 'assert':
      return (
        <>
          <Field label={t('Selector')}>
            <SelectorInput value={readField('selector')} onChange={writeField('selector')} placeholder={t('Element to assert')} onPick={onPickSelector} />
          </Field>
          <Field label={t('Expected')} hint={t('Leave empty to only assert the element exists.')}>
            <TextInput value={readField('value')} onChange={writeField('value')} placeholder={t('Expected text or value')} />
          </Field>
        </>
      );

    case 'evaluate':
      return (
        <>
          <Field label={t('Variable')} hint={t("The script's return value is stored under this key.")}>
            <TextInput value={c.variable || ''} onChange={v => updateConfig('variable', v)} placeholder="result_variable" mono />
          </Field>
          <Field label={t('Script')}>
            <CodeEditor value={c.script || step.script || ''} onChange={v => updateConfig('script', v)} language="javascript" minRows={6} placeholder={'// Runs in the page context — return the value to extract\nreturn document.querySelector("h1")?.textContent;'} />
          </Field>
        </>
      );

    case 'extract': {
      const attr = c.attribute || 'textContent';
      const standard = ['textContent', 'href', 'src', 'value', 'innerHTML'];
      const isCustom = !standard.includes(attr);
      return (
        <>
          <Field label={t('Selector')}>
            <SelectorInput value={readField('selector')} onChange={writeField('selector')} placeholder={t('Element to extract from')} onPick={onPickSelector} />
          </Field>
          <Field label={t('Variable')}>
            <TextInput value={c.variable || ''} onChange={v => updateConfig('variable', v)} placeholder="output_key" mono />
          </Field>
          <Field label={t('Attribute')}>
            <div className="flex items-center gap-2">
              <SelectInput
                value={isCustom ? 'custom' : attr}
                onChange={v => updateConfig('attribute', v === 'custom' ? '' : v)}
                options={[...standard.map(s => ({ value: s, label: s })), { value: 'custom', label: t('custom…') }]}
              />
              {isCustom && (
                <input
                  value={attr}
                  onChange={e => updateConfig('attribute', e.target.value)}
                  placeholder="data-id, aria-label…"
                  className={clsx(inputClass, 'font-mono text-xs flex-1')}
                />
              )}
            </div>
          </Field>
        </>
      );
    }

    case 'api_call':
    case 'login_post': {
      const isLogin = step.type === 'login_post';
      return (
        <>
          {isLogin && (
            <p className="text-xs text-secondary">
              {t('A sign-in replayed as a request. It authenticates the workflow (sets the session cookie / returns a token) so the api_call steps that follow reuse it.')}
            </p>
          )}
          <Field label={isLogin ? t('Sign-in request') : t('Request')}>
            <div className="flex items-center gap-2">
              <SelectInput
                value={c.method || 'POST'}
                onChange={v => updateConfig('method', v)}
                options={['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map(m => ({ value: m, label: m }))}
              />
              <input
                value={c.url || ''}
                onChange={e => updateConfig('url', e.target.value)}
                placeholder="https://api.example.com/endpoint"
                className={clsx(inputClass, 'font-mono text-xs flex-1')}
              />
            </div>
          </Field>
          <Field label={t('Headers')} hint={t('Values may use {{secret:...}} and {{extracted:...}}.')}>
            <KVRows
              value={c.headers}
              onChange={v => updateConfig('headers', v)}
              keyPlaceholder="Authorization"
              valPlaceholder="Bearer {{extracted:token}}"
            />
          </Field>
          {(c.method || 'POST') !== 'GET' && (
            <Field label={t('Body')}>
              {/* api_call/login_post request body. The CLOUD recorder stores it under `body_template`,
                  the DESKTOP (Rust) recorder under `body`. BOTH replay engines read `body` FIRST then fall
                  back to `body_template` (http_lane.py / step_eval.rs), so READ `body` first (else a cloud
                  step renders empty) and WRITE `body` (else a desktop step's edit is ignored at replay). */}
              <CodeEditor value={c.body ?? c.body_template ?? ''} onChange={v => updateConfig('body', v)} language="json" minRows={4} placeholder={'{\n  "key": "value"\n}'} />
            </Field>
          )}
          {!isLogin && (
            <>
              <Field label={t('Variable')} hint={t('The response is stored under this key.')}>
                <TextInput value={c.variable || ''} onChange={v => updateConfig('variable', v)} placeholder="response_variable" mono />
              </Field>
              <Field label={t('Response fields')} hint={t('Pull values from the JSON response (JSONPath) to reuse as {{extracted:name}} in later steps.')}>
                <KVRows
                  value={c.response_extractions}
                  onChange={v => updateConfig('response_extractions', v)}
                  keyPlaceholder="token"
                  valPlaceholder="$.data.token"
                />
              </Field>
            </>
          )}
        </>
      );
    }

    case 'ai_fill':
      return (
        <Field label={t('Instruction')}>
          <TextArea value={c.instruction || ''} onChange={v => updateConfig('instruction', v)} placeholder={t('Describe what to fill and how')} mono={false} />
        </Field>
      );

    case 'ai_continue':
      return (
        <>
          <Field label={t('Task')}>
            <TextArea value={c.task || ''} onChange={v => updateConfig('task', v)} placeholder={t('Describe the task for the AI agent')} mono={false} />
          </Field>
          <Field label={t('Max actions')}>
            <NumberInput value={c.max_actions || 10} onChange={v => updateConfig('max_actions', v)} min={1} max={100} />
          </Field>
        </>
      );

    case 'ai_navigate':
      return (
        <>
          <Field label={t('Start URL')}>
            <TextInput value={c.url || ''} onChange={v => updateConfig('url', v)} placeholder="https://…" mono />
          </Field>
          <Field label={t('Goal')}>
            <TextArea value={c.goal || ''} onChange={v => updateConfig('goal', v)} placeholder={t('Describe what the AI should achieve')} mono={false} />
          </Field>
          <div className="pl-[100px]">
            <Toggle checked={c.use_vision !== false} onChange={v => updateConfig('use_vision', v)} label={t('Use vision (screenshots)')} />
          </div>
        </>
      );

    case 'end_point': {
      const cond = c.condition_type || 'immediate';
      const condValueLabel = cond.includes('url') ? t('URL') : cond.includes('text') ? t('Text') : t('Selector');
      const condValue = cond.includes('url') ? (c.url || '') : cond.includes('text') ? (c.text || '') : (c.selector || '');
      const setCondValue = (v: string) => {
        if (cond.includes('url')) updateConfig('url', v);
        else if (cond.includes('text')) updateConfig('text', v);
        else updateConfig('selector', v);
      };
      return (
        <>
          <Field label={t('Condition')}>
            <SelectInput
              value={cond}
              onChange={v => updateConfig('condition_type', v)}
              options={[
                { value: 'immediate', label: t('Immediate') },
                { value: 'url_contains', label: t('URL contains') },
                { value: 'url_equals', label: t('URL equals') },
                { value: 'element_visible', label: t('Element visible') },
                { value: 'element_exists', label: t('Element exists') },
                { value: 'text_visible', label: t('Text visible') },
              ]}
            />
          </Field>
          {cond !== 'immediate' && (
            <>
              <Field label={condValueLabel}>
                <TextInput value={condValue} onChange={setCondValue} placeholder={t('Condition value')} mono />
              </Field>
              <Field label={t('Timeout')}>
                <NumberInput value={c.timeout_ms || 30000} onChange={v => updateConfig('timeout_ms', v)} min={1000} step={1000} />
              </Field>
            </>
          )}
          <Field label={t('Message')}>
            <TextInput value={c.success_message || ''} onChange={v => updateConfig('success_message', v)} placeholder={t('Success message')} />
          </Field>
        </>
      );
    }

    case 'wait_for_tab':
      return (
        <Field label={t('Tab URL')} hint={t("Pattern the tab opened by the site should match (optional check).")}>
          <TextInput value={readField('url')} onChange={writeField('url')} placeholder={t('Expected tab URL pattern')} mono />
        </Field>
      );

    case 'open_tab':
      return (
        <Field label={t('Tab URL')} hint={t('URL to open in the new tab.')}>
          <TextInput value={readField('url')} onChange={writeField('url')} placeholder={t('https://example.com')} mono />
        </Field>
      );

    case 'captcha':
      return (
        <>
          <Field label={t('Type')}>
            <SelectInput
              value={c.captcha_type || 'recaptcha_v2'}
              onChange={v => updateConfig('captcha_type', v)}
              options={[
                { value: 'recaptcha_v2', label: 'reCAPTCHA v2' },
                { value: 'recaptcha_v3', label: 'reCAPTCHA v3' },
                { value: 'hcaptcha', label: 'hCaptcha' },
                { value: 'turnstile', label: 'Cloudflare Turnstile' },
              ]}
            />
          </Field>
          <Field label={t('Selector')}>
            <TextInput value={readField('selector')} onChange={writeField('selector')} placeholder={t('Captcha element selector')} mono />
          </Field>
        </>
      );

    case 'codegen':
      return (
        <Field label={t('Script')}>
          <CodeEditor value={c.script || step.script || ''} onChange={v => updateConfig('script', v)} language="javascript" minRows={8} placeholder={"// Playwright script block\nawait page.click('.btn');"} />
        </Field>
      );

    case 'tab_closed':
      return <p className="text-xs text-tertiary pl-[100px]">{t('No configuration — marks the point where the child tab closes and control returns to the parent tab.')}</p>;

    case 'wait_for_change': {
      const watchKind: string = o.watch_kind || (o.region ? 'region' : 'selector');
      const changeKind: string = o.change_kind || (watchKind === 'region' ? 'visual' : 'text');
      const region = (o.region as Record<string, number>) || { x: 0, y: 0, width: 200, height: 100 };
      const setRegion = (patch: Record<string, number>) => updateOptions('region', { ...region, ...patch });
      const setWatchKind = (v: string) =>
        onUpdate({ options: { ...o, watch_kind: v, change_kind: v === 'region' ? 'visual' : (changeKind === 'visual' ? 'text' : changeKind) } });
      return (
        <>
          <Field label={t('Watch')}>
            <SelectInput
              value={watchKind}
              onChange={setWatchKind}
              options={[
                { value: 'selector', label: t('A page element') },
                { value: 'region', label: t('A screen region') },
              ]}
            />
          </Field>
          {watchKind === 'selector' && (
            <>
              <Field label={t('Selector')}>
                <SelectorInput value={readField('selector')} onChange={writeField('selector')} placeholder=".price, #status" onPick={onPickSelector} />
              </Field>
              <Field label={t('Change in')}>
                <SelectInput
                  value={changeKind}
                  onChange={v => updateOptions('change_kind', v)}
                  options={[
                    { value: 'text', label: t('Text') },
                    { value: 'html', label: t('HTML') },
                    { value: 'attribute', label: t('Attribute') },
                    { value: 'visual', label: t('Visual (screenshot)') },
                  ]}
                />
              </Field>
              {changeKind === 'attribute' && (
                <Field label={t('Attribute')}>
                  <TextInput value={o.attribute || ''} onChange={v => updateOptions('attribute', v)} placeholder="value, class…" mono />
                </Field>
              )}
            </>
          )}
          {watchKind === 'region' && (
            <Field label={t('Region')} hint={t('Pixel box on the page to watch for a visual change.')}>
              <div className="grid grid-cols-4 gap-2">
                {(['x', 'y', 'width', 'height'] as const).map(f => (
                  <UiNumberInput
                    key={f}
                    value={region[f] ?? 0}
                    onChange={v => setRegion({ [f]: v ?? 0 })}
                    placeholder={f}
                    size="sm"
                    aria-label={f}
                  />
                ))}
              </div>
            </Field>
          )}
          <Field label={t('Output')} hint={t('The detected value is stored under this key.')}>
            <TextInput
              value={o.output_name || c.variable || ''}
              onChange={v => onUpdate({ options: { ...o, output_name: v, variable: v } })}
              placeholder="price"
              mono
            />
          </Field>
          <Field label={t('Baseline')}>
            <SelectInput
              value={o.baseline_mode || 'in_run'}
              onChange={v => updateOptions('baseline_mode', v)}
              options={[
                { value: 'in_run', label: t('Wait until it changes (this run)') },
                { value: 'since_last_run', label: t('Changed since last run') },
              ]}
            />
          </Field>
          <Field label={t('Timeout')}>
            <NumberInput value={o.timeout_ms || 30000} onChange={v => updateOptions('timeout_ms', v)} min={1000} step={1000} />
          </Field>
          <Field label={t('If no change')}>
            <SelectInput
              value={o.on_no_change || 'fail'}
              onChange={v => updateOptions('on_no_change', v)}
              options={[
                { value: 'fail', label: t('Fail the run') },
                { value: 'continue', label: t('Continue anyway') },
              ]}
            />
          </Field>
        </>
      );
    }

    case 'upload': {
      // A buyer-bound SLOT decouples shared/marketplace workflows from any concrete
      // file: the creator declares a named slot, the buyer binds their own file at
      // run time. A linked file_id and a slot are mutually exclusive — switching to
      // slot mode clears the file_id (so a creator file can never leak, §10/§4.2).
      const useSlot = c.file_slot !== undefined && c.file_slot !== null;
      const mode = c.mode || 'auto';
      const setUseSlot = (v: boolean) => {
        if (v) onUpdate({ config: { ...c, file_slot: c.file_slot || '', file_id: undefined, file_name: undefined } });
        else onUpdate({ config: { ...c, file_slot: undefined } });
      };
      return (
        <>
          {useSlot ? (
            <Field label={t('Slot')} hint={t('A name the buyer binds their own file to (e.g. "resume"). No file is shipped with the workflow.')}>
              <TextInput value={c.file_slot || ''} onChange={v => updateConfig('file_slot', v)} placeholder="resume" mono />
            </Field>
          ) : (
            <Field label={t('File')} hint={t('The stored file to set on the file input.')}>
              <FileLink
                fileId={c.file_id}
                filename={c.file_name}
                onPick={f => onUpdate({ config: { ...c, file_id: f.file_id, file_name: f.filename } })}
                onClear={() => onUpdate({ config: { ...c, file_id: undefined, file_name: undefined } })}
              />
            </Field>
          )}
          <Field label={t('Selector')} hint={t('The <input type=file> (Input mode) or the button that opens the file dialog (Chooser mode).')}>
            <TextInput value={readField('selector')} onChange={writeField('selector')} placeholder="input[type=file], button.upload" mono />
          </Field>
          <Field label={t('Mode')} hint={t('Auto picks the right approach from the selector.')}>
            <SelectInput
              value={mode}
              onChange={v => updateConfig('mode', v)}
              options={[
                { value: 'auto', label: t('Auto') },
                { value: 'input', label: t('File input') },
                { value: 'chooser', label: t('File chooser (click)') },
              ]}
            />
          </Field>
          <div className="flex items-center gap-5 pl-[100px]">
            <Toggle checked={!!c.is_multiple} onChange={v => updateConfig('is_multiple', v)} label={t('Allow multiple files')} />
            <Toggle checked={useSlot} onChange={setUseSlot} label={t('Use a slot (shared / marketplace)')} />
          </div>
        </>
      );
    }

    case 'wait_for_download':
      return (
        <>
          <Field label={t('Save as')} hint={t('Name this captured file so a later step or workflow can reuse it.')}>
            <TextInput value={c.output_key || ''} onChange={v => updateConfig('output_key', v)} placeholder="invoice_pdf" mono />
          </Field>
          <Field label={t('Filename hint')} hint={t('Expected name of the downloaded file (optional — used as a fallback label).')}>
            <TextInput value={c.suggested_filename || ''} onChange={v => updateConfig('suggested_filename', v)} placeholder="invoice.pdf" />
          </Field>
          <p className="text-[11px] text-tertiary pl-[100px] leading-relaxed">
            {t('The previous step (a click or link) should trigger the download. The captured file is stored in your library and can be passed into another workflow’s upload step.')}
          </p>
        </>
      );

    default:
      return (
        <>
          <Field label={t('Selector')}>
            <TextInput value={readField('selector')} onChange={writeField('selector')} mono />
          </Field>
          <Field label={t('Value')}>
            <TextInput value={readField('value')} onChange={writeField('value')} />
          </Field>
        </>
      );
  }
};
