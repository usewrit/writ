import React from 'react';
import type { WorkflowStep } from '../../types/api';
import i18n from '../../i18n';
import {
  GlobeAltIcon,
  ArrowRightIcon,
  CursorArrowRaysIcon,
  PencilIcon,
  ListBulletIcon,
  CheckIcon,
  XMarkIcon,
  CommandLineIcon,
  ClockIcon,
  ArrowsUpDownIcon,
  EyeIcon,
  CameraIcon,
  ArrowTopRightOnSquareIcon,
  CheckCircleIcon,
  SparklesIcon,
  RocketLaunchIcon,
  FlagIcon,
  ShieldCheckIcon,
  CodeBracketIcon,
  PlusIcon,
  LinkIcon,
  HandRaisedIcon,
  ArrowPathIcon,
  ArrowUpTrayIcon,
  ArrowDownTrayIcon,
} from '@heroicons/react/24/outline';

export type StepType = WorkflowStep['type'];

export type StepGroup = 'Navigation' | 'Interaction' | 'Input' | 'Control' | 'Data' | 'AI' | 'Tabs' | 'Advanced';

export interface StepTypeMeta {
  type: StepType;
  label: string;
  Icon: React.ElementType;
  group: StepGroup;
  description: string;
  /** Hide from the "add step" palette (recorder-generated marker steps). */
  internal?: boolean;
}

/**
 * Single source of truth for workflow step types.
 * Every surface (detail page editor, recorder, wizards) should read from here.
 */
export const STEP_TYPES: StepTypeMeta[] = [
  { type: 'navigate',         label: 'Navigate',       Icon: GlobeAltIcon,               group: 'Navigation',  description: 'Go to a URL' },
  { type: 'navigated_to',     label: 'Navigated To',   Icon: ArrowRightIcon,             group: 'Navigation',  description: 'Expected URL after a click', internal: true },
  { type: 'click',            label: 'Click',          Icon: CursorArrowRaysIcon,        group: 'Interaction', description: 'Click an element' },
  { type: 'hover',            label: 'Hover',          Icon: HandRaisedIcon,             group: 'Interaction', description: 'Hover over an element' },
  { type: 'press',            label: 'Press Key',      Icon: CommandLineIcon,            group: 'Interaction', description: 'Press a keyboard key' },
  { type: 'scroll',           label: 'Scroll',         Icon: ArrowsUpDownIcon,           group: 'Interaction', description: 'Scroll the page or a container' },
  { type: 'scroll_into_view', label: 'Scroll to View', Icon: EyeIcon,                    group: 'Interaction', description: 'Bring an element into view' },
  { type: 'fill',             label: 'Fill',           Icon: PencilIcon,                 group: 'Input',       description: 'Fill a text field' },
  { type: 'type',             label: 'Type',           Icon: CommandLineIcon,            group: 'Input',       description: 'Type text key by key' },
  { type: 'select',           label: 'Select',         Icon: ListBulletIcon,             group: 'Input',       description: 'Pick a dropdown option' },
  { type: 'check',            label: 'Check',          Icon: CheckIcon,                  group: 'Input',       description: 'Check a checkbox' },
  { type: 'uncheck',          label: 'Uncheck',        Icon: XMarkIcon,                  group: 'Input',       description: 'Uncheck a checkbox' },
  { type: 'upload',           label: 'Upload File',    Icon: ArrowUpTrayIcon,            group: 'Input',       description: 'Set a stored file on a file input' },
  { type: 'wait',             label: 'Wait',           Icon: ClockIcon,                  group: 'Control',     description: 'Pause for a duration' },
  { type: 'screenshot',       label: 'Screenshot',     Icon: CameraIcon,                 group: 'Control',     description: 'Capture the page' },
  { type: 'assert',           label: 'Assert',         Icon: CheckCircleIcon,            group: 'Control',     description: 'Verify element or text' },
  { type: 'wait_for_change',  label: 'Wait for Change',Icon: ArrowPathIcon,              group: 'Control',     description: 'Wait until a selector or region changes' },
  { type: 'end_point',        label: 'End Point',      Icon: FlagIcon,                   group: 'Control',     description: 'Mark workflow completion' },
  { type: 'captcha',          label: 'Captcha',        Icon: ShieldCheckIcon,            group: 'Control',     description: 'Solve a captcha challenge' },
  { type: 'twofa',            label: 'Enter 2FA code', Icon: ShieldCheckIcon,            group: 'Control',     description: 'Enter the one-time 2FA sign-in code', internal: true },
  { type: 'extract',          label: 'Extract',        Icon: ArrowTopRightOnSquareIcon,  group: 'Data',        description: 'Read data from an element' },
  { type: 'evaluate',         label: 'Evaluate JS',    Icon: CodeBracketIcon,            group: 'Data',        description: 'Run JavaScript on the page' },
  { type: 'api_call',         label: 'API Call',       Icon: LinkIcon,                   group: 'Data',        description: 'Make an HTTP request' },
  { type: 'login_post',       label: 'Sign-in request',Icon: ShieldCheckIcon,            group: 'Data',        description: 'Authenticate via a request (no form)', internal: true },
  { type: 'wait_for_download',label: 'Wait for Download',Icon: ArrowDownTrayIcon,        group: 'Data',        description: 'Capture a file the page downloads' },
  // ONE authorable AI step. `AI Fill` / `AI Continue` / `AI Navigate` were three names for
  // the same idea — "describe it, the AI does it" — so the palette now offers `ai_continue`
  // only. `ai_navigate` is the older wire spelling of that SAME step and executes the
  // identical agent-brain path; `ai_fill` is the superseded one-shot form fill. Both stay in
  // the catalog, hidden from the palette, so steps saved under them still render and edit.
  { type: 'ai_continue',      label: 'AI Task',        Icon: SparklesIcon,               group: 'AI',          description: 'Describe a task — the AI does it on the page' },
  { type: 'ai_navigate',      label: 'AI Task (goal)', Icon: RocketLaunchIcon,           group: 'AI',          description: 'Older spelling of AI Task — runs the same step', internal: true },
  { type: 'ai_fill',          label: 'AI Fill (legacy)', Icon: SparklesIcon,             group: 'AI',          description: 'Superseded one-shot form fill — use AI Task', internal: true },
  { type: 'wait_for_tab',     label: 'Tab Opened by Site', Icon: ArrowTopRightOnSquareIcon, group: 'Tabs',        description: 'Wait for the site to open a new tab (popup / target=_blank)' },
  { type: 'open_tab',         label: 'Open New Tab',   Icon: PlusIcon,                   group: 'Tabs',        description: 'Open a new tab yourself and go to a URL' },
  { type: 'tab_closed',       label: 'Tab Closed',     Icon: XMarkIcon,                  group: 'Tabs',        description: 'Return to the parent tab' },
  { type: 'codegen',          label: 'Script Block',   Icon: CodeBracketIcon,            group: 'Advanced',    description: 'Raw Playwright script' },
];

export const STEP_META: Record<string, StepTypeMeta> = Object.fromEntries(STEP_TYPES.map(t => [t.type, t]));

export const GROUP_ORDER: StepGroup[] = ['Navigation', 'Interaction', 'Input', 'Control', 'Data', 'AI', 'Tabs', 'Advanced'];

/**
 * Monochrome node styling per group — scannability comes from shade + shape,
 * never colored accents (design language: monochrome only).
 */
export const GROUP_NODE_STYLE: Record<StepGroup, string> = {
  Navigation:  'bg-zinc-900 text-white',
  Interaction: 'bg-zinc-700 text-white',
  Input:       'bg-canvas0 text-white',
  Control:     'bg-zinc-200 text-zinc-700',
  Data:        'bg-white text-ink border border-zinc-300',
  AI:          'bg-white text-ink border border-dashed border-zinc-400',
  Tabs:        'bg-zinc-100 text-zinc-600 border border-zinc-300',
  Advanced:    'bg-white text-ink border border-zinc-300',
};

export function stepMeta(type: string): StepTypeMeta {
  return STEP_META[type] || { type: type as StepType, label: type, Icon: CursorArrowRaysIcon, group: 'Advanced', description: '' };
}

export interface StepDetail {
  label: string;
  value: string;
  mono?: boolean;
  sensitive?: boolean;
}

export interface StepInfo {
  /** Primary one-line summary for fast scanning. */
  summary: string;
  details: StepDetail[];
  badges: string[];
}

/** Extract human-readable details + badges from a step (handles the step.x / config.x / options.x spread). */
export function getStepInfo(step: WorkflowStep): StepInfo {
  const config = step.config || {};
  const options = step.options || {};
  const details: StepDetail[] = [];
  const badges: string[] = [];

  switch (step.type) {
    case 'navigate':
      if (step.url || config.url) details.push({ label: i18n.t('URL'), value: step.url || config.url, mono: true });
      break;
    case 'navigated_to':
      if (step.url || config.url) details.push({ label: i18n.t('URL'), value: step.url || config.url, mono: true });
      badges.push(i18n.t('Click-triggered'));
      break;
    case 'click':
      if (options.label || options.tag) details.push({ label: i18n.t('Target'), value: options.label || options.tag });
      if (step.selector || config.selector) details.push({ label: i18n.t('Selector'), value: step.selector || config.selector, mono: true });
      break;
    case 'fill':
    case 'type': {
      const fieldName = options.label || options.field_name || config.field_name || '';
      const val = step.value || config.value || '';
      const isSensitive = options.is_sensitive || config.is_sensitive || val.includes('{{secret:');
      if (fieldName) details.push({ label: i18n.t('Field'), value: fieldName });
      if (val) details.push({ label: i18n.t('Value'), value: isSensitive ? '••••••••' : val, mono: true, sensitive: isSensitive });
      if (step.selector) details.push({ label: i18n.t('Selector'), value: step.selector, mono: true });
      const cat = options.field_category || config.field_category || '';
      if (cat && cat !== 'text') badges.push(cat);
      if (isSensitive) badges.push(i18n.t('Sensitive'));
      if (options.via_keyboard) badges.push(i18n.t('Keyboard'));
      if (options.from_autocomplete) badges.push(i18n.t('Autocomplete'));
      if (options.from_datepicker || config.from_datepicker) badges.push(i18n.t('Date picker'));
      break;
    }
    case 'select':
      if (step.value || config.value) details.push({ label: i18n.t('Option'), value: step.value || config.value });
      if (step.selector) details.push({ label: i18n.t('Selector'), value: step.selector, mono: true });
      if (options.from_custom_dropdown) badges.push(i18n.t('Custom dropdown'));
      break;
    case 'check':
    case 'uncheck':
      if (options.label) details.push({ label: i18n.t('Label'), value: options.label });
      if (step.selector) details.push({ label: i18n.t('Selector'), value: step.selector, mono: true });
      break;
    case 'press':
      if (step.value || config.key) details.push({ label: i18n.t('Key'), value: step.value || config.key });
      break;
    case 'wait':
      details.push({ label: i18n.t('Duration'), value: `${config.value || config.duration || options.duration || 1000}ms` });
      break;
    case 'scroll':
      if (step.selector) details.push({ label: i18n.t('Container'), value: step.selector, mono: true });
      else details.push({ label: i18n.t('Target'), value: i18n.t('Page') });
      if (options.scrollToBottom) badges.push(i18n.t('To bottom'));
      break;
    case 'scroll_into_view':
      if (step.selector) details.push({ label: i18n.t('Element'), value: step.selector, mono: true });
      break;
    case 'hover':
      if (step.selector) details.push({ label: i18n.t('Element'), value: step.selector, mono: true });
      break;
    case 'screenshot':
      if (config.name) details.push({ label: i18n.t('Name'), value: config.name });
      break;
    case 'assert':
      if (step.selector) details.push({ label: i18n.t('Selector'), value: step.selector, mono: true });
      if (step.value) details.push({ label: i18n.t('Expected'), value: step.value });
      break;
    case 'evaluate':
      if (config.variable) details.push({ label: i18n.t('Variable'), value: config.variable });
      if (config.script || step.script) {
        const s = config.script || step.script || '';
        details.push({ label: i18n.t('Script'), value: s.length > 60 ? s.substring(0, 60) + '…' : s, mono: true });
      }
      break;
    case 'extract':
      details.push({ label: i18n.t('Variable'), value: config.variable || 'data' });
      if (step.selector) details.push({ label: i18n.t('Selector'), value: step.selector, mono: true });
      if (config.attribute && config.attribute !== 'textContent') badges.push(config.attribute);
      break;
    case 'api_call': {
      const method = (config.method || 'POST').toUpperCase();
      if (config.url) {
        let path = config.url;
        try { path = new URL(config.url).pathname; } catch { /* keep raw */ }
        details.push({ label: i18n.t('URL'), value: `${method} ${path}`, mono: true });
      }
      if (config.variable) details.push({ label: i18n.t('Variable'), value: config.variable });
      break;
    }
    // One merged AI step, two wire spellings: `task` is what the editor writes, `goal`
    // what steps saved as `ai_navigate` carry. Both execute the same agent brain, so both
    // summarise identically. No `Vision` badge: the brain screenshots on demand — the old
    // `use_vision` flag only ever reached the retired eager-screenshot loop.
    case 'ai_continue':
    case 'ai_navigate': {
      const task = config.task || config.goal || '';
      if (task) details.push({ label: i18n.t('Task'), value: task });
      if (config.url) details.push({ label: i18n.t('Start'), value: config.url, mono: true });
      const maxSteps = config.max_actions || config.max_steps;
      if (maxSteps) badges.push(i18n.t('Max {{n}}', { n: maxSteps }));
      badges.push(i18n.t('Autonomous'));
      break;
    }
    // Superseded one-shot form fill — still replays exactly as recorded, so it keeps its
    // own summary rather than borrowing the agent step's.
    case 'ai_fill':
      if (config.instruction) details.push({ label: i18n.t('Instruction'), value: config.instruction });
      badges.push(i18n.t('AI powered'));
      break;
    case 'end_point': {
      const cond = config.condition_type || 'immediate';
      if (cond === 'element_visible' && config.selector) details.push({ label: i18n.t('Condition'), value: `Visible: ${config.selector}`, mono: true });
      else if (cond === 'element_exists' && config.selector) details.push({ label: i18n.t('Condition'), value: `Exists: ${config.selector}`, mono: true });
      else if (cond === 'text_visible' && config.text) details.push({ label: i18n.t('Condition'), value: `Text: "${config.text}"` });
      else if (cond === 'url_contains' && config.url) details.push({ label: i18n.t('Condition'), value: `URL contains ${config.url}`, mono: true });
      else if (cond === 'url_equals' && config.url) details.push({ label: i18n.t('Condition'), value: `URL is ${config.url}`, mono: true });
      else details.push({ label: i18n.t('Condition'), value: i18n.t('Immediate') });
      if (config.success_message) details.push({ label: i18n.t('Message'), value: config.success_message });
      if (cond !== 'immediate') badges.push(i18n.t('{{n}}s timeout', { n: (config.timeout_ms || 30000) / 1000 }));
      badges.push(i18n.t('Completes workflow'));
      break;
    }
    case 'captcha': {
      const ct = config.captcha_type || options.captcha_type || 'auto-detect';
      const ctLabel = ct === 'recaptcha_v2' ? 'reCAPTCHA v2' : ct === 'recaptcha_v3' ? 'reCAPTCHA v3' : ct === 'hcaptcha' ? 'hCaptcha' : ct === 'turnstile' ? 'Turnstile' : ct;
      details.push({ label: i18n.t('Type'), value: ctLabel });
      badges.push(i18n.t('Trusted agents only'));
      break;
    }
    case 'wait_for_tab':
      if (step.url || config.url) details.push({ label: i18n.t('Tab URL'), value: step.url || config.url, mono: true });
      badges.push(i18n.t('Opened by site'));
      break;
    case 'open_tab':
      if (step.url || config.url) details.push({ label: i18n.t('Tab URL'), value: step.url || config.url, mono: true });
      badges.push(i18n.t('You open it'));
      break;
    case 'tab_closed':
      badges.push(i18n.t('Back to parent'));
      break;
    case 'wait_for_change': {
      const watchKind = options.watch_kind || (options.region ? 'region' : 'selector');
      if (watchKind === 'region') {
        const r = options.region || {};
        details.push({ label: i18n.t('Region'), value: `${r.x ?? 0},${r.y ?? 0} · ${r.width ?? r.w ?? 0}×${r.height ?? r.h ?? 0}`, mono: true });
      } else if (step.selector || config.selector) {
        details.push({ label: i18n.t('Selector'), value: step.selector || config.selector, mono: true });
      }
      const outName = options.output_name || config.variable;
      if (outName) details.push({ label: i18n.t('Output'), value: outName, mono: true });
      const changeKind = options.change_kind || (watchKind === 'region' ? 'visual' : 'text');
      badges.push(i18n.t('Change: {{kind}}', { kind: changeKind }));
      if ((options.baseline_mode || 'in_run') === 'since_last_run') badges.push(i18n.t('Since last run'));
      if ((options.on_no_change || 'fail') === 'continue') badges.push(i18n.t('Continue if unchanged'));
      break;
    }
    case 'codegen': {
      const s = config.script || step.script || '';
      if (s) details.push({ label: i18n.t('Script'), value: s.length > 60 ? s.substring(0, 60) + '…' : s, mono: true });
      break;
    }
    case 'upload': {
      if (config.file_slot !== undefined && config.file_slot !== null) {
        details.push({ label: i18n.t('Slot'), value: config.file_slot || i18n.t('(unset)'), mono: true });
        badges.push(i18n.t('Buyer-bound'));
      } else if (config.file_name || config.file_id) {
        details.push({ label: i18n.t('File'), value: config.file_name || config.file_id });
      }
      if (step.selector || config.selector) details.push({ label: i18n.t('Selector'), value: step.selector || config.selector, mono: true });
      if (config.is_multiple) badges.push(i18n.t('Multiple'));
      if (config.mode && config.mode !== 'auto') badges.push(config.mode === 'chooser' ? i18n.t('Chooser') : i18n.t('File input'));
      break;
    }
    case 'wait_for_download': {
      if (config.output_key) details.push({ label: i18n.t('Save as'), value: config.output_key, mono: true });
      if (config.suggested_filename) details.push({ label: i18n.t('Filename'), value: config.suggested_filename });
      badges.push(i18n.t('Captures a file'));
      break;
    }
  }

  if (step.optional) badges.push(i18n.t('Optional'));
  // Surface every placeholder the step actually references — one badge per unique
  // name, shown as `{{name}}` (or `{{secret:name}}`) so it's visually distinct
  // from the plain text badges above. Secrets ARE included on purpose: the runner
  // needs to know which secret keys a step depends on so they can wire them up
  // before running. The scan walks the whole step recursively so a placeholder
  // nested in `config`/`options`/an api-call `headers` object still counts.
  for (const name of collectPlaceholders(step)) badges.push(`{{${name}}}`);

  return { summary: details[0]?.value || step.description || '', details, badges };
}

/** Every unique placeholder NAME (input like `userId`, or secret like
 *  `secret:api_key`) referenced anywhere in a string leaf of `v`. Insertion-
 *  ordered so badges render in the order the user first wrote them — stable
 *  for a given step definition. Shared with the Run modal's secret prompt. */
export function collectPlaceholders(v: unknown): Set<string> {
  const out = new Set<string>();
  const walk = (x: unknown): void => {
    if (typeof x === 'string') {
      for (const m of x.matchAll(PLACEHOLDER_RE)) {
        const name = m[1].trim();
        if (name) out.add(name);
      }
      return;
    }
    if (Array.isArray(x)) { x.forEach(walk); return; }
    if (x && typeof x === 'object') { Object.values(x).forEach(walk); }
  };
  walk(v);
  return out;
}

// Captures the placeholder body (`\{\{ NAME \}\}`). Lazy `[^{}]+` never crosses
// an intervening `{`/`}`, so `{{a}}{{b}}` yields two distinct captures and a
// stray `{{ }}` (whitespace only) is discarded by the trim/empty check below.
const PLACEHOLDER_RE = /\{\{\s*([^{}]+?)\s*\}\}/g;

/** Create a fresh step of the given type with sensible defaults. */
export function createStep(type: StepType): WorkflowStep {
  const step: WorkflowStep = { id: crypto.randomUUID(), type, enabled: true };
  switch (type) {
    case 'wait': step.config = { duration: 1000 }; break;
    case 'ai_continue': step.config = { max_actions: 10 }; break;
    case 'end_point': step.config = { condition_type: 'immediate' }; break;
    case 'api_call': step.config = { method: 'GET' }; break;
    case 'extract': step.config = { attribute: 'textContent' }; break;
    case 'captcha': step.config = { captcha_type: 'recaptcha_v2' }; break;
    case 'open_tab': step.config = { url: '' }; break;
    case 'upload': step.config = { mode: 'auto', is_multiple: false }; break;
    case 'wait_for_download': step.config = { output_key: '' }; break;
    case 'wait_for_change':
      step.options = {
        watch_kind: 'selector',
        change_kind: 'text',
        output_name: 'change',
        variable: 'change',
        baseline_mode: 'in_run',
        timeout_ms: 30000,
        on_no_change: 'fail',
      };
      break;
    case 'press': step.value = 'Enter'; break;
  }
  return step;
}

/** Searchable haystack for the filter box. */
export function stepSearchText(step: WorkflowStep): string {
  const meta = stepMeta(step.type);
  const { details, badges } = getStepInfo(step);
  return [
    step.type, meta.label, step.description || '', step.selector || '', step.value || '', step.url || '',
    ...details.map(d => `${d.label} ${d.sensitive ? '' : d.value}`),
    ...badges,
  ].join(' ').toLowerCase();
}
