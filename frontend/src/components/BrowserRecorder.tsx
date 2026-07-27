import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Dialog, Transition } from '@headlessui/react';
import {
  useRecorderSelection,
  type SelectionMode,
  type SelectionRegion,
  type ElementInfo,
} from './wizard/useRecorderSelection';
import { useRegisterRecorderActivity } from './RecorderActivityContext';
import {
  XMarkIcon,
  ArrowPathIcon,
  TrashIcon,
  CheckCircleIcon,
  CursorArrowRaysIcon,
  CodeBracketIcon,
  ChevronUpIcon,
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  KeyIcon,
  ListBulletIcon,
  SparklesIcon,
  PaperAirplaneIcon,
  PlayIcon,
  GlobeAltIcon,
  PencilSquareIcon,
  ClipboardDocumentListIcon,
  CheckIcon,
  ClockIcon,
  CameraIcon,
  EyeIcon,
  ArrowsUpDownIcon,
  ArrowUturnLeftIcon,
  ArrowRightIcon,
  LinkIcon,
  HashtagIcon,
  SwatchIcon,
  CalendarIcon,
  MagnifyingGlassIcon,
  ChevronUpDownIcon,
  BoltIcon,
  ArrowTopRightOnSquareIcon,
  PlusIcon,
  ExclamationTriangleIcon,
  ShieldCheckIcon,
  QrCodeIcon,
  ArrowUpTrayIcon,
  ArrowDownTrayIcon,
  StopIcon,
} from '@heroicons/react/24/outline';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import i18n from '../i18n';
import { triggerMini } from '../onboarding/miniTrigger';
import { AIScriptAssistant } from './AIScriptAssistant';
import { useRecorderCapability } from '../hooks/useRecorderCapability';
import { getAccessToken } from '../utils/auth';
import { statusStyle } from '../utils/statusStyle';
import { ConnectAgentPanel } from './ConnectAgentPanel';
import { PersonaWizard } from './workflows/PersonaWizard';
import { AuthenticatorImportModal } from './workflows/AuthenticatorImportModal';
import type { WorkflowStep } from '../types/api';
import { createStep, stepMeta, GROUP_NODE_STYLE } from './steps/stepMeta';
import { StepConfigForm } from './steps/StepConfigForm';
import { StepTypePalette } from './steps/StepTypePalette';
import { Checkbox, NumberInput, Select, Switch } from './ui';

/**
 * Mint a single-use, short-lived WebSocket auth ticket so the long-lived JWT does
 * not have to ride in the WS query string (where it leaks into proxy/access logs).
 * The access token is sent in the Authorization header (never in a URL). Returns
 * null on any failure so the caller can fall back to the legacy ?token= path.
 */
async function mintWsTicket(): Promise<string | null> {
  const token = getAccessToken();
  if (!token) return null;
  try {
    const resp = await fetch('/api/ws-ticket', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    });
    if (!resp.ok) return null;
    const data = await resp.json();
    return typeof data?.ticket === 'string' ? data.ticket : null;
  } catch {
    return null;
  }
}

interface RecordedStep {
  id: string;  // Unique ID for each step
  type: string;
  timestamp: number;
  selector?: string;
  url?: string;
  value?: string;
  description?: string;
  coordinates?: { x: number; y: number };
  options?: Record<string, any>;
  /** Step-type config (e.g. api_call: {method,url,headers,body_template,response_extractions,timeout_ms}). */
  config?: Record<string, any>;
  /** Optional steps don't stop the run on failure. */
  optional?: boolean;
  /** Disabled steps are skipped at run time. */
  enabled?: boolean;
}

/** An AI-proposed edit to an EXISTING recorded step. Targets a step by stable id
 *  (preferred) or array index; `update` carries only the fields to change. */
interface StepEdit {
  op: 'update' | 'delete' | 'move';
  id?: string;
  index?: number;
  to?: number;
  step?: Partial<RecordedStep> & { config?: Record<string, any>; options?: Record<string, any> };
}

/** A network request/response pair detected live during recording (full NetworkCall). */
interface DetectedRequest {
  id: string;
  method: string;
  url: string;
  request_headers?: Record<string, string>;
  request_body?: string;
  request_content_type?: string;
  response_status?: number;
  response_headers?: Record<string, string>;
  response_body?: string;
  response_content_type?: string;
}

interface DisplayStep {
  id: string;
  index: number;
  type: string;
  IconComponent: HeroIcon;
  title: string;
  description: string;
  selector?: string;
  value?: string;
  url?: string;
  editable: boolean;
  isSensitive?: boolean;
  inputType?: string;
  isFromPicker?: boolean;
  isFromAutocomplete?: boolean;
  isFromCustomDropdown?: boolean;
  isViaKeyboard?: boolean;
  isInTab?: boolean;
  isTabBoundary?: boolean;
  /** JS script body (extract/evaluate steps) — enables the fast-test button. */
  script?: string;
}

/** A single proposed change from AI Optimize (reviewable before applying). */
interface OptimizeChange {
  action: string;            // removed | replaced | reordered | added
  step_indices: number[];
  description: string;
  reason: string;
  risk: string;              // safe | caution | high
}

interface DetectedCredential {
  field_name: string;
  field_type: string;
  selector: string;
  value: string;
}

interface CapturedApiRequest {
  id: string;
  timestamp: number;
  method: string;
  url: string;
  headers: Record<string, string>;
  body: any;
  content_type: string;
  response?: {
    status: number;
    headers: Record<string, string>;
    body: any;
  };
  // User labels
  function_name?: string;
  is_auth?: boolean;
}

export interface StreamingHandler {
  name: string;
  step_range: [number, number];
  input_variables: string[];
  extract_fields: string[];
}

/** Imperative live-page helpers an embedder (e.g. ContentMonitorPanel) can call — screenshot for AI
 *  selector-find, DOM for grounding, raw evaluate for selector validation. Same shape as the old
 *  RecorderPreviewHandle so the monitor panel swaps RecorderPreview→BrowserRecorder transparently. */
export interface RecorderApiHandle {
  getScreenshot: () => string | null;
  getDOM: () => Promise<string | null>;
  evaluate: (script: string) => Promise<any>;
}

interface BrowserRecorderProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (steps: RecordedStep[], name: string, credentials?: Record<string, string>, formData?: Record<string, string>, segments?: any[]) => void;
  /** Fires whenever the (editable) recorded steps change, so an embedding wizard
   *  can keep its draft in sync with what the user actually sees/edited —
   *  without depending on the explicit Save action. */
  onStepsChange?: (steps: RecordedStep[]) => void;
  /** Fires whenever the recorder's step-group "functions" (segments) change, so an
   *  embedding wizard keeps its draft in sync without depending on the explicit Save
   *  — otherwise advancing the wizard any other way drops the created functions. */
  onSegmentsChange?: (segments: any[]) => void;
  /** When true, also captures HTTP POST/PUT/PATCH requests for API recording */
  apiMode?: boolean;
  /** Called when saving in API mode with captured functions */
  onSaveApi?: (functions: Record<string, any>, name: string) => void;
  /** When true, renders as a plain div without Dialog wrapper (for embedding in wizard) */
  embedded?: boolean;
  /** Pre-fill the URL bar and optionally auto-connect on mount */
  initialUrl?: string;
  /** When true + initialUrl set, auto-connects on mount */
  autoConnect?: boolean;
  /** Preload an existing workflow's steps so the user can open it "live" and
   *  replay/jump to any step to continue editing (vs. recording from scratch). */
  initialSteps?: RecordedStep[];
  /** Streaming mode: adds handler markers, advanced script editor, handler config */
  streamingMode?: boolean;
  /** Preferred agent ID to connect to (from execution target picker) */
  preferredAgentId?: string;
  /** Fired when the user creates/imports a persona from the in-recorder login
   *  prompt, so an embedding wizard can attach it as the workflow's default. */
  onPersonaCreated?: (personaId: number) => void;
  /** Fires whenever the detected login credentials change, so an embedding wizard
   *  keeps its draft in sync without depending on the explicit Save action. */
  onCredentialsChange?: (credentials: Record<string, string>) => void;
  /** Fires whenever the captured (non-sensitive) form data changes, for the same
   *  live-sync reason as onCredentialsChange. */
  onFormDataChange?: (formData: Record<string, string>) => void;
  /** Streaming mode only: fires whenever the streaming handler/script config
   *  changes, so the wizard can create without the recorder's own Save. */
  onStreamingConfigChange?: (config: {
    handlers: StreamingHandler[];
    advancedScriptEnabled: boolean;
    advancedScript: string;
    setupStepsCount: number;
  }) => void;
  /** Monitor "check target" selection mode: turns canvas clicks/drags into element/area/zone picks
   *  instead of recorded interactions. `null`/undefined = normal recording. The picked
   *  selectors/regions fire the callbacks below (used by ContentMonitorPanel to build selectors). */
  selectionMode?: SelectionMode | null;
  /** click mode: a picked element → its CSS selector + info. */
  onElementClick?: (info: ElementInfo) => void;
  /** area mode: every element inside the dragged rect. */
  onElementsFound?: (infos: ElementInfo[]) => void;
  /** zone mode: a viewport region for a visual (screenshot) check. */
  onZoneDrawn?: (region: SelectionRegion) => void;
  /** Already-defined visual zones to draw persistently over the live frame. */
  selectionZones?: Array<{ id: string; region: SelectionRegion }>;
  /** Zone id to emphasize (e.g. hovered in the sidebar). */
  highlightZoneId?: string | null;
  /** Populated with imperative live-page helpers (getScreenshot/getDOM/evaluate) once mounted. */
  apiRef?: React.MutableRefObject<RecorderApiHandle | null>;
  /** Monitor mode: fold the check-target picking into the recorder's own chrome — the
   *  spine gains a "Targets" tab (rendered from `monitorPanel`), the Steps tab becomes
   *  "Setup" (login/nav the checker replays), and workflow-only affordances (Extract /
   *  Return / Requests / Functions) are hidden. All the normal recording plumbing stays,
   *  so monitor creation shares the SAME BrowserRecorder as the workflow recorder. */
  monitorMode?: boolean;
  /** The selectors/check-target panel node (MonitorTargetsPanel) slotted into the Targets tab. */
  monitorPanel?: React.ReactNode;
  /** The action switcher (Browse/Click/Area/Zone/CSS) — floated as a toolbar just under
   *  the URL bar at the top of the stage. */
  monitorToolbar?: React.ReactNode;
  /** Monitor mode: routes the bottom "Ask AI" dock to selector-finding instead of the
   *  workflow agent loop. Given the user's description, it finds + generates check-target
   *  selectors and resolves to a summary string shown in the dock transcript. */
  onMonitorAiFind?: (prompt: string) => Promise<string>;
  /** Count shown on the Targets tab badge. */
  monitorTargetCount?: number;
  /** Fires with the live page URL so a monitor embedder keeps its target URL synced. */
  onUrlChange?: (url: string) => void;
}

type ConnectionState = 'disconnected' | 'connecting' | 'connected' | 'recording' | 'error' | 'needs_agent';

type HeroIcon = React.ForwardRefExoticComponent<React.SVGProps<SVGSVGElement> & { title?: string; titleId?: string }>;

const STEP_TYPE_ICON: Record<string, HeroIcon> = {
  navigate: GlobeAltIcon,
  click: CursorArrowRaysIcon,
  fill: PencilSquareIcon,
  type: PencilSquareIcon,
  press: HashtagIcon,
  select: ClipboardDocumentListIcon,
  check: CheckIcon,
  uncheck: XMarkIcon,
  scroll: ArrowsUpDownIcon,
  scroll_into_view: EyeIcon,
  wait: ClockIcon,
  screenshot: CameraIcon,
  hover: CursorArrowRaysIcon,
  'fill-password': KeyIcon,
  'fill-date': CalendarIcon,
  'fill-time': ClockIcon,
  'fill-color': SwatchIcon,
  'fill-range': ArrowsUpDownIcon,
  'fill-autocomplete': MagnifyingGlassIcon,
  'select-custom': ChevronUpDownIcon,
  'scroll-container': ArrowsUpDownIcon,
  extract: ArrowTopRightOnSquareIcon,
  wait_for_change: EyeIcon,
  return: ArrowUturnLeftIcon,
  navigated_to: ArrowRightIcon,
  wait_for_tab: ArrowTopRightOnSquareIcon,
  open_tab: PlusIcon,
  tab_closed: XMarkIcon,
  evaluate: CodeBracketIcon,
  api_call: LinkIcon,
  upload: ArrowUpTrayIcon,
  wait_for_download: ArrowDownTrayIcon,
};

// Render a value string with {{tags}} highlighted as colored chips
const renderValueWithTags = (value: string) => {
  const parts = value.split(/(\{\{[^}]+\}\})/g);
  if (parts.length === 1) return `"${value}"`;
  return (
    <span>
      "
      {parts.map((part, i) => {
        if (part.startsWith('{{') && part.endsWith('}}')) {
          const tag = part.slice(2, -2);
          return (
            <span key={i} className="inline-flex items-center px-1 py-0 bg-ink/10 text-ink rounded text-[10px] font-mono mx-0.5 font-medium">
              {tag}
            </span>
          );
        }
        return <span key={i}>{part}</span>;
      })}
      "
    </span>
  );
};

const getStepIconComponent = (step: RecordedStep): HeroIcon => {
  const options = step.options || {};

  if (step.type === 'fill') {
    if (options.is_sensitive || options.field_type === 'password') return STEP_TYPE_ICON['fill-password'];
    if (options.input_type === 'date' || options.from_datepicker) return STEP_TYPE_ICON['fill-date'];
    if (options.input_type === 'time') return STEP_TYPE_ICON['fill-time'];
    if (options.input_type === 'color') return STEP_TYPE_ICON['fill-color'];
    if (options.input_type === 'range') return STEP_TYPE_ICON['fill-range'];
    if (options.from_autocomplete) return STEP_TYPE_ICON['fill-autocomplete'];
  }

  if (step.type === 'select') {
    if (options.from_custom_dropdown) return STEP_TYPE_ICON['select-custom'];
  }

  if (step.type === 'scroll') {
    if (options.container || step.selector) return STEP_TYPE_ICON['scroll-container'];
  }

  return STEP_TYPE_ICON[step.type] || CursorArrowRaysIcon;
};

// Get step title based on type and options
const getStepTitle = (step: RecordedStep): string => {
  const options = step.options || {};

  if (step.type === 'fill') {
    if (options.is_sensitive) return i18n.t('Password');
    if (options.input_type === 'date' || options.from_datepicker) return i18n.t('Date Picker');
    if (options.input_type === 'time') return i18n.t('Time Picker');
    if (options.input_type === 'color') return i18n.t('Color Picker');
    if (options.input_type === 'range') return i18n.t('Slider');
    if (options.from_autocomplete) return i18n.t('Autocomplete');
    return i18n.t('Fill');
  }

  if (step.type === 'select') {
    if (options.from_custom_dropdown) return i18n.t('Dropdown');
    return i18n.t('Select');
  }

  if (step.type === 'check') {
    const label = options.label || '';
    return label ? i18n.t('Check "{{label}}"', { label: label.substring(0, 20) }) : i18n.t('Checkbox');
  }

  if (step.type === 'uncheck') {
    const label = options.label || '';
    return label ? i18n.t('Uncheck "{{label}}"', { label: label.substring(0, 20) }) : i18n.t('Uncheck');
  }

  if (step.type === 'scroll') {
    if (options.container || step.selector) {
      return options.scrollToBottom ? i18n.t('Scroll to Bottom') : i18n.t('Scroll Container');
    }
    return i18n.t('Scroll Page');
  }

  if (step.type === 'scroll_into_view') {
    return i18n.t('Scroll to Element');
  }

  if (step.type === 'press') {
    return i18n.t('Press {{key}}', { key: step.value || i18n.t('Key') });
  }

  if (step.type === 'navigated_to') {
    return i18n.t('Page Navigated');
  }

  if (step.type === 'wait_for_tab') {
    return i18n.t('Tab Opened by Site');
  }

  if (step.type === 'open_tab') {
    return i18n.t('Open New Tab');
  }

  if (step.type === 'tab_closed') {
    return i18n.t('Tab Closed');
  }

  if (step.type === 'api_call') {
    const fn = step.config?.function_name;
    const method = step.config?.method || 'GET';
    return fn ? i18n.t('API: {{fn}}', { fn }) : i18n.t('API {{method}}', { method });
  }

  return step.type.charAt(0).toUpperCase() + step.type.slice(1).replace(/_/g, ' ');
};

// ── Captured-request → workflow-step helpers ───────────────────────────────

/** Short "path?query" form of a URL for compact display. */
const prettyPath = (url: string): string => {
  try {
    const u = new URL(url);
    return u.pathname + (u.search ? u.search.slice(0, 40) : '');
  } catch {
    return url;
  }
};

/** Derive a snake_case function name from a request, e.g. POST /api/auth/login → post_login. */
const deriveApiFunctionName = (url: string, method: string): string => {
  let slug = 'call';
  try {
    const segments = new URL(url).pathname.split('/').filter(Boolean);
    // Prefer the last non-id-looking segment (skip pure numbers / long hashes / uuids).
    const meaningful = segments.filter(
      (s) => !/^\d+$/.test(s) && !/^[0-9a-f-]{8,}$/i.test(s)
    );
    slug = meaningful.slice(-1)[0] || segments.slice(-1)[0] || 'call';
  } catch {
    /* keep default */
  }
  const base = slug.replace(/[^a-zA-Z0-9]+/g, '_').toLowerCase().replace(/^_+|_+$/g, '');
  return `${method.toLowerCase()}_${base || 'call'}`;
};

const isPlainObject = (v: any): boolean =>
  !!v && typeof v === 'object' && !Array.isArray(v);

/**
 * The recorder's evaluate_js runs `page.evaluate(script)` raw, which only accepts
 * a JS expression — not a function body. AI-generated extraction scripts are
 * function bodies that use `return`, so wrap those in an IIFE so the fast-test
 * matches how the script actually runs at execution time.
 *
 * BUT a script that already starts with `(` is itself an expression/IIFE (often an
 * async IIFE: `(async () => { ... return x; })()`). Its `return` lives INSIDE the
 * IIFE body, not at the top level, so it must NOT be wrapped — wrapping it in a
 * sync `(() => { ... })()` drops the inner promise (no `return`), so Playwright
 * gets an unawaited Promise and serializes it to `null`. Mirror the agent's own
 * backend check (saas_bridge evaluate_js: `s.starts_with('(')`).
 */
const wrapScriptForEval = (s: string): string => {
  const t = s.trim();
  if (t.startsWith('(')) return s; // already an expression/IIFE — run as-is
  return /\breturn\b/.test(t) ? `(() => { ${s} })()` : s;
};

/**
 * Auto-suggest response_extractions (name → JSONPath) from a JSON response body.
 * Deterministic, client-side heuristic: surfaces likely tokens/ids/auth fields at
 * the top level, one level deep, and inside arrays-of-objects. No AI required.
 */
const autoSuggestExtractions = (responseBody?: string): Record<string, string> => {
  if (!responseBody) return {};
  let data: any;
  try {
    data = JSON.parse(responseBody);
  } catch {
    return {};
  }
  if (!isPlainObject(data)) return {};

  const out: Record<string, string> = {};
  const sanitize = (k: string) => k.replace(/[^a-zA-Z0-9]+/g, '_').replace(/^_+|_+$/g, '').toLowerCase();
  const considerScalar = (key: string, path: string) => {
    const k = key.toLowerCase();
    const isAuthish = /(^|_)(token|access_token|refresh_token|jwt|auth|api_key|apikey|secret|session|csrf|bearer)($|_)/.test(k);
    const isId = /(^id$|_id$|^uuid$|^slug$|^code$)/.test(k);
    if (isAuthish || isId) {
      const name = sanitize(key) || k;
      if (name && !(name in out)) out[name] = path;
    }
  };

  for (const [key, val] of Object.entries(data)) {
    const path = `$.${key}`;
    if (typeof val === 'string' || typeof val === 'number' || typeof val === 'boolean') {
      considerScalar(key, path);
    } else if (isPlainObject(val)) {
      for (const [k2, v2] of Object.entries(val as Record<string, any>)) {
        if (typeof v2 === 'string' || typeof v2 === 'number' || typeof v2 === 'boolean') {
          considerScalar(k2, `${path}.${k2}`);
        }
      }
    } else if (Array.isArray(val) && val.length > 0 && isPlainObject(val[0])) {
      for (const [k2, v2] of Object.entries(val[0])) {
        if ((typeof v2 === 'string' || typeof v2 === 'number') && /(^id$|_id$)/.test(k2.toLowerCase())) {
          const name = sanitize(`${key}_${k2}`);
          if (name && !(name in out)) out[name] = `$.${key}[0].${k2}`;
        }
      }
    }
  }
  return out;
};

/**
 * StepFormPanel — the single per-type edit surface, shared by manual step creation
 * and inline editing of existing steps. Mirrors the workflow StepsEditor: a header,
 * the canonical StepConfigForm (handles EVERY step type), a Note field, an Optional
 * toggle, and confirm/cancel actions.
 */
const StepFormPanel: React.FC<{
  step: WorkflowStep;
  onChange: (updates: Partial<WorkflowStep>) => void;
  onConfirm: () => void;
  onCancel: () => void;
  confirmLabel: string;
  onPickSelector?: (apply: (selector: string) => void) => void;
}> = ({ step, onChange, onConfirm, onCancel, confirmLabel, onPickSelector }) => {
  const { t } = useTranslation();
  const meta = stepMeta(step.type);
  const Icon = meta.Icon;
  // No card chrome here — the panel is always hosted inside StepBuilderDialog, which
  // supplies the frame and the width the per-type form needs to breathe.
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <span className={clsx('w-6 h-6 rounded-md flex items-center justify-center shrink-0', GROUP_NODE_STYLE[meta.group])}>
          <Icon className="w-3 h-3" />
        </span>
        <span className="text-sm font-semibold text-ink">{meta.label}</span>
        <span className="text-[11px] text-tertiary truncate">{meta.description}</span>
      </div>

      <StepConfigForm step={step} onUpdate={onChange} onPickSelector={onPickSelector} />

      {/* Note */}
      <div className="flex items-start gap-3">
        <label className="text-xs text-secondary shrink-0 w-[88px] pt-2">{t('Note')}</label>
        <input
          value={step.description || ''}
          onChange={e => onChange({ description: e.target.value })}
          placeholder={t('Optional description')}
          className="flex-1 px-3 py-1.5 text-sm bg-canvas border border-border rounded-lg outline-none focus:border-ink/30 focus:ring-2 focus:ring-ink/5"
        />
      </div>
      <div className="flex items-center gap-5 pl-[100px]">
        <Checkbox
          checked={step.optional || false}
          onChange={e => onChange({ optional: e.target.checked })}
          label={t("Optional — failures don't stop the run")}
        />
      </div>

      <div className="flex gap-2 pt-1">
        <button onClick={onCancel} className="flex-1 px-2 py-1.5 bg-hover text-secondary rounded-lg text-xs">{t('Cancel')}</button>
        <button onClick={onConfirm} className="flex-1 px-2 py-1.5 bg-ink text-white rounded-lg text-xs font-semibold shadow-sm">{confirmLabel}</button>
      </div>
    </div>
  );
};

/**
 * StepBuilderDialog — centered, roomy overlay that hosts the step palette and the
 * per-type form. The steps spine is only 320px wide (w-80), far too cramped for the
 * StepConfigForm's 88px labels + indented controls, so creating/editing a step pops
 * out here with real width instead of being squeezed/cropped in the column.
 */
const StepBuilderDialog: React.FC<{
  open: boolean;
  onClose: () => void;
  title: string;
  onBack?: () => void;
  /** Temporarily hide (without unmounting) — used while picking an element so the
   *  live browser canvas is reachable; the dialog's draft/edit state is preserved. */
  hidden?: boolean;
  children: React.ReactNode;
}> = ({ open, onClose, title, onBack, hidden = false, children }) => {
  const { t } = useTranslation();
  return (
    <Transition show={open && !hidden} appear as={React.Fragment}>
      <Dialog onClose={onClose} className="relative z-[70]">
        <Transition.Child
          as={React.Fragment}
          enter="ease-out duration-150" enterFrom="opacity-0" enterTo="opacity-100"
          leave="ease-in duration-100" leaveFrom="opacity-100" leaveTo="opacity-0"
        >
          <div className="fixed inset-0 bg-black/40 backdrop-blur-sm" aria-hidden="true" />
        </Transition.Child>
        <div className="fixed inset-0 flex items-center justify-center p-4">
          <Transition.Child
            as={React.Fragment}
            enter="ease-out duration-150" enterFrom="opacity-0 scale-95" enterTo="opacity-100 scale-100"
            leave="ease-in duration-100" leaveFrom="opacity-100 scale-100" leaveTo="opacity-0 scale-95"
          >
            <Dialog.Panel className="w-full max-w-xl max-h-[85vh] flex flex-col bg-surface border border-border rounded-2xl shadow-2xl overflow-hidden">
              <div className="flex items-center gap-2 px-4 py-3 border-b border-border shrink-0">
                {onBack && (
                  <button onClick={onBack} className="p-1 -ml-1 text-tertiary hover:text-ink rounded hover:bg-chrome transition-colors" title={t('Back')}>
                    <ChevronLeftIcon className="w-4 h-4" />
                  </button>
                )}
                <Dialog.Title className="text-sm font-semibold text-ink flex-1 truncate">{title}</Dialog.Title>
                <button onClick={onClose} className="p-1 -mr-1 text-tertiary hover:text-ink rounded hover:bg-chrome transition-colors" title={t('Close')}>
                  <XMarkIcon className="w-4 h-4" />
                </button>
              </div>
              <div className="flex-1 min-h-0 overflow-y-auto p-4">{children}</div>
            </Dialog.Panel>
          </Transition.Child>
        </div>
      </Dialog>
    </Transition>
  );
};

/**
 * Manual step adder — pick ANY step type from the shared grouped palette, then fill
 * its type-specific config via the same StepConfigForm the workflow editor uses. The
 * whole flow runs inside StepBuilderDialog so every step type gets full-width fields.
 */
const ManualStepAdder: React.FC<{
  onAddStep: (step: RecordedStep) => void;
  onPickSelector?: (apply: (selector: string) => void) => void;
  pickActive?: boolean;
}> = ({ onAddStep, onPickSelector, pickActive = false }) => {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const [draft, setDraft] = React.useState<WorkflowStep | null>(null);

  const close = () => { setOpen(false); setDraft(null); };

  const confirmStep = () => {
    if (!draft) return;
    const recorded: RecordedStep = {
      ...(draft as RecordedStep),
      id: draft.id || `manual_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
      type: draft.type,
      timestamp: Date.now(),
    };
    onAddStep(recorded);
    close();
  };

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="w-full px-3 py-2 border border-dashed border-border rounded-lg text-secondary hover:text-ink hover:border-gray-500 text-xs flex items-center justify-center gap-1.5 transition-colors"
      >
        <PlusIcon className="w-3.5 h-3.5" /> {t('Add Step Manually')}
      </button>

      <StepBuilderDialog
        open={open}
        onClose={close}
        title={draft ? t('Configure step') : t('Add a step')}
        onBack={draft ? () => setDraft(null) : undefined}
        hidden={pickActive}
      >
        {draft ? (
          <StepFormPanel
            step={draft}
            onChange={updates => setDraft(d => (d ? { ...d, ...updates } : d))}
            onConfirm={confirmStep}
            onCancel={close}
            confirmLabel={t('Add Step')}
            onPickSelector={onPickSelector}
          />
        ) : (
          <StepTypePalette embedded onSelect={type => setDraft(createStep(type))} onCancel={close} />
        )}
      </StepBuilderDialog>
    </>
  );
};

export const BrowserRecorder: React.FC<BrowserRecorderProps> = ({
  isOpen,
  onClose: _onClose,
  onSave: _onSave,
  onStepsChange,
  onSegmentsChange,
  apiMode = false,
  onSaveApi,
  embedded = false,
  initialUrl,
  autoConnect: _autoConnect = false,
  initialSteps,
  streamingMode = false,
  preferredAgentId,
  onPersonaCreated,
  onCredentialsChange,
  onFormDataChange,
  onStreamingConfigChange,
  selectionMode = null,
  onElementClick,
  onElementsFound,
  onZoneDrawn,
  selectionZones = [],
  highlightZoneId = null,
  apiRef,
  monitorMode = false,
  monitorPanel = null,
  monitorToolbar = null,
  onMonitorAiFind,
  monitorTargetCount = 0,
  onUrlChange,
}) => {
  const { t } = useTranslation();
  // Connection state
  const [connectionState, setConnectionState] = useState<ConnectionState>('disconnected');
  // Mirror into a ref so async flows (e.g. replayToStep waiting for a fresh
  // session to reach 'recording') can read the latest state without re-binding.
  const connectionStateRef = useRef(connectionState);
  connectionStateRef.current = connectionState;
  // Publish "a live browser session is running" to the app shell so the sidebar
  // can warn before a main-nav click tears down the session + unsaved steps.
  useRegisterRecorderActivity(
    connectionState === 'connecting' ||
      connectionState === 'connected' ||
      connectionState === 'recording',
  );
  // Plan-aware recording gate. 'connect_local' = free user must connect a local
  // agent (no cloud quota / no agent online); 'waiting_cloud' = paid user, cloud
  // is busy — auto-wait. Drives the ConnectAgentPanel overlay + auto-retry.
  const [gateKind, setGateKind] = useState<'connect_local' | 'waiting_cloud' | null>(null);
  const {
    capability,
    canAttempt,
    isPaidCloud,
    loading: capabilityLoading,
  } = useRecorderCapability(isOpen);
  // Mirror into a ref so the WS onclose handler (captured in a useCallback that
  // doesn't depend on capability) always reads the latest paid/cloud state.
  const isPaidCloudRef = useRef(isPaidCloud);
  isPaidCloudRef.current = isPaidCloud;
  const [_sessionId, setSessionId] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const MAX_RECONNECT_ATTEMPTS = 5;
  const wasRecordingRef = useRef(false);

  // Idle timeout — close session if no user activity for 5 minutes
  const IDLE_TIMEOUT_MS = 5 * 60 * 1000;
  const idleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const resetIdleTimer = useCallback(() => {
    if (idleTimerRef.current) clearTimeout(idleTimerRef.current);
    idleTimerRef.current = setTimeout(() => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        toast(t('Recording stopped — idle timeout'), { icon: '⏱' });
        // Stop recording inline (can't reference stopRecording here)
        wasRecordingRef.current = false;
        try { wsRef.current.send(JSON.stringify({ type: 'stop' })); } catch {}
        wsRef.current.close();
        wsRef.current = null;
        setConnectionState('disconnected');
      }
    }, IDLE_TIMEOUT_MS);
  }, []);

  // API capture state (only used when apiMode=true)
  const [capturedApiRequests, setCapturedApiRequests] = useState<CapturedApiRequest[]>([]);
  const [_showApiPanel, setShowApiPanel] = useState(apiMode);
  const [expandedApiId, setExpandedApiId] = useState<string | null>(null);
  const [labelingApiId, setLabelingApiId] = useState<string | null>(null);
  const [apiLabelForm, setApiLabelForm] = useState({ name: '', is_auth: false });
  const [apiParamFields, setApiParamFields] = useState<Record<string, Record<string, string>>>({});
  const [apiExtractions, setApiExtractions] = useState<Record<string, Record<string, string>>>({});

  // Detected requests during NORMAL workflow recording (api_captured events).
  // The user can click one to convert it into an api_call workflow step.
  const [detectedRequests, setDetectedRequests] = useState<DetectedRequest[]>([]);
  const [serverRenderedNotices, setServerRenderedNotices] = useState<{ url: string; message: string }[]>([]);
  const [addedRequestKeys, setAddedRequestKeys] = useState<Set<string>>(new Set());
  const [showDetected, setShowDetected] = useState(true);

  // Recording state
  const [url, setUrl] = useState(initialUrl || 'https://');
  const [currentUrl, setCurrentUrl] = useState('');
  const [steps, setSteps] = useState<RecordedStep[]>([]);
  // Keep an embedding wizard's draft in sync with the live (edited) steps the
  // user sees — so "Create" saves exactly what's on screen, not a stale snapshot
  // that only updated on an explicit Save. Use a ref so an unstable callback
  // identity can't retrigger the effect.
  const onStepsChangeRef = useRef(onStepsChange);
  onStepsChangeRef.current = onStepsChange;
  useEffect(() => {
    onStepsChangeRef.current?.(steps);
  }, [steps]);
  const [displaySteps, setDisplaySteps] = useState<DisplayStep[]>([]);
  // id of the step whose inline edit form is open (null = none).
  const [editingStepId, setEditingStepId] = useState<string | null>(null);
  const [workflowName, setWorkflowName] = useState('');
  const [detectedCredentials, setDetectedCredentials] = useState<DetectedCredential[]>([]);
  // A 2FA/OTP field was seen during recording (recorder emits `twofa_detected`).
  const [detected2fa, setDetected2fa] = useState(false);
  // How the code is delivered (totp | email_otp | sms | unknown) — pre-selects
  // the persona 2FA method in the prefill. Best-effort hint from the page text.
  const [detected2faChannel, setDetected2faChannel] = useState<string | null>(null);
  // Offer to turn the recorded login into a persona (so runs sign in unattended).
  const [showPersonaWizard, setShowPersonaWizard] = useState(false);
  const [showAuthImport, setShowAuthImport] = useState(false);
  const [personaPromptDismissed, setPersonaPromptDismissed] = useState(false);
  const twofaToastShownRef = useRef(false);
  const [capturedFormData, setCapturedFormData] = useState<Record<string, string>>({});

  // UI state
  const [screenshot, setScreenshot] = useState<string | null>(null);
  // Monitor mode opens the rail by default so the Targets panel is always in reach.
  const [showSteps, setShowSteps] = useState(!!monitorMode);
  // Spine segmented switch: Steps (timeline, default) / Requests (API calls) / Functions
  // (segments + handlers) / Targets (monitor check-target picker). Monitor mode defaults
  // to the Targets tab since choosing what to watch is the primary action.
  const [spineTab, setSpineTab] = useState<'steps' | 'requests' | 'functions' | 'targets'>(monitorMode ? 'targets' : 'steps');
  const stepsAutoExpandedRef = useRef(false);
  const [showCode, setShowCode] = useState(false);
  const [generatedCode, setGeneratedCode] = useState('');

  // Tab state
  const [openTabs, setOpenTabs] = useState<{ index: number; url: string; title: string; active: boolean }[]>([]);

  // Recording options
  const [recordWaitSteps, setRecordWaitSteps] = useState(true);

  // Extraction mode state
  const [isExtracting, setIsExtracting] = useState(false);
  // "Pick element from page" for a step's selector field. While active the step
  // modal hides so the live canvas is clickable; the captured selector is handed to
  // the field's apply callback (held in a ref so the WS handler can reach it).
  const [pickActive, setPickActive] = useState(false);
  const pickApplyRef = useRef<((selector: string) => void) | null>(null);
  const [extractHighlight, setExtractHighlight] = useState<{ rect: { x: number; y: number; w: number; h: number }; selector: string } | null>(null);
  const [extractElementInfo, setExtractElementInfo] = useState<{
    selector: string; tag: string; text: string; ariaLabel: string | null;
    rect: { x: number; y: number; w: number; h: number };
  } | null>(null);
  const [showExtractPopover, setShowExtractPopover] = useState(false);
  const [extractOutputName, setExtractOutputName] = useState('');
  const [extractType, setExtractType] = useState('text');

  // Streaming mode state
  const [streamingHandlers, setStreamingHandlers] = useState<StreamingHandler[]>([]);
  const [activeHandlerName, setActiveHandlerName] = useState<string | null>(null);
  const [activeHandlerStart, setActiveHandlerStart] = useState<number | null>(null);
  const [showHandlerNameInput, setShowHandlerNameInput] = useState(false);
  const [handlerNameInput, setHandlerNameInput] = useState('');
  const [expandedHandlerId, setExpandedHandlerId] = useState<string | null>(null);
  const [streamingAdvancedEnabled, setStreamingAdvancedEnabled] = useState(false);
  const [streamingAdvancedScript, setStreamingAdvancedScript] = useState('');
  const [showAdvancedScript, setShowAdvancedScript] = useState(false);

  const DEFAULT_STREAMING_SCRIPT = `ps.on("message", ({ action, data, requestId }) => {
  // Handle incoming API commands
  ps.respond(requestId, { ok: true });
});`;

  // Live-sync streaming handler/script config to the wizard so "Done recording"
  // → Create works without the recorder's own Save (which is being removed).
  const onStreamingConfigChangeRef = useRef(onStreamingConfigChange);
  onStreamingConfigChangeRef.current = onStreamingConfigChange;
  useEffect(() => {
    if (!streamingMode) return;
    const setupStepsCount = Math.min(
      streamingHandlers.length > 0
        ? Math.min(...streamingHandlers.map((h) => h.step_range[0]))
        : steps.length,
      steps.length,
    );
    onStreamingConfigChangeRef.current?.({
      handlers: streamingHandlers,
      advancedScriptEnabled: streamingAdvancedEnabled,
      advancedScript: streamingAdvancedScript,
      setupStepsCount,
    });
  }, [streamingMode, streamingHandlers, streamingAdvancedEnabled, streamingAdvancedScript, steps]);

  // AI Assist state
  // Studio AI dock: always mounted bottom-center while the stage is live. Resting
  // = slim capsule; engaged = expands UPWARD into the chat transcript. This flag
  // tracks the engaged/expanded state (the old showAIChat toggle is retired).
  const [aiDockExpanded, setAiDockExpanded] = useState(false);
  // The AI dock rests semi-hidden at the bottom and slides up when hovered/engaged.
  const [aiDockHovered, setAiDockHovered] = useState(false);
  const [aiChatInput, setAiChatInput] = useState('');
  const [aiChatMessages, setAiChatMessages] = useState<Array<{ role: 'user' | 'assistant'; content: string; actions?: any[]; kind?: 'thought' | 'run' | 'result' }>>([]);
  const [aiChatLoading, setAiChatLoading] = useState(false);
  // Stable id for this AI-assist chat. Sent as `user` so the streaming provider
  // routes this conversation to its OWN browser tab instead of the shared one.
  // Regenerated whenever the chat is cleared (= a new conversation).
  const newChatConvId = () =>
    `recorder-${(globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.floor(Math.random() * 1e9)}`)}`;
  const [aiChatConvId, setAiChatConvId] = useState<string>(newChatConvId);
  // Unified agent: finished artifact awaiting the user's confirmation before it
  // enters the workflow (preview-then-confirm). Shaped per recorder mode.
  const [pendingChatSteps, setPendingChatSteps] = useState<{ summary: string; mode: string; steps: RecordedStep[]; script?: string; handlerName?: string; edits?: StepEdit[]; scriptMode?: 'append' | 'replace' } | null>(null);
  // Test result for the embedded script in a pending chat artifact.
  const [pendingChatTest, setPendingChatTest] = useState<{ loading?: boolean; ok?: boolean; result?: any; error?: string } | null>(null);
  // Autonomy: 'autonomous' runs the whole loop; 'assist' pauses for approval before each browser batch.
  const [agentAutonomy, setAgentAutonomy] = useState<'autonomous' | 'assist'>('autonomous');
  // In assist mode, a batch of proposed actions awaiting the user's Run/Skip.
  const [pendingAgentActions, setPendingAgentActions] = useState<{ thought: string; actions: any[] } | null>(null);
  const chatStopRef = useRef(false);
  // Aborts the in-flight /ai-assist/agent request so Stop interrupts mid-turn
  // instead of only at the next loop iteration.
  const chatAbortRef = useRef<AbortController | null>(null);
  // Resumable agent-loop context (survives assist-mode pauses across user clicks).
  const agentCtxRef = useRef<{ instruction: string; history: any[]; observation: any; iteration: number; max: number } | null>(null);
  // On-demand screenshot: set when the AI runs get_screenshot; sent as
  // screenshot_b64 on the NEXT turn (we no longer push a screenshot every turn).
  const pendingScreenshotRef = useRef<string | null>(null);
  const [showAIExtract, setShowAIExtract] = useState(false);
  const [aiExtractGoal, setAiExtractGoal] = useState('');
  // Generated-but-not-yet-added extraction script, so the user can fast-test it
  // (run it live, see the result) BEFORE committing it as a workflow step.
  const [extractDraft, setExtractDraft] = useState<{ script: string; message: string } | null>(null);
  const [extractTestResult, setExtractTestResult] = useState<{ loading?: boolean; ok?: boolean; result?: any; error?: string } | null>(null);
  const [aiExtractLoading, setAiExtractLoading] = useState(false);
  // --- Agentic scraper builder: AI drives the live browser (ephemerally, no
  // recorded steps) to explore + test, then authors a reusable evaluate script. ---
  const [scraperRunning, setScraperRunning] = useState(false);
  const [scraperLog, setScraperLog] = useState<{ kind: 'thought' | 'run' | 'result' | 'error' | 'done'; text: string }[]>([]);
  const [scraperDraft, setScraperDraft] = useState<{ script: string; variable: string; iframe?: string; summary: string } | null>(null);
  const [scraperTestResult, setScraperTestResult] = useState<{ loading?: boolean; ok?: boolean; result?: any; error?: string } | null>(null);
  const scraperStopRef = useRef(false);
  const [showAIScriptAssistant, setShowAIScriptAssistant] = useState(false);
  const [aiScriptGenerating, setAiScriptGenerating] = useState(false);
  const [manualTestLoading, setManualTestLoading] = useState(false);
  const [manualTestResult, setManualTestResult] = useState<any>(null);
  const [optimizeLoading, setOptimizeLoading] = useState(false);
  // Proposed optimization, held for review (Apply/Discard) — never auto-applied,
  // so AI optimize can't silently break the workflow.
  const [pendingOptimization, setPendingOptimization] = useState<{
    steps: RecordedStep[]; changes: OptimizeChange[]; warnings: string[]; removed_count: number;
  } | null>(null);
  const [optimizeNote, setOptimizeNote] = useState<string>('');
  // Per-step fast-test results, keyed by step id.
  const [scriptTests, setScriptTests] = useState<Record<string, { loading?: boolean; ok?: boolean; result?: any; error?: string }>>({});

  // Visual replay / "play to here": re-execute recorded steps 0..N on the live
  // page so the browser lands exactly at the selected step. `statuses` is keyed
  // by step index and drives the per-step cursor highlight in the timeline.
  type ReplayStatus = 'running' | 'done' | 'skipped' | 'failed' | 'cancelled';
  const [replayState, setReplayState] = useState<{
    running: boolean;
    target: number | null;
    current: number | null;
    statuses: Record<number, ReplayStatus>;
  }>({ running: false, target: null, current: null, statuses: {} });
  // Correlation id of the in-flight replay — guards against stale frames from a
  // superseded run updating the UI.
  const replayReqRef = useRef<string | null>(null);
  const [detectedSegments, setDetectedSegments] = useState<Array<{
    name: string; segment_type: string; step_indices: number[];
    depends_on: string[]; extract_outputs: string[];
  }>>([]);

  // Live-sync the step-group functions to an embedding wizard (same pattern as
  // onStepsChange) so "Create" persists them even if the user never hits the
  // recorder's own Save. Streaming mode owns `segments` via its own onSave path,
  // so don't emit there (would clobber the streaming_config segment).
  const onSegmentsChangeRef = useRef(onSegmentsChange);
  onSegmentsChangeRef.current = onSegmentsChange;
  useEffect(() => {
    if (streamingMode) return;
    // Only emit a non-empty set: a fresh mount (e.g. navigating back to this step)
    // starts with [] and must not clobber functions already synced to the wizard.
    if (detectedSegments.length > 0) onSegmentsChangeRef.current?.(detectedSegments);
  }, [detectedSegments, streamingMode]);

  // Live-sync detected credentials to the wizard (same rationale as onStepsChange)
  // so "Done recording" → Create persists them without the recorder's own Save.
  const onCredentialsChangeRef = useRef(onCredentialsChange);
  onCredentialsChangeRef.current = onCredentialsChange;
  useEffect(() => {
    if (detectedCredentials.length === 0) return;
    const credentials: Record<string, string> = {};
    detectedCredentials.forEach((cred) => { credentials[cred.field_name] = cred.value; });
    onCredentialsChangeRef.current?.(credentials);
  }, [detectedCredentials]);

  // Live-sync captured (non-sensitive) form data to the wizard.
  const onFormDataChangeRef = useRef(onFormDataChange);
  onFormDataChangeRef.current = onFormDataChange;
  useEffect(() => {
    if (Object.keys(capturedFormData).length === 0) return;
    onFormDataChangeRef.current?.(capturedFormData);
  }, [capturedFormData]);

  // Function builder state
  const [functionBuilderOpen, setFunctionBuilderOpen] = useState(false);
  const [selectedStepIndices, setSelectedStepIndices] = useState<Set<number>>(new Set());
  const [functionName, setFunctionName] = useState('');

  const toggleStepSelection = useCallback((index: number) => {
    setSelectedStepIndices(prev => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }, []);

  const createFunction = useCallback(() => {
    if (!functionName.trim() || selectedStepIndices.size === 0) return;
    const indices = Array.from(selectedStepIndices).sort((a, b) => a - b);
    const hasExtracts = indices.some(i => steps[i]?.type === 'extract');
    setDetectedSegments(prev => [...prev, {
      name: functionName.trim().replace(/\s+/g, '_').toLowerCase(),
      segment_type: hasExtracts ? 'extract' : 'action',
      step_indices: indices,
      depends_on: [],
      extract_outputs: indices
        .filter(i => steps[i]?.type === 'extract')
        .map(i => steps[i]?.value || `output_${i}`),
    }]);
    setFunctionName('');
    setSelectedStepIndices(new Set());
    setFunctionBuilderOpen(false);
    toast.success(t('Function "{{name}}" created with {{n}} steps', { name: functionName.trim(), n: indices.length }));
  }, [functionName, selectedStepIndices, steps]);

  const cancelFunctionBuilder = useCallback(() => {
    setFunctionBuilderOpen(false);
    setSelectedStepIndices(new Set());
    setFunctionName('');
  }, []);

  // Select overlay state for native dropdowns
  const [selectOverlay, setSelectOverlay] = useState<{
    show: boolean;
    selector: string;
    options: Array<{ value: string; text: string; selected: boolean; disabled: boolean; index: number }>;
    position: { x: number; y: number; width: number; height: number; selectTop: number };
    name: string;
  } | null>(null);

  // Native picker overlay state (date, time, color, etc.)
  const [pickerOverlay, setPickerOverlay] = useState<{
    show: boolean;
    pickerType: 'date' | 'time' | 'datetime-local' | 'month' | 'week' | 'color';
    selector: string;
    currentValue: string;
    position: { x: number; y: number; width: number; height: number };
    min?: string;
    max?: string;
    step?: string;
  } | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const imageRef = useRef<HTMLImageElement | null>(null);

  // Monitor "check target" selection: a container wrapping the canvas + the real streamed frame size
  // (the daemon streams at the agent's viewport, object-contain-scaled), so the shared selection hook
  // can map clicks/drags to page coordinates. Only active when `selectionMode` is set.
  const selectionContainerRef = useRef<HTMLDivElement>(null);
  const frameSizeRef = useRef<{ w: number; h: number }>({ w: 1280, h: 800 });
  // Throttle for the cursor trajectory streamed during normal recording (~50ms).
  const lastMouseMoveRef = useRef<number>(0);
  const selection = useRecorderSelection({
    wsRef,
    canvasRef,
    containerRef: selectionContainerRef,
    frameSizeRef,
    connected: connectionState === 'recording' || connectionState === 'connected',
    mode: selectionMode,
    onElementClick,
    onElementsFound,
    onZoneDrawn,
  });

  // Imperative live-page helpers exposed via `apiRef` (used by ContentMonitorPanel for AI
  // selector-find + selector validation). `getDOM`/`evaluate` ride a one-off WS message listener
  // (the same pattern the recorder already uses for eval_result) so they don't disturb the main loop.
  const wsAwait = useCallback((action: object, responseType: string, timeoutMs: number): Promise<any> => {
    return new Promise((resolve) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) { resolve(null); return; }
      let settled = false;
      const handler = (ev: MessageEvent) => {
        if (typeof ev.data !== 'string') return;
        try {
          const m = JSON.parse(ev.data);
          if (m?.type === responseType) { settled = true; ws.removeEventListener('message', handler); resolve(m); }
        } catch { /* ignore non-JSON / binary frames */ }
      };
      ws.addEventListener('message', handler);
      try { ws.send(JSON.stringify(action)); } catch { ws.removeEventListener('message', handler); resolve(null); return; }
      setTimeout(() => { if (!settled) { ws.removeEventListener('message', handler); resolve(null); } }, timeoutMs);
    });
  }, []);

  const apiGetScreenshot = useCallback((): string | null => {
    const c = canvasRef.current;
    if (!c) return null;
    try { return c.toDataURL('image/jpeg', 0.8).split(',')[1] || null; } catch { return null; }
  }, []);
  const apiGetDOM = useCallback(async (): Promise<string | null> => {
    const m = await wsAwait({ type: 'action', action: 'get_dom' }, 'dom_content', 10000);
    return m ? (m.html ?? m.content ?? null) : null;
  }, [wsAwait]);
  const apiEvaluate = useCallback(async (script: string): Promise<any> => {
    // Raw evaluate (not wrapped) so selector-validation expressions run as-is, matching RecorderPreview.
    const m = await wsAwait({ type: 'action', action: 'evaluate_js', script }, 'eval_result', 5000);
    return m ? (m.error ? null : m.result) : null;
  }, [wsAwait]);

  useEffect(() => {
    if (!apiRef) return;
    apiRef.current = { getScreenshot: apiGetScreenshot, getDOM: apiGetDOM, evaluate: apiEvaluate };
    return () => { if (apiRef) apiRef.current = null; };
  }, [apiRef, apiGetScreenshot, apiGetDOM, apiEvaluate]);

  // Monitor mode: keep the embedder's target URL pointed at the live page. Prefer the
  // navigated page (`currentUrl`); fall back to the typed URL bar once it's a real value
  // (never the "https://" placeholder, which would blank out a mode-step URL).
  useEffect(() => {
    if (!onUrlChange) return;
    const effective = currentUrl || (url && url !== 'https://' ? url : '');
    if (effective) onUrlChange(effective);
  }, [onUrlChange, currentUrl, url]);

  // Reset state when modal opens/closes
  useEffect(() => {
    if (isOpen) {
      // Preload an existing workflow's steps (live-edit mode) or start empty.
      const seed = initialSteps && initialSteps.length ? initialSteps : [];
      const firstNav = seed.find(s => s.type === 'navigate' && s.url)?.url;
      setUrl(firstNav || initialUrl || 'https://');
      setCurrentUrl('');
      setSteps(seed);
      setDisplaySteps([]);
      setReplayState({ running: false, target: null, current: null, statuses: {} });
      replayReqRef.current = null;
      setScreenshot(null);
      setSessionId(null);
      setConnectionState('disconnected');
      setGateKind(null);
      setWorkflowName('');
      setGeneratedCode('');
      setDetectedCredentials([]);
      setDetected2fa(false);
      setDetected2faChannel(null);
      setPersonaPromptDismissed(false);
      twofaToastShownRef.current = false;
      setOpenTabs([]);
      // Reset API capture state
      setCapturedApiRequests([]);
      setShowApiPanel(apiMode);
      setExpandedApiId(null);
      setApiParamFields({});
      // Reset detected-requests (live API capture) state
      setDetectedRequests([]);
      setServerRenderedNotices([]);
      setPendingOptimization(null);
      setOptimizeNote('');
      setScriptTests({});
      setExtractDraft(null);
      setExtractTestResult(null);
      setAddedRequestKeys(new Set());
      setApiExtractions({});
      // Reset AI state
      setAiDockExpanded(false);
      setAiChatMessages([]);
      setAiChatInput('');
      setAiChatConvId(newChatConvId());  // fresh conversation → fresh streaming tab
      setShowAIExtract(false);
      setAiExtractGoal('');
    } else {
      // Send stop and close WebSocket when modal closes
      wasRecordingRef.current = false;
      reconnectAttemptsRef.current = 0;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (wsRef.current) {
        if (wsRef.current.readyState === WebSocket.OPEN) {
          try { wsRef.current.send(JSON.stringify({ type: 'stop' })); } catch {}
        }
        wsRef.current.close();
        wsRef.current = null;
      }
      setConnectionState('disconnected');
    }
  }, [isOpen]);

  // Clean up on unmount and page close
  useEffect(() => {
    const cleanup = () => {
      wasRecordingRef.current = false;
      reconnectAttemptsRef.current = 0;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (idleTimerRef.current) clearTimeout(idleTimerRef.current);
      if (wsRef.current) {
        const ws = wsRef.current;
        wsRef.current = null;
        ws.onclose = null;
        ws.onerror = null;
        if (ws.readyState === WebSocket.OPEN) {
          try { ws.send(JSON.stringify({ type: 'stop' })); } catch {}
        }
        ws.close();
      }
    };
    // Reset idle timer on any user activity
    const onActivity = () => { if (wsRef.current) resetIdleTimer(); };
    window.addEventListener('beforeunload', cleanup);
    window.addEventListener('mousemove', onActivity, { passive: true });
    window.addEventListener('keydown', onActivity, { passive: true });
    window.addEventListener('click', onActivity, { passive: true });
    window.addEventListener('scroll', onActivity, { passive: true });
    return () => {
      window.removeEventListener('beforeunload', cleanup);
      window.removeEventListener('mousemove', onActivity);
      window.removeEventListener('keydown', onActivity);
      window.removeEventListener('click', onActivity);
      window.removeEventListener('scroll', onActivity);
      cleanup();
    };
  }, [resetIdleTimer]);

  const lastFrameUrlRef = useRef('');
  const autoConnectTriggeredRef = useRef(false);

  // Convert steps to display format with tab nesting awareness
  useEffect(() => {
    let inTab = false;
    const converted = steps.map((step, index) => {
      const options = step.options || {};
      const isTabOpener = step.type === 'wait_for_tab' || step.type === 'open_tab';
      if (isTabOpener) inTab = true;
      const isInTab = inTab && !isTabOpener && step.type !== 'tab_closed';
      if (step.type === 'tab_closed') inTab = false;
      return {
        id: step.id,
        index,
        type: step.type,
        IconComponent: getStepIconComponent(step),
        title: getStepTitle(step),
        description: step.description || `${step.type} ${step.selector || step.url || ''}`,
        selector: step.selector,
        value: step.value,
        url: step.url,
        editable: true,
        isSensitive: options.is_sensitive || options.field_type === 'password',
        inputType: options.input_type || options.field_type,
        isFromPicker: options.from_datepicker || ['date', 'time', 'color'].includes(options.input_type),
        isFromAutocomplete: options.from_autocomplete,
        isFromCustomDropdown: options.from_custom_dropdown,
        isViaKeyboard: options.via_keyboard,
        isInTab,
        isTabBoundary: isTabOpener || step.type === 'tab_closed',
        script: step.config?.script,
      };
    });
    setDisplaySteps(converted);
  }, [steps]);

  // Connect to recorder WebSocket. `startUrlOverride` lets a caller (e.g. replay
  // from a cold start) begin the session at a specific URL without racing the
  // `url` state update through this callback's stale closure.
  const connect = useCallback(async (startUrlOverride?: string) => {
    const startUrl = (typeof startUrlOverride === 'string' && startUrlOverride.startsWith('http'))
      ? startUrlOverride : url;
    if (!startUrl || !startUrl.startsWith('http')) {
      toast.error(t('Please enter a valid URL'));
      return;
    }

    // Supersede any existing socket. Detach its handlers first so its async
    // onclose/onerror can't fire after the new socket is assigned to
    // wsRef.current and clobber the live ref/state (reconnect / StrictMode
    // double-mount race). The reconnect path already nulls wsRef in onclose,
    // so this block is a no-op there.
    if (wsRef.current) {
      const stale = wsRef.current;
      stale.onopen = stale.onmessage = stale.onerror = stale.onclose = null;
      try { stale.close(); } catch {}
      wsRef.current = null;
    }

    setConnectionState('connecting');

    // Connect to backend recorder proxy (backend handles recorder selection transparently)
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // Connect through ws-gateway (handles load balancing + recorder discovery).
    // The browser can't set an Authorization header on a WebSocket, so the gateway
    // authenticates the frontend via a query-string credential (verifyFrontendAuth).
    // Prefer a SINGLE-USE, short-lived ticket (minted over an authenticated HTTP
    // request — Bearer in the header, never logged in a URL) so the long-lived JWT
    // no longer leaks into proxy/access logs. The ws-gateway redeems ?ticket= for
    // the bound JWT and verifies it. The ?token= fallback is used ONLY when the
    // ticket mint fails (store down / not yet deployed) so the recorder never
    // hard-breaks; on the normal path the JWT is NOT placed in the URL at all.
    // Without a credential the gateway closes with 4001.
    const params = new URLSearchParams();
    const _accessToken = getAccessToken();
    const _ticket = await mintWsTicket();
    if (_ticket) {
      params.set('ticket', _ticket);
    } else if (_accessToken) {
      // Fallback: ticketing unavailable — fall back to the legacy ?token= path.
      params.set('token', _accessToken);
    }
    if (preferredAgentId && preferredAgentId !== 'auto') params.set('agent_id', preferredAgentId);
    const _qs = params.toString();
    const wsUrl = `${wsProtocol}//${window.location.host}/ws/record${_qs ? `?${_qs}` : ''}`;

    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        // Send start command with recording options
        if (apiMode) {
          ws.send(JSON.stringify({
            type: 'start_api_record',
            url: startUrl,
          }));
        } else {
          ws.send(JSON.stringify({
            type: 'start',
            url: startUrl,
            options: {
              record_wait_steps: recordWaitSteps,
            }
          }));
        }
      };

      ws.binaryType = 'arraybuffer';

      ws.onmessage = (event) => {
        // Binary frames = screencast (raw JPEG) — fast path, no JSON overhead
        if (event.data instanceof ArrayBuffer) {
          const buf = event.data as ArrayBuffer;
          if (buf.byteLength < 4) return;
          const dv = new DataView(buf);
          const urlLen = dv.getUint32(0);
          if (urlLen > 0 && urlLen < 2048) {
            const urlBytes = new Uint8Array(buf, 4, urlLen);
            const frameUrl = new TextDecoder().decode(urlBytes);
            if (frameUrl && frameUrl !== lastFrameUrlRef.current) {
              lastFrameUrlRef.current = frameUrl;
              setCurrentUrl(frameUrl);
            }
          }
          const jpegData = buf.slice(4 + urlLen);
          const cvs = canvasRef.current;
          if (cvs && typeof createImageBitmap !== 'undefined') {
            const canvasEl = cvs;
            const blob = new Blob([jpegData], { type: 'image/jpeg' });
            createImageBitmap(blob).then(function(bitmap) {
              const ctx = canvasEl.getContext('2d');
              if (!ctx) return;
              if (canvasEl.width !== bitmap.width || canvasEl.height !== bitmap.height) {
                canvasEl.width = bitmap.width;
                canvasEl.height = bitmap.height;
              }
              // Track the real frame size so selection clicks/drags map into the agent's actual
              // viewport (1280x800 Python vs 1920x1080 Rust), not the displayed canvas size.
              if (bitmap.width > 0 && bitmap.height > 0) {
                frameSizeRef.current = { w: bitmap.width, h: bitmap.height };
              }
              ctx.drawImage(bitmap, 0, 0);
              bitmap.close();
            }).catch(function() {});
          }
          return;
        }

        // Text frames = JSON messages (steps, events, etc.)
        const data = JSON.parse(event.data);

        // Monitor "check target" picker responses (element_info / elements_in_region) are consumed
        // here — they're transient picks, NOT recorded steps, so they never reach the switch below.
        // consumeMessage only matches those two frame types (only sent in selection mode), so it's a
        // safe no-op during normal recording; its identity is stable so this closure stays correct.
        if (selection.consumeMessage(data)) {
          return;
        }

        switch (data.type) {
          case 'started':
            setConnectionState('recording');
            setSessionId(data.sessionId);
            setCurrentUrl(data.url);
            if (reconnectAttemptsRef.current > 0) {
              toast.success(t('Recorder reconnected!'));
            } else {
              toast.success(t('Recording started!'));
            }
            wasRecordingRef.current = true;
            reconnectAttemptsRef.current = 0;
            resetIdleTimer();
            break;

          case 'screenshot':
            if (data.url) setCurrentUrl(data.url);
            setScreenshot('data:image/jpeg;base64,' + data.data);
            break;

          case 'step_recorded':
            setSteps(prev => [...prev, data.step]);
            resetIdleTimer();
            // Auto-expand steps panel on first recorded step
            if (!stepsAutoExpandedRef.current) {
              stepsAutoExpandedRef.current = true;
              setShowSteps(true);
            }
            // Extract credentials from fill steps with is_sensitive
            if (data.step?.type === 'fill' && data.step?.options?.is_sensitive) {
              const fieldName = data.step.options.field_name || data.step.options.field_type || 'password';
              setDetectedCredentials(prev => {
                const existing = prev.find(c => c.field_name === fieldName);
                if (existing) {
                  return prev.map(c => c.field_name === fieldName ? { ...c, value: data.step.value } : c);
                }
                return [...prev, {
                  field_name: fieldName,
                  field_type: data.step.options.field_type || 'password',
                  selector: data.step.selector,
                  value: data.step.value,
                }];
              });
              toast.success(t('Credential detected: {{name}}', { name: fieldName }), { duration: 2000 });
            }
            break;

          case 'twofa_detected':
            // The recorder spotted a one-time-code field and emitted a `twofa`
            // step (the literal code is never recorded). Flag it so we can offer
            // to set up a persona that solves 2FA automatically on future runs.
            setDetected2fa(true);
            if (data.channel_hint && data.channel_hint !== 'unknown') {
              setDetected2faChannel(data.channel_hint);
            }
            if (!twofaToastShownRef.current) {
              twofaToastShownRef.current = true;
              toast.success(t('2FA step detected — set up automatic codes with a persona.'), { duration: 3500 });
            }
            break;

          case 'step_updated':
            // Update an existing step by ID (e.g., fill value changed)
            setSteps(prev => prev.map(step =>
              step.id === data.id ? { ...step, ...data.step } : step
            ));
            break;

          case 'form_data_captured':
            // Recorder sends actual typed values separately — collect them
            if (data.is_sensitive) {
              setDetectedCredentials(prev => {
                const existing = prev.find(c => c.field_name === data.key);
                if (existing) {
                  return prev.map(c => c.field_name === data.key ? { ...c, value: data.value } : c);
                }
                return [...prev, {
                  field_name: data.key,
                  field_type: data.field_type,
                  selector: data.selector,
                  value: data.value,
                }];
              });
              toast.success(t('Credential detected: {{name}}', { name: data.field_name || data.key }), { duration: 2000 });
            } else {
              setCapturedFormData(prev => ({ ...prev, [data.key]: data.value }));
            }
            break;

          case 'sensitive_field_detected':
            // Legacy: credential detected during recording
            setDetectedCredentials(prev => {
              const existing = prev.find(c => c.field_name === data.field_name);
              if (existing) {
                return prev.map(c =>
                  c.field_name === data.field_name ? { ...c, value: data.value } : c
                );
              }
              return [...prev, {
                field_name: data.field_name || data.field_type,
                field_type: data.field_type,
                selector: data.selector,
                value: data.value,
              }];
            });
            toast.success(t('Sensitive field detected: {{name}}', { name: data.field_name || data.field_type }), { duration: 2000 });
            break;

          case 'navigation':
            setCurrentUrl(data.url);
            break;

          case 'tab_list':
            setOpenTabs(data.tabs || []);
            break;

          case 'stopped': {
            setConnectionState('connected');
            // Merge: the recorder's authoritative UI steps + any api_call steps the
            // user click-added live (which the recorder doesn't know about), ordered
            // chronologically by timestamp.
            const recorderSteps: RecordedStep[] = data.steps || [];
            setSteps(prev => {
              const injected = prev.filter(s => s.type === 'api_call');
              if (injected.length === 0) return recorderSteps;
              return [...recorderSteps, ...injected].sort(
                (a, b) => (a.timestamp || 0) - (b.timestamp || 0)
              );
            });
            // Surface any captured calls that only arrived in the stop payload.
            if (Array.isArray(data.network_calls) && data.network_calls.length > 0) {
              setDetectedRequests(prev => {
                const seen = new Set(prev.map(r => `${r.method} ${r.url}`));
                const additions: DetectedRequest[] = data.network_calls
                  .filter((c: any) => c && c.url && !seen.has(`${c.method} ${c.url}`))
                  .map((c: any, i: number) => ({
                    id: `det_stop_${Date.now()}_${i}`,
                    method: c.method,
                    url: c.url,
                    request_headers: c.request_headers,
                    request_body: c.request_body,
                    request_content_type: c.request_content_type,
                    response_status: c.response_status,
                    response_headers: c.response_headers,
                    response_body: c.response_body,
                    response_content_type: c.response_content_type,
                  }));
                return additions.length ? [...prev, ...additions] : prev;
              });
            }
            toast.success(t('Recording stopped. {{n}} steps recorded.', { n: data.stepCount }));
            break;
          }

          // ── Visual replay ("play to here") progress ──────────────────
          case 'replay_progress': {
            if (data.request_id && data.request_id !== replayReqRef.current) break;
            const idx = data.index as number;
            setReplayState(prev => ({
              ...prev,
              current: data.status === 'running' ? idx : (prev.current === idx ? null : prev.current),
              statuses: { ...prev.statuses, [idx]: data.status as ReplayStatus },
            }));
            break;
          }

          case 'replay_done': {
            if (data.request_id && data.request_id !== replayReqRef.current) break;
            replayReqRef.current = null;
            setReplayState(prev => ({ ...prev, running: false, current: null }));
            const landed = (typeof data.stopped_at === 'number' ? data.stopped_at : 0) + 1;
            if (data.cancelled) {
              toast(t('Replay stopped'), { icon: '⏹' });
            } else if (data.failed > 0) {
              toast(t('Replayed to step {{n}} — {{f}} step(s) could not run', { n: landed, f: data.failed }), { icon: '↺' });
            } else {
              toast.success(t('Replayed to step {{n}}', { n: landed }));
            }
            break;
          }

          case 'replay_error':
            replayReqRef.current = null;
            setReplayState(prev => ({ ...prev, running: false, current: null }));
            toast.error(data.error || t('Replay failed'));
            break;

          case 'error':
            // Recorder errors are user-actionable — show the full detail.
            toast.error(data.message || data.error || t('Recorder error'));
            if (connectionState === 'connecting') {
              setConnectionState('error');
            }
            break;

          case 'pong':
            // Heartbeat response
            break;

          // API recording mode messages
          case 'api_record_started':
            setConnectionState('recording');
            setSessionId(data.sessionId);
            setCurrentUrl(data.url);
            setShowApiPanel(true);
            toast.success(t('API recording started!'));
            break;

          case 'api_request_captured':
            setCapturedApiRequests(prev => [...prev, data.request]);
            break;

          case 'api_response_captured':
            setCapturedApiRequests(prev =>
              prev.map(r => r.id === data.request_id ? { ...r, response: data.response } : r)
            );
            break;

          case 'api_record_stopped':
            setConnectionState('connected');
            toast.success(t('API recording stopped. {{n}} requests captured.', { n: data.request_count }));
            break;

          // Live API capture during NORMAL workflow recording — the recorder
          // streams each unique endpoint (full request/response) as it's hit.
          case 'api_captured': {
            const call = data.call;
            if (!call || !call.url) break;
            const key = `${call.method} ${call.url}`;
            setDetectedRequests(prev => {
              if (prev.some(r => `${r.method} ${r.url}` === key)) return prev;
              return [...prev, {
                id: `det_${Date.now()}_${prev.length}`,
                method: call.method,
                url: call.url,
                request_headers: call.request_headers,
                request_body: call.request_body,
                request_content_type: call.request_content_type,
                response_status: call.response_status,
                response_headers: call.response_headers,
                response_body: call.response_body,
                response_content_type: call.response_content_type,
              }];
            });
            break;
          }

          // The recorder couldn't find an API on a page (server-rendered HTML).
          case 'page_no_api': {
            if (!data.url) break;
            setServerRenderedNotices(prev =>
              prev.some(n => n.url === data.url)
                ? prev
                : [...prev, { url: data.url, message: data.message || t('This page appears server-rendered — no API to capture here.') }]
            );
            break;
          }

          case 'select_options':
            // Backend detected a native select click - show options overlay
            setSelectOverlay({
              show: true,
              selector: data.selector,
              options: data.options || [],
              position: data.position || { x: 0, y: 0, width: 200, height: 30, selectTop: 0 },
              name: data.name || '',
            });
            break;

          case 'native_picker':
            // Backend detected a native picker click (date, time, color, etc.)
            setPickerOverlay({
              show: true,
              pickerType: data.pickerType,
              selector: data.selector,
              currentValue: data.currentValue || '',
              position: data.position || { x: 0, y: 0, width: 200, height: 30 },
              min: data.min,
              max: data.max,
              step: data.step,
            });
            break;

          case 'element_info':
            // "Pick element from page" for a selector field: hand the selector to the
            // waiting field, exit pick mode, and let the step modal re-appear.
            if (pickApplyRef.current && data.selector) {
              pickApplyRef.current(data.selector);
              pickApplyRef.current = null;
              setPickActive(false);
              setIsExtracting(false);
              if (wsRef.current) {
                wsRef.current.send(JSON.stringify({ type: 'action', action: 'clear_highlight' }));
              }
              toast.success(t('Selector captured: {{selector}}', { selector: data.selector }));
              break;
            }
            // Extraction mode: user clicked an element, show confirmation
            if (data.selector) {
              setExtractElementInfo(data);
              setShowExtractPopover(true);
              setExtractOutputName(data.ariaLabel?.replace(/\s+/g, '_').substring(0, 30) || 'extracted_data');
            }
            break;

          case 'highlight':
            // Extraction mode: hover highlight
            setExtractHighlight(data.rect ? data : null);
            break;
        }
      };

      ws.onerror = (error) => {
        // Ignore errors from a socket already superseded by a newer connect().
        if (wsRef.current !== ws) return;
        console.error('WebSocket error:', error);
      };

      ws.onclose = (event) => {
        // A previous connection's close fires AFTER the new socket is assigned
        // to wsRef.current. Without this guard it would null the live socket's
        // ref (and drive reconnect/state for the wrong socket) — leaving
        // state='recording' but wsRef.current=null, so every send silently
        // early-returns.
        if (wsRef.current !== ws) return;
        wsRef.current = null;

        // Plan/capacity refusals from the gateway (pre-recording). Surface the
        // blocking gate instead of a silent close. 4009 = cloud quota exhausted
        // /disabled; 4003 = no recorder available right now.
        if (!wasRecordingRef.current && (event.code === 4009 || event.code === 4003)) {
          // Paid (unlimited cloud) + no agent free => auto-wait for capacity.
          // Everyone else => connect a local agent (free quota is spent / the
          // user is on a bring-your-own-agent plan).
          const waitForCloud = event.code === 4003 && isPaidCloudRef.current;
          setGateKind(waitForCloud ? 'waiting_cloud' : 'connect_local');
          setConnectionState('needs_agent');
          return;
        }

        // If we were recording, try to reconnect automatically
        if (wasRecordingRef.current && reconnectAttemptsRef.current < MAX_RECONNECT_ATTEMPTS) {
          const delay = Math.min(1000 * Math.pow(1.5, reconnectAttemptsRef.current), 10000);
          reconnectAttemptsRef.current += 1;
          setConnectionState('connecting');
          toast(t('Recorder disconnected — reconnecting ({{n}}/{{max}})...', { n: reconnectAttemptsRef.current, max: MAX_RECONNECT_ATTEMPTS }), { icon: '🔄' });

          reconnectTimerRef.current = setTimeout(() => {
            connect();
          }, delay);
        } else if (wasRecordingRef.current) {
          setConnectionState('error');
          toast.error(t('Recorder connection lost. Please try again.'));
          wasRecordingRef.current = false;
          reconnectAttemptsRef.current = 0;
        } else {
          setConnectionState('disconnected');
        }
      };

    } catch (error) {
      console.error('Failed to connect:', error);
      setConnectionState('error');
      toast.error(t('Failed to connect to recorder'));
    }
  }, [url, connectionState]);

  // Stop recording
  const stopRecording = useCallback(() => {
    wasRecordingRef.current = false;
    reconnectAttemptsRef.current = 0;
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: apiMode ? 'stop_api_record' : 'stop' }));
    }
  }, [apiMode]);

  // Auto-connect when opened with a URL — no manual button needed
  const autoConnectFailsRef = useRef(0);
  useEffect(() => {
    if (!isOpen) {
      autoConnectTriggeredRef.current = false;
      autoConnectFailsRef.current = 0;
      return;
    }
    if (connectionState !== 'disconnected') {
      if (connectionState === 'recording' || connectionState === 'connected') {
        autoConnectFailsRef.current = 0; // reset on success
      }
      return;
    }
    // Stop retrying after 3 failures — user can refresh to retry
    if (autoConnectFailsRef.current >= 3) return;

    const targetUrl = initialUrl || url;
    if (!targetUrl || targetUrl === 'https://' || targetUrl.length < 10) return;

    // Plan-aware pre-flight gate: don't even open a recording WS we know will be
    // refused. Wait for the capability probe to load (fail open if it errors),
    // then if we can't record (free plan, no cloud quota, no local agent online)
    // surface the blocking ConnectAgentPanel instead of a silent WS failure.
    if (capabilityLoading && !capability) return; // still probing
    if (capability && !canAttempt) {
      setGateKind('connect_local');
      setConnectionState('needs_agent');
      return;
    }

    autoConnectFailsRef.current += 1;
    const delay = Math.min(1000 * Math.pow(2, autoConnectFailsRef.current - 1), 8000);
    const timer = setTimeout(() => {
      connect();
    }, delay);
    return () => clearTimeout(timer);
  }, [isOpen, connectionState, initialUrl, url, connect, capability, canAttempt, capabilityLoading]);

  // Gate resolution: while blocked on 'needs_agent', watch for the condition
  // that unblocks us. 'connect_local' clears the moment a local agent comes
  // online (or cloud frees up); 'waiting_cloud' just retries on a timer until an
  // infra agent is free. Either way recording then starts automatically.
  useEffect(() => {
    if (connectionState !== 'needs_agent') return;
    if (gateKind === 'connect_local' && canAttempt) {
      autoConnectFailsRef.current = 0;
      setGateKind(null);
      setConnectionState('disconnected'); // re-triggers the auto-connect effect
      return;
    }
    if (gateKind === 'waiting_cloud') {
      const timer = setTimeout(() => {
        autoConnectFailsRef.current = 0;
        setGateKind(null);
        setConnectionState('disconnected'); // retry — another infra agent may be free
      }, 6000);
      return () => clearTimeout(timer);
    }
  }, [connectionState, gateKind, canAttempt]);

  // Handle select option from overlay
  const handleSelectOption = useCallback((option: { value: string; text: string; index: number }) => {
    if (!wsRef.current || !selectOverlay) return;

    wsRef.current.send(JSON.stringify({
      type: 'action',
      action: 'select_option',
      selector: selectOverlay.selector,
      value: option.value,
      text: option.text,
      index: option.index,
    }));

    // Close the overlay
    setSelectOverlay(null);
  }, [selectOverlay]);

  // Close select overlay without selecting
  const closeSelectOverlay = useCallback(() => {
    setSelectOverlay(null);
  }, []);

  // Handle picker value submission
  const handlePickerValue = useCallback((value: string) => {
    if (!wsRef.current || !pickerOverlay) return;

    wsRef.current.send(JSON.stringify({
      type: 'action',
      action: 'set_picker_value',
      selector: pickerOverlay.selector,
      value: value,
      pickerType: pickerOverlay.pickerType,
    }));

    // Close the overlay
    setPickerOverlay(null);
  }, [pickerOverlay]);

  // Close picker overlay without selecting
  const closePickerOverlay = useCallback(() => {
    setPickerOverlay(null);
  }, []);

  // Handle canvas click - send to browser
  const handleCanvasClick = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    if (connectionState !== 'recording' || !wsRef.current) return;

    const canvas = canvasRef.current;
    if (!canvas) return;

    const rect = canvas.getBoundingClientRect();
    // Scale into the agent's ACTUAL viewport (1280x800 Python vs 1920x1080 Rust),
    // tracked from the live frame — NOT a hardcoded 1280x800, which lands clicks at
    // the wrong spot on a 1920-wide stream. Defaults to 1280x800 until the first frame.
    const scaleX = frameSizeRef.current.w / rect.width;
    const scaleY = frameSizeRef.current.h / rect.height;

    const x = Math.round((e.clientX - rect.left) * scaleX);
    const y = Math.round((e.clientY - rect.top) * scaleY);

    if (isExtracting) {
      // Extraction mode: get element info instead of clicking
      wsRef.current.send(JSON.stringify({
        type: 'action',
        action: 'get_element_info',
        x,
        y,
      }));
      return;
    }

    wsRef.current.send(JSON.stringify({
      type: 'action',
      action: 'click',
      x,
      y,
    }));
  }, [connectionState, isExtracting]);

  // Handle canvas mouse move: extraction hover highlighting, or — during normal
  // recording — the live cursor trajectory.
  const handleCanvasMouseMove = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    if (connectionState !== 'recording' || !wsRef.current) return;

    const canvas = canvasRef.current;
    if (!canvas) return;

    const rect = canvas.getBoundingClientRect();
    // Scale into the agent's ACTUAL viewport (1280x800 Python vs 1920x1080 Rust),
    // tracked from the live frame — NOT a hardcoded 1280x800, which lands clicks at
    // the wrong spot on a 1920-wide stream. Defaults to 1280x800 until the first frame.
    const scaleX = frameSizeRef.current.w / rect.width;
    const scaleY = frameSizeRef.current.h / rect.height;

    const x = Math.round((e.clientX - rect.left) * scaleX);
    const y = Math.round((e.clientY - rect.top) * scaleY);

    if (isExtracting) {
      // Extraction mode: highlight the element under the cursor (100ms throttle).
      const now = Date.now();
      if ((window as any).__lastHighlight && now - (window as any).__lastHighlight < 100) return;
      (window as any).__lastHighlight = now;
      wsRef.current.send(JSON.stringify({ type: 'action', action: 'highlight_element', x, y }));
      return;
    }

    // Normal recording: stream the cursor trajectory (throttled ~50ms). The agent
    // moves the REAL cursor for each sample, which is the only thing that makes
    // hover-only UI — dropdown menus, tooltips, "show on hover" buttons — open in
    // the live page; it also buffers a downsampled path the recorder attaches to
    // the next click (human-behavior layer). Without this the canvas was inert
    // unless you were extracting, so hover-gated elements could never be reached.
    // Silently ignored by an agent that doesn't implement `mousemove`.
    const now = Date.now();
    if (lastMouseMoveRef.current && now - lastMouseMoveRef.current < 50) return;
    lastMouseMoveRef.current = now;
    wsRef.current.send(JSON.stringify({ type: 'action', action: 'mousemove', x, y }));
  }, [isExtracting, connectionState]);

  // Confirm extraction step
  const confirmExtractStep = useCallback(() => {
    if (!extractElementInfo || !wsRef.current) return;

    wsRef.current.send(JSON.stringify({
      type: 'action',
      action: 'add_extract_step',
      selector: extractElementInfo.selector,
      output_name: extractOutputName || 'extracted_data',
      extract_type: extractType,
      description: `Extract ${extractType} from ${extractElementInfo.selector.substring(0, 40)}`,
    }));

    setShowExtractPopover(false);
    setExtractElementInfo(null);
    setExtractOutputName('');
    toast.success(t('Extraction step added'));
  }, [extractElementInfo, extractOutputName, extractType]);

  // Handle keyboard input
  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (connectionState !== 'recording' || !wsRef.current) return;

    // Prevent default for most keys when recording
    if (!e.metaKey && !e.ctrlKey) {
      e.preventDefault();
    }

    const specialKeys = ['Enter', 'Tab', 'Escape', 'Backspace', 'Delete', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'];

    if (specialKeys.includes(e.key)) {
      wsRef.current.send(JSON.stringify({
        type: 'action',
        action: 'press',
        key: e.key,
      }));
    } else if (e.key.length === 1) {
      wsRef.current.send(JSON.stringify({
        type: 'action',
        action: 'type',
        text: e.key,
      }));
    }
  }, [connectionState]);

  // Handle scroll - include mouse position to detect container scrolls (textarea, etc.)
  const handleWheel = useCallback((e: React.WheelEvent) => {
    if (connectionState !== 'recording' || !wsRef.current) return;

    const canvas = canvasRef.current;
    if (!canvas) return;

    // Calculate mouse position in viewport coordinates (same as click handler)
    const rect = canvas.getBoundingClientRect();
    // Scale into the agent's ACTUAL viewport (1280x800 Python vs 1920x1080 Rust),
    // tracked from the live frame — NOT a hardcoded 1280x800, which lands clicks at
    // the wrong spot on a 1920-wide stream. Defaults to 1280x800 until the first frame.
    const scaleX = frameSizeRef.current.w / rect.width;
    const scaleY = frameSizeRef.current.h / rect.height;
    const x = Math.round((e.clientX - rect.left) * scaleX);
    const y = Math.round((e.clientY - rect.top) * scaleY);

    wsRef.current.send(JSON.stringify({
      type: 'action',
      action: 'scroll',
      deltaX: e.deltaX,
      deltaY: e.deltaY,
      x,
      y,
    }));
  }, [connectionState]);

  // Draw screenshot to canvas
  useEffect(() => {
    if (screenshot && canvasRef.current) {
      const canvas = canvasRef.current;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;

      if (!imageRef.current) {
        imageRef.current = new Image();
      }

      imageRef.current.onload = () => {
        canvas.width = imageRef.current!.width;
        canvas.height = imageRef.current!.height;
        ctx.drawImage(imageRef.current!, 0, 0);
      };

      imageRef.current.src = screenshot;
    }
  }, [screenshot]);

  // Capture canvas screenshot as base64 (no data: prefix)
  const getCanvasScreenshot = useCallback((): string | null => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    try {
      const dataUrl = canvas.toDataURL('image/jpeg', 0.7);
      return dataUrl.replace(/^data:image\/jpeg;base64,/, '');
    } catch {
      return null;
    }
  }, []);

  // AI Chat: send message to backend
  // sendAIChatMessage (the unified agent loop) is defined later, after the
  // wsAgentAction / sampleForModel helpers it depends on.

  // AI Chat: apply a suggested action to the recorder
  const applySingleAction = useCallback((action: any, ws: WebSocket) => {
    const actionType = action.type;
    if (actionType === 'click' && action.selector) {
      ws.send(JSON.stringify({ type: 'action', action: 'click', selector: action.selector }));
    } else if (actionType === 'fill' && action.selector) {
      ws.send(JSON.stringify({ type: 'action', action: 'type', selector: action.selector, text: action.value || '' }));
    } else if (actionType === 'navigate' && action.value) {
      ws.send(JSON.stringify({ type: 'action', action: 'navigate', url: action.value }));
    } else if (actionType === 'scroll') {
      ws.send(JSON.stringify({ type: 'action', action: 'scroll', deltaX: 0, deltaY: action.value === 'up' ? -300 : 300, x: 640, y: 400 }));
    } else if (actionType === 'press') {
      ws.send(JSON.stringify({ type: 'action', action: 'press', key: action.value || 'Enter' }));
    } else if (actionType === 'extract' && action.selector) {
      ws.send(JSON.stringify({
        type: 'action', action: 'add_extract_step',
        selector: action.selector,
        output_name: action.output_name || action.value || 'data',
        extract_type: action.extract_type || 'text',
        description: action.description || '',
      }));
    } else if (actionType === 'select' && action.selector) {
      ws.send(JSON.stringify({ type: 'action', action: 'select_option', selector: action.selector, value: action.value || '' }));
    } else if (actionType === 'wait') {
      return false; // skip
    } else {
      return false;
    }
    return true;
  }, []);

  const applyAIAction = useCallback((action: any) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      toast.error(t('Recorder not connected'));
      return;
    }
    const ws = wsRef.current;
    const actionType = action.type;

    if (actionType === 'batch' && Array.isArray(action.actions)) {
      // Send batch as a single message — recorder handles sequencing
      ws.send(JSON.stringify({
        type: 'action',
        action: 'batch',
        actions: action.actions.map((a: any) => ({
          action: a.type,
          selector: a.selector,
          text: a.value,
          value: a.value,
          key: a.value,
          url: a.value,
          output_name: a.output_name,
          extract_type: a.extract_type,
          description: a.description,
        })),
      }));
      toast.success(t('Batch: {{n}} actions applied', { n: action.actions.length }));
    } else {
      const ok = applySingleAction(action, ws);
      if (ok) {
        toast.success(t('Applied: {{action}}', { action: action.description || actionType }));
      } else {
        toast.error(t("Can't apply action type: {{type}}", { type: actionType }));
      }
    }
  }, [applySingleAction]);

  // AI Extract: generate extraction steps
  const generateAIExtractSteps = useCallback(async () => {
    if (!aiExtractGoal.trim() || aiExtractLoading) return;
    setAiExtractLoading(true);

    const screenshotB64 = getCanvasScreenshot();
    if (!screenshotB64) {
      toast.error(t('Could not capture screenshot'));
      setAiExtractLoading(false);
      return;
    }

    try {
      const { default: client } = await import('../api/client');
      const resp = await client.post('/ai-assist/generate-extract', {
        screenshot_b64: screenshotB64,
        page_url: currentUrl,
        goal: aiExtractGoal.trim(),
        steps,  // workflow context (navigation that led here)
      }, { timeout: 120000 });
      const data = resp.data;

      // Hold the generated script as a draft so the user can test it live
      // before committing it as a step (don't add or close the dialog yet).
      const step = data.steps?.[0];
      if (step?.config?.script) {
        setExtractDraft({ script: step.config.script, message: data.message || t('AI extraction script') });
        setExtractTestResult(null);
      } else {
        toast.error(t('AI did not return an extraction script — try rephrasing'));
      }

      if (data.credits_used) {
        toast(data.credits_used === 1 ? t('Used {{n}} credit', { n: data.credits_used }) : t('Used {{n}} credits', { n: data.credits_used }), { icon: '✨', duration: 2000 });
      }
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.response?.data?.error || err.message || t('AI request failed');
      toast.error(detail);
    } finally {
      setAiExtractLoading(false);
    }
  }, [aiExtractGoal, aiExtractLoading, currentUrl, getCanvasScreenshot, steps]);

  // Optimize workflow via AI
  const optimizeWorkflow = useCallback(async () => {
    if (optimizeLoading || steps.length === 0) return;
    setOptimizeLoading(true);
    setPendingOptimization(null);
    setOptimizeNote('');

    try {
      const { default: client } = await import('../api/client');
      const screenshotB64 = getCanvasScreenshot();
      const resp = await client.post('/ai-assist/optimize-workflow', {
        steps,
        screenshot_b64: screenshotB64 || undefined,
        page_url: currentUrl || undefined,
        // Give the optimizer the captured API calls so it can fold UI sequences
        // into a single api_call step, plus values to parameterize.
        network_calls: detectedRequests,
        form_data: capturedFormData,
        credential_keys: detectedCredentials.map(c => c.field_name),
      }, { timeout: 120000 });
      const data = resp.data;
      const changes: OptimizeChange[] = data.changes || [];
      const warnings: string[] = data.warnings || [];

      if (changes.length > 0) {
        // Hold for review — do NOT apply yet (never silently rewrite the flow).
        setPendingOptimization({
          steps: data.steps || steps,
          changes,
          warnings,
          removed_count: data.removed_count || 0,
        });
        toast.success(changes.length === 1 ? t('AI proposes {{n}} change — review below', { n: changes.length }) : t('AI proposes {{n}} changes — review below', { n: changes.length }));
      } else {
        setOptimizeNote(warnings[0] || t('Workflow looks good — no safe changes found.'));
      }
      if (data.credits_used) {
        toast(data.credits_used === 1 ? t('Used {{n}} credit', { n: data.credits_used }) : t('Used {{n}} credits', { n: data.credits_used }), { icon: '✨', duration: 2000 });
      }
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.response?.data?.error || err.message || t('Optimization failed');
      toast.error(detail);
    } finally {
      setOptimizeLoading(false);
    }
  }, [optimizeLoading, steps, currentUrl, getCanvasScreenshot, detectedRequests, capturedFormData, detectedCredentials]);

  const applyOptimization = useCallback(() => {
    if (!pendingOptimization) return;
    setSteps(pendingOptimization.steps);
    const n = pendingOptimization.removed_count;
    toast.success(
      n > 1 ? t('Applied AI optimization — {{n}} steps removed', { n })
        : n === 1 ? t('Applied AI optimization — {{n}} step removed', { n })
        : t('Applied AI optimization')
    );
    setOptimizeNote(t('Optimization applied.'));
    setPendingOptimization(null);
  }, [pendingOptimization]);

  const discardOptimization = useCallback(() => {
    setPendingOptimization(null);
    setOptimizeNote(t('Optimization discarded — workflow unchanged.'));
  }, []);

  // Run a JS script live in the recorder browser and resolve its return value.
  const wsEvalScript = useCallback((script: string, timeoutMs = 10000): Promise<any> => {
    return new Promise((resolve, reject) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        reject(new Error(t('Browser not connected — start recording to test')));
        return;
      }
      const timer = setTimeout(() => { ws.removeEventListener('message', h); reject(new Error(t('Timed out ({{n}}s)', { n: Math.round(timeoutMs / 1000) }))); }, timeoutMs);
      const h = (e: MessageEvent) => {
        try {
          const m = JSON.parse(e.data);
          if (m.type === 'eval_result') {
            clearTimeout(timer); ws.removeEventListener('message', h);
            if (m.error) reject(new Error(m.error)); else resolve(m.result);
          }
        } catch { /* ignore non-JSON frames */ }
      };
      ws.addEventListener('message', h);
      ws.send(JSON.stringify({ type: 'action', action: 'evaluate_js', script: wrapScriptForEval(script) }));
    });
  }, []);

  // AI-Extract draft: test the generated script before adding it as a step.
  const testExtractDraft = useCallback(() => {
    if (!extractDraft) return;
    setExtractTestResult({ loading: true });
    wsEvalScript(extractDraft.script)
      .then(r => setExtractTestResult({ ok: true, result: r }))
      .catch(e => setExtractTestResult({ error: e.message }));
  }, [extractDraft, wsEvalScript]);

  const addExtractDraft = useCallback(() => {
    if (!extractDraft) return;
    // AI chat returns a JS snippet (page.evaluate). Record it as an `evaluate`
    // step so the engine RUNS the script — not an `extract` step (CSS/text
    // selector), which would ignore the script and fall back to text_content.
    // Mirrors the scraper-builder path (applyScraperDraft).
    setSteps(prev => [...prev, {
      id: crypto.randomUUID(),
      type: 'evaluate',
      timestamp: Date.now(),
      value: 'extracted_data',
      description: extractDraft.message || t('AI extraction script'),
      config: { variable: 'extracted_data', script: extractDraft.script },
      options: {},
    }]);
    toast.success(t('Extraction step added'));
    setExtractDraft(null);
    setExtractTestResult(null);
    setShowAIExtract(false);
    setAiExtractGoal('');
  }, [extractDraft]);

  const discardExtractDraft = useCallback(() => {
    setExtractDraft(null);
    setExtractTestResult(null);
  }, []);

  // --- Agentic scraper builder ---

  // Send one or more ephemeral agent actions to the recorder and resolve with
  // {results, observation}. These execute real Playwright actions on the live
  // page but are NOT recorded as workflow steps (server-side suppression).
  const wsAgentAction = useCallback((actions: any[], timeoutMs = 90000): Promise<{ results: any[]; observation: any }> => {
    return new Promise((resolve, reject) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        reject(new Error(t('Browser not connected — start recording first')));
        return;
      }
      const requestId = crypto.randomUUID();
      const timer = setTimeout(() => { ws.removeEventListener('message', h); reject(new Error(t('Agent action timed out ({{n}}s)', { n: Math.round(timeoutMs / 1000) }))); }, timeoutMs);
      const h = (e: MessageEvent) => {
        try {
          const m = JSON.parse(e.data);
          if (m.type === 'agent_action_result' && m.request_id === requestId) {
            clearTimeout(timer); ws.removeEventListener('message', h);
            if (m.error) reject(new Error(m.error));
            else resolve({ results: m.results || [], observation: m.observation || {} });
          }
        } catch { /* ignore non-JSON frames */ }
      };
      ws.addEventListener('message', h);
      ws.send(JSON.stringify({ type: 'agent_action', request_id: requestId, actions }));
    });
  }, []);

  // Compact a (possibly large) value for sending back to the model: keep the
  // first couple of array items + note totals, cap strings, bound overall size.
  const sampleForModel = useCallback((value: any): any => {
    // Send the FULL result unless it's genuinely large — the model needs real
    // detail to verify its extraction. Only when the serialized value exceeds the
    // budget do we down-sample (and even then keep a generous head + long strings).
    const BUDGET = 24000; // chars
    try {
      const full = JSON.stringify(value);
      if (full != null && full.length <= BUDGET) return value; // small enough → send whole
    } catch { /* not serializable — fall through to sampling */ }
    const sample = (v: any): any => {
      if (Array.isArray(v)) {
        const head = v.slice(0, 10).map(sample);
        return v.length > 10 ? [...head, `…(${v.length} items total)`] : head;
      }
      if (v && typeof v === 'object') {
        const out: any = {};
        for (const k of Object.keys(v)) out[k] = sample(v[k]);
        return out;
      }
      if (typeof v === 'string' && v.length > 2000) return v.slice(0, 2000) + '…';
      return v;
    };
    try {
      return sample(value);
    } catch {
      return String(value).slice(0, BUDGET);
    }
  }, []);

  // The auto-loop: AI drives the browser to understand the page, tests candidate
  // extraction code on real data, then returns a finished scraper script.
  const runScraperBuilder = useCallback(async () => {
    const goal = aiExtractGoal.trim();
    if (!goal || scraperRunning) return;
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      toast.error(t('Start recording first — the AI needs a live browser to drive'));
      return;
    }
    setScraperRunning(true);
    setScraperDraft(null);
    setScraperTestResult(null);
    scraperStopRef.current = false;
    setScraperLog([{ kind: 'thought', text: t('Starting — the AI will drive the browser to understand the page and build your extractor…') }]);

    const { default: client } = await import('../api/client');
    const MAX = 14;
    const history: any[] = [];
    let observation: any = null;

    try {
      for (let i = 0; i < MAX; i++) {
        if (scraperStopRef.current) { setScraperLog(p => [...p, { kind: 'error', text: t('Stopped.') }]); break; }

        const screenshotB64 = getCanvasScreenshot();
        let data: any;
        try {
          const resp = await client.post('/ai-assist/build-scraper', {
            goal,
            page_url: currentUrl,
            screenshot_b64: screenshotB64 || undefined,
            iteration: i,
            max_iterations: MAX,
            observation,
            history: history.slice(-16),
            network_calls: detectedRequests,
          }, { timeout: 120000 });
          data = resp.data;
        } catch (err: any) {
          const detail = err?.response?.data?.detail || err?.response?.data?.error || err.message || t('request failed');
          setScraperLog(p => [...p, { kind: 'error', text: t('AI error: {{detail}}', { detail }) }]);
          break;
        }

        if (data.credits_used) toast(data.credits_used === 1 ? t('AI used {{n}} credit', { n: data.credits_used }) : t('AI used {{n}} credits', { n: data.credits_used }), { icon: '✨', duration: 1500 });
        if (data.thought) setScraperLog(p => [...p, { kind: 'thought', text: data.thought }]);

        if (data.action === 'done' && data.script) {
          setScraperDraft({ script: data.script, variable: data.variable || 'items', iframe: data.iframe || undefined, summary: data.summary || data.thought || '' });
          setScraperLog(p => [...p, { kind: 'done', text: data.summary || t('Extractor ready — review and test below.') }]);
          break;
        }

        const actions: any[] = Array.isArray(data.actions) ? data.actions : [];
        if (actions.length === 0) {
          // No actions and not done — just refresh observation and continue.
          try { const r = await wsAgentAction([], 30000); observation = r.observation; } catch { /* keep prior observation */ }
          history.push({ thought: data.thought, actions: [], results: [] });
          if (i === MAX - 1) setScraperLog(p => [...p, { kind: 'error', text: t('Reached the iteration limit without a finished script. Refine the goal and retry.') }]);
          continue;
        }

        setScraperLog(p => [...p, { kind: 'run', text: actions.map(a => a.action + (a.selector ? ` ${a.selector}` : a.url ? ` ${a.url}` : '')).join(', ').slice(0, 200) }]);

        try {
          const { results, observation: obs } = await wsAgentAction(actions, 90000);
          observation = obs;
          const sampledResults = results.map((r: any) => (r && 'eval_result' in r ? { ...r, eval_result: sampleForModel(r.eval_result) } : r));
          history.push({ thought: data.thought, actions, results: sampledResults });
          const preview = sampledResults.map((r: any) => r?.error ? `✗ ${r.action}: ${r.error}` : (r?.eval_result !== undefined ? `${r.action} → ${JSON.stringify(r.eval_result).slice(0, 200)}` : `✓ ${r?.action}`)).join('\n');
          setScraperLog(p => [...p, { kind: 'result', text: preview.slice(0, 600) }]);
        } catch (err: any) {
          history.push({ thought: data.thought, actions, results: [{ error: String(err?.message || err).slice(0, 300) }] });
          setScraperLog(p => [...p, { kind: 'error', text: String(err?.message || err).slice(0, 300) }]);
        }

        if (i === MAX - 1) setScraperLog(p => [...p, { kind: 'error', text: t('Reached the iteration limit without a finished script. Refine the goal and retry.') }]);
      }
    } finally {
      setScraperRunning(false);
    }
  }, [aiExtractGoal, scraperRunning, currentUrl, getCanvasScreenshot, detectedRequests, wsAgentAction, sampleForModel]);

  const stopScraperBuilder = useCallback(() => { scraperStopRef.current = true; }, []);

  // Test the finished scraper live (full run — allow a long timeout since it may paginate).
  const testScraperDraft = useCallback(() => {
    if (!scraperDraft) return;
    setScraperTestResult({ loading: true });
    wsEvalScript(scraperDraft.script, 180000)
      .then(r => setScraperTestResult({ ok: true, result: r }))
      .catch(e => setScraperTestResult({ error: e.message }));
  }, [scraperDraft, wsEvalScript]);

  const applyScraperDraft = useCallback(() => {
    if (!scraperDraft) return;
    const config: Record<string, any> = { variable: scraperDraft.variable || 'items', script: scraperDraft.script };
    if (scraperDraft.iframe) config.iframe = scraperDraft.iframe;
    setSteps(prev => [...prev, {
      id: crypto.randomUUID(),
      type: 'evaluate',
      timestamp: Date.now(),
      description: scraperDraft.summary || t('AI extraction script'),
      value: scraperDraft.variable || 'items',
      config,
      options: {},
    }]);
    toast.success(t('Extraction step added'));
    setScraperDraft(null);
    setScraperTestResult(null);
    setScraperLog([]);
    setShowAIExtract(false);
    setAiExtractGoal('');
  }, [scraperDraft]);

  const discardScraperDraft = useCallback(() => {
    setScraperDraft(null);
    setScraperTestResult(null);
    setScraperLog([]);
  }, []);

  // The recorder mode the AI agent should reason in (tunes its system prompt + output).
  const recorderMode = apiMode ? 'api' : streamingMode ? 'streaming' : 'manual';

  // Execute one batch of ephemeral browser actions and fold the result into the
  // resumable agent context (history + latest observation).
  const executeAgentActions = useCallback(async (thought: string, actions: any[]) => {
    const ctx = agentCtxRef.current;
    if (!ctx) return;
    if (actions.length === 0) {
      try { const r = await wsAgentAction([], 30000); ctx.observation = r.observation; } catch { /* keep prior */ }
      ctx.history.push({ thought, actions: [], results: [] });
      return;
    }
    setAiChatMessages(prev => [...prev, { role: 'assistant', content: actions.map((a: any) => a.action + (a.selector ? ` ${a.selector}` : a.url ? ` ${a.url}` : '')).join(', ').slice(0, 160), kind: 'run' }]);
    try {
      const { results, observation } = await wsAgentAction(actions, 90000);
      ctx.observation = observation;
      const sampled = results.map((r: any) => {
        let out = (r && 'eval_result' in r) ? { ...r, eval_result: sampleForModel(r.eval_result) } : r;
        // get_screenshot: stash the image for the NEXT turn's screenshot_b64 and
        // strip the base64 out of the text history (it would bloat the prompt).
        if (out && typeof out === 'object' && out.screenshot_b64) {
          pendingScreenshotRef.current = out.screenshot_b64;
          const { screenshot_b64, ...rest } = out;
          out = { ...rest, screenshot_captured: true };
        }
        return out;
      });
      ctx.history.push({ thought, actions, results: sampled });
    } catch (err: any) {
      ctx.history.push({ thought, actions, results: [{ error: String(err?.message || err).slice(0, 300) }] });
      setAiChatMessages(prev => [...prev, { role: 'assistant', content: String(err?.message || err).slice(0, 200), kind: 'result' }]);
    }
  }, [wsAgentAction, sampleForModel]);

  // Drive the agent loop until it answers, finishes, errors, or (in assist mode)
  // needs the user to approve a browser batch. Re-entrant: approve/skip resume it.
  const runAgentLoop = useCallback(async () => {
    const ctx = agentCtxRef.current;
    if (!ctx) return;
    setAiChatLoading(true);
    const connected = !!wsRef.current && wsRef.current.readyState === WebSocket.OPEN;
    const { default: client } = await import('../api/client');

    while (ctx.iteration < ctx.max) {
      if (chatStopRef.current) { setAiChatMessages(prev => [...prev, { role: 'assistant', content: t('Stopped.') }]); break; }
      // Only attach a screenshot when the AI explicitly requested one last turn
      // via get_screenshot — we no longer push an image every turn.
      const screenshotB64 = pendingScreenshotRef.current;
      pendingScreenshotRef.current = null;

      let data: any;
      const ac = new AbortController();
      chatAbortRef.current = ac;
      try {
        const resp = await client.post('/ai-assist/agent', {
          instruction: ctx.instruction,
          mode: recorderMode,
          conversation: aiChatMessages.slice(-10).map(m => ({ role: m.role, content: m.content })),
          page_url: currentUrl,
          screenshot_b64: screenshotB64 || undefined,
          observation: ctx.observation,
          steps,
          network_calls: detectedRequests,
          // Streaming mode: let the agent SEE the current advanced script so it can
          // edit it (script_mode:"replace") instead of only appending.
          advanced_script: streamingAdvancedScript,
          advanced_enabled: streamingAdvancedEnabled,
          iteration: ctx.iteration,
          max_iterations: ctx.max,
          history: ctx.history.slice(-16),
          conversation_id: aiChatConvId,
        }, { timeout: 120000, signal: ac.signal });
        data = resp.data;
      } catch (err: any) {
        // Stop pressed → request aborted: end quietly (the top-of-loop check or
        // stopAIChat already shows "Stopped.").
        if (chatStopRef.current || err?.code === 'ERR_CANCELED' || err?.name === 'CanceledError' || err?.name === 'AbortError') {
          break;
        }
        const status = err?.response?.status;
        const detail = err?.response?.data?.detail || err?.response?.data?.error || err.message || t('AI request failed');
        let errorMsg = t('Error: {{detail}}', { detail });
        if (status === 402) errorMsg = t('Insufficient funds.');
        else if (status === 429) errorMsg = t('Rate limited — wait a moment.');
        else if (status === 502 || status === 503) errorMsg = t('AI service unavailable.');
        else if (!err.response) errorMsg = t('Could not reach server.');
        setAiChatMessages(prev => [...prev, { role: 'assistant', content: errorMsg }]);
        break;
      } finally {
        chatAbortRef.current = null;
      }
      if (chatStopRef.current) { setAiChatMessages(prev => [...prev, { role: 'assistant', content: t('Stopped.') }]); break; }
      ctx.iteration += 1;
      if (data.credits_used) toast(data.credits_used === 1 ? t('AI used {{n}} credit', { n: data.credits_used }) : t('AI used {{n}} credits', { n: data.credits_used }), { icon: '✨', duration: 1500 });

      if (data.action === 'retry') {
        // The backend couldn't parse the model's JSON. Feed the exact error back
        // into the loop so the model self-corrects on its next turn (bounded by
        // ctx.max) — instead of dead-ending or dumping the raw blob in chat.
        ctx.history.push({
          thought: '(your previous reply was rejected)',
          actions: [],
          results: [{ success: false, system: 'json_parse_error', error: data.message || 'Your previous reply was not valid JSON. Reply with ONLY a valid JSON object.' }],
        });
        setAiChatMessages(prev => [...prev, { role: 'assistant', content: t('Fixing an invalid response…'), kind: 'thought' }]);
        continue;
      }

      if (data.action === 'ask') {
        setAiChatMessages(prev => [...prev, { role: 'assistant', content: data.message || data.thought || t('(no response)') }]);
        break;
      }

      if (data.action === 'done') {
        const summary = data.summary || data.message || data.thought || t('Done.');
        const stepsRaw: any[] = Array.isArray(data.steps_to_add) ? data.steps_to_add : [];

        // ── The AI must not declare an extraction "done" on faith. If it produced
        // an extract/evaluate step, run its script ONCE to confirm it returns real
        // data before we even present it. On a malformed step (no script), an
        // error, or empty data → push the failure back into the loop so the AI
        // fixes it (we do NOT present it). A pure timeout is inconclusive, so we
        // warn and let the user judge. We NEVER auto-apply: a verified step is only
        // PREVIEWED below for the user to confirm/Apply.
        const extractionStep = stepsRaw.find((s: any) => ['extract', 'evaluate'].includes(s?.type));
        const verifyScript: string = extractionStep?.config?.script || '';

        // Premature done: the AI declared "done" (with a page-derived artifact)
        // on the very first turn, without running a SINGLE action to inspect or
        // test the page — so any "verified" claim in its thought is unfounded.
        // Push back and require it to actually explore/verify with run_actions first.
        const ranSomething = ctx.history.some((h: any) => Array.isArray(h?.actions) && h.actions.some((a: any) => a?.action && a.action !== 'verify'));
        const alreadyNudged = ctx.history.some((h: any) => Array.isArray(h?.actions) && h.actions.some((a: any) => a?.purpose === 'premature done'));
        if (!ranSomething && !alreadyNudged && (extractionStep || (recorderMode === 'streaming' && data.script))) {
          ctx.history.push({ thought: data.thought || '', actions: [{ action: 'verify', purpose: 'premature done' }],
            results: [{ success: false, verification: 'REJECTED: you returned "done" without running ANY actions to inspect or test the page — you have not actually verified anything yet. Use run_actions with evaluate_js to confirm your extraction returns real data (and check for pagination / dynamic loading) BEFORE returning done.' }] });
          setAiChatMessages(prev => [...prev, { role: 'assistant', content: t('The AI tried to finish without testing the page — asking it to verify first.'), kind: 'result' }]);
          continue;
        }

        if (extractionStep && !verifyScript) {
          ctx.history.push({ thought: data.thought || '', actions: [{ action: 'verify', purpose: 'validate done' }],
            results: [{ success: false, verification: 'REJECTED: your extraction step has no runnable config.script. Provide the script and verify it returns data with evaluate_js before returning done.' }] });
          setAiChatMessages(prev => [...prev, { role: 'assistant', content: t('That step has no runnable script — asking the AI to provide and verify one.'), kind: 'result' }]);
          continue;
        }

        if (verifyScript && connected) {
          const looksEmpty = (v: any): boolean => {
            if (v == null) return true;
            if (Array.isArray(v)) return v.length === 0;
            if (typeof v === 'object') {
              if (typeof v.total === 'number') return v.total === 0;
              const arrs = Object.values(v).filter(x => Array.isArray(x)) as any[][];
              if (arrs.length) return arrs.every(a => a.length === 0);
              return Object.keys(v).length === 0;
            }
            if (typeof v === 'string') return v.trim() === '';
            return false;
          };
          const countOf = (v: any): number => Array.isArray(v) ? v.length
            : (v && typeof v === 'object'
                ? (typeof v.total === 'number' ? v.total : ((Object.values(v).find(x => Array.isArray(x)) as any[] | undefined)?.length ?? 1))
                : (v == null ? 0 : 1));

          setAiChatMessages(prev => [...prev, { role: 'assistant', content: t('Verifying the extraction on the live page…'), kind: 'thought' }]);
          let evalRes: any, evalErr: string | null = null, timedOut = false;
          try {
            // allow_network: this is the user-initiated VERIFY of a proposed
            // extraction script (not the brain's free-form probing), so it may
            // read the site's own JSON API via fetch/XHR. The autonomous loop
            // actions above never set this, so autonomous network stays blocked.
            const { results } = await wsAgentAction([{ action: 'evaluate_js', script: verifyScript, allow_network: true }], 90000);
            const r: any = results?.[0];
            if (r?.error) evalErr = String(r.error); else evalRes = r?.eval_result;
          } catch (e: any) {
            const m = String(e?.message || e);
            if (/timed out/i.test(m)) timedOut = true; else evalErr = m;
          }

          if (!timedOut && (evalErr || looksEmpty(evalRes))) {
            const why = evalErr ? `failed: ${evalErr}` : 'returned no data (0 items)';
            const whyUser = evalErr ? t('failed: {{error}}', { error: evalErr }) : t('returned no data (0 items)');
            // Feed the AI ONLY success/failure + the reason — never the extracted
            // data. The verify may read the site's API (allow_network), so the
            // result can hold sensitive content; the model must fix the script
            // from the failure signal alone, not by seeing the data.
            ctx.history.push({ thought: data.thought || '', actions: [{ action: 'evaluate_js', purpose: 'verify done script' }],
              results: [{ action: 'evaluate_js', success: false, error: evalErr || undefined,
                verification: `REJECTED: your "done" extraction ${why}. (The result data is withheld for privacy — fix from selectors/page structure, not by inspecting the data.) Do NOT return done until evaluate_js confirms it returns real data.` }] });
            setAiChatMessages(prev => [...prev, { role: 'assistant', content: t('Extraction {{why}} — asking the AI to fix it before finalizing.', { why: whyUser }), kind: 'result' }]);
            continue;
          }
          setAiChatMessages(prev => [...prev, { role: 'assistant', content: timedOut
            ? t('⚠️ Could not verify within 90s (script may be slow) — review carefully before applying.')
            : t('Verified ✓ returned {{n}} item(s). Review and Apply when ready.', { n: countOf(evalRes) }), kind: 'result' }]);
        }

        // Verified (or nothing to verify) → PREVIEW for the user to confirm. Never auto-applied.
        if (data.message && data.message !== summary) setAiChatMessages(prev => [...prev, { role: 'assistant', content: data.message }]);
        const proposedSteps: RecordedStep[] = stepsRaw.map((s: any) => ({
          id: crypto.randomUUID(),
          type: s.type || 'evaluate',
          timestamp: Date.now(),
          description: s.description || summary,
          selector: s.selector,
          url: s.url || s.config?.url,
          value: s.value ?? s.config?.variable,
          config: s.config || {},
          options: s.options || {},
        }));
        // Edits to EXISTING steps (update/delete/move) and, for streaming, whether the
        // returned script appends to or replaces the current advanced script.
        const proposedEdits: StepEdit[] = Array.isArray(data.step_edits) ? data.step_edits : [];
        const scriptMode: 'append' | 'replace' = data.script_mode === 'replace' ? 'replace' : 'append';
        if (proposedSteps.length > 0 || proposedEdits.length > 0 || (data.script && recorderMode === 'streaming')) {
          setPendingChatTest(null);
          setPendingChatSteps({ summary, mode: recorderMode, steps: proposedSteps, script: data.script || undefined, handlerName: data.handler_name || undefined, edits: proposedEdits, scriptMode });
          setAiChatMessages(prev => [...prev, { role: 'assistant', content: `${summary}\n\n${t('Review below — Test it, then Apply to add it.')}` }]);
        } else {
          setAiChatMessages(prev => [...prev, { role: 'assistant', content: summary }]);
        }
        break;
      }

      // run_actions
      const actions: any[] = Array.isArray(data.actions) ? data.actions : [];
      if (data.thought) setAiChatMessages(prev => [...prev, { role: 'assistant', content: data.thought, kind: 'thought' }]);
      if (actions.length > 0 && !connected) {
        setAiChatMessages(prev => [...prev, { role: 'assistant', content: t('I need a live browser to do that — start recording first.') }]);
        break;
      }
      // Assist mode: pause for approval only when the batch CHANGES the page.
      // Read-only probes (evaluate_js / read_text / inspect / wait) run unattended.
      const READONLY_ACTIONS = new Set(['evaluate_js', 'read_text', 'get_text', 'inspect_field', 'wait']);
      const changesPage = actions.some((a: any) => !READONLY_ACTIONS.has(a.action));
      if (agentAutonomy === 'assist' && actions.length > 0 && changesPage) {
        setPendingAgentActions({ thought: data.thought || '', actions });
        setAiChatLoading(false);
        return; // resumed by approveAgentActions / skipAgentActions
      }
      await executeAgentActions(data.thought, actions);
      if (chatStopRef.current) { setAiChatMessages(prev => [...prev, { role: 'assistant', content: t('Stopped.') }]); break; }
    }

    if (ctx.iteration >= ctx.max && !chatStopRef.current) {
      setAiChatMessages(prev => [...prev, { role: 'assistant', content: t('Reached the step limit — ask me to continue if needed.') }]);
    }
    setAiChatLoading(false);
  }, [recorderMode, currentUrl, aiChatMessages, aiChatConvId, steps, detectedRequests, streamingAdvancedScript, streamingAdvancedEnabled, agentAutonomy, executeAgentActions]);

  // Unified agent chat entry: the model decides per message whether to answer,
  // drive the live browser (ephemerally — no recorded steps), or finalize an
  // artifact for the current mode. Finished artifacts are previewed for confirm.
  const sendAIChatMessage = useCallback(() => {
    if (!aiChatInput.trim() || aiChatLoading || pendingAgentActions) return;
    const instruction = aiChatInput.trim();
    setAiChatInput('');
    setAiChatMessages(prev => [...prev, { role: 'user', content: instruction }]);
    // Monitor mode: the dock finds check-target selectors from the user's description
    // instead of driving the browser / proposing workflow steps.
    if (monitorMode && onMonitorAiFind) {
      setAiChatLoading(true);
      onMonitorAiFind(instruction)
        .then((summary) => setAiChatMessages(prev => [...prev, { role: 'assistant', content: summary }]))
        .catch((err) => setAiChatMessages(prev => [...prev, { role: 'assistant', content: String(err?.message || err) }]))
        .finally(() => setAiChatLoading(false));
      return;
    }
    setPendingChatSteps(null);
    setPendingChatTest(null);
    chatStopRef.current = false;
    agentCtxRef.current = { instruction, history: [], observation: null, iteration: 0, max: 12 };
    runAgentLoop();
  }, [aiChatInput, aiChatLoading, pendingAgentActions, runAgentLoop, monitorMode, onMonitorAiFind]);

  // Assist mode: approve the proposed batch → run it → resume the loop.
  const approveAgentActions = useCallback(async () => {
    const p = pendingAgentActions;
    if (!p) return;
    setPendingAgentActions(null);
    await executeAgentActions(p.thought, p.actions);
    await runAgentLoop();
  }, [pendingAgentActions, executeAgentActions, runAgentLoop]);

  // Assist mode: skip the proposed batch (tell the model) → resume the loop.
  const skipAgentActions = useCallback(async () => {
    const p = pendingAgentActions;
    if (!p) return;
    setPendingAgentActions(null);
    agentCtxRef.current?.history.push({ thought: p.thought, actions: p.actions, results: [{ skipped: true, note: 'user skipped these actions' }] });
    await runAgentLoop();
  }, [pendingAgentActions, runAgentLoop]);

  const stopAIChat = useCallback(() => {
    chatStopRef.current = true;
    chatAbortRef.current?.abort();   // interrupt the in-flight /ai-assist/agent request
    setPendingAgentActions(null);
    setAiChatLoading(false);          // reflect stopped state immediately
  }, []);

  // Test the embedded script of a pending chat artifact live (long timeout — a
  // scraper may paginate). Lets the user verify before Apply.
  const testPendingChatScript = useCallback(() => {
    if (!pendingChatSteps) return;
    const script = pendingChatSteps.script || pendingChatSteps.steps.find(s => s.config?.script)?.config?.script;
    if (!script) { toast(t('No script to test in this artifact')); return; }
    setPendingChatTest({ loading: true });
    wsEvalScript(script, 180000)
      .then(r => setPendingChatTest({ ok: true, result: r }))
      .catch(e => setPendingChatTest({ error: e.message }));
  }, [pendingChatSteps, wsEvalScript]);

  const applyPendingChatSteps = useCallback(() => {
    if (!pendingChatSteps) return;
    if (pendingChatSteps.mode === 'streaming' && pendingChatSteps.script) {
      if ((pendingChatSteps.scriptMode || 'append') === 'replace') {
        // The AI rewrote the whole script (it could see the current one) → replace it.
        setStreamingAdvancedScript(pendingChatSteps.script);
        toast.success(t('Streaming script updated'));
      } else {
        // New handler → append to the advanced script editor.
        setStreamingAdvancedScript(prev => (prev ? prev + '\n\n' : '') + pendingChatSteps.script);
        toast.success(t('Handler added to streaming script'));
      }
      setStreamingAdvancedEnabled(true);
    }
    // Apply edits to EXISTING steps, then append any new ones. Edits resolve by stable
    // id (array-index fallback) against ONE working copy so update → delete → move stay
    // order-independent.
    const edits = pendingChatSteps.edits || [];
    if (edits.length > 0) {
      setSteps(prev => {
        const next = [...prev];
        const findIdx = (e: StepEdit) => (e.id ? next.findIndex(s => s.id === e.id) : (typeof e.index === 'number' ? e.index : -1));
        for (const e of edits) {
          if (e.op !== 'update' || !e.step) continue;
          const i = findIdx(e);
          if (i < 0 || i >= next.length) continue;
          const cur = next[i];
          const patch = e.step;
          next[i] = {
            ...cur,
            ...patch,
            id: cur.id, // never let a patch clobber the stable id
            config: { ...(cur.config || {}), ...(patch.config || {}) },
            options: { ...(cur.options || {}), ...(patch.options || {}) },
          };
        }
        const delIdxs = edits.filter(e => e.op === 'delete').map(findIdx).filter(i => i >= 0 && i < next.length).sort((a, b) => b - a);
        for (const i of delIdxs) next.splice(i, 1);
        for (const e of edits) {
          if (e.op !== 'move') continue;
          const i = findIdx(e);
          const to = typeof e.to === 'number' ? Math.max(0, Math.min(next.length - 1, e.to)) : -1;
          if (i < 0 || i >= next.length || to < 0) continue;
          const [moved] = next.splice(i, 1);
          next.splice(to, 0, moved);
        }
        return next;
      });
    }
    if (pendingChatSteps.steps.length > 0) {
      setSteps(prev => [...prev, ...pendingChatSteps.steps]);
    }
    const nAdded = pendingChatSteps.steps.length;
    const nEdits = edits.length;
    if (nAdded > 0 && nEdits === 0) {
      toast.success(nAdded === 1 ? t('{{n}} step added', { n: nAdded }) : t('{{n}} steps added', { n: nAdded }));
    } else if (nEdits > 0 && nAdded === 0) {
      toast.success(nEdits === 1 ? t('{{n}} step updated', { n: nEdits }) : t('{{n}} steps updated', { n: nEdits }));
    } else if (nAdded > 0 && nEdits > 0) {
      toast.success(t('Applied {{a}} new · {{e}} edited', { a: nAdded, e: nEdits }));
    }
    setPendingChatSteps(null);
    setPendingChatTest(null);
  }, [pendingChatSteps]);

  const discardPendingChatSteps = useCallback(() => { setPendingChatSteps(null); setPendingChatTest(null); }, []);

  // Run a step's JS script live in the recorder browser (fast test) and show the result.
  const runScriptTest = useCallback((stepId: string, script: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      toast.error(t('Browser not connected — start recording to test'));
      return;
    }
    const ws = wsRef.current;
    setScriptTests(prev => ({ ...prev, [stepId]: { loading: true } }));
    const timer = setTimeout(() => {
      ws.removeEventListener('message', handler);
      setScriptTests(prev => ({ ...prev, [stepId]: { error: t('Timed out (10s)') } }));
    }, 10000);
    const handler = (e: MessageEvent) => {
      try {
        const m = JSON.parse(e.data);
        if (m.type === 'eval_result') {
          clearTimeout(timer);
          ws.removeEventListener('message', handler);
          if (m.error) setScriptTests(prev => ({ ...prev, [stepId]: { error: m.error } }));
          else setScriptTests(prev => ({ ...prev, [stepId]: { ok: true, result: m.result } }));
        }
      } catch { /* ignore non-JSON frames */ }
    };
    ws.addEventListener('message', handler);
    ws.send(JSON.stringify({ type: 'action', action: 'evaluate_js', script: wrapScriptForEval(script) }));
  }, []);

  // Re-execute recorded steps 0..index on the LIVE page so the browser is
  // positioned exactly at that step — the spine of visual replay and the
  // "open a saved workflow live and jump in to edit it" flow. If there's no
  // live session yet, one is opened first (seeded from the first navigate step).
  const replayToStep = useCallback(async (index: number) => {
    if (replayState.running) return;
    if (index < 0 || index >= steps.length) return;

    // Ensure a live recording session is up; if not, start one then continue.
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN || connectionStateRef.current !== 'recording') {
      const firstNav = steps.find(s => s.type === 'navigate' && s.url)?.url;
      if (firstNav) setUrl(firstNav);
      toast(t('Opening a live browser to replay…'), { icon: '▶' });
      await connect(firstNav);
      const ready = await new Promise<boolean>((resolve) => {
        const startedAt = Date.now();
        const iv = setInterval(() => {
          if (wsRef.current?.readyState === WebSocket.OPEN && connectionStateRef.current === 'recording') {
            clearInterval(iv); resolve(true);
          } else if (Date.now() - startedAt > 20000) {
            clearInterval(iv); resolve(false);
          }
        }, 200);
      });
      if (!ready) { toast.error(t('Could not open a live browser')); return; }
    }

    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const reqId = crypto.randomUUID();
    replayReqRef.current = reqId;
    // Clear any prior run's status; mark the new run live.
    setReplayState({ running: true, target: index, current: null, statuses: {} });
    ws.send(JSON.stringify({
      type: 'replay_steps',
      request_id: reqId,
      up_to_index: index,
      step_delay_ms: 350,
      steps: steps.slice(0, index + 1),
    }));
  }, [replayState.running, steps, connect]);

  const cancelReplay = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'replay_cancel' }));
    }
    replayReqRef.current = null;
    setReplayState(prev => ({ ...prev, running: false, current: null }));
  }, []);

  // Detect workflow segments (code-based, no credits)
  const detectSegments = useCallback(async () => {
    if (steps.length === 0) return;
    const hasExtracts = steps.some(s => s.type === 'extract');
    if (!hasExtracts) {
      setDetectedSegments([]);
      return;
    }
    try {
      const { default: client } = await import('../api/client');
      const resp = await client.post('/ai-assist/detect-segments', { steps });
      setDetectedSegments(resp.data.segments || []);
    } catch {
      setDetectedSegments([]);
    }
  }, [steps]);

  // Auto-detect segments when recording stops and steps have extracts. Manual
  // recordings only — streaming uses handlers/advanced-script and apiMode captures
  // API calls, so step-group functions don't apply there (and would be discarded
  // on save), which would make an auto-detected function summary misleading.
  useEffect(() => {
    if (apiMode || streamingMode) return;
    if (connectionState === 'connected' && steps.length > 0 && steps.some(s => s.type === 'extract')) {
      detectSegments();
    }
  }, [connectionState]);

  // Delete a step
  const deleteStep = (index: number) => {
    setSteps(prev => prev.filter((_, i) => i !== index));
    setEditingStepId(prev => (prev === steps[index]?.id ? null : prev));
  };

  // Edit an existing step's fields in place (same per-type form as the workflow editor).
  const updateStepAt = (index: number, updates: Partial<WorkflowStep>) => {
    setSteps(prev => prev.map((s, i) => (i === index ? { ...s, ...(updates as Partial<RecordedStep>) } : s)));
  };

  // Begin "pick element from page" for a selector field. Requires a live recording
  // session; hides the step modal (via pickActive) and turns canvas clicks into
  // element-info probes (via isExtracting). The captured selector lands in `apply`.
  const startPickSelector = useCallback((apply: (selector: string) => void) => {
    if (connectionState !== 'recording' || !wsRef.current) {
      toast.error(t('Start recording to pick an element from the page'));
      return;
    }
    pickApplyRef.current = apply;
    setPickActive(true);
    setIsExtracting(true);
    toast(t('Click an element in the browser to use its selector'));
  }, [connectionState, t]);

  const cancelPickSelector = useCallback(() => {
    pickApplyRef.current = null;
    setPickActive(false);
    setIsExtracting(false);
    wsRef.current?.send(JSON.stringify({ type: 'action', action: 'clear_highlight' }));
  }, []);

  // Move step up/down
  const moveStep = (index: number, direction: 'up' | 'down') => {
    setSteps(prev => {
      const newSteps = [...prev];
      const targetIndex = direction === 'up' ? index - 1 : index + 1;
      if (targetIndex < 0 || targetIndex >= newSteps.length) return prev;
      [newSteps[index], newSteps[targetIndex]] = [newSteps[targetIndex], newSteps[index]];
      return newSteps;
    });
  };

  // Save workflow
  // Convert a detected request/response pair into a real api_call workflow step,
  // auto-detecting returned data as response_extractions.
  const addRequestAsStep = (req: DetectedRequest) => {
    // Replace any captured credential/form value with a reusable placeholder
    // ({{secret:field}} for credentials, {{field}} for form data) so the step is
    // parameterized rather than hardcoding the values typed during recording.
    const parameterize = (val: any): any => {
      if (typeof val === 'string') {
        for (const c of detectedCredentials) {
          if (c.value && val === c.value) return `{{secret:${c.field_name}}}`;
        }
        for (const [k, v] of Object.entries(capturedFormData)) {
          if (v && val === v) return `{{${k}}}`;
        }
        return val;
      }
      if (Array.isArray(val)) return val.map(parameterize);
      if (isPlainObject(val)) {
        const out: Record<string, any> = {};
        for (const [k, v] of Object.entries(val)) out[k] = parameterize(v);
        return out;
      }
      return val;
    };

    let body_template: any = {};
    if (req.request_body) {
      try {
        body_template = parameterize(JSON.parse(req.request_body));
      } catch {
        body_template = { raw: parameterize(req.request_body) };
      }
    }
    const headers = parameterize({ ...(req.request_headers || {}) }) as Record<string, string>;
    const response_extractions = autoSuggestExtractions(req.response_body);
    const fn = deriveApiFunctionName(req.url, req.method);
    const step: RecordedStep = {
      id: `api_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
      type: 'api_call',
      timestamp: Date.now(),
      description: `${req.method} ${prettyPath(req.url)}`,
      config: {
        function_name: fn,
        method: req.method,
        url: req.url,
        headers,
        body_template,
        response_extractions,
        timeout_ms: 30000,
      },
    };
    setSteps((prev) => [...prev, step]);
    setShowSteps(true);
    setAddedRequestKeys((prev) => new Set(prev).add(`${req.method} ${req.url}`));
    const n = Object.keys(response_extractions).length;
    const extractSuffix = n === 0 ? '' : n === 1 ? t(' (+{{n}} extraction)', { n }) : t(' (+{{n}} extractions)', { n });
    toast.success(t('Added API step: {{fn}}', { fn }) + extractSuffix);
  };

  // This component has no explicit Save (name + Save button). The wizard creates
  // from the live-synced draft (onStepsChange / onCredentialsChange / onFormDataChange /
  // onSegmentsChange / onStreamingConfigChange) when the app-bar primary "Done recording"
  // advances to Finalize. The `onSave` prop stays on the public interface for any
  // non-wizard caller but is not driven from inside this component.

  const recorderContent = (
              <div className={clsx("w-full flex flex-col bg-surface", embedded ? "h-full overflow-hidden" : "h-full")}>
                {/* URL Bar — shown ONLY before recording. Once recording starts the
                    app-bar carries the live URL + status, and the contextual on-stage
                    toolbar (below, over the stage) takes over the recording controls.
                    The connect-on-Enter logic is preserved here as the pre-connect
                    surface. */}
                {connectionState !== 'recording' && (
                <div className="flex items-center gap-2 px-3 py-2 bg-surface border-b border-border">
                  {/* Status dot */}
                  <span className={clsx(
                    'w-2 h-2 rounded-full shrink-0',
                    connectionState === 'connected' && 'bg-ink',
                    connectionState === 'connecting' && 'bg-tertiary animate-status-pulse',
                    connectionState === 'disconnected' && 'bg-border',
                    connectionState === 'error' && 'bg-ink',
                    connectionState === 'needs_agent' && 'bg-tertiary animate-status-pulse',
                  )} title={connectionState} />

                  <input
                    type="text"
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                    placeholder={t('Enter URL to record...')}
                    className="flex-1 px-3 py-1.5 bg-canvas border border-border rounded-lg text-sm text-ink placeholder:text-tertiary focus:ring-2 focus:ring-ink/5 focus:border-ink/30 disabled:opacity-60 transition-all font-mono text-xs"
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && connectionState === 'disconnected') {
                        connect();
                      }
                    }}
                  />

                  {/* Recording options */}
                  <label className="flex items-center gap-2 cursor-pointer select-none">
                    <Switch
                      size="sm"
                      checked={recordWaitSteps}
                      onChange={() => setRecordWaitSteps(!recordWaitSteps)}
                    />
                    <span className="text-xs text-secondary whitespace-nowrap">{t('Record waits')}</span>
                  </label>
                </div>
                )}

                {/* Recording controls were relocated to the contextual on-stage
                    toolbar (rendered over the stage, only while recording). */}

                {/* Tab bar — only show when multiple tabs are open */}
                {openTabs.length > 1 && (
                  <div className="flex items-center gap-0.5 px-3 py-1 bg-zinc-100 border-b border-border overflow-x-auto">
                    {openTabs.map((tab) => (
                      <div
                        key={tab.index}
                        className={clsx(
                          'flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs max-w-[200px] cursor-pointer transition-colors group',
                          tab.active
                            ? 'bg-white text-ink shadow-sm border border-border'
                            : 'text-secondary hover:bg-white/60 hover:text-ink'
                        )}
                        onClick={() => {
                          if (tab.active || !wsRef.current) return;
                          wsRef.current.send(JSON.stringify({
                            type: 'action',
                            action: 'switch_tab',
                            tab_index: tab.index,
                          }));
                        }}
                      >
                        <span className="truncate">{tab.title}</span>
                        {openTabs.length > 1 && (
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              if (!wsRef.current) return;
                              wsRef.current.send(JSON.stringify({
                                type: 'action',
                                action: 'close_tab',
                                tab_index: tab.index,
                              }));
                            }}
                            className="opacity-0 group-hover:opacity-100 p-0.5 hover:bg-zinc-200 rounded transition-opacity"
                          >
                            <XMarkIcon className="w-3 h-3" />
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                )}

                {/* Main Content */}
                <div className="flex-1 flex overflow-hidden min-h-0 relative">
                  {/* AI Dock — ALWAYS mounted bottom-center whenever the stage is
                      live (recording OR connected). Resting = slim capsule; engaged
                      = expands UPWARD into the transcript. Handlers no-op gracefully
                      when not actively recording (the agent loop detects a dead WS). */}
                  {(connectionState === 'recording' || connectionState === 'connected') && (
                    <div
                      className={clsx(
                        // Positioning wrapper: centers the dock over the stage, but when the
                        // steps/selectors panel is open its region RETREATS to the left of that
                        // panel (w-80 + right-3 gap ≈ 21rem) so the two never overlap. The dock's
                        // width below is relative to THIS wrapper, so it shrinks to fit the
                        // remaining space — responsive on any viewport.
                        'absolute bottom-0 left-0 z-40 flex justify-center items-end pointer-events-none transition-[right] duration-300 ease-out',
                        showSteps ? 'right-[21rem]' : 'right-0',
                      )}
                    >
                    <div
                      onMouseEnter={() => setAiDockHovered(true)}
                      onMouseLeave={() => setAiDockHovered(false)}
                      className={clsx(
                      // Rests semi-hidden at the bottom (only a sliver peeks); slides up on
                      // hover or when engaged (input focused / transcript open).
                      'pointer-events-auto w-[min(680px,92%)] flex flex-col rounded-2xl border border-border bg-surface/90 backdrop-blur-xl shadow-2xl overflow-hidden transition-transform duration-300 ease-out',
                      (aiDockExpanded || aiDockHovered)
                        ? '-translate-y-4'
                        : 'translate-y-[calc(100%-2.25rem)]',
                      aiDockExpanded ? 'max-h-[460px]' : ''
                    )}>
                      {/* Peek handle — what advertises the dock while it rests at the bottom.
                          h-9 matches the 2.25rem sliver that stays visible, so this labelled
                          "Ask AI" bar (with a nudging chevron) is exactly what pokes up. The
                          whole bar lifts + expands the dock on click; hidden once engaged. */}
                      {!aiDockExpanded && (
                        <button
                          type="button"
                          onClick={() => { setAiDockExpanded(true); setShowSteps(false); }}
                          className="shrink-0 h-9 flex items-center justify-center gap-1.5 text-[12px] font-medium text-ink hover:bg-chrome transition-colors"
                          title={t('Ask AI')}
                        >
                          <SparklesIcon className="h-3.5 w-3.5 text-secondary" />
                          <span>{t('Ask AI')}</span>
                          <ChevronUpIcon className={clsx('h-3.5 w-3.5 text-tertiary', !aiDockHovered && 'animate-bounce')} />
                        </button>
                      )}
                      {/* Header — shown only when engaged (the resting capsule is its
                          own slim row at the bottom). */}
                      {aiDockExpanded && (
                      <div className="px-4 py-2.5 flex items-center justify-between shrink-0 border-b border-border/60">
                        <span className="text-ink font-medium flex items-center gap-2 text-sm">
                          <SparklesIcon className="h-4 w-4 text-secondary" />
                          {t('AI Assistant')}
                        </span>
                        <div className="flex items-center gap-1.5">
                          {/* Autonomy: Auto runs the whole loop; Assist asks before each page-changing batch */}
                          <div className="flex rounded-md border border-border overflow-hidden" title={t('Auto: runs end-to-end · Assist: approve actions that change the page (read-only probes run automatically)')}>
                            {(['autonomous', 'assist'] as const).map(m => (
                              <button
                                key={m}
                                onClick={() => setAgentAutonomy(m)}
                                disabled={aiChatLoading}
                                className={clsx(
                                  'px-1.5 py-0.5 text-[9px] font-medium transition disabled:opacity-50',
                                  agentAutonomy === m ? 'bg-ink text-white' : 'bg-canvas text-secondary hover:bg-chrome'
                                )}
                              >
                                {m === 'autonomous' ? t('Auto') : t('Assist')}
                              </button>
                            ))}
                          </div>
                          <button onClick={() => setAiDockExpanded(false)} className="p-0.5 hover:bg-chrome rounded" title={t('Collapse')}>
                            <ChevronDownIcon className="h-3.5 w-3.5 text-secondary" />
                          </button>
                        </div>
                      </div>
                      )}
                      {aiDockExpanded && (
                      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-2 min-h-0">
                        {aiChatMessages.length === 0 && (
                          <p className="text-[11px] text-tertiary text-center py-2">
                            {monitorMode ? t('Describe what to watch on this page — the AI finds the selectors and adds them.')
                              : recorderMode === 'api' ? t('Ask the AI to build an API call — it can trigger and inspect requests for you.')
                              : recorderMode === 'streaming' ? t('Ask the AI to build streaming functions — it writes an advanced ps.fn script that drives the live page.')
                              : t('Ask the AI to do anything here — extract data, find an item, build an extractor. It drives the browser and adds the step for you.')}
                          </p>
                        )}
                        {aiChatMessages.map((msg, i) => (
                          msg.kind === 'thought' || msg.kind === 'run' || msg.kind === 'result' ? (
                            <div key={i} className={clsx(
                              'text-[10px] leading-snug px-1',
                              msg.kind === 'run' && 'text-ink font-mono',
                              msg.kind === 'result' && 'text-secondary font-mono whitespace-pre-wrap break-all',
                              msg.kind === 'thought' && 'text-tertiary italic',
                            )}>
                              {msg.kind === 'run' && <span className="text-tertiary">▸ </span>}{msg.content}
                            </div>
                          ) : (
                          <div key={i} className={clsx('text-xs', msg.role === 'user' ? 'text-right' : '')}>
                            <div className={clsx(
                              'inline-block max-w-full rounded-lg px-2.5 py-1.5 text-left',
                              msg.role === 'user'
                                ? 'bg-ink text-white'
                                : 'bg-hover text-secondary'
                            )}>
                              <p className="whitespace-pre-wrap break-words">{msg.content}</p>
                              {msg.actions && msg.actions.length > 0 && (
                                <div className="mt-1.5 space-y-0.5 border-t border-border pt-1.5">
                                  <p className="text-[9px] text-tertiary font-medium uppercase">{t('Actions:')}</p>
                                  {msg.actions.map((action: any, j: number) => (
                                    <button
                                      key={j}
                                      onClick={() => applyAIAction(action)}
                                      className="w-full text-left px-1.5 py-1 bg-canvas/50 hover:bg-ink/10 rounded text-[10px] flex items-center gap-1.5 transition group"
                                    >
                                      <span className="text-ink font-mono text-[9px] shrink-0">{action.type}</span>
                                      <span className="truncate text-secondary group-hover:text-ink">{action.description || action.selector || action.value || ''}</span>
                                      <PaperAirplaneIcon className="h-2.5 w-2.5 text-secondary opacity-0 group-hover:opacity-100 shrink-0 ml-auto" />
                                    </button>
                                  ))}
                                </div>
                              )}
                            </div>
                          </div>
                          )
                        ))}
                        {aiChatLoading && (
                          <div className="flex items-center gap-1.5 text-[10px] text-tertiary">
                            <ArrowPathIcon className="h-3 w-3 animate-spin" />
                            {t('Working…')}
                          </div>
                        )}
                        {/* Assist mode — approve the AI's next page-changing batch before it runs */}
                        {pendingAgentActions && (
                          <div className="border border-ink/30 rounded-lg p-2 bg-canvas/60 space-y-1.5">
                            <p className="text-[9px] text-tertiary font-medium uppercase">{t('AI wants to change the page')}</p>
                            {pendingAgentActions.thought && <p className="text-[10px] text-secondary italic">{pendingAgentActions.thought}</p>}
                            <div className="space-y-0.5">
                              {pendingAgentActions.actions.map((a: any, j: number) => (
                                <div key={j} className="text-[10px] font-mono text-ink">▸ {a.action}{a.selector ? ` ${a.selector}` : a.url ? ` ${a.url}` : a.script ? ` ${String(a.script).slice(0, 60)}…` : ''}</div>
                              ))}
                            </div>
                            <div className="flex gap-1.5 pt-0.5">
                              <button onClick={approveAgentActions} className="flex-1 px-2 py-1 bg-accent-strong hover:bg-accent-strong/90 text-accent-on rounded text-[10px] font-semibold shadow-sm flex items-center justify-center gap-1">
                                <PlayIcon className="h-3 w-3" /> {t('Run')}
                              </button>
                              <button onClick={skipAgentActions} className="px-2 py-1 bg-hover hover:bg-active border border-border text-secondary rounded text-[10px]">
                                {t('Skip')}
                              </button>
                              <button onClick={stopAIChat} className="px-2 py-1 bg-hover hover:bg-active border border-border text-secondary rounded text-[10px]">
                                {t('Stop')}
                              </button>
                            </div>
                          </div>
                        )}
                        {/* Finished artifact — Test, then confirm before it enters the workflow */}
                        {pendingChatSteps && (
                          <div className="border border-border rounded-lg p-2 bg-canvas/60 space-y-1.5">
                            <p className="text-[10px] text-secondary">{pendingChatSteps.summary}</p>
                            {pendingChatSteps.steps.map((s, j) => (
                              <div key={j} className="text-[10px]">
                                <span className="font-mono text-ink">{s.type}</span>
                                <span className="text-tertiary"> — {s.description}</span>
                                {(s.config?.script || s.config?.url) && (
                                  <pre className="mt-0.5 font-mono text-[9px] text-ink/70 bg-surface border border-border rounded p-1 max-h-24 overflow-auto whitespace-pre-wrap break-all">{s.config?.script || s.config?.url}</pre>
                                )}
                              </div>
                            ))}
                            {pendingChatSteps.edits && pendingChatSteps.edits.length > 0 && (
                              <div className="space-y-0.5">
                                {pendingChatSteps.edits.map((e, j) => {
                                  const idx = e.id ? steps.findIndex(s => s.id === e.id) : (typeof e.index === 'number' ? e.index : -1);
                                  const target = idx >= 0 ? steps[idx] : undefined;
                                  const pos = idx >= 0 ? `#${idx + 1}` : (e.id ? e.id.slice(0, 6) : `#${(e.index ?? 0) + 1}`);
                                  const label = e.op === 'update' ? t('Update {{pos}}', { pos }) : e.op === 'delete' ? t('Delete {{pos}}', { pos }) : t('Move {{pos}} → {{to}}', { pos, to: (e.to ?? 0) + 1 });
                                  return (
                                    <div key={`edit-${j}`} className="text-[10px]">
                                      <span className="font-mono text-ink">{label}</span>
                                      {target && <span className="text-tertiary"> — {target.type}{target.description ? ` ${target.description}` : ''}</span>}
                                    </div>
                                  );
                                })}
                              </div>
                            )}
                            {pendingChatSteps.scriptMode === 'replace' && pendingChatSteps.script && (
                              <p className="text-[10px] text-tertiary">{t('Replaces the current advanced script')}</p>
                            )}
                            {pendingChatSteps.script && (
                              <pre className="font-mono text-[9px] text-ink/70 bg-surface border border-border rounded p-1 max-h-28 overflow-auto whitespace-pre-wrap break-all">{pendingChatSteps.script}</pre>
                            )}
                            {(pendingChatSteps.script || pendingChatSteps.steps.some(s => s.config?.script)) && (
                              <button
                                onClick={testPendingChatScript}
                                disabled={pendingChatTest?.loading}
                                className="w-full px-2 py-1 bg-hover hover:bg-active border border-border text-secondary rounded text-[10px] font-medium flex items-center justify-center gap-1 disabled:opacity-50"
                              >
                                {pendingChatTest?.loading
                                  ? <><ArrowPathIcon className="h-3 w-3 animate-spin" /> {t('Testing…')}</>
                                  : <><PlayIcon className="h-3 w-3" /> {t('Test live')}</>}
                              </button>
                            )}
                            {pendingChatTest && !pendingChatTest.loading && (
                              pendingChatTest.error ? (
                                <div className="text-[10px] text-secondary flex items-start gap-1"><ExclamationTriangleIcon className="h-3 w-3 shrink-0 mt-0.5" /> {pendingChatTest.error}</div>
                              ) : (
                                <pre className="font-mono text-[9px] text-ink/70 bg-hover/50 border border-border rounded p-1 max-h-32 overflow-auto whitespace-pre-wrap break-all">{(() => { try { return JSON.stringify(pendingChatTest.result, null, 2).slice(0, 1000); } catch { return String(pendingChatTest.result).slice(0, 1000); } })()}</pre>
                              )
                            )}
                            <div className="flex gap-1.5 pt-0.5">
                              <button onClick={applyPendingChatSteps} className="flex-1 px-2 py-1 bg-accent-strong hover:bg-accent-strong/90 text-accent-on rounded text-[10px] font-semibold shadow-sm flex items-center justify-center gap-1">
                                <CheckIcon className="h-3 w-3" /> {t('Apply')}
                              </button>
                              <button onClick={discardPendingChatSteps} className="px-2 py-1 bg-hover hover:bg-active border border-border text-secondary rounded text-[10px]">
                                {t('Discard')}
                              </button>
                            </div>
                          </div>
                        )}
                      </div>
                      )}
                      {/* Quick-chips — folded-in shortcuts (Build a scraper / Extract /
                          Find an item / Write a handler). Build-a-scraper opens the
                          dedicated panel; the rest prefill the chat input. Shown only
                          when engaged, above the input. */}
                      {aiDockExpanded && (
                        <div className="px-3 pt-2 flex flex-wrap items-center gap-1.5 shrink-0">
                          <button
                            onClick={() => { setShowAIExtract(true); }}
                            className="px-2 py-1 bg-hover hover:bg-active text-secondary rounded-md text-[10px] font-medium flex items-center gap-1 transition"
                            title={t('AI generates an extraction script from a description')}
                          >
                            <ArrowTopRightOnSquareIcon className="h-3 w-3" />
                            {t('Build an extractor')}
                          </button>
                          <button
                            onClick={() => setAiChatInput(t('Extract '))}
                            disabled={aiChatLoading || !!pendingAgentActions}
                            className="px-2 py-1 bg-hover hover:bg-active text-secondary rounded-md text-[10px] font-medium flex items-center gap-1 transition disabled:opacity-50"
                          >
                            <SparklesIcon className="h-3 w-3" />
                            {t('Extract')}
                          </button>
                          <button
                            onClick={() => setAiChatInput(t('Find an item: '))}
                            disabled={aiChatLoading || !!pendingAgentActions}
                            className="px-2 py-1 bg-hover hover:bg-active text-secondary rounded-md text-[10px] font-medium flex items-center gap-1 transition disabled:opacity-50"
                          >
                            <MagnifyingGlassIcon className="h-3 w-3" />
                            {t('Find an item')}
                          </button>
                          <button
                            onClick={() => setAiChatInput(t('Write a handler that '))}
                            disabled={aiChatLoading || !!pendingAgentActions}
                            className="px-2 py-1 bg-hover hover:bg-active text-secondary rounded-md text-[10px] font-medium flex items-center gap-1 transition disabled:opacity-50"
                          >
                            <BoltIcon className="h-3 w-3" />
                            {t('Write a handler')}
                          </button>
                        </div>
                      )}
                      <div className="p-3 shrink-0 bg-surface/80">
                        <div className="flex gap-2 max-w-2xl mx-auto w-full items-center">
                          {/* Resting capsule = inline autonomy control; engaged hides it
                              into the header. */}
                          {!aiDockExpanded && (
                            <div className="flex rounded-md border border-border overflow-hidden shrink-0" title={t('Auto: runs end-to-end · Assist: approve actions that change the page (read-only probes run automatically)')}>
                              {(['autonomous', 'assist'] as const).map(m => (
                                <button
                                  key={m}
                                  onClick={() => setAgentAutonomy(m)}
                                  disabled={aiChatLoading}
                                  className={clsx(
                                    'px-1.5 py-1 text-[9px] font-medium transition disabled:opacity-50',
                                    agentAutonomy === m ? 'bg-ink text-white' : 'bg-canvas text-secondary hover:bg-chrome'
                                  )}
                                >
                                  {m === 'autonomous' ? t('Auto') : t('Assist')}
                                </button>
                              ))}
                            </div>
                          )}
                          <input
                            type="text"
                            value={aiChatInput}
                            onChange={(e) => setAiChatInput(e.target.value)}
                            // Engaging the AI input collapses the floating steps rail so the
                            // transcript has room and nothing overlaps the AI dock.
                            onFocus={() => { setAiDockExpanded(true); setShowSteps(false); }}
                            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); e.stopPropagation(); setAiDockExpanded(true); sendAIChatMessage(); } }}
                            placeholder={pendingAgentActions ? t('Approve or skip the action above…') : monitorMode ? t('Describe what to watch — AI finds the selectors…') : aiDockExpanded ? t('Ask the AI to do anything — drive the browser, extract data, build an extractor…') : t('Ask AI, or take over and click…')}
                            disabled={aiChatLoading || !!pendingAgentActions}
                            className="flex-1 px-3 py-2.5 bg-canvas border border-border rounded-lg text-sm text-ink placeholder:text-tertiary focus:ring-1 focus:ring-ink/20 disabled:opacity-50"
                          />
                          {aiChatLoading ? (
                            <button
                              onClick={stopAIChat}
                              className="px-2 py-1.5 bg-hover hover:bg-active border border-border text-secondary rounded transition"
                              title={t('Stop')}
                            >
                              <XMarkIcon className="h-3 w-3" />
                            </button>
                          ) : (
                            <button
                              onClick={() => { setAiDockExpanded(true); sendAIChatMessage(); }}
                              disabled={!aiChatInput.trim()}
                              className="px-2 py-1.5 bg-accent-strong hover:bg-accent-strong/90 disabled:bg-hover disabled:text-tertiary text-accent-on rounded transition"
                            >
                              <PaperAirplaneIcon className="h-3 w-3" />
                            </button>
                          )}
                          {/* Resting capsule expand affordance */}
                          {!aiDockExpanded && (aiChatMessages.length > 0 || pendingAgentActions || pendingChatSteps) && (
                            <button
                              onClick={() => setAiDockExpanded(true)}
                              className="px-2 py-1.5 bg-hover hover:bg-active border border-border text-secondary rounded transition shrink-0"
                              title={t('Expand')}
                            >
                              <ChevronUpIcon className="h-3 w-3" />
                            </button>
                          )}
                        </div>
                      </div>
                    </div>
                    </div>
                  )}
                  {/* Browser View — full-bleed stage filling edge-to-edge under the
                      app-bar to the bottom (no footer). The canvas frame inside keeps a
                      fixed 1280x800 logical aspect so the streamed framebuffer maps 1:1. */}
                  <div className="relative flex-1 flex items-center justify-center bg-canvas overflow-hidden min-w-0 min-h-0 p-1">
                    {/* Monitor action switcher — a floating toolbar pinned just under the URL
                        bar at the top of the stage (Browse / Click / Area / Zone / CSS). Drops
                        below the recording browser-nav toolbar while capturing setup steps. */}
                    {monitorMode && monitorToolbar && (connectionState === 'connected' || connectionState === 'recording') && (
                      <div className={clsx(
                        'absolute left-1/2 -translate-x-1/2 z-40 transition-all duration-300',
                        connectionState === 'recording' ? 'top-16' : 'top-3',
                      )}>
                        {monitorToolbar}
                      </div>
                    )}
                    {connectionState === 'needs_agent' && (
                      <ConnectAgentPanel
                        kind={gateKind || 'connect_local'}
                        capability={capability}
                      />
                    )}
                    {(connectionState === 'disconnected' || connectionState === 'connecting' || connectionState === 'error') && (
                      <div className="text-center">
                        {connectionState === 'connecting' ? (
                          <>
                            <ArrowPathIcon className="h-5 w-5 text-secondary animate-spin mx-auto mb-2" />
                            <p className="text-xs text-secondary">{t('Loading browser...')}</p>
                          </>
                        ) : connectionState === 'error' ? (
                          <>
                            <p className="text-xs text-secondary">{t('Connection failed — retrying...')}</p>
                          </>
                        ) : (
                          <p className="text-xs text-tertiary">{t('Enter a URL above to start')}</p>
                        )}
                      </div>
                    )}

                    {(connectionState === 'recording' || connectionState === 'connected') && (
                      // Full-bleed stage: the canvas keeps its fixed 1280x800 logical
                      // aspect (so the streamed framebuffer is never distorted and the
                      // per-axis click/extraction scaling in handleCanvasClick /
                      // handleCanvasMouseMove stays exact), but it now maximizes into the
                      // edge-to-edge stage instead of a centered letterbox. Only a 1px
                      // inset bezel ring is carried for the rounded/floating look.
                      <div ref={selectionContainerRef} className="relative aspect-[1280/800] h-full w-auto max-w-full max-h-full rounded-xl overflow-hidden ring-1 ring-inset ring-border/70">
                        <canvas
                          ref={canvasRef}
                          onClick={selectionMode ? selection.canvasProps.onClick : handleCanvasClick}
                          onMouseMove={selectionMode ? undefined : handleCanvasMouseMove}
                          onKeyDown={handleKeyDown}
                          onWheel={handleWheel}
                          tabIndex={0}
                          className={clsx(
                            'w-full h-full block',
                            // In "check target" selection mode, letterbox (object-contain) so the
                            // shared hook's coordinate math is exact + the frame is never distorted
                            // for visual-zone picking. Normal recording keeps the full-bleed stretch.
                            selectionMode ? 'object-contain' : '',
                            isExtracting
                              ? 'cursor-cell'
                              : selectionMode === 'click' || (!selectionMode && connectionState === 'recording')
                              ? 'cursor-crosshair'
                              : ''
                          )}
                          style={{ outline: 'none' }}
                        />
                        {/* Monitor "check target" selection overlays (element flash, visual zones,
                            transient pick error, and the zone/area drawing surface). */}
                        {selectionMode && (connectionState === 'recording' || connectionState === 'connected') && (
                          <>
                            {selectionZones.map((z) => {
                              const d = selection.toDisplayRect(z.region);
                              if (!d) return null;
                              const hot = z.id === highlightZoneId;
                              return (
                                <div
                                  key={z.id}
                                  className={clsx(
                                    'absolute rounded-[2px] pointer-events-none z-20 transition-all',
                                    hot ? 'border-2 border-ink bg-ink/15' : 'border border-ink/40 bg-ink/[0.04]',
                                  )}
                                  style={{ left: d.left, top: d.top, width: d.width, height: d.height }}
                                />
                              );
                            })}
                            {selection.flash && (
                              <div
                                className="absolute border-2 border-ink bg-ink/10 rounded pointer-events-none z-20"
                                style={{ left: selection.flash.left, top: selection.flash.top, width: selection.flash.width, height: selection.flash.height }}
                              />
                            )}
                            {selection.pickError && (
                              <div className="absolute bottom-3 left-1/2 -translate-x-1/2 z-30 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-ink text-white text-[11px] shadow-lg pointer-events-none">
                                <ExclamationTriangleIcon className="w-3.5 h-3.5" />
                                {selection.pickError}
                              </div>
                            )}
                            {selection.drawingActive && (
                              <div
                                className="absolute inset-0 cursor-crosshair z-10"
                                onMouseDown={selection.drawingHandlers.onMouseDown}
                                onMouseMove={selection.drawingHandlers.onMouseMove}
                                onMouseUp={selection.drawingHandlers.onMouseUp}
                              >
                                {selection.drawing && selection.drawing.w > 0 && (
                                  <div
                                    className={clsx(
                                      'absolute rounded pointer-events-none border-2 border-ink bg-ink/10',
                                      selectionMode === 'area' && 'border-dashed',
                                    )}
                                    style={{ left: selection.drawing.x, top: selection.drawing.y, width: selection.drawing.w, height: selection.drawing.h }}
                                  />
                                )}
                              </div>
                            )}
                          </>
                        )}
                        {connectionState === 'recording' && !isExtracting && (
                          <div className="absolute top-3 left-3 px-2.5 py-1.5 bg-ink text-white text-[11px] font-medium rounded-lg flex items-center gap-1.5 shadow-sm">
                            <span className="w-1.5 h-1.5 bg-white rounded-full animate-pulse" />
                            {t('REC')}
                          </div>
                        )}
                        {isExtracting && (
                          <div className="absolute top-3 left-3 px-2.5 py-1.5 bg-ink text-white text-[11px] font-medium rounded-lg flex items-center gap-2 shadow-sm">
                            <span className="w-1.5 h-1.5 bg-white rounded-full" />
                            {pickActive ? t('Click an element to use its selector') : t('EXTRACT — Click element to select')}
                            {pickActive && (
                              <button onClick={cancelPickSelector} className="ml-0.5 px-1.5 py-0.5 rounded bg-white/20 hover:bg-white/30 text-[10px] font-medium transition-colors">
                                {t('Cancel')}
                              </button>
                            )}
                          </div>
                        )}

                        {/* Contextual on-stage toolbar — shown ONLY while recording.
                            Demoted from the old standing recording toolbar; floats over
                            the top of the stage so the recording controls live with the
                            canvas instead of in a permanent header. All handler wiring is
                            preserved verbatim. */}
                        {connectionState === 'recording' && (
                          <div className="absolute top-3 left-1/2 -translate-x-1/2 z-40 flex items-center gap-1.5 rounded-xl border border-border bg-surface/85 backdrop-blur-xl shadow-lg px-1.5 py-1">
                            {/* Browser controls — back / forward / reload + an editable
                                address bar so the user drives the live browser while
                                recording. Extract / Templates / Return were removed:
                                every step is added and managed in the steps spine. */}
                            <button
                              onClick={() => wsRef.current?.send(JSON.stringify({ type: 'action', action: 'back' }))}
                              className="p-1.5 rounded-lg text-secondary hover:bg-active hover:text-ink transition-colors"
                              title={t('Back')}
                            >
                              <ChevronLeftIcon className="h-4 w-4" />
                            </button>
                            <button
                              onClick={() => wsRef.current?.send(JSON.stringify({ type: 'action', action: 'forward' }))}
                              className="p-1.5 rounded-lg text-secondary hover:bg-active hover:text-ink transition-colors"
                              title={t('Forward')}
                            >
                              <ChevronRightIcon className="h-4 w-4" />
                            </button>
                            <button
                              onClick={() => { if (currentUrl && wsRef.current) wsRef.current.send(JSON.stringify({ type: 'action', action: 'navigate', url: currentUrl })); }}
                              className="p-1.5 rounded-lg text-secondary hover:bg-active hover:text-ink transition-colors"
                              title={t('Reload')}
                            >
                              <ArrowPathIcon className="h-4 w-4" />
                            </button>
                            <input
                              key={currentUrl}
                              defaultValue={currentUrl}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') {
                                  e.preventDefault();
                                  const raw = (e.target as HTMLInputElement).value.trim();
                                  if (raw && wsRef.current) {
                                    const u = /^https?:\/\//i.test(raw) ? raw : `https://${raw}`;
                                    wsRef.current.send(JSON.stringify({ type: 'action', action: 'navigate', url: u }));
                                  }
                                }
                              }}
                              placeholder={t('Enter URL…')}
                              spellCheck={false}
                              className="w-48 sm:w-72 px-2.5 py-1.5 bg-canvas border border-border rounded-lg text-xs font-mono text-ink placeholder:text-tertiary focus:outline-none focus:border-ink/30"
                            />
                            <div className="w-px h-5 bg-border" />
                            {/* Stop recording — ends the live session; the live-synced
                                draft settles and the app-bar primary advances to Finalize. */}
                            <button
                              onClick={() => {
                                if (isExtracting) {
                                  setIsExtracting(false);
                                  if (wsRef.current) wsRef.current.send(JSON.stringify({ type: 'action', action: 'clear_highlight' }));
                                  setExtractHighlight(null);
                                  setShowExtractPopover(false);
                                }
                                stopRecording();
                              }}
                              className="px-2.5 py-1.5 rounded-lg font-semibold shadow-sm flex items-center gap-1.5 text-xs transition bg-accent-strong hover:bg-accent-strong/90 text-accent-on"
                              title={t('Stop recording')}
                            >
                              <StopIcon className="h-3.5 w-3.5" />
                              {t('Stop')}
                            </button>
                            {/* The standing "AI assist" toggle is retired — the AI
                                dock is now always mounted bottom-center. */}
                            {/* Streaming mode: handler markers */}
                            {streamingMode && (
                              <>
                                <div className="w-px h-5 bg-border" />
                                {activeHandlerName ? (
                                  <>
                                    <span className="px-2 py-1 bg-ink/10 text-ink rounded text-[11px] font-medium animate-status-pulse">
                                      {t('Recording: {{name}}', { name: activeHandlerName })}
                                    </span>
                                    <button
                                      onClick={() => {
                                        if (activeHandlerStart !== null) {
                                          setStreamingHandlers(prev => [...prev, {
                                            name: activeHandlerName,
                                            step_range: [activeHandlerStart, steps.length] as [number, number],
                                            input_variables: [],
                                            extract_fields: [],
                                          }]);
                                        }
                                        setActiveHandlerName(null);
                                        setActiveHandlerStart(null);
                                        toast.success(t('Handler "{{name}}" defined', { name: activeHandlerName }));
                                      }}
                                      className="px-2.5 py-1.5 bg-accent-strong hover:bg-accent-strong/90 text-accent-on rounded-lg font-semibold shadow-sm text-xs transition-colors"
                                    >
                                      {t('End Handler')}
                                    </button>
                                  </>
                                ) : showHandlerNameInput ? (
                                  <div className="flex items-center gap-1">
                                    <input
                                      type="text"
                                      value={handlerNameInput}
                                      onChange={(e) => setHandlerNameInput(e.target.value)}
                                      onKeyDown={(e) => {
                                        if (e.key === 'Enter' && handlerNameInput.trim()) {
                                          const name = handlerNameInput.trim().replace(/[^a-zA-Z0-9_-]/g, '_');
                                          setActiveHandlerName(name);
                                          setActiveHandlerStart(steps.length);
                                          setHandlerNameInput('');
                                          setShowHandlerNameInput(false);
                                          toast(t('Recording handler "{{name}}" — perform the steps, then click End Handler', { name }));
                                        }
                                        if (e.key === 'Escape') setShowHandlerNameInput(false);
                                      }}
                                      placeholder="handler_name"
                                      autoFocus
                                      className="w-28 px-2 py-1.5 text-xs font-mono border border-zinc-300 rounded bg-white text-ink"
                                    />
                                    <button
                                      onClick={() => {
                                        if (!handlerNameInput.trim()) return;
                                        const name = handlerNameInput.trim().replace(/[^a-zA-Z0-9_-]/g, '_');
                                        setActiveHandlerName(name);
                                        setActiveHandlerStart(steps.length);
                                        setHandlerNameInput('');
                                        setShowHandlerNameInput(false);
                                      }}
                                      disabled={!handlerNameInput.trim()}
                                      className="px-2 py-1.5 bg-ink text-white rounded text-xs disabled:opacity-30"
                                    >
                                      {t('Start')}
                                    </button>
                                    <button
                                      onClick={() => { setShowHandlerNameInput(false); setHandlerNameInput(''); }}
                                      className="px-2 py-1.5 text-secondary hover:text-ink text-xs"
                                    >
                                      ✕
                                    </button>
                                  </div>
                                ) : (
                                  <button
                                    onClick={() => setShowHandlerNameInput(true)}
                                    className="px-2.5 py-1.5 bg-hover hover:bg-active text-secondary border border-border rounded-lg font-medium flex items-center gap-1.5 text-xs transition-colors"
                                  >
                                    <BoltIcon className="h-3.5 w-3.5" />
                                    {t('+ Handler')}
                                  </button>
                                )}
                              </>
                            )}
                          </div>
                        )}

                        {/* Extraction highlight overlay */}
                        {isExtracting && extractHighlight && canvasRef.current && (() => {
                          const canvas = canvasRef.current!;
                          const cRect = canvas.getBoundingClientRect();
                          const sx = cRect.width / 1280;
                          const sy = cRect.height / 800;
                          const r = extractHighlight.rect;
                          return (
                            <div
                              className="absolute pointer-events-none border-2 border-dashed border-ink/60 bg-ink/5 rounded"
                              style={{
                                left: r.x * sx,
                                top: r.y * sy,
                                width: r.w * sx,
                                height: r.h * sy,
                              }}
                            >
                              <span className="absolute -top-5 left-0 text-[10px] bg-ink text-white px-1 rounded whitespace-nowrap">
                                {extractHighlight.selector}
                              </span>
                            </div>
                          );
                        })()}

                        {/* Extraction confirmation popover */}
                        {showExtractPopover && extractElementInfo && (
                          <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 bg-surface border border-border rounded-xl shadow-2xl p-4 w-80 z-50">
                            <div className="text-sm text-ink font-medium mb-3 flex items-center gap-2">
                              <ArrowTopRightOnSquareIcon className="h-4 w-4 text-secondary" /> {t('Extract Element')}
                            </div>
                            <div className="space-y-2 text-xs">
                              <div>
                                <span className="text-secondary">{t('Selector:')}</span>
                                <code className="ml-1 text-ink bg-canvas px-1.5 py-0.5 rounded font-mono text-[10px]">{extractElementInfo.selector}</code>
                              </div>
                              <div>
                                <span className="text-secondary">{t('Tag:')}</span>
                                <span className="ml-1 text-secondary">&lt;{extractElementInfo.tag}&gt;</span>
                              </div>
                              {extractElementInfo.text && (
                                <div>
                                  <span className="text-secondary">{t('Text:')}</span>
                                  <span className="ml-1 text-secondary">{extractElementInfo.text.substring(0, 60)}</span>
                                </div>
                              )}
                              <div className="pt-2 border-t border-border">
                                <label className="text-secondary">{t('Output name:')}</label>
                                <input
                                  value={extractOutputName}
                                  onChange={(e) => setExtractOutputName(e.target.value)}
                                  className="mt-1 w-full px-2 py-1.5 bg-canvas border border-border rounded text-ink text-xs"
                                  placeholder={t('e.g., place_name')}
                                />
                              </div>
                              <div>
                                <label className="text-secondary">{t('Type:')}</label>
                                <div className="mt-1">
                                  <Select
                                    value={extractType}
                                    onChange={setExtractType}
                                    size="sm"
                                    options={[
                                      { value: 'text', label: t('Text content') },
                                      { value: 'attribute', label: t('Attribute') },
                                      { value: 'computed', label: t('Custom script') },
                                      { value: 'all_text', label: t('All matching (text array)') },
                                    ]}
                                  />
                                </div>
                              </div>
                            </div>
                            <div className="flex gap-2 mt-3">
                              <button
                                onClick={() => { setShowExtractPopover(false); setExtractElementInfo(null); }}
                                className="flex-1 px-3 py-1.5 bg-hover text-secondary rounded-lg text-xs hover:bg-chrome"
                              >
                                {t('Cancel')}
                              </button>
                              <button
                                onClick={confirmExtractStep}
                                className="flex-1 px-3 py-1.5 bg-accent-strong text-accent-on rounded-lg text-xs hover:bg-accent-strong/90 font-semibold shadow-sm"
                              >
                                {t('Add Step')}
                              </button>
                            </div>
                          </div>
                        )}

                        {/* Select Options Overlay */}
                        {selectOverlay && selectOverlay.show && canvasRef.current && (
                          <>
                            {/* Backdrop to close on click outside */}
                            <div
                              className="absolute inset-0 z-10"
                              onClick={closeSelectOverlay}
                            />
                            {/* Options dropdown */}
                            <div
                              className="absolute z-20 bg-white border border-gray-300 rounded-md shadow-lg overflow-hidden"
                              style={{
                                left: `${(selectOverlay.position.x / 1280) * canvasRef.current.getBoundingClientRect().width}px`,
                                top: `${(selectOverlay.position.y / 800) * canvasRef.current.getBoundingClientRect().height}px`,
                                minWidth: `${Math.max((selectOverlay.position.width / 1280) * canvasRef.current.getBoundingClientRect().width, 150)}px`,
                                maxHeight: '200px',
                              }}
                            >
                              <div className="bg-gray-100 px-3 py-1.5 text-xs font-medium text-tertiary border-b">
                                {selectOverlay.name || t('Select an option')}
                              </div>
                              <div className="overflow-y-auto max-h-[180px]">
                                {selectOverlay.options.map((option, idx) => (
                                  <button
                                    key={idx}
                                    onClick={() => handleSelectOption(option)}
                                    disabled={option.disabled}
                                    className={clsx(
                                      'w-full text-left px-3 py-2 text-sm transition-colors',
                                      option.disabled
                                        ? 'text-secondary cursor-not-allowed bg-gray-50'
                                        : option.selected
                                          ? 'bg-ink/10 text-ink hover:bg-ink/15'
                                          : 'text-gray-700 hover:bg-gray-100'
                                    )}
                                  >
                                    {option.text || option.value || t('Option {{n}}', { n: idx + 1 })}
                                  </button>
                                ))}
                              </div>
                            </div>
                          </>
                        )}

                        {/* Native Picker Overlay (date, time, color, etc.) */}
                        {pickerOverlay && pickerOverlay.show && canvasRef.current && (
                          <>
                            {/* Backdrop to close on click outside */}
                            <div
                              className="absolute inset-0 z-10"
                              onClick={closePickerOverlay}
                            />
                            {/* Picker container */}
                            <div
                              className="absolute z-20 bg-white border border-gray-300 rounded-lg shadow-lg overflow-hidden"
                              style={{
                                left: `${(pickerOverlay.position.x / 1280) * canvasRef.current.getBoundingClientRect().width}px`,
                                top: `${(pickerOverlay.position.y / 800) * canvasRef.current.getBoundingClientRect().height}px`,
                                minWidth: '200px',
                              }}
                            >
                              <div className="bg-gray-100 px-3 py-2 text-xs font-medium text-tertiary border-b flex items-center justify-between">
                                <span>
                                  {pickerOverlay.pickerType === 'date' && t('Select Date')}
                                  {pickerOverlay.pickerType === 'time' && t('Select Time')}
                                  {pickerOverlay.pickerType === 'datetime-local' && t('Select Date & Time')}
                                  {pickerOverlay.pickerType === 'month' && t('Select Month')}
                                  {pickerOverlay.pickerType === 'week' && t('Select Week')}
                                  {pickerOverlay.pickerType === 'color' && t('Select Color')}
                                </span>
                                <button
                                  onClick={closePickerOverlay}
                                  className="text-secondary hover:text-tertiary"
                                >
                                  ✕
                                </button>
                              </div>
                              <div className="p-3">
                                {/* Date Picker */}
                                {pickerOverlay.pickerType === 'date' && (
                                  <input
                                    type="date"
                                    defaultValue={pickerOverlay.currentValue}
                                    min={pickerOverlay.min}
                                    max={pickerOverlay.max}
                                    onChange={(e) => handlePickerValue(e.target.value)}
                                    className="w-full px-3 py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-ink focus:border-ink"
                                    autoFocus
                                  />
                                )}

                                {/* Time Picker */}
                                {pickerOverlay.pickerType === 'time' && (
                                  <input
                                    type="time"
                                    defaultValue={pickerOverlay.currentValue}
                                    min={pickerOverlay.min}
                                    max={pickerOverlay.max}
                                    step={pickerOverlay.step}
                                    onChange={(e) => handlePickerValue(e.target.value)}
                                    className="w-full px-3 py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-ink focus:border-ink"
                                    autoFocus
                                  />
                                )}

                                {/* DateTime Picker */}
                                {pickerOverlay.pickerType === 'datetime-local' && (
                                  <input
                                    type="datetime-local"
                                    defaultValue={pickerOverlay.currentValue}
                                    min={pickerOverlay.min}
                                    max={pickerOverlay.max}
                                    onChange={(e) => handlePickerValue(e.target.value)}
                                    className="w-full px-3 py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-ink focus:border-ink"
                                    autoFocus
                                  />
                                )}

                                {/* Month Picker */}
                                {pickerOverlay.pickerType === 'month' && (
                                  <input
                                    type="month"
                                    defaultValue={pickerOverlay.currentValue}
                                    min={pickerOverlay.min}
                                    max={pickerOverlay.max}
                                    onChange={(e) => handlePickerValue(e.target.value)}
                                    className="w-full px-3 py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-ink focus:border-ink"
                                    autoFocus
                                  />
                                )}

                                {/* Week Picker */}
                                {pickerOverlay.pickerType === 'week' && (
                                  <input
                                    type="week"
                                    defaultValue={pickerOverlay.currentValue}
                                    min={pickerOverlay.min}
                                    max={pickerOverlay.max}
                                    onChange={(e) => handlePickerValue(e.target.value)}
                                    className="w-full px-3 py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-ink focus:border-ink"
                                    autoFocus
                                  />
                                )}

                                {/* Color Picker */}
                                {pickerOverlay.pickerType === 'color' && (
                                  <div className="space-y-2">
                                    <input
                                      type="color"
                                      defaultValue={pickerOverlay.currentValue || '#000000'}
                                      onChange={(e) => handlePickerValue(e.target.value)}
                                      className="w-full h-12 cursor-pointer border border-gray-300 rounded-md"
                                      autoFocus
                                    />
                                    <input
                                      type="text"
                                      defaultValue={pickerOverlay.currentValue || '#000000'}
                                      placeholder="#000000"
                                      onChange={(e) => {
                                        if (/^#[0-9A-Fa-f]{6}$/.test(e.target.value)) {
                                          handlePickerValue(e.target.value);
                                        }
                                      }}
                                      className="w-full px-3 py-1 text-sm border border-gray-300 rounded-md font-mono"
                                    />
                                  </div>
                                )}
                              </div>
                            </div>
                          </>
                        )}
                      </div>
                    )}
                  </div>

                  {/* AI Extract Modal (overlay on canvas) */}
                  {showAIExtract && connectionState === 'recording' && (
                    <div className="absolute inset-0 bg-black/60 flex items-center justify-center z-50">
                      <div className="bg-surface border border-border rounded-xl shadow-2xl p-5 w-[460px] max-h-[88%] overflow-y-auto">
                        <div className="flex items-center gap-2 mb-4">
                          <SparklesIcon className="h-5 w-5 text-secondary" />
                          <span className="text-ink font-medium">{t('AI Extract Builder')}</span>
                        </div>
                        <p className="text-xs text-secondary mb-3">
                          {t('Describe the data you want to extract.')} <span className="text-ink font-medium">{t('Build extractor')}</span> {t('lets the AI drive the live browser — clicking into detail pages, paginating, testing on real data — then writes one reusable script. Its test actions are')} <span className="text-ink">{t('not')}</span> {t('recorded as steps.')}
                        </p>
                        <textarea
                          value={aiExtractGoal}
                          onChange={(e) => setAiExtractGoal(e.target.value)}
                          placeholder={t("e.g. Get every product in this category — open each item's detail page for the SKU and description, and go through all pages")}
                          rows={3}
                          disabled={aiExtractLoading || scraperRunning}
                          className="w-full px-3 py-2 bg-canvas border border-border rounded-lg text-sm text-ink placeholder:text-tertiary focus:ring-2 focus:ring-ink/10 resize-none disabled:opacity-50"
                          autoFocus
                          onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); runScraperBuilder(); } }}
                        />
                        <button
                          onClick={runScraperBuilder}
                          disabled={aiExtractLoading || scraperRunning || !aiExtractGoal.trim()}
                          className="w-full mt-3 px-3 py-2 bg-accent-strong hover:bg-accent-strong/90 text-accent-on rounded-lg text-sm font-semibold shadow-sm flex items-center justify-center gap-2 disabled:opacity-50 disabled:bg-hover disabled:text-tertiary"
                        >
                          {scraperRunning
                            ? <><ArrowPathIcon className="h-4 w-4 animate-spin" /> {t('Building — AI is driving the browser…')}</>
                            : <><SparklesIcon className="h-4 w-4" /> {t('Build extractor (AI drives the browser)')}</>}
                        </button>
                        <div className="flex gap-2 mt-2">
                          <button
                            onClick={() => { setShowAIExtract(false); setAiExtractGoal(''); setExtractDraft(null); setExtractTestResult(null); discardScraperDraft(); }}
                            disabled={aiExtractLoading || scraperRunning}
                            className="flex-1 px-3 py-2 bg-hover text-secondary rounded-lg text-sm hover:bg-chrome disabled:opacity-50"
                          >
                            {t('Cancel')}
                          </button>
                          {scraperRunning ? (
                            <button
                              onClick={stopScraperBuilder}
                              className="flex-1 px-3 py-2 bg-hover hover:bg-active border border-border text-secondary rounded-lg text-sm font-medium flex items-center justify-center gap-2"
                            >
                              <XMarkIcon className="h-4 w-4" /> {t('Stop')}
                            </button>
                          ) : (
                            <button
                              onClick={generateAIExtractSteps}
                              disabled={aiExtractLoading || !aiExtractGoal.trim()}
                              title={t('One-shot: generate a single-page extraction script without driving the browser')}
                              className="flex-1 px-3 py-2 bg-hover hover:bg-active border border-border text-secondary rounded-lg text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-50"
                            >
                              {aiExtractLoading
                                ? <><ArrowPathIcon className="h-4 w-4 animate-spin" /> {t('Generating…')}</>
                                : <>{extractDraft ? t('Regenerate') : t('Quick extract')}</>}
                            </button>
                          )}
                        </div>

                        {/* Live build log */}
                        {scraperLog.length > 0 && (
                          <div className="mt-4 border-t border-border pt-3">
                            <p className="text-[9px] text-tertiary font-medium uppercase mb-1.5">{t('Build log')}</p>
                            <div className="space-y-1 max-h-44 overflow-y-auto">
                              {scraperLog.map((l, i) => (
                                <div key={i} className={clsx(
                                  'text-[10px] leading-snug',
                                  l.kind === 'thought' && 'text-secondary',
                                  l.kind === 'run' && 'text-ink font-mono',
                                  l.kind === 'result' && 'text-ink/70 font-mono whitespace-pre-wrap break-all',
                                  l.kind === 'error' && 'text-secondary flex items-start gap-1',
                                  l.kind === 'done' && 'text-ink font-medium',
                                )}>
                                  {l.kind === 'run' && <span className="text-tertiary">▸ </span>}
                                  {l.kind === 'error' && <ExclamationTriangleIcon className="h-3 w-3 shrink-0 mt-0.5" />}
                                  {l.text}
                                </div>
                              ))}
                            </div>
                          </div>
                        )}

                        {/* Draft review — test the generated script before adding it */}
                        {extractDraft && (
                          <div className="mt-4 border-t border-border pt-3 space-y-2">
                            <p className="text-[11px] text-secondary">{extractDraft.message}</p>
                            <pre className="text-[10px] font-mono text-ink/80 bg-canvas border border-border rounded-lg p-2 max-h-28 overflow-auto whitespace-pre-wrap break-all">{extractDraft.script}</pre>
                            <div className="flex gap-2">
                              <button
                                onClick={testExtractDraft}
                                disabled={extractTestResult?.loading}
                                className="px-3 py-1.5 bg-hover hover:bg-active border border-border text-secondary rounded-lg text-xs font-medium flex items-center gap-1.5 disabled:opacity-50"
                              >
                                {extractTestResult?.loading
                                  ? <><ArrowPathIcon className="h-3.5 w-3.5 animate-spin" /> {t('Testing…')}</>
                                  : <><PlayIcon className="h-3.5 w-3.5" /> {t('Test live')}</>}
                              </button>
                              <button onClick={addExtractDraft} className="flex-1 px-3 py-1.5 bg-accent-strong hover:bg-accent-strong/90 text-accent-on rounded-lg text-xs font-semibold shadow-sm flex items-center justify-center gap-1.5">
                                <CheckIcon className="h-3.5 w-3.5" /> {t('Add as step')}
                              </button>
                              <button onClick={discardExtractDraft} className="px-3 py-1.5 bg-hover hover:bg-active border border-border text-secondary rounded-lg text-xs">
                                {t('Discard')}
                              </button>
                            </div>
                            {extractTestResult && !extractTestResult.loading && (
                              extractTestResult.error ? (
                                <div className="text-[10px] text-secondary flex items-start gap-1"><ExclamationTriangleIcon className="h-3.5 w-3.5 shrink-0 mt-0.5" /> {extractTestResult.error}</div>
                              ) : (
                                <pre className="text-[10px] font-mono text-ink/70 bg-hover/50 border border-border rounded-lg p-2 max-h-32 overflow-auto whitespace-pre-wrap break-all">{(() => { try { return JSON.stringify(extractTestResult.result, null, 2).slice(0, 800); } catch { return String(extractTestResult.result).slice(0, 800); } })()}</pre>
                              )
                            )}
                          </div>
                        )}

                        {/* Finished scraper review — test the full script before adding it */}
                        {scraperDraft && (
                          <div className="mt-4 border-t border-border pt-3 space-y-2">
                            <p className="text-[11px] text-secondary">{scraperDraft.summary}</p>
                            <div className="flex items-center gap-2 text-[10px] text-tertiary">
                              <span>{t('output:')} <span className="text-ink font-mono">{scraperDraft.variable}</span></span>
                              {scraperDraft.iframe && <span>{t('iframe:')} <span className="text-ink font-mono">{scraperDraft.iframe}</span></span>}
                            </div>
                            <pre className="text-[10px] font-mono text-ink/80 bg-canvas border border-border rounded-lg p-2 max-h-44 overflow-auto whitespace-pre-wrap break-all">{scraperDraft.script}</pre>
                            <div className="flex gap-2">
                              <button
                                onClick={testScraperDraft}
                                disabled={scraperTestResult?.loading}
                                className="px-3 py-1.5 bg-hover hover:bg-active border border-border text-secondary rounded-lg text-xs font-medium flex items-center gap-1.5 disabled:opacity-50"
                              >
                                {scraperTestResult?.loading
                                  ? <><ArrowPathIcon className="h-3.5 w-3.5 animate-spin" /> {t('Running…')}</>
                                  : <><PlayIcon className="h-3.5 w-3.5" /> {t('Test full run')}</>}
                              </button>
                              <button onClick={applyScraperDraft} className="flex-1 px-3 py-1.5 bg-accent-strong hover:bg-accent-strong/90 text-accent-on rounded-lg text-xs font-semibold shadow-sm flex items-center justify-center gap-1.5">
                                <CheckIcon className="h-3.5 w-3.5" /> {t('Add as step')}
                              </button>
                              <button onClick={discardScraperDraft} className="px-3 py-1.5 bg-hover hover:bg-active border border-border text-secondary rounded-lg text-xs">
                                {t('Discard')}
                              </button>
                            </div>
                            {scraperTestResult && !scraperTestResult.loading && (
                              scraperTestResult.error ? (
                                <div className="text-[10px] text-secondary flex items-start gap-1"><ExclamationTriangleIcon className="h-3.5 w-3.5 shrink-0 mt-0.5" /> {scraperTestResult.error}</div>
                              ) : (
                                <pre className="text-[10px] font-mono text-ink/70 bg-hover/50 border border-border rounded-lg p-2 max-h-40 overflow-auto whitespace-pre-wrap break-all">{(() => { try { return JSON.stringify(scraperTestResult.result, null, 2).slice(0, 1200); } catch { return String(scraperTestResult.result).slice(0, 1200); } })()}</pre>
                              )
                            )}
                          </div>
                        )}

                        <p className="text-[10px] text-tertiary mt-2 text-center">{t('Billed to your wallet')} &middot; {'⌘'}{t('+Enter to build')}</p>
                      </div>
                    </div>
                  )}

                  {/* Steps SPINE — hairline-collapsed by default; opens to a glass column
                      with a one-layer segmented switch (Steps / Requests / Functions).
                      Progressive disclosure: the Requests and Functions tabs only appear
                      when there is something to show, so an empty recording leaves the
                      stage maximal. */}
                  {(() => {
                    // What the spine can surface right now.
                    const requestCount = apiMode ? capturedApiRequests.length : detectedRequests.length;
                    const hasRequests = requestCount > 0 || serverRenderedNotices.length > 0;
                    const functionBuilderAvailable = steps.length >= 2 && !apiMode && !streamingMode;
                    const hasFunctions =
                      detectedSegments.length > 0 ||
                      (streamingMode && (streamingHandlers.length > 0 || true)) ||
                      functionBuilderAvailable;
                    // The spine has nothing to show at all → no rail, browser maximal.
                    // Monitor mode always keeps the rail (the Targets panel is essential).
                    const spineEmpty = !monitorMode && steps.length === 0 && !hasRequests && !hasFunctions;
                    // Keep the active tab valid as content appears/disappears. Monitor mode
                    // only ever shows Setup (steps) + Targets, so collapse anything else to Targets.
                    const activeTab: 'steps' | 'requests' | 'functions' | 'targets' = monitorMode
                      ? (spineTab === 'steps' ? 'steps' : 'targets')
                      : spineTab === 'targets' ? 'steps'
                      : spineTab === 'requests' && !hasRequests ? 'steps'
                      : spineTab === 'functions' && !hasFunctions ? 'steps'
                      : spineTab;

                    if (spineEmpty && !showSteps) {
                      // Nothing recorded yet and the rail is closed: render nothing so the
                      // browser stage owns the entire area (the rail floats now).
                      return null;
                    }

                    const tabs: Array<{ id: 'steps' | 'requests' | 'functions' | 'targets'; label: string; show: boolean; count?: number }> = monitorMode
                      ? [
                          { id: 'targets', label: t('Targets'), show: true, count: monitorTargetCount || undefined },
                          { id: 'steps', label: t('Setup'), show: true, count: steps.length || undefined },
                        ]
                      : [
                          { id: 'steps', label: t('Steps'), show: true, count: steps.length },
                          { id: 'requests', label: t('Requests'), show: hasRequests, count: requestCount || undefined },
                          { id: 'functions', label: t('Functions'), show: hasFunctions, count: (detectedSegments.length + streamingHandlers.length) || undefined },
                        ];

                    return (
                  <>
                    {/* Reopen pill — fades/scales in when the rail is collapsed. */}
                    <button
                      onClick={() => setShowSteps(true)}
                      title={t('Show steps')}
                      className={clsx(
                        "absolute top-3 right-3 z-30 flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-surface/85 backdrop-blur-xl border border-border shadow-lg text-tertiary hover:text-ink transition-all duration-300",
                        showSteps ? "opacity-0 scale-95 pointer-events-none" : "opacity-100 scale-100"
                      )}
                    >
                      <ChevronLeftIcon className="h-3.5 w-3.5" />
                      <span className="text-[11px] font-medium text-ink">{monitorMode ? t('Targets') : t('Steps')}</span>
                      {(monitorMode ? monitorTargetCount : steps.length) > 0 && (
                        <span className="text-[10px] font-bold text-ink bg-hover rounded-full min-w-[18px] h-[18px] px-1 flex items-center justify-center">{monitorMode ? monitorTargetCount : steps.length}</span>
                      )}
                    </button>

                    {/* Floating rail — a detached glass card (same language as the floating
                        URL bar) so the browser keeps the FULL width behind it. Slides off to
                        the right when collapsed instead of snapping, for a smooth transition. */}
                    <div className={clsx(
                      "absolute right-3 bottom-3 z-30 w-80 max-w-[calc(100%-1.5rem)] bg-surface/85 backdrop-blur-xl border border-border rounded-2xl shadow-2xl flex flex-col overflow-hidden transition-all duration-300 ease-out",
                      // Tuck below the top toolbar band so the centered URL / monitor toolbar
                      // (z-40) never sits on top of the panel. The monitor toolbar itself drops
                      // to top-16 while recording, so clear that lower position too.
                      monitorMode && connectionState === 'recording' ? 'top-28'
                        : (connectionState === 'recording' || monitorMode) ? 'top-16'
                        : 'top-3',
                      showSteps ? "translate-x-0 opacity-100" : "translate-x-[calc(100%+0.75rem)] opacity-0 pointer-events-none"
                    )}>
                    <>

                    {/* Spine header — collapse control + one-layer segmented switch */}
                    <div className="px-3 pt-3 pb-2 border-b border-border">
                      <div className="flex items-center gap-2 mb-2">
                        <button
                          onClick={() => setShowSteps(false)}
                          className="p-1 hover:bg-chrome rounded text-tertiary hover:text-ink transition-colors"
                          title={t('Collapse')}
                        >
                          <ChevronRightIcon className="h-3.5 w-3.5" />
                        </button>
                        <span className="text-xs font-medium text-ink">{monitorMode ? t('Monitor') : t('Recording')}</span>
                        {/* Add a step manually — Extract is a click-to-select mode on the
                            canvas; Return appends a return-data step. Replaces the old
                            toolbar Extract/Return: steps are added where they live.
                            Hidden in monitor mode (monitors watch, they don't extract/return). */}
                        {connectionState === 'recording' && !monitorMode && (
                          <div className="ml-auto flex items-center gap-0.5">
                            <button
                              onClick={() => {
                                setIsExtracting(!isExtracting);
                                if (isExtracting && wsRef.current) wsRef.current.send(JSON.stringify({ type: 'action', action: 'clear_highlight' }));
                                setExtractHighlight(null);
                                setShowExtractPopover(false);
                              }}
                              className={clsx(
                                'flex items-center gap-1 px-1.5 py-1 rounded text-[10px] font-medium transition-colors',
                                isExtracting ? 'bg-ink text-white' : 'text-secondary hover:bg-chrome hover:text-ink',
                              )}
                              title={t('Extract element — click an element on the page to add an extract step')}
                            >
                              <ArrowTopRightOnSquareIcon className="h-3.5 w-3.5" />
                              {t('Extract')}
                            </button>
                            <button
                              onClick={() => {
                                if (!wsRef.current) return;
                                wsRef.current.send(JSON.stringify({ type: 'action', action: 'add_extract_step', selector: '', output_name: '', extract_type: 'return', description: 'Return extracted data to caller' }));
                                toast.success(t('Return step added — workflow will return extracted data'));
                              }}
                              className="flex items-center gap-1 px-1.5 py-1 rounded text-[10px] font-medium text-secondary hover:bg-chrome hover:text-ink transition-colors"
                              title={t('Return data — append a step that returns the extracted data to the caller')}
                            >
                              <ArrowUturnLeftIcon className="h-3.5 w-3.5" />
                              {t('Return')}
                            </button>
                          </div>
                        )}
                      </div>
                      <div className="flex items-center gap-0.5 p-0.5 bg-hover/60 rounded-lg">
                        {tabs.filter(tb => tb.show).map(tb => (
                          <button
                            key={tb.id}
                            onClick={() => setSpineTab(tb.id)}
                            className={clsx(
                              'flex-1 flex items-center justify-center gap-1.5 px-2 py-1 rounded-md text-[11px] font-medium transition-colors',
                              activeTab === tb.id ? 'bg-surface text-ink shadow-sm' : 'text-secondary hover:text-ink',
                            )}
                          >
                            {tb.label}
                            {tb.count != null && tb.count > 0 && (
                              <span className={clsx(
                                'text-[9px] px-1 py-0.5 rounded-full tabular-nums',
                                activeTab === tb.id ? 'bg-hover text-secondary' : 'bg-surface/70 text-tertiary',
                              )}>{tb.count}</span>
                            )}
                          </button>
                        ))}
                      </div>
                    </div>

                    {/* === TARGETS TAB === (monitor check-target picker — the selectors panel
                        the embedder supplies, folded into the recorder's own rail) */}
                    {activeTab === 'targets' && (
                      <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
                        {monitorPanel}
                      </div>
                    )}

                    {/* === REQUESTS TAB === */}
                    {activeTab === 'requests' && (<>

                    {/* AI Assistant chat moved to the full-height left sidebar (see content row). */}

                    {/* Detected Requests — live API capture during normal recording.
                        Calm opt-in: a single "N API calls found" chip toggles the list. */}
                    {!apiMode && (detectedRequests.length > 0 || serverRenderedNotices.length > 0) && (
                      <div className="border-b border-border">
                        <button
                          onClick={() => setShowDetected(v => !v)}
                          className="w-full px-4 py-3 flex items-center justify-between hover:bg-chrome transition-colors"
                        >
                          <span className="flex items-center gap-2 text-[11px] text-secondary">
                            <LinkIcon className="h-4 w-4 text-tertiary" />
                            {detectedRequests.length === 1
                              ? t('{{n}} API call found', { n: detectedRequests.length })
                              : t('{{n}} API calls found', { n: detectedRequests.length })}
                          </span>
                          {showDetected ? <ChevronUpIcon className="h-4 w-4 text-tertiary" /> : <ChevronDownIcon className="h-4 w-4 text-tertiary" />}
                        </button>
                        {showDetected && (
                          <div className="px-3 pb-3 space-y-1.5 max-h-[60vh] overflow-y-auto">
                            {serverRenderedNotices.map((n, i) => (
                              <div key={`srn_${i}`} className="flex items-start gap-1.5 px-2 py-1.5 rounded bg-hover/40 border border-border">
                                <ExclamationTriangleIcon className="h-3.5 w-3.5 text-tertiary shrink-0 mt-0.5" />
                                <div className="min-w-0">
                                  <p className="text-[11px] text-secondary leading-tight">{n.message}</p>
                                  <p className="text-[10px] text-tertiary font-mono truncate" title={n.url}>{prettyPath(n.url)}</p>
                                </div>
                              </div>
                            ))}
                            {detectedRequests.length === 0 && serverRenderedNotices.length > 0 && (
                              <p className="text-[11px] text-tertiary text-center py-1">{t('No API calls detected yet')}</p>
                            )}
                            {detectedRequests.map((req) => {
                              const key = `${req.method} ${req.url}`;
                              const added = addedRequestKeys.has(key);
                              return (
                                <div
                                  key={req.id}
                                  className={clsx(
                                    'group flex items-center gap-2 px-2 py-1.5 rounded border transition',
                                    added ? 'border-border bg-ink/[0.03] opacity-70' : 'border-border hover:bg-chrome'
                                  )}
                                >
                                  <span className="text-[9px] font-bold font-mono px-1.5 py-0.5 rounded bg-hover text-secondary shrink-0">{req.method}</span>
                                  <div className="min-w-0 flex-1">
                                    <p className="text-[11px] text-ink font-mono truncate" title={req.url}>{prettyPath(req.url)}</p>
                                    <p className="text-[10px] text-tertiary">
                                      {req.response_status ?? '—'}
                                      {req.response_content_type ? ` · ${req.response_content_type.split(';')[0]}` : ''}
                                    </p>
                                  </div>
                                  {added ? (
                                    <span className="flex items-center gap-1 text-[10px] text-secondary shrink-0"><CheckIcon className="h-3.5 w-3.5" /> {t('Added')}</span>
                                  ) : (
                                    <button
                                      onClick={() => addRequestAsStep(req)}
                                      className="shrink-0 flex items-center gap-1 px-2 py-1 text-[10px] font-medium text-ink bg-hover hover:bg-ink hover:text-white rounded transition"
                                      title={t('Add as workflow step')}
                                    >
                                      <PlusIcon className="h-3 w-3" /> {t('Step')}
                                    </button>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    )}

                    </>)}

                    {/* === STEPS TAB === (the recorded / AI step timeline — default) */}
                    {activeTab === 'steps' && (
                    <div className="flex-1 min-h-0 flex flex-col">
                      {/* Visual-replay toolbar: click a step to re-run up to it live. */}
                      {displaySteps.length > 0 && !apiMode && (
                        <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-surface/60 shrink-0">
                          {replayState.running ? (
                            <>
                              <ArrowPathIcon className="h-3.5 w-3.5 text-amber-600 animate-spin shrink-0" />
                              <span className="text-[11px] text-secondary truncate">
                                {replayState.current !== null
                                  ? t('Replaying step {{n}} of {{total}}…', { n: (replayState.current ?? 0) + 1, total: (replayState.target ?? 0) + 1 })
                                  : t('Replaying…')}
                              </span>
                              <button
                                onClick={cancelReplay}
                                className="ml-auto flex items-center gap-1 px-2 py-1 text-[10px] font-medium text-ink bg-hover hover:bg-ink hover:text-white rounded transition"
                              >
                                <StopIcon className="h-3 w-3" /> {t('Stop')}
                              </button>
                            </>
                          ) : (
                            <>
                              <EyeIcon className="h-3.5 w-3.5 text-tertiary shrink-0" />
                              <span className="text-[11px] text-tertiary truncate">
                                {t('Click a step to replay up to it in the live browser')}
                              </span>
                              <button
                                onClick={() => replayToStep(steps.length - 1)}
                                className="ml-auto flex items-center gap-1 px-2 py-1 text-[10px] font-medium text-ink bg-hover hover:bg-ink hover:text-white rounded transition"
                                title={t('Replay the whole workflow in the live browser')}
                              >
                                <PlayIcon className="h-3 w-3" /> {t('Replay all')}
                              </button>
                            </>
                          )}
                        </div>
                      )}
                      <div className="flex-1 overflow-y-auto px-3 py-3">
                        {displaySteps.length === 0 ? (
                          <div className="text-center py-10">
                            <div className="w-10 h-10 rounded-full bg-hover border border-border flex items-center justify-center mx-auto mb-3">
                              <CursorArrowRaysIcon className="h-5 w-5 text-tertiary" />
                            </div>
                            <p className="text-xs text-secondary">{t('Interact with the browser')}</p>
                            <p className="text-[11px] text-tertiary mt-0.5">{t('Steps will appear here')}</p>
                          </div>
                        ) : (() => {
                          // Pre-compute function membership per step
                          const segForStep = new Map<number, typeof detectedSegments[0]>();
                          detectedSegments.forEach(seg => {
                            seg.step_indices.forEach(idx => segForStep.set(idx, seg));
                          });

                          // Group display steps into runs: either a function group or ungrouped steps
                          type Run = { type: 'steps'; items: typeof displaySteps } | { type: 'function'; seg: typeof detectedSegments[0]; items: typeof displaySteps };
                          const runs: Run[] = [];
                          let i = 0;
                          while (i < displaySteps.length) {
                            const seg = segForStep.get(displaySteps[i].index);
                            if (seg) {
                              const fnItems: typeof displaySteps = [];
                              while (i < displaySteps.length && segForStep.get(displaySteps[i].index) === seg) {
                                fnItems.push(displaySteps[i]);
                                i++;
                              }
                              runs.push({ type: 'function', seg, items: fnItems });
                            } else {
                              const plain: typeof displaySteps = [];
                              while (i < displaySteps.length && !segForStep.has(displaySteps[i].index)) {
                                plain.push(displaySteps[i]);
                                i++;
                              }
                              runs.push({ type: 'steps', items: plain });
                            }
                          }

                          // Render a single step row
                          const renderStepRow = (step: typeof displaySteps[0], arrIdx: number, isLastGlobal: boolean) => {
                            const StepIcon = step.IconComponent;
                            const handlerMatch = streamingMode
                              ? streamingHandlers.find(h => step.index >= h.step_range[0] && step.index < h.step_range[1])
                              : null;
                            const handlerStart = streamingMode
                              ? streamingHandlers.find(h => step.index === h.step_range[0])
                              : null;
                            // Visual-replay cursor state for this step.
                            const replayStatus = replayState.statuses[step.index];
                            const replaySS = replayStatus
                              ? statusStyle(replayStatus === 'done' ? 'completed' : replayStatus)
                              : null;
                            const isReplayCursor = replayState.current === step.index;
                            // Clicking a step re-runs everything up to it on the live page —
                            // disabled while building functions, editing, in API mode, or mid-replay.
                            const canReplay = !functionBuilderOpen && !editingStepId && !apiMode && !replayState.running;

                            return (
                              <React.Fragment key={step.id}>
                                {handlerStart && (
                                  <div className="flex items-center gap-1.5 pl-8 py-1.5 mb-1">
                                    <div className="h-px flex-1 bg-border" />
                                    <span className="text-[9px] font-bold text-secondary uppercase tracking-wider flex items-center gap-1">
                                      <BoltIcon className="h-2.5 w-2.5" />
                                      {handlerStart.name}
                                    </span>
                                    <div className="h-px flex-1 bg-border" />
                                  </div>
                                )}
                                {step.isTabBoundary && (step.type === 'wait_for_tab' || step.type === 'open_tab') && (
                                  <div className="flex items-center gap-1.5 pl-8 py-1 mb-1">
                                    <div className="h-px flex-1 bg-border" />
                                    <span className="text-[9px] uppercase tracking-wider text-tertiary">
                                      {step.type === 'open_tab' ? t('New tab (you open it)') : t('New tab (opened by site)')}
                                    </span>
                                    <div className="h-px flex-1 bg-border" />
                                  </div>
                                )}
                                <div
                                  className={clsx(
                                    "group relative flex items-start gap-3 animate-step-enter rounded-lg",
                                    !isLastGlobal && "pb-2.5",
                                    step.isInTab && "ml-5",
                                    (functionBuilderOpen || canReplay) && "cursor-pointer",
                                    functionBuilderOpen && selectedStepIndices.has(step.index) && "ring-2 ring-ink/20 bg-ink/[0.03]",
                                    isReplayCursor && "ring-2 ring-amber-400/60 bg-amber-50/50",
                                  )}
                                  style={{ animationDelay: `${Math.min(arrIdx * 30, 300)}ms` }}
                                  onClick={
                                    functionBuilderOpen
                                      ? () => toggleStepSelection(step.index)
                                      : canReplay
                                        ? () => replayToStep(step.index)
                                        : undefined
                                  }
                                  title={canReplay ? t('Replay to here — re-run steps up to this point in the live browser') : undefined}
                                >
                                  {/* Timeline connector */}
                                  {!isLastGlobal && (
                                    <div className="absolute w-px bg-border top-7 bottom-0 left-[13px]" />
                                  )}

                                  {/* Node */}
                                  <div className={clsx(
                                    "relative z-10 flex-shrink-0 w-[26px] h-[26px] rounded-full flex items-center justify-center border transition-all",
                                    replayStatus === 'running' && "ring-2 ring-amber-400 animate-pulse",
                                    functionBuilderOpen && selectedStepIndices.has(step.index)
                                      ? "bg-ink text-white border-ink scale-110"
                                      : functionBuilderOpen
                                        ? "bg-hover border-border text-secondary hover:border-ink/40"
                                        : handlerMatch
                                          ? "bg-ink text-white border-ink"
                                          : step.isTabBoundary
                                            ? "bg-hover border-border text-secondary"
                                            : "bg-ink text-white border-ink",
                                  )}>
                                    {replayStatus === 'running'
                                      ? <ArrowPathIcon className="h-3 w-3 animate-spin" />
                                      : functionBuilderOpen && selectedStepIndices.has(step.index)
                                        ? <CheckIcon className="h-3 w-3" />
                                        : <StepIcon className="h-3 w-3" />
                                    }
                                    {/* Replay outcome dot (done / skipped / failed) */}
                                    {replaySS && replayStatus !== 'running' && (
                                      <span
                                        className={clsx('absolute -top-0.5 -right-0.5 h-2.5 w-2.5 rounded-full ring-2 ring-surface', replaySS.dot)}
                                        title={t('Replay: {{status}}', { status: replayStatus })}
                                      />
                                    )}
                                  </div>

                                  {/* Content */}
                                  <div className="flex-1 min-w-0 pt-0.5 rounded-lg px-2 py-1.5 -ml-0.5 transition-colors hover:bg-chrome">
                                    <div className="flex items-center gap-1.5">
                                      <span className="text-[10px] font-medium text-tertiary tabular-nums w-4 shrink-0">
                                        {step.index + 1}
                                      </span>
                                      <span className="text-xs font-medium text-ink truncate">
                                        {step.title}
                                      </span>
                                      {step.isSensitive && (
                                        <span className="px-1.5 py-0.5 text-[9px] bg-ink/8 text-secondary rounded-full font-medium">{t('SENSITIVE')}</span>
                                      )}
                                      {step.isFromPicker && (
                                        <span className="px-1.5 py-0.5 text-[9px] bg-ink/8 text-secondary rounded-full font-medium">{t('PICKER')}</span>
                                      )}
                                      {step.isFromAutocomplete && (
                                        <span className="px-1.5 py-0.5 text-[9px] bg-ink/8 text-secondary rounded-full font-medium">{t('AUTO')}</span>
                                      )}
                                      {step.isFromCustomDropdown && (
                                        <span className="px-1.5 py-0.5 text-[9px] bg-ink/8 text-secondary rounded-full font-medium">{t('CUSTOM')}</span>
                                      )}
                                      {step.isViaKeyboard && (
                                        <span className="px-1.5 py-0.5 text-[9px] bg-ink/8 text-secondary rounded-full font-medium">{t('KB')}</span>
                                      )}
                                    </div>
                                    {step.description && (
                                      <p className="text-[11px] text-secondary truncate mt-0.5 pl-[22px]">{step.description}</p>
                                    )}
                                    {step.value && (
                                      <div className="text-[11px] text-ink/70 truncate flex items-center gap-1 mt-0.5 pl-[22px]">
                                        <span className="text-tertiary shrink-0">&rarr;</span>
                                        {step.isSensitive ? (
                                          <span className="text-secondary font-mono">••••••••</span>
                                        ) : (
                                          <span className="font-mono">{renderValueWithTags(step.value)}</span>
                                        )}
                                      </div>
                                    )}
                                    {step.selector && (
                                      <code className="text-[10px] text-tertiary block truncate mt-0.5 pl-[22px] font-mono">{step.selector}</code>
                                    )}
                                    {step.script && scriptTests[step.id] && (
                                      <div className="mt-1 pl-[22px]">
                                        {scriptTests[step.id].loading ? (
                                          <span className="text-[10px] text-tertiary flex items-center gap-1"><ArrowPathIcon className="h-3 w-3 animate-spin" /> {t('Testing…')}</span>
                                        ) : scriptTests[step.id].error ? (
                                          <span className="text-[10px] text-secondary flex items-center gap-1" title={scriptTests[step.id].error}><ExclamationTriangleIcon className="h-3 w-3" /> {scriptTests[step.id].error}</span>
                                        ) : (
                                          <code className="text-[10px] text-ink/70 font-mono block max-h-20 overflow-y-auto whitespace-pre-wrap break-all bg-hover/50 rounded px-1.5 py-1 border border-border">
                                            {(() => { try { return JSON.stringify(scriptTests[step.id].result, null, 0).slice(0, 500); } catch { return String(scriptTests[step.id].result).slice(0, 500); } })()}
                                          </code>
                                        )}
                                      </div>
                                    )}
                                  </div>

                                  {/* Actions */}
                                  <div
                                    className={clsx(
                                      "flex flex-col gap-0.5 transition-opacity pt-0.5",
                                      functionBuilderOpen ? "hidden" : "opacity-0 group-hover:opacity-100",
                                    )}
                                    onClick={e => e.stopPropagation()}
                                  >
                                    <button
                                      onClick={() => setEditingStepId(prev => (prev === step.id ? null : step.id))}
                                      className={clsx('p-1 hover:bg-chrome rounded', editingStepId === step.id ? 'text-ink' : 'text-secondary hover:text-ink')}
                                      title={t('Edit step')}
                                    >
                                      <PencilSquareIcon className="h-3 w-3" />
                                    </button>
                                    <button onClick={() => moveStep(step.index, 'up')} disabled={step.index === 0} className="p-1 hover:bg-chrome rounded disabled:opacity-30">
                                      <ChevronUpIcon className="h-3 w-3 text-secondary" />
                                    </button>
                                    <button onClick={() => moveStep(step.index, 'down')} disabled={step.index === displaySteps.length - 1} className="p-1 hover:bg-chrome rounded disabled:opacity-30">
                                      <ChevronDownIcon className="h-3 w-3 text-secondary" />
                                    </button>
                                    {step.type === 'extract' && connectionState === 'recording' && wsRef.current && (
                                      <button
                                        onClick={() => {
                                          if (!wsRef.current) return;
                                          const rawStep = steps[step.index];
                                          wsRef.current.send(JSON.stringify({
                                            type: 'action', action: 'test_extract',
                                            selector: rawStep?.selector || '',
                                            extract_type: rawStep?.options?.extract_type || 'text',
                                            output_name: rawStep?.options?.output_name || 'test',
                                          }));
                                          toast(t('Testing extraction...'), { duration: 1500 });
                                        }}
                                        className="p-1 hover:bg-chrome rounded text-secondary"
                                        title={t('Test this extraction')}
                                      >
                                        <PlayIcon className="h-3 w-3" />
                                      </button>
                                    )}
                                    {step.script && connectionState === 'recording' && (
                                      <button
                                        onClick={() => runScriptTest(step.id, step.script!)}
                                        disabled={scriptTests[step.id]?.loading}
                                        className="p-1 hover:bg-chrome rounded text-secondary disabled:opacity-40"
                                        title={t('Fast test — run this script live')}
                                      >
                                        <PlayIcon className="h-3 w-3" />
                                      </button>
                                    )}
                                    <button onClick={() => deleteStep(step.index)} className="p-1 hover:bg-chrome rounded text-secondary hover:text-ink">
                                      <TrashIcon className="h-3 w-3" />
                                    </button>
                                  </div>
                                </div>
                                {editingStepId === step.id && steps[step.index] && (
                                  <StepBuilderDialog
                                    open
                                    onClose={() => setEditingStepId(null)}
                                    title={t('Edit step')}
                                    hidden={pickActive}
                                  >
                                    <StepFormPanel
                                      step={steps[step.index] as WorkflowStep}
                                      onChange={updates => updateStepAt(step.index, updates)}
                                      onConfirm={() => setEditingStepId(null)}
                                      onCancel={() => setEditingStepId(null)}
                                      confirmLabel={t('Done')}
                                      onPickSelector={startPickSelector}
                                    />
                                  </StepBuilderDialog>
                                )}
                              </React.Fragment>
                            );
                          };

                          return (
                          <div className="relative space-y-0">
                            {runs.map((run, runIdx) => {
                              if (run.type === 'function') {
                                // Wrapped function group
                                return (
                                  <div key={`fn-${run.seg.name}-${runIdx}`} className="relative my-2 first:mt-0 last:mb-0">
                                    {/* Function container */}
                                    <div className="rounded-lg border border-ink/10 bg-ink/[0.02] overflow-hidden">
                                      {/* Function label bar */}
                                      <div className="flex items-center gap-2 px-3 py-1.5 bg-ink/[0.04] border-b border-ink/8">
                                        <BoltIcon className="h-3 w-3 text-ink/60" />
                                        <span className="text-[11px] font-mono font-medium text-ink">{run.seg.name}</span>
                                        <span className="text-[9px] text-tertiary ml-auto">{run.items.length === 1 ? t('{{n}} step', { n: run.items.length }) : t('{{n}} steps', { n: run.items.length })}</span>
                                      </div>
                                      {/* Steps inside function */}
                                      <div className="px-2 py-2">
                                        {run.items.map((step, idx) => renderStepRow(step, step.index, idx === run.items.length - 1))}
                                      </div>
                                    </div>
                                    {/* Connector between function block and next section */}
                                    {runIdx < runs.length - 1 && (
                                      <div className="flex justify-center py-1">
                                        <div className="w-px h-3 bg-border" />
                                      </div>
                                    )}
                                  </div>
                                );
                              }
                              // Plain steps (no function)
                              return (
                                <div key={`steps-${runIdx}`}>
                                  {run.items.map((step, idx) => {
                                    const globalLast = runIdx === runs.length - 1 && idx === run.items.length - 1;
                                    return renderStepRow(step, step.index, globalLast);
                                  })}
                                  {/* Connector to next function block */}
                                  {runIdx < runs.length - 1 && runs[runIdx + 1].type === 'function' && (
                                    <div className="flex justify-center py-1">
                                      <div className="w-px h-3 bg-border" />
                                    </div>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                          );
                        })()}
                      </div>

                    {/* Manual Step Add */}
                    {connectionState === 'recording' && (
                      <div className="border-t border-border p-2">
                        <ManualStepAdder
                          onAddStep={(step) => {
                            setSteps(prev => [...prev, step]);
                            toast.success(t('Added: {{type}} step', { type: step.type }));
                          }}
                          onPickSelector={startPickSelector}
                          pickActive={pickActive}
                        />
                      </div>
                    )}

                    {/* Function Builder — manual recordings only. In streaming mode the
                        callable surface is handlers / advanced-script (step-groups get
                        replaced by the streaming config on save), and apiMode captures
                        API calls, so the builder is hidden in both. */}
                    {steps.length >= 2 && !apiMode && !streamingMode && (
                      <div className="border-t border-border">
                        {functionBuilderOpen ? (
                          <div className="animate-overlay-enter">
                            {/* Header */}
                            <div className="px-4 py-2.5 bg-ink/[0.03] border-b border-border flex items-center justify-between">
                              <div className="flex items-center gap-2">
                                <div className="w-5 h-5 rounded-full bg-ink text-white flex items-center justify-center">
                                  <BoltIcon className="h-3 w-3" />
                                </div>
                                <span className="text-xs font-medium text-ink">{t('Create Function')}</span>
                              </div>
                              <button
                                onClick={cancelFunctionBuilder}
                                className="p-1 hover:bg-chrome rounded"
                              >
                                <XMarkIcon className="h-3.5 w-3.5 text-secondary" />
                              </button>
                            </div>

                            {/* Instructions */}
                            <div className="px-4 py-2">
                              <p className="text-[11px] text-secondary">
                                {t('Click steps above to select them, then name your function.')}
                              </p>
                            </div>

                            {/* Selected steps preview */}
                            {selectedStepIndices.size > 0 && (
                              <div className="px-4 pb-2 animate-step-enter">
                                <div className="flex flex-wrap gap-1">
                                  {Array.from(selectedStepIndices).sort((a, b) => a - b).map(idx => {
                                    const ds = displaySteps.find(s => s.index === idx);
                                    if (!ds) return null;
                                    const Icon = ds.IconComponent;
                                    return (
                                      <span
                                        key={idx}
                                        className="inline-flex items-center gap-1 px-2 py-1 bg-ink/8 rounded-md text-[10px] text-ink font-medium animate-step-enter cursor-pointer hover:bg-ink/15 transition-colors"
                                        onClick={() => toggleStepSelection(idx)}
                                      >
                                        <Icon className="h-2.5 w-2.5 text-secondary" />
                                        {idx + 1}. {ds.title}
                                        <XMarkIcon className="h-2.5 w-2.5 text-tertiary ml-0.5" />
                                      </span>
                                    );
                                  })}
                                </div>
                              </div>
                            )}

                            {/* Name input + create */}
                            <div className="px-4 pb-3 space-y-2">
                              <input
                                type="text"
                                value={functionName}
                                onChange={(e) => setFunctionName(e.target.value)}
                                placeholder={t('Function name...')}
                                className="w-full px-3 py-2 bg-canvas border border-border rounded-lg text-xs text-ink placeholder:text-tertiary focus:ring-2 focus:ring-ink/10 focus:border-ink/30 transition-all"
                                autoFocus
                                onKeyDown={(e) => { if (e.key === 'Enter') createFunction(); }}
                              />
                              <div className="flex gap-2">
                                <button
                                  onClick={cancelFunctionBuilder}
                                  className="flex-1 px-3 py-2 bg-hover hover:bg-active text-secondary rounded-lg text-xs font-medium transition-colors"
                                >
                                  {t('Cancel')}
                                </button>
                                <button
                                  onClick={createFunction}
                                  disabled={!functionName.trim() || selectedStepIndices.size === 0}
                                  className="flex-1 px-3 py-2 bg-accent-strong hover:bg-accent-strong/90 text-accent-on rounded-lg text-xs font-semibold shadow-sm flex items-center justify-center gap-1.5 disabled:opacity-30 disabled:bg-hover disabled:text-tertiary transition-colors"
                                >
                                  <BoltIcon className="h-3 w-3" />
                                  {t('Create ({{n}})', { n: selectedStepIndices.size })}
                                </button>
                              </div>
                            </div>
                          </div>
                        ) : (
                          <div className="p-3">
                            <button
                              onClick={() => { setFunctionBuilderOpen(true); triggerMini('functions'); }}
                              className="w-full px-3 py-2 border border-dashed border-border rounded-lg text-secondary hover:text-ink hover:border-ink/30 text-xs flex items-center justify-center gap-1.5 transition-colors"
                            >
                              <BoltIcon className="h-3.5 w-3.5" />
                              {t('Group Steps into Function')}
                            </button>
                          </div>
                        )}
                      </div>
                    )}
                    </div>
                    )}
                    {/* === END STEPS TAB === */}

                    {/* Offer to capture the recorded login as a persona so runs
                        sign in (and pass 2FA) unattended. Shows once a login or a
                        2FA step is detected. */}
                    {activeTab === 'steps' && (() => {
                      const hasLogin =
                        detectedCredentials.length > 0 || detected2fa || steps.some((s) => s.type === 'twofa');
                      if (!hasLogin || personaPromptDismissed) return null;
                      return (
                        <div className="border-t border-border bg-hover/20">
                          <div className="px-4 py-3 space-y-2">
                            <div className="flex items-start gap-2">
                              <ShieldCheckIcon className="h-4 w-4 text-secondary mt-0.5 shrink-0" />
                              <div className="flex-1 min-w-0">
                                <div className="text-xs text-ink font-medium">{t('Run this unattended')}</div>
                                <p className="text-[10px] text-tertiary mt-0.5">
                                  {detected2fa
                                    ? t('This login uses 2FA. A persona signs in and enters codes automatically on every run.')
                                    : t('Save a persona so future runs sign in automatically.')}
                                </p>
                              </div>
                              <button
                                onClick={() => setPersonaPromptDismissed(true)}
                                className="text-tertiary hover:text-ink shrink-0"
                                title={t('Dismiss')}
                              >
                                <XMarkIcon className="h-3.5 w-3.5" />
                              </button>
                            </div>
                            <div className="flex items-center gap-2 pl-6">
                              <button
                                onClick={() => setShowPersonaWizard(true)}
                                className="flex items-center gap-1.5 px-2.5 py-1 bg-accent-strong text-accent-on text-[11px] font-semibold shadow-sm rounded-lg hover:bg-accent-strong/90 transition-colors"
                              >
                                <PlusIcon className="h-3.5 w-3.5" /> {t('Create persona')}
                              </button>
                              {detected2fa && (
                                <button
                                  onClick={() => setShowAuthImport(true)}
                                  className="flex items-center gap-1.5 px-2.5 py-1 bg-surface text-ink border border-border text-[11px] font-medium rounded-lg hover:bg-chrome transition-colors"
                                >
                                  <QrCodeIcon className="h-3.5 w-3.5" /> {t('Import 2FA secret')}
                                </button>
                              )}
                            </div>
                          </div>
                        </div>
                      );
                    })()}

                    {/* Detected Data Section */}
                    {activeTab === 'steps' && detectedCredentials.length > 0 && (
                      <div className="border-t border-border">
                        <div className="px-4 py-2.5 border-b border-border bg-hover/40">
                          <div className="flex items-center gap-2">
                            <KeyIcon className="h-4 w-4 text-secondary" />
                            <span className="text-ink font-medium text-xs">
                              {t('Credentials ({{n}})', { n: detectedCredentials.length })}
                            </span>
                          </div>
                          <p className="text-[10px] text-tertiary mt-0.5 pl-6">
                            {t('Stored securely, required at runtime')}
                          </p>
                        </div>
                        <div className="p-2 space-y-1 max-h-40 overflow-y-auto">
                          {detectedCredentials.map((cred, idx) => (
                            <div
                              key={idx}
                              className="flex items-center gap-2 p-2 bg-hover/30 rounded-lg"
                            >
                              <KeyIcon className="h-3.5 w-3.5 text-secondary shrink-0" />
                              <div className="flex-1 min-w-0">
                                <div className="text-xs text-ink font-medium">
                                  {cred.field_name}
                                </div>
                                <div className="text-[10px] text-tertiary">
                                  {cred.field_type}
                                </div>
                              </div>
                              <span className="text-[10px] text-tertiary font-mono">
                                ••••••••
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Workflow Summary */}
                    {activeTab === 'steps' && steps.length > 0 && (
                      <div className="border-t border-border">
                        <div className="px-4 py-2.5 border-b border-border bg-hover/40">
                          <div className="flex items-center gap-2">
                            <ListBulletIcon className="h-4 w-4 text-secondary" />
                            <span className="text-ink font-medium text-xs">{t('Summary')}</span>
                          </div>
                        </div>
                        <div className="px-4 py-2.5 space-y-1.5 text-[11px]">
                          <div className="flex justify-between text-secondary">
                            <span>{t('Steps')}</span>
                            <span className="text-ink font-medium tabular-nums">{steps.length}</span>
                          </div>
                          {steps.filter(s => s.type === 'fill').length > 0 && (
                            <div className="flex justify-between text-secondary">
                              <span>{t('Inputs')}</span>
                              <span className="text-ink tabular-nums">{steps.filter(s => s.type === 'fill').length}</span>
                            </div>
                          )}
                          {steps.filter(s => s.type === 'click').length > 0 && (
                            <div className="flex justify-between text-secondary">
                              <span>{t('Clicks')}</span>
                              <span className="text-ink tabular-nums">{steps.filter(s => s.type === 'click').length}</span>
                            </div>
                          )}
                          {steps.filter(s => s.type === 'extract').length > 0 && (
                            <div className="flex justify-between text-secondary">
                              <span>{t('Extractions')}</span>
                              <span className="text-ink tabular-nums">{steps.filter(s => s.type === 'extract').length}</span>
                            </div>
                          )}
                          {detectedCredentials.length > 0 && (
                            <div className="flex justify-between text-secondary">
                              <span>{t('Credentials')}</span>
                              <span className="text-ink tabular-nums">{detectedCredentials.length}</span>
                            </div>
                          )}
                          {detectedSegments.length > 0 && (
                            <div className="flex justify-between text-secondary">
                              <span>{t('Functions')}</span>
                              <span className="text-ink tabular-nums">{detectedSegments.length}</span>
                            </div>
                          )}
                        </div>
                      </div>
                    )}

                    {/* === FUNCTIONS TAB === grouped segments + streaming handlers */}
                    {/* Functions / Segments */}
                    {activeTab === 'functions' && detectedSegments.length > 0 && connectionState !== 'recording' && (
                      <div className="border-t border-border">
                        <div className="px-4 py-2.5 border-b border-border bg-hover/40">
                          <div className="flex items-center gap-2">
                            <BoltIcon className="h-4 w-4 text-secondary" />
                            <span className="text-ink font-medium text-xs">
                              {t('Functions ({{n}})', { n: detectedSegments.length })}
                            </span>
                          </div>
                        </div>
                        <div className="p-2 space-y-1">
                          {detectedSegments.map((seg, i) => (
                            <div key={i} className="px-2.5 py-2 bg-hover/30 rounded-lg text-xs group animate-step-enter" style={{ animationDelay: `${i * 50}ms` }}>
                              <div className="flex items-center gap-2">
                                <span className="px-1.5 py-0.5 rounded text-[9px] font-medium bg-ink/8 text-secondary uppercase">
                                  {seg.segment_type}
                                </span>
                                <span className="text-ink font-medium truncate font-mono text-[11px]">{seg.name}</span>
                                <span className="text-tertiary ml-auto shrink-0 text-[10px] tabular-nums">{seg.step_indices.length === 1 ? t('{{n}} step', { n: seg.step_indices.length }) : t('{{n}} steps', { n: seg.step_indices.length })}</span>
                                <button
                                  onClick={() => setDetectedSegments(prev => prev.filter((_, j) => j !== i))}
                                  className="p-0.5 opacity-0 group-hover:opacity-100 hover:bg-chrome rounded transition-opacity"
                                >
                                  <TrashIcon className="h-3 w-3 text-tertiary" />
                                </button>
                              </div>
                              {seg.extract_outputs.length > 0 && (
                                <div className="text-[10px] text-secondary mt-1 pl-0.5 font-mono">
                                  {t('returns:')} {seg.extract_outputs.join(', ')}
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Streaming: Handler Groups */}
                    {activeTab === 'functions' && streamingMode && streamingHandlers.length > 0 && (
                      <div className="border-t border-border">
                        <div className="px-4 py-2.5 border-b border-border bg-hover/40">
                          <div className="flex items-center gap-2">
                            <BoltIcon className="h-4 w-4 text-secondary" />
                            <span className="text-ink font-medium text-xs">{t('Handlers ({{n}})', { n: streamingHandlers.length })}</span>
                          </div>
                        </div>
                        <div className="p-2 space-y-1">
                          {streamingHandlers.map((h, i) => (
                            <div key={h.name} className="rounded-lg border border-border overflow-hidden">
                              <div
                                className="flex items-center justify-between px-2.5 py-1.5 bg-canvas cursor-pointer hover:bg-chrome text-xs"
                                onClick={() => setExpandedHandlerId(expandedHandlerId === h.name ? null : h.name)}
                              >
                                <div className="flex items-center gap-1.5">
                                  <code className="text-[11px] font-mono font-medium text-ink">{h.name}</code>
                                  <span className="text-[10px] text-secondary bg-zinc-200/60 px-1 py-0.5 rounded">
                                    {h.step_range[0]}–{h.step_range[1]}
                                  </span>
                                </div>
                                <button
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setStreamingHandlers(prev => prev.filter((_, j) => j !== i));
                                  }}
                                  className="p-0.5 text-tertiary hover:text-ink"
                                >
                                  <TrashIcon className="w-3 h-3" />
                                </button>
                              </div>
                              {expandedHandlerId === h.name && (
                                <div className="px-2.5 py-2 space-y-2 border-t border-border bg-white text-xs">
                                  <div className="flex gap-1.5 items-center">
                                    <label className="text-[10px] text-secondary w-12 shrink-0">{t('Steps:')}</label>
                                    <div className="w-16">
                                      <NumberInput
                                        min={0}
                                        max={steps.length}
                                        value={h.step_range[0]}
                                        onChange={(v) => setStreamingHandlers(prev => prev.map((x, j) => j === i ? { ...x, step_range: [v ?? 0, x.step_range[1]] } : x))}
                                        size="sm"
                                        hideSteppers
                                      />
                                    </div>
                                    <span className="text-secondary">–</span>
                                    <div className="w-16">
                                      <NumberInput
                                        min={0}
                                        max={steps.length}
                                        value={h.step_range[1]}
                                        onChange={(v) => setStreamingHandlers(prev => prev.map((x, j) => j === i ? { ...x, step_range: [x.step_range[0], v ?? 0] } : x))}
                                        size="sm"
                                        hideSteppers
                                      />
                                    </div>
                                  </div>
                                  <div>
                                    <label className="text-[10px] text-secondary block mb-0.5">{t('Input vars:')}</label>
                                    <input type="text" placeholder="message, user_id"
                                      value={h.input_variables.join(', ')}
                                      onChange={(e) => setStreamingHandlers(prev => prev.map((x, j) => j === i ? { ...x, input_variables: e.target.value.split(',').map(s => s.trim()).filter(Boolean) } : x))}
                                      className="w-full px-1.5 py-0.5 border border-border rounded text-[11px]"
                                    />
                                  </div>
                                  <div>
                                    <label className="text-[10px] text-secondary block mb-0.5">{t('Extract:')}</label>
                                    <input type="text" placeholder="status, response"
                                      value={h.extract_fields.join(', ')}
                                      onChange={(e) => setStreamingHandlers(prev => prev.map((x, j) => j === i ? { ...x, extract_fields: e.target.value.split(',').map(s => s.trim()).filter(Boolean) } : x))}
                                      className="w-full px-1.5 py-0.5 border border-border rounded text-[11px]"
                                    />
                                  </div>
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                        {/* Add handler post-recording */}
                        {connectionState !== 'recording' && (
                          <div className="px-2 pb-2">
                            <button
                              onClick={() => {
                                const name = prompt(t('Handler name:'));
                                if (!name) return;
                                const clean = name.trim().replace(/[^a-zA-Z0-9_-]/g, '_');
                                setStreamingHandlers(prev => [...prev, {
                                  name: clean,
                                  step_range: [0, steps.length] as [number, number],
                                  input_variables: [],
                                  extract_fields: [],
                                }]);
                                setExpandedHandlerId(clean);
                              }}
                              className="w-full px-2 py-1.5 text-[11px] font-medium text-secondary border border-dashed border-border rounded-lg hover:bg-chrome transition-colors"
                            >
                              {t('+ Add Handler')}
                            </button>
                          </div>
                        )}
                      </div>
                    )}

                    {/* Streaming: Advanced Script */}
                    {activeTab === 'functions' && streamingMode && (
                      <div className="border-t border-border">
                        <button
                          onClick={() => {
                            setShowAdvancedScript(!showAdvancedScript);
                            if (!showAdvancedScript && !streamingAdvancedScript) {
                              setStreamingAdvancedScript(DEFAULT_STREAMING_SCRIPT);
                              setStreamingAdvancedEnabled(true);
                            }
                          }}
                          className="w-full px-3 py-2.5 flex items-center gap-2 text-xs font-medium text-ink hover:bg-chrome transition-colors"
                        >
                          <CodeBracketIcon className="w-3.5 h-3.5 text-secondary" />
                          {t('Advanced Script')}
                          {showAdvancedScript ? <ChevronDownIcon className="w-3 h-3 text-secondary ml-auto" /> : <ChevronRightIcon className="w-3 h-3 text-secondary ml-auto" />}
                        </button>
                        {showAdvancedScript && (
                          <div className="px-3 pb-3 space-y-2">
                            <div className="flex items-center justify-between">
                              <Checkbox
                                size="sm"
                                checked={streamingAdvancedEnabled}
                                onChange={(e) => setStreamingAdvancedEnabled(e.target.checked)}
                                label={t('Enable persistent script')}
                              />
                              {streamingAdvancedEnabled && (
                                <button
                                  onClick={() => setShowAIScriptAssistant(true)}
                                  className="flex items-center gap-1 px-2 py-1 text-[11px] font-medium text-ink bg-chrome rounded-md hover:bg-chrome transition-colors"
                                >
                                  <SparklesIcon className="w-3 h-3" />
                                  {t('AI Assist')}
                                </button>
                              )}
                            </div>

                            {streamingAdvancedEnabled && (
                              <div className="relative">
                                <textarea
                                  value={streamingAdvancedScript}
                                  onChange={(e) => setStreamingAdvancedScript(e.target.value)}
                                  rows={10}
                                  className="w-full px-3 py-2 text-[11px] font-mono bg-canvas text-ink rounded-lg border border-border resize-y leading-relaxed focus:ring-2 focus:ring-ink/10"
                                  spellCheck={false}
                                  disabled={aiScriptGenerating}
                                />
                                {/* Generating overlay */}
                                {aiScriptGenerating && (
                                  <div className="absolute inset-0 bg-surface/80 backdrop-blur-[1px] rounded-lg flex items-center justify-center gap-2 z-10">
                                    <ArrowPathIcon className="w-4 h-4 animate-spin text-secondary" />
                                    <span className="text-[11px] font-medium text-secondary">{t('Generating script...')}</span>
                                  </div>
                                )}
                              </div>
                            )}

                            {/* Test button */}
                            {streamingAdvancedEnabled && streamingAdvancedScript.trim() && (
                              <div className="space-y-1.5">
                                <button
                                  onClick={async () => {
                                    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN || manualTestLoading) return;
                                    setManualTestLoading(true);
                                    setManualTestResult(null);
                                    try {
                                      const script = streamingAdvancedScript;
                                      const actionMatches = script.match(/action\s*===?\s*["'](\w+)["']/g) || [];
                                      const actions = actionMatches.map((m: string) => m.match(/["'](\w+)["']/)?.[1]).filter(Boolean);
                                      const testAction = actions[0] || 'test';
                                      const testData: Record<string, any> = {};
                                      if (testAction.toLowerCase().includes('send') || testAction.toLowerCase().includes('message')) testData.message = 'Hello from test';

                                      const ws = wsRef.current!;
                                      const r: any = await new Promise((resolve, reject) => {
                                        const timer = setTimeout(() => { ws.removeEventListener('message', h); reject(new Error(t('Test timed out (30s)'))); }, 30000);
                                        const h = (e: MessageEvent) => {
                                          try {
                                            const m = JSON.parse(e.data);
                                            if (m.type === 'eval_result') {
                                              clearTimeout(timer); ws.removeEventListener('message', h);
                                              if (m.error) reject(new Error(m.error));
                                              else resolve(m.result);
                                            }
                                          } catch {}
                                        };
                                        ws.addEventListener('message', h);
                                        ws.send(JSON.stringify({ type: 'action', action: 'test_streaming_script', script, test_action: testAction, test_data: testData }));
                                      });
                                      setManualTestResult(r?.ok === false ? { success: false, error: r.error } : { success: true, result: r });
                                    } catch (e: any) {
                                      setManualTestResult({ success: false, error: e.message });
                                    } finally {
                                      setManualTestLoading(false);
                                    }
                                  }}
                                  disabled={manualTestLoading}
                                  className="flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-medium text-secondary bg-hover rounded-md hover:bg-active disabled:opacity-50 transition-colors"
                                >
                                  {manualTestLoading ? (
                                    <><ArrowPathIcon className="w-3 h-3 animate-spin" /> {t('Testing...')}</>
                                  ) : (
                                    <><PlayIcon className="w-3 h-3" /> {t('Test Script')}</>
                                  )}
                                </button>

                                {/* Test result */}
                                {manualTestResult && (
                                  <div className={clsx(
                                    'px-2.5 py-2 rounded-lg text-[10px] border',
                                    manualTestResult.success ? 'bg-hover/50 border-border text-ink' : 'bg-hover/50 border-border text-ink',
                                  )}>
                                    <span className="font-semibold font-mono">{manualTestResult.success ? t('Pass') : t('Failed')}</span>
                                    {manualTestResult.result?.message && <span className="ml-1.5 text-secondary">— {manualTestResult.result.message}</span>}
                                    {manualTestResult.error && <p className="mt-0.5 font-mono text-secondary">{manualTestResult.error}</p>}
                                    {manualTestResult.result?.actions?.length > 0 && (
                                      <div className="mt-1">
                                        {manualTestResult.result.actions.map((a: any, i: number) => (
                                          <div key={i} className="font-mono text-secondary">
                                            {a.ok ? '+' : '-'} {a.fn}({a.args?.map((x: any) => typeof x === 'string' ? `"${x}"` : x).join(', ')})
                                          </div>
                                        ))}
                                      </div>
                                    )}
                                    {manualTestResult.result?.response && (
                                      <div className="mt-1">
                                        <span className="text-tertiary">{t('Response:')} </span>
                                        <pre className="font-mono text-secondary whitespace-pre-wrap">{JSON.stringify(manualTestResult.result.response.data, null, 2)}</pre>
                                      </div>
                                    )}
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    )}

                    {/* Post-record review affordance — shown once recording stops.
                        The standing full-width "AI Optimize" button and the in-panel
                        name + Save form are gone: naming + commit now live on the Studio
                        app-bar, and Optimize is a quiet review affordance (it can also be
                        triggered from the AI dock). The inline diff Apply/Discard review
                        is preserved verbatim. */}
                    {activeTab === 'steps' && steps.length > 0 && connectionState !== 'recording' && !apiMode && (
                      <div className="border-t border-border animate-summary-enter">
                        <div className="px-4 py-3">
                          {/* Quiet review affordance */}
                          {!pendingOptimization && (
                            <button
                              onClick={optimizeWorkflow}
                              disabled={optimizeLoading}
                              className="w-full px-3 py-1.5 rounded-lg flex items-center justify-center gap-1.5 text-[11px] text-tertiary hover:text-ink hover:bg-chrome disabled:opacity-50 transition-colors mb-2"
                            >
                              {optimizeLoading ? (
                                <>
                                  <ArrowPathIcon className="h-3.5 w-3.5 animate-spin" />
                                  {t('Reviewing…')}
                                </>
                              ) : (
                                <>
                                  <SparklesIcon className="h-3.5 w-3.5" />
                                  {t('Review with AI')}
                                </>
                              )}
                            </button>
                          )}
                          {pendingOptimization && (
                            <div className="bg-hover/50 rounded-lg p-2.5 border border-border mb-2 space-y-2">
                              <p className="text-[11px] font-medium text-ink">
                                {t('Proposed changes ({{n}}) — review before applying', { n: pendingOptimization.changes.length })}
                              </p>
                              <div className="space-y-1.5 max-h-56 overflow-y-auto">
                                {pendingOptimization.changes.map((c, i) => {
                                  const needsReview = c.risk === 'caution' || c.risk === 'high';
                                  return (
                                    <div key={i} className="text-[10px] border border-border rounded p-1.5 bg-surface/60">
                                      <div className="flex items-center gap-1.5">
                                        <span className="font-mono uppercase text-[8px] px-1 py-0.5 rounded bg-hover text-secondary shrink-0">{c.action}</span>
                                        <span className="text-ink flex-1">{c.description}</span>
                                        {needsReview && (
                                          <span className="shrink-0 flex items-center gap-0.5 text-tertiary" title={t('risk: {{risk}}', { risk: c.risk })}>
                                            <ExclamationTriangleIcon className="h-3 w-3" /> {t('review')}
                                          </span>
                                        )}
                                      </div>
                                      {c.reason && <p className="text-tertiary mt-0.5 pl-0.5">{c.reason}</p>}
                                    </div>
                                  );
                                })}
                                {pendingOptimization.warnings.map((w, i) => (
                                  <p key={`w_${i}`} className="text-[10px] text-tertiary flex items-start gap-1">
                                    <ExclamationTriangleIcon className="h-3 w-3 shrink-0 mt-0.5" />
                                    <span>{w}</span>
                                  </p>
                                ))}
                              </div>
                              <div className="flex gap-1.5">
                                <button onClick={applyOptimization} className="flex-1 px-2 py-1.5 bg-accent-strong hover:bg-accent-strong/90 text-accent-on rounded text-[11px] font-semibold shadow-sm flex items-center justify-center gap-1">
                                  <CheckIcon className="h-3.5 w-3.5" /> {t('Apply')}
                                </button>
                                <button onClick={discardOptimization} className="flex-1 px-2 py-1.5 bg-hover hover:bg-active border border-border text-secondary rounded text-[11px] font-medium">
                                  {t('Discard')}
                                </button>
                              </div>
                            </div>
                          )}
                          {!pendingOptimization && optimizeNote && (
                            <p className="text-[10px] text-tertiary mb-2 px-0.5">{optimizeNote}</p>
                          )}
                        </div>
                        {/* Naming and the single forward control ("Done recording" →
                            Finalize) live in the Studio app-bar, not here. Steps,
                            credentials, form data and segments live-sync to the
                            wizard continuously via the onStepsChange /
                            onCredentialsChange / onFormDataChange /
                            onSegmentsChange callbacks. */}
                      </div>
                    )}

                    {/* API Calls Panel (API recording mode) — lives in the Requests tab */}
                    {activeTab === 'requests' && apiMode && capturedApiRequests.length > 0 && (
                      <div className="border-t border-border flex flex-col flex-1 min-h-0">
                        <div className="px-4 py-2.5 border-b border-border bg-hover/40">
                          <div className="flex items-center justify-between">
                            <span className="text-ink font-medium text-xs flex items-center gap-2">
                              <LinkIcon className="h-4 w-4 text-secondary" /> {t('API Calls ({{n}})', { n: capturedApiRequests.length })}
                            </span>
                            <span className="text-[10px] text-tertiary">
                              {t('{{n}} labeled', { n: capturedApiRequests.filter(r => r.function_name).length })}
                            </span>
                          </div>
                        </div>
                        <div className="flex-1 overflow-y-auto divide-y divide-gray-700/50">
                          {capturedApiRequests.map((req) => {
                            const isExp = expandedApiId === req.id;
                            const sc = req.response ? (req.response.status < 300 ? 'text-ink' : req.response.status < 400 ? 'text-secondary' : 'text-ink font-bold') : 'text-tertiary';
                            return (
                              <div key={req.id} className={clsx(req.function_name && 'bg-ink/[0.03] border-l-2 border-ink')}>
                                <div className="px-3 py-1.5 flex items-center gap-1.5 cursor-pointer hover:bg-chrome text-xs"
                                  onClick={() => setExpandedApiId(isExp ? null : req.id)}>
                                  <span className={clsx('font-mono font-bold px-1 py-0.5 rounded text-[10px]',
                                    req.method === 'POST' ? 'bg-ink/10 text-ink' :
                                    req.method === 'PUT' ? 'bg-ink/10 text-ink' :
                                    req.method === 'DELETE' ? 'bg-ink/10 text-ink font-bold' : 'bg-hover text-secondary'
                                  )}>{req.method}</span>
                                  <span className={clsx('text-[10px] font-mono', sc)}>{req.response?.status || '...'}</span>
                                  <span className="text-secondary truncate flex-1 font-mono">{(() => { try { return new URL(req.url).pathname; } catch { return req.url; } })()}</span>
                                  {req.function_name && (
                                    <span className="text-[10px] px-1 py-0.5 bg-ink/10 text-ink rounded font-medium">
                                      {req.is_auth ? '🔐' : ''}{req.function_name}
                                    </span>
                                  )}
                                </div>
                                {isExp && (
                                  <div className="px-3 pb-2 space-y-2">
                                    {/* Label as function */}
                                    <div className="flex items-center gap-1.5">
                                      {labelingApiId === req.id ? (
                                        <>
                                          <input type="text" value={apiLabelForm.name}
                                            onChange={(e) => setApiLabelForm({ ...apiLabelForm, name: e.target.value.replace(/[^a-zA-Z0-9_]/g, '') })}
                                            placeholder="functionName" className="flex-1 px-2 py-1 bg-canvas border border-border rounded text-[10px] text-ink outline-none font-mono" />
                                          <Checkbox
                                            checked={apiLabelForm.is_auth}
                                            onChange={(e) => setApiLabelForm({ ...apiLabelForm, is_auth: e.target.checked })}
                                            label={t('Auth')}
                                            size="sm"
                                          />

                                          <button onClick={() => {
                                            if (apiLabelForm.name) {
                                              setCapturedApiRequests(prev => prev.map(r => r.id === req.id ? { ...r, function_name: apiLabelForm.name, is_auth: apiLabelForm.is_auth } : r));
                                              setLabelingApiId(null);
                                            }
                                          }} className="px-2 py-1 bg-ink text-white rounded text-[10px]">{t('OK')}</button>
                                          <button onClick={() => setLabelingApiId(null)} className="px-2 py-1 bg-hover text-secondary rounded text-[10px]">{t('X')}</button>
                                        </>
                                      ) : (
                                        <button onClick={() => { setLabelingApiId(req.id); setApiLabelForm({ name: req.function_name || '', is_auth: req.is_auth || false }); }}
                                          className="px-2 py-1 bg-hover hover:bg-chrome text-secondary rounded text-[10px]">
                                          {req.function_name ? t('Rename: {{name}}', { name: req.function_name }) : t('+ Label as Function')}
                                        </button>
                                      )}
                                    </div>
                                    {/* URL */}
                                    <div className="text-[10px] text-tertiary font-mono truncate">{req.url}</div>
                                    {/* Request Body */}
                                    {req.body && (
                                      <div>
                                        <p className="text-[10px] text-tertiary mb-0.5">{t('Request Body')} <span className="text-tertiary">{t('(click to parameterize)')}</span></p>
                                        <div className="bg-canvas/50 rounded p-1.5 max-h-28 overflow-y-auto">
                                          {typeof req.body === 'object' ? (
                                            <div className="space-y-0.5">
                                              {Object.entries(req.body).map(([k, v]) => {
                                                const isParam = !!(apiParamFields[req.id]?.[k]);
                                                return (
                                                  <div key={k} className="text-[10px] font-mono flex items-center gap-1">
                                                    <span className="text-ink">{k}</span>
                                                    <span className="text-tertiary">:</span>
                                                    <span className={clsx('px-1 py-0.5 rounded cursor-pointer',
                                                      isParam ? 'bg-ink/10 text-ink font-bold' : 'text-secondary hover:bg-surface'
                                                    )} onClick={() => {
                                                      setApiParamFields(prev => {
                                                        const f = { ...(prev[req.id] || {}) };
                                                        if (f[k]) delete f[k]; else f[k] = k;
                                                        return { ...prev, [req.id]: f };
                                                      });
                                                    }}>
                                                      {isParam ? `{{${k}}}` : JSON.stringify(v)}
                                                    </span>
                                                  </div>
                                                );
                                              })}
                                            </div>
                                          ) : (
                                            <pre className="text-[10px] text-secondary whitespace-pre-wrap">{String(req.body).substring(0, 300)}</pre>
                                          )}
                                        </div>
                                      </div>
                                    )}
                                    {/* Response Body */}
                                    {req.response?.body && typeof req.response.body === 'object' && (
                                      <div>
                                        <p className="text-[10px] text-tertiary mb-0.5">{t('Response')} <span className="text-tertiary">{t('(click to extract)')}</span></p>
                                        <div className="bg-canvas/50 rounded p-1.5 max-h-28 overflow-y-auto">
                                          <div className="space-y-0.5">
                                            {Object.entries(req.response.body).map(([k, v]) => {
                                              const isExtracted = !!(apiExtractions[req.id]?.[k]);
                                              return (
                                                <div key={k} className="text-[10px] font-mono flex items-center gap-1">
                                                  <span className="text-ink">{k}</span>
                                                  <span className="text-tertiary">:</span>
                                                  <span className={clsx('px-1 py-0.5 rounded cursor-pointer',
                                                    isExtracted ? 'bg-ink/10 text-ink font-bold' : 'text-secondary hover:bg-surface'
                                                  )} onClick={() => {
                                                    setApiExtractions(prev => {
                                                      const f = { ...(prev[req.id] || {}) };
                                                      if (f[k]) delete f[k]; else f[k] = `$.${k}`;
                                                      return { ...prev, [req.id]: f };
                                                    });
                                                  }}>
                                                    {isExtracted ? `[extract: ${k}]` : (typeof v === 'string' ? `"${String(v).substring(0, 40)}"` : JSON.stringify(v))}
                                                  </span>
                                                </div>
                                              );
                                            })}
                                          </div>
                                        </div>
                                      </div>
                                    )}
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                        {/* API Save Section */}
                        {connectionState !== 'recording' && capturedApiRequests.some(r => r.function_name) && (
                          <div className="p-3 border-t border-border space-y-2">
                            <input type="text" value={workflowName} onChange={(e) => setWorkflowName(e.target.value)}
                              placeholder={t('API Workflow name...')} className="w-full px-3 py-2 bg-canvas border border-border rounded-lg text-ink text-sm placeholder:text-tertiary focus:ring-2 focus:ring-ink/10" />
                            <button onClick={() => {
                              if (!workflowName.trim()) { toast.error(t('Enter a workflow name')); return; }
                              const labeled = capturedApiRequests.filter(r => r.function_name);
                              if (labeled.length === 0) { toast.error(t('Label at least one request')); return; }
                              const funcs: Record<string, any> = {};
                              labeled.forEach((req, idx) => {
                                const params = apiParamFields[req.id] || {};
                                const extr = apiExtractions[req.id] || {};
                                let bodyTemplate = req.body;
                                if (typeof bodyTemplate === 'object' && bodyTemplate) {
                                  bodyTemplate = JSON.parse(JSON.stringify(bodyTemplate));
                                  for (const [field, paramName] of Object.entries(params)) {
                                    bodyTemplate[field] = `{{${paramName}}}`;
                                  }
                                }
                                funcs[req.function_name!] = {
                                  label: req.function_name,
                                  is_auth: req.is_auth || false,
                                  order: idx,
                                  request: { method: req.method, url: req.url, headers: req.headers, body_template: bodyTemplate || {} },
                                  response_extractions: extr,
                                  parameters: Object.values(params),
                                  secrets: [],
                                };
                              });
                              if (onSaveApi) onSaveApi(funcs, workflowName.trim());
                            }} className="w-full px-4 py-2 bg-accent-strong hover:bg-accent-strong/90 text-accent-on rounded-lg font-semibold shadow-sm flex items-center justify-center gap-2">
                              <CheckCircleIcon className="h-5 w-5" />
                              {t('Save API Workflow ({{n}} functions)', { n: capturedApiRequests.filter(r => r.function_name).length })}
                            </button>
                          </div>
                        )}
                      </div>
                    )}
                    </>
                    </div>
                  </>
                    );
                  })()}

                  {/* AI Script Assistant — side panel */}
                  {showAIScriptAssistant && (() => {
                    // Shared: evaluate JS in recorder browser via WebSocket
                    const wsEval = (script: string): Promise<any> => {
                      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN)
                        return Promise.reject(new Error(t('Browser not connected')));
                      return new Promise((resolve, reject) => {
                        const ws = wsRef.current!;
                        const timer = setTimeout(() => { ws.removeEventListener('message', h); reject(new Error(t('Eval timed out (10s) — recorder may not support evaluate_js'))); }, 10000);
                        const h = (e: MessageEvent) => {
                          try {
                            const m = JSON.parse(e.data);
                            if (m.type === 'eval_result') {
                              clearTimeout(timer); ws.removeEventListener('message', h);
                              if (m.error) reject(new Error(m.error));
                              else resolve(m.result);
                            } else if (m.type === 'error' && m.message?.includes('Script error')) {
                              // Fallback: recorder returned error instead of eval_result
                              clearTimeout(timer); ws.removeEventListener('message', h);
                              reject(new Error(m.message));
                            }
                          } catch {}
                        };
                        ws.addEventListener('message', h);
                        ws.send(JSON.stringify({ type: 'action', action: 'evaluate_js', script }));
                      });
                    };
                    return (
                      <div className={clsx(
                        "absolute right-3 bottom-3 z-20 w-72 max-w-[calc(100%-1.5rem)] bg-surface/90 backdrop-blur-xl border border-border rounded-2xl shadow-2xl overflow-hidden",
                        // Tuck below the recording URL/nav toolbar (z-40) so it isn't overlapped.
                        connectionState === 'recording' ? 'top-16' : 'top-3',
                      )}>
                        <AIScriptAssistant
                          open={showAIScriptAssistant}
                          onClose={() => setShowAIScriptAssistant(false)}
                          getScreenshot={getCanvasScreenshot}
                          pageUrl={currentUrl}
                          handlerNames={streamingHandlers.map((h: any) => h.name)}
                          currentScript={streamingAdvancedScript}
                          onApplyScript={(script) => { setStreamingAdvancedScript(script); setStreamingAdvancedEnabled(true); }}
                          onGeneratingChange={setAiScriptGenerating}
                          evaluateInPage={wsEval}
                          onTestScript={async (script) => {
                            try {
                              // Extract action names from the script to build realistic test data
                              const actionMatches = script.match(/action\s*===?\s*["'](\w+)["']/g) || [];
                              const actions = actionMatches.map((m: string) => {
                                const match = m.match(/["'](\w+)["']/);
                                return match ? match[1] : null;
                              }).filter(Boolean);
                              const testAction = actions[0] || 'test';
                              const testData: Record<string, any> = {};
                              if (testAction.toLowerCase().includes('send') || testAction.toLowerCase().includes('message')) testData.message = 'Hello from test';
                              if (testAction.toLowerCase().includes('search') || testAction.toLowerCase().includes('query')) testData.query = 'test query';

                              // Use test_streaming_script — executes REAL Playwright actions
                              if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN)
                                return { success: false, error: t('Browser not connected') };
                              const ws = wsRef.current;
                              const r: any = await new Promise((resolve, reject) => {
                                const timer = setTimeout(() => { ws.removeEventListener('message', h); reject(new Error(t('Test timed out (30s)'))); }, 30000);
                                const h = (e: MessageEvent) => {
                                  try {
                                    const m = JSON.parse(e.data);
                                    if (m.type === 'eval_result') {
                                      clearTimeout(timer); ws.removeEventListener('message', h);
                                      if (m.error) reject(new Error(m.error));
                                      else resolve(m.result);
                                    }
                                  } catch {}
                                };
                                ws.addEventListener('message', h);
                                ws.send(JSON.stringify({
                                  type: 'action',
                                  action: 'test_streaming_script',
                                  script,
                                  test_action: testAction,
                                  test_data: testData,
                                }));
                              });
                              return r?.ok === false ? { success: false, error: r.error } : { success: true, result: r };
                            } catch (e: any) { return { success: false, error: e.message }; }
                          }}
                        />
                      </div>
                    );
                  })()}
                </div>

                {/* Code Modal */}
                {showCode && (
                  <div className="absolute inset-0 bg-black/80 flex items-center justify-center p-8">
                    <div className="bg-surface rounded-xl max-w-4xl w-full max-h-[80vh] flex flex-col">
                      <div className="flex items-center justify-between p-4 border-b border-border">
                        <span className="text-ink font-medium">{t('Generated Playwright Code')}</span>
                        <button
                          onClick={() => setShowCode(false)}
                          className="p-2 hover:bg-chrome rounded-lg"
                        >
                          <XMarkIcon className="h-5 w-5 text-secondary" />
                        </button>
                      </div>
                      <div className="flex-1 overflow-auto p-4">
                        <pre className="text-sm text-secondary font-mono whitespace-pre-wrap">
                          {generatedCode}
                        </pre>
                      </div>
                      <div className="p-4 border-t border-border flex justify-end gap-2">
                        <button
                          onClick={() => {
                            navigator.clipboard.writeText(generatedCode);
                            toast.success(t('Copied to clipboard'));
                          }}
                          className="px-4 py-2 bg-hover hover:bg-chrome text-ink rounded-lg"
                        >
                          {t('Copy Code')}
                        </button>
                        <button
                          onClick={() => setShowCode(false)}
                          className="px-4 py-2 bg-ink hover:bg-ink/90 text-ink rounded-lg"
                        >
                          {t('Close')}
                        </button>
                      </div>
                    </div>
                  </div>
                )}

                {/* Turn the recorded login into a persona (prefilled from the
                    recording: site domain, username, and detected 2FA channel). */}
                <PersonaWizard
                  isOpen={showPersonaWizard}
                  onClose={() => setShowPersonaWizard(false)}
                  prefill={{
                    target_domain: (() => {
                      try { return currentUrl ? new URL(currentUrl).hostname.replace(/^www\./, '') : undefined; }
                      catch { return undefined; }
                    })(),
                    login_username: detectedCredentials.find(
                      (c) => /user|email|login/i.test(`${c.field_type} ${c.field_name}`) && !/pass/i.test(c.field_type),
                    )?.value || undefined,
                    twofa_method: (() => {
                      const has2fa = detected2fa || steps.some((s) => s.type === 'twofa');
                      if (!has2fa) return 'none';
                      // Use the detected delivery channel; default to authenticator
                      // (the only channel a stored seed can mirror) when unknown.
                      if (detected2faChannel === 'email_otp') return 'email_otp';
                      if (detected2faChannel === 'sms') return 'sms';
                      return 'totp';
                    })(),
                  }}
                  onSaved={(persona) => {
                    setShowPersonaWizard(false);
                    setPersonaPromptDismissed(true);
                    if (persona?.id && onPersonaCreated) {
                      onPersonaCreated(persona.id);
                      toast.success(t('Persona saved and attached to this workflow.'));
                    } else {
                      toast.success(t('Persona saved — attach it when you save this workflow.'));
                    }
                  }}
                />

                <AuthenticatorImportModal
                  isOpen={showAuthImport}
                  onClose={() => setShowAuthImport(false)}
                  defaultDomain={(() => {
                    try { return currentUrl ? new URL(currentUrl).hostname.replace(/^www\./, '') : undefined; }
                    catch { return undefined; }
                  })()}
                  onImported={(created) => {
                    setShowAuthImport(false);
                    setPersonaPromptDismissed(true);
                    // Attach the first imported persona as this workflow's default.
                    if (created?.[0]?.id && onPersonaCreated) {
                      onPersonaCreated(created[0].id);
                      toast.success(t('Persona attached to this workflow.'));
                    }
                  }}
                />
              </div>
  );

  if (embedded) {
    if (!isOpen) return null;
    return recorderContent;
  }

  return (
    <Transition show={isOpen} as={React.Fragment}>
      <Dialog as="div" className="relative z-50" onClose={() => {}}>
        <Transition.Child
          as={React.Fragment}
          enter="ease-out duration-300"
          enterFrom="opacity-0"
          enterTo="opacity-100"
          leave="ease-in duration-200"
          leaveFrom="opacity-100"
          leaveTo="opacity-0"
        >
          <div className="fixed inset-0 bg-black/70 backdrop-blur-sm" />
        </Transition.Child>

        <div className="fixed inset-0 overflow-hidden">
          <div className="flex h-full">
            <Transition.Child
              as={React.Fragment}
              enter="ease-out duration-300"
              enterFrom="opacity-0 scale-95"
              enterTo="opacity-100 scale-100"
              leave="ease-in duration-200"
              leaveFrom="opacity-100 scale-100"
              leaveTo="opacity-0 scale-95"
            >
              <Dialog.Panel className="w-full h-full flex flex-col bg-canvas">
                {recorderContent}
              </Dialog.Panel>
            </Transition.Child>
          </div>
        </div>
      </Dialog>
    </Transition>
  );
};
