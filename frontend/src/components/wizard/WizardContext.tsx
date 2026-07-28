import React, { createContext, useContext, useReducer, useCallback, useMemo } from 'react';
import { WorkflowStep } from '../../types/api';
import { defaultSchedule, type ScheduleValue } from '../../utils/schedule';

// ── Types ──────────────────────────────────────────────────────────────────

export type WizardMode = 'content_monitor' | 'manual_workflow' | 'ai_workflow' | 'extract_scrape' | 'api_workflow' | 'streaming_workflow' | 'site_crawl';
// Note: extract_scrape is kept for backward compatibility with existing workflows.
// site_crawl = Dragnet whole-site crawl (fans out across the self-hosted agent fleet).
export type WizardStepId = 'mode' | 'configure' | 'finalize';

// ── Smart Finalize: "What now?" connectors ───────────────────────────────────
// Multi-select exposure modifiers chosen on the Finalize sheet and applied AFTER
// the workflow is created (see UnifiedCreationWizard.handleSubmit). A workflow can
// be exposed several ways at once (e.g. API + MCP). 'save' is the implicit default
// (private, no exposure) and is never stored in the set.
export type WizardConnector = 'api' | 'mcp' | 'webhook' | 'schedule' | 'automate' | 'sell';

/** Preset schedule intervals (ms) reused from the workflow scheduler vocabulary. */
export const SCHEDULE_PRESETS: Array<{ id: string; labelKey: string; ms: number }> = [
  { id: 'hourly', labelKey: 'Every hour', ms: 60 * 60 * 1000 },
  { id: 'every6h', labelKey: 'Every 6 hours', ms: 6 * 60 * 60 * 1000 },
  { id: 'daily', labelKey: 'Daily', ms: 24 * 60 * 60 * 1000 },
  { id: 'weekly', labelKey: 'Weekly', ms: 7 * 24 * 60 * 60 * 1000 },
];

/**
 * Intent-aware default connectors per mode. Preselected (the user can change
 * them). content_monitor leans on its own triggers + alerting automations, so it
 * defaults to none here (Triggers live in Advanced settings).
 */
export function defaultConnectorsForMode(mode: WizardMode | null): WizardConnector[] {
  switch (mode) {
    case 'manual_workflow':
    case 'api_workflow':
      return ['api', 'mcp'];
    case 'streaming_workflow':
      return ['api', 'mcp'];
    case 'ai_workflow':
      // A one-off autonomous AI session isn't a schedulable entity at creation time.
      // The RECORDED workflow it saves on finish can be scheduled later from the
      // workflows list, so no connectors are pre-selected here.
      return [];
    case 'content_monitor':
      return ['automate'];
    case 'site_crawl':
      // A crawl exposes its collected dataset as an API post-crawl, from the
      // crawl detail page — the wizard doesn't attach connectors at start time.
      return [];
    default:
      return [];
  }
}

/** A data extractor to auto-create on a selector when the monitor is saved. */
export interface SelectorExtractorSeed {
  name: string;
  outputName: string;
  extractType: 'text' | 'attribute' | 'regex' | 'css' | 'json_path';
  config?: Record<string, any>;
  isArray?: boolean;
  defaultValue?: string;
}

export interface WizardSelector {
  id: string;
  name: string;
  selector: string;
  description: string;
  checkType: 'text' | 'html' | 'visual';
  ignoreRegex: string;
  preview: string;
  enabled: boolean;
  // x/y are viewport-relative; scroll_x/scroll_y record the page scroll when the
  // zone was drawn so the monitor can re-scroll there before clipping (else a
  // below-the-fold zone watches the wrong pixels). `viewport` is the frame size
  // those coords were measured in — the check opens its browser context at that
  // size, so dropping it makes the zone clip the wrong pixels (see
  // `SelectionRegion` in useRecorderSelection.ts).
  region?: {
    x: number; y: number; width: number; height: number;
    scroll_x?: number; scroll_y?: number;
    viewport?: { width: number; height: number };
  };
  /** Optional extractors to create alongside this selector (e.g. a numeric price). */
  extractors?: SelectorExtractorSeed[];
}

export interface ExtractorConfig {
  id: string;
  selectorId: string;
  name: string;
  extractType: 'text' | 'attribute' | 'regex' | 'css' | 'json_path';
  expression: string;
  outputName: string;
  isArray: boolean;
  defaultValue: string;
}

export interface WizardTrigger {
  id: string;
  name: string;
  eventType: string;
  conditions: Record<string, any>;
  actions: Array<{ type: string; config: any }>;
  enabled: boolean;
}

export interface WizardWebhook {
  id: string;
  name: string;
  token?: string;
  customPath?: string;
  waitForResult: boolean;
  workflowId?: number;
}

export interface WizardState {
  currentStep: WizardStepId;
  completedSteps: Set<WizardStepId>;

  mode: WizardMode | null;

  config: {
    name: string;
    description: string;
    url: string;

    // Content Monitor
    selectors: WizardSelector[];
    checkPeriodMs: number | null;
    // Precise recurrence for the check cadence (interval | daily | weekly). Interval
    // kind uses checkPeriodMs; daily/weekly use time/days/tz. Absent ⇒ 'interval'.
    scheduleKind: 'interval' | 'daily' | 'weekly';
    scheduleTime: string; // 'HH:MM' local (daily/weekly)
    scheduleDays: number[]; // 1..7 ISO (weekly)
    scheduleTz: string; // IANA
    requiresPlaywright: boolean;
    preferredRegion: string | null;
    /** Recorded setup steps the checker replays before each check (login, nav, etc.).
        Only attached to the target when non-empty so plain checks stay on the fast path. */
    monitorSetupSteps: WorkflowStep[];

    // Manual / API Workflow
    recordedSteps: WorkflowStep[];
    credentials: Record<string, string>;
    formData: Record<string, string>;
    apiFunctions: Record<string, any>;
    workflowType: string;
    // Step-group "functions" created in the recorder (raw recorder segments)
    segments: Array<{
      name: string;
      segment_type: string;
      step_indices: number[];
      depends_on?: string[];
      extract_outputs?: string[];
    }>;

    // AI Workflow
    goal: string;
    siteDescription: string;
    availableData: Array<{ key: string; value: string; isSecret?: boolean }>;
    maxSteps: number;
    useVision: boolean;
    aiMode: 'standard' | 'intelligent';
    userContext: string;
    autoValidate: boolean;
    /** Force this session's AI through the managed cloud gateway instead of the agent's local keys. */
    useWritAi?: boolean;
    /** When the autonomous AI session finishes, save the steps it took as a reusable
        workflow. Forwarded as `generate_workflow` to the coordinator's AI-session start. */
    generateWorkflow: boolean;

    // Extract/Scrape
    extractors: ExtractorConfig[];

    // Streaming
    streamingHandlers: Array<{
      name: string;
      type: 'steps';
      step_range: [number, number];
      input_variables: string[];
      extract_fields: string[];
    }>;
    advancedScript: string;
    advancedScriptEnabled: boolean;
    setupStepsCount: number;
    maxDurationSeconds: number;
    multiConversation: boolean;
    maxConcurrentConversations: number;
    openaiCompatEnabled: boolean;
    openaiDefaultHandler: string;
    openaiModelName: string;

    // Site crawl (Dragnet) — the self-hosted coordinator does deterministic crawls
    // only (no AI executor); the one axis is the output shape.
    /**
     * Which operation: 'crawl' = seed → follow links → many pages; 'scrape' = fetch
     * just the entry URL, no link-following (a depth-0 crawl); 'map' = list the
     * site's URLs and hand-pick the entry set (creates nothing).
     */
    crawlVerb: 'crawl' | 'scrape' | 'map';
    crawlOutput: 'markdown' | 'schema';
    crawlRenderMode: 'auto' | 'http' | 'browser';
    crawlOcrMode: 'auto' | 'off' | 'force';
    crawlMaxDepth: number;
    crawlPageBudget: number;
    /** Concurrent shards/agents (maps to the coordinator's max_concurrent_shards). */
    crawlConcurrency: number;
    /** Pages per shard (max_concurrent_shards × shard_size = pages in flight). */
    crawlShardSize: number;
    crawlDelayMs: number;
    crawlRespectRobots: boolean;
    crawlSameDomain: boolean;
    crawlAllowSubdomains: boolean;
    /** One path pattern per line (textarea); parsed at submit. */
    crawlIncludePaths: string;
    crawlExcludePaths: string;
    crawlPersonaId: number | null;
    /**
     * Plain-English GOAL for the crawl. The coordinator derives include/exclude
     * paths from it and RANKS the frontier by relevance, so the page budget is
     * spent on matching pages first. Blank = every page in scope (plain sweep).
     */
    crawlIntent: string;
    /** Hand-picked entry URLs from the Map step. Empty = auto-discover (sitemap). */
    crawlSeedUrls: string[];
    /** Drop discovered URLs scoring below this against the goal (0 = keep all). */
    crawlRelevanceThreshold: number;
    // Content selection — which page ELEMENTS the scrape keeps (applies to scrape + crawl).
    crawlContentPreset: 'main' | 'full' | 'readable';
    crawlIncludeComments: boolean;
    crawlKeepImages: boolean;
    crawlKeepTables: boolean;
    crawlKeepLinks: boolean;
    crawlExcludeSelectors: string;
    crawlIncludeSelectors: string;

    // Execution settings
    timeoutMs: number;
    retryCount: number;
    headless: boolean;
    fastMode: boolean;
    trustedAgentsOnly: boolean;
    executionTarget: string;
    defaultPersonaId: number | null;
  };

  triggers: {
    triggerRules: WizardTrigger[];
    webhookTriggers: WizardWebhook[];
    skipTriggers: boolean;
  };

  apiConfig: {
    linkedApiKeyId: number | null;
    createNewKey: boolean;
    newKeyLabel: string;
    newKeyRole: string;
    skipApiKey: boolean;
  };

  // Smart Finalize: the chosen "What now?" connectors + their inline settings.
  expose: {
    connectors: Set<WizardConnector>;
    /** Schedule interval (ms) when the 'schedule' connector is selected. Kept in sync with
        workflowSchedule.intervalMs so the create call has a single interval-kind source. */
    scheduleIntervalMs: number;
    /** Structured recurrence for the workflow 'schedule' connector (interval kind ⇒
        scheduleIntervalMs; daily/weekly carry time/days/tz). Threaded into the create call
        via scheduleToPayload(...). */
    workflowSchedule?: ScheduleValue;
    /** Tracks whether the user has manually touched the connector set yet, so the
        intent-aware defaults only re-seed while still untouched. */
    connectorsTouched: boolean;
  };

  testResults: {
    taskId: number | null;
    status: 'idle' | 'running' | 'success' | 'failed';
    durationMs: number | null;
    extractedData: Record<string, any> | null;
    error: string | null;
    screenshots: Array<{ step: number; action: string; data_base64: string }>;
    selectorResults: Array<{ selectorId: string; matched: boolean; content: string }>;
  };

  createdIds: {
    targetId: number | null;
    workflowId: number | null;
    aiSessionId: number | null;
    triggerRuleIds: number[];
    webhookTriggerIds: number[];
    apiKeyId: number | null;
    apiKeyValue: string | null;
  };

  isSubmitting: boolean;
  submitError: string | null;
}

// ── Steps per mode ─────────────────────────────────────────────────────────

const STEPS_BY_MODE: Record<WizardMode, WizardStepId[]> = {
  content_monitor: ['mode', 'configure', 'finalize'],
  manual_workflow: ['mode', 'configure', 'finalize'],
  ai_workflow: ['mode', 'configure', 'finalize'],
  extract_scrape: ['mode', 'configure', 'finalize'],
  api_workflow: ['mode', 'configure', 'finalize'],
  streaming_workflow: ['mode', 'configure', 'finalize'],
  // A crawl is fire-and-forget: pick the site (mode) + scope (configure), then
  // launch. No Finalize connectors — the dataset is exposed later from the crawl's
  // own detail page. Configure is the last step, so its primary button submits.
  site_crawl: ['mode', 'configure'],
};

const ALL_STEPS: WizardStepId[] = ['mode', 'configure', 'finalize'];

export function getStepsForMode(mode: WizardMode | null): WizardStepId[] {
  if (!mode) return ['mode'];
  return STEPS_BY_MODE[mode];
}

// ── Initial state ──────────────────────────────────────────────────────────

const initialState: WizardState = {
  currentStep: 'mode',
  completedSteps: new Set(),
  mode: null,
  config: {
    name: '',
    description: '',
    url: '',
    selectors: [],
    checkPeriodMs: 60000,
    scheduleKind: 'interval',
    scheduleTime: '12:00',
    scheduleDays: [],
    scheduleTz: (() => {
      try {
        return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
      } catch {
        return 'UTC';
      }
    })(),
    requiresPlaywright: false,
    preferredRegion: null,
    monitorSetupSteps: [],
    recordedSteps: [],
    credentials: {},
    formData: {},
    apiFunctions: {},
    workflowType: 'recorded',
    segments: [],
    goal: '',
    siteDescription: '',
    availableData: [],
    maxSteps: 20,
    useVision: true,
    aiMode: 'standard',
    userContext: '',
    autoValidate: true,
    useWritAi: false,
    generateWorkflow: true,
    extractors: [],
    streamingHandlers: [],
    advancedScript: '',
    advancedScriptEnabled: false,
    setupStepsCount: 0,
    maxDurationSeconds: 3600,
    multiConversation: false,
    maxConcurrentConversations: 3,
    openaiCompatEnabled: false,
    openaiDefaultHandler: 'chat',
    openaiModelName: 'streaming',
    crawlVerb: 'crawl',
    crawlOutput: 'markdown',
    crawlRenderMode: 'auto',
    crawlOcrMode: 'auto',
    crawlMaxDepth: 4,
    crawlPageBudget: 1000,
    crawlConcurrency: 6,
    crawlShardSize: 25,
    crawlDelayMs: 250,
    crawlRespectRobots: true,
    crawlSameDomain: true,
    crawlAllowSubdomains: true,
    crawlIncludePaths: '',
    crawlExcludePaths: '',
    crawlPersonaId: null,
    crawlIntent: '',
    crawlSeedUrls: [],
    crawlRelevanceThreshold: 0,
    crawlContentPreset: 'main',
    crawlIncludeComments: true,
    crawlKeepImages: true,
    crawlKeepTables: true,
    crawlKeepLinks: true,
    crawlExcludeSelectors: '',
    crawlIncludeSelectors: '',
    timeoutMs: 30000,
    retryCount: 2,
    headless: true,
    fastMode: true,
    trustedAgentsOnly: false,
    executionTarget: 'auto',
    defaultPersonaId: null,
  },
  triggers: {
    triggerRules: [],
    webhookTriggers: [],
    skipTriggers: false,
  },
  apiConfig: {
    linkedApiKeyId: null,
    createNewKey: false,
    newKeyLabel: '',
    newKeyRole: 'client',
    skipApiKey: false,
  },
  expose: {
    connectors: new Set<WizardConnector>(),
    scheduleIntervalMs: 24 * 60 * 60 * 1000,
    workflowSchedule: defaultSchedule(24 * 60 * 60 * 1000),
    connectorsTouched: false,
  },
  testResults: {
    taskId: null,
    status: 'idle',
    durationMs: null,
    extractedData: null,
    error: null,
    screenshots: [],
    selectorResults: [],
  },
  createdIds: {
    targetId: null,
    workflowId: null,
    aiSessionId: null,
    triggerRuleIds: [],
    webhookTriggerIds: [],
    apiKeyId: null,
    apiKeyValue: null,
  },
  isSubmitting: false,
  submitError: null,
};

// ── Actions ────────────────────────────────────────────────────────────────

type WizardAction =
  | { type: 'SET_MODE'; mode: WizardMode }
  | { type: 'GO_TO_STEP'; step: WizardStepId }
  | { type: 'COMPLETE_STEP'; step: WizardStepId }
  | { type: 'UPDATE_CONFIG'; updates: Partial<WizardState['config']> }
  | { type: 'UPDATE_TRIGGERS'; updates: Partial<WizardState['triggers']> }
  | { type: 'UPDATE_API_CONFIG'; updates: Partial<WizardState['apiConfig']> }
  | { type: 'TOGGLE_CONNECTOR'; connector: WizardConnector }
  | { type: 'SET_SCHEDULE_INTERVAL'; ms: number }
  | { type: 'UPDATE_EXPOSE'; updates: Partial<WizardState['expose']> }
  | { type: 'UPDATE_TEST_RESULTS'; updates: Partial<WizardState['testResults']> }
  | { type: 'UPDATE_CREATED_IDS'; updates: Partial<WizardState['createdIds']> }
  | { type: 'SET_SUBMITTING'; submitting: boolean }
  | { type: 'SET_SUBMIT_ERROR'; error: string | null }
  | { type: 'RESET' };

function wizardReducer(state: WizardState, action: WizardAction): WizardState {
  switch (action.type) {
    case 'SET_MODE': {
      // Seed intent-aware connector defaults — but only while the user hasn't
      // manually edited the picker yet, so changing modes mid-flow doesn't wipe a
      // deliberate selection.
      const expose = state.expose.connectorsTouched
        ? state.expose
        : { ...state.expose, connectors: new Set(defaultConnectorsForMode(action.mode)) };
      return {
        ...state,
        mode: action.mode,
        expose,
      };
    }
    case 'GO_TO_STEP':
      return { ...state, currentStep: action.step };
    case 'COMPLETE_STEP': {
      const newCompleted = new Set(state.completedSteps);
      newCompleted.add(action.step);
      return { ...state, completedSteps: newCompleted };
    }
    case 'UPDATE_CONFIG':
      return { ...state, config: { ...state.config, ...action.updates } };
    case 'UPDATE_TRIGGERS':
      return { ...state, triggers: { ...state.triggers, ...action.updates } };
    case 'UPDATE_API_CONFIG':
      return { ...state, apiConfig: { ...state.apiConfig, ...action.updates } };
    case 'TOGGLE_CONNECTOR': {
      const connectors = new Set(state.expose.connectors);
      if (connectors.has(action.connector)) connectors.delete(action.connector);
      else connectors.add(action.connector);
      return { ...state, expose: { ...state.expose, connectors, connectorsTouched: true } };
    }
    case 'SET_SCHEDULE_INTERVAL':
      return { ...state, expose: { ...state.expose, scheduleIntervalMs: action.ms } };
    case 'UPDATE_EXPOSE':
      return { ...state, expose: { ...state.expose, ...action.updates } };
    case 'UPDATE_TEST_RESULTS':
      return { ...state, testResults: { ...state.testResults, ...action.updates } };
    case 'UPDATE_CREATED_IDS':
      return { ...state, createdIds: { ...state.createdIds, ...action.updates } };
    case 'SET_SUBMITTING':
      return { ...state, isSubmitting: action.submitting };
    case 'SET_SUBMIT_ERROR':
      return { ...state, submitError: action.error };
    case 'RESET':
      return {
        ...initialState,
        completedSteps: new Set(),
        expose: { ...initialState.expose, connectors: new Set<WizardConnector>() },
      };
    default:
      return state;
  }
}

// ── Context ────────────────────────────────────────────────────────────────

interface WizardContextValue {
  state: WizardState;
  dispatch: React.Dispatch<WizardAction>;
  steps: WizardStepId[];
  currentStepIndex: number;
  canGoNext: boolean;
  canGoBack: boolean;
  goNext: () => void;
  goBack: () => void;
  goToStep: (step: WizardStepId) => void;
  setMode: (mode: WizardMode) => void;
  updateConfig: (updates: Partial<WizardState['config']>) => void;
  updateTriggers: (updates: Partial<WizardState['triggers']>) => void;
  updateApiConfig: (updates: Partial<WizardState['apiConfig']>) => void;
  toggleConnector: (connector: WizardConnector) => void;
  setScheduleInterval: (ms: number) => void;
  updateTestResults: (updates: Partial<WizardState['testResults']>) => void;
  updateCreatedIds: (updates: Partial<WizardState['createdIds']>) => void;
  completeCurrentStep: () => void;
}

const WizardCtx = createContext<WizardContextValue | null>(null);

export function useWizard(): WizardContextValue {
  const ctx = useContext(WizardCtx);
  if (!ctx) throw new Error('useWizard must be used within WizardProvider');
  return ctx;
}

// ── Provider ───────────────────────────────────────────────────────────────

export const WizardProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [state, dispatch] = useReducer(wizardReducer, {
    ...initialState,
    completedSteps: new Set<WizardStepId>(),
    expose: { ...initialState.expose, connectors: new Set<WizardConnector>() },
  });

  const steps = useMemo(() => getStepsForMode(state.mode), [state.mode]);
  const currentStepIndex = steps.indexOf(state.currentStep);

  const canGoBack = currentStepIndex > 0;
  const canGoNext = useMemo(() => {
    if (currentStepIndex >= steps.length - 1) return false;

    // On the configure step, require recorded steps for manual recording mode
    if (state.currentStep === 'configure') {
      const mode = state.mode;
      if (mode === 'manual_workflow') {
        return state.config.recordedSteps.length > 0;
      }
    }

    return true;
  }, [currentStepIndex, steps.length, state.currentStep, state.mode, state.config.recordedSteps.length]);

  const goNext = useCallback(() => {
    if (!canGoNext) return;
    dispatch({ type: 'COMPLETE_STEP', step: state.currentStep });
    dispatch({ type: 'GO_TO_STEP', step: steps[currentStepIndex + 1] });
  }, [canGoNext, state.currentStep, steps, currentStepIndex]);

  const goBack = useCallback(() => {
    if (!canGoBack) return;
    dispatch({ type: 'GO_TO_STEP', step: steps[currentStepIndex - 1] });
  }, [canGoBack, steps, currentStepIndex]);

  const goToStep = useCallback((step: WizardStepId) => {
    dispatch({ type: 'GO_TO_STEP', step });
  }, []);

  const setMode = useCallback((mode: WizardMode) => {
    dispatch({ type: 'SET_MODE', mode });
  }, []);

  const updateConfig = useCallback((updates: Partial<WizardState['config']>) => {
    dispatch({ type: 'UPDATE_CONFIG', updates });
  }, []);

  const updateTriggers = useCallback((updates: Partial<WizardState['triggers']>) => {
    dispatch({ type: 'UPDATE_TRIGGERS', updates });
  }, []);

  const updateApiConfig = useCallback((updates: Partial<WizardState['apiConfig']>) => {
    dispatch({ type: 'UPDATE_API_CONFIG', updates });
  }, []);

  const toggleConnector = useCallback((connector: WizardConnector) => {
    dispatch({ type: 'TOGGLE_CONNECTOR', connector });
  }, []);

  const setScheduleInterval = useCallback((ms: number) => {
    dispatch({ type: 'SET_SCHEDULE_INTERVAL', ms });
  }, []);

  const updateTestResults = useCallback((updates: Partial<WizardState['testResults']>) => {
    dispatch({ type: 'UPDATE_TEST_RESULTS', updates });
  }, []);

  const updateCreatedIds = useCallback((updates: Partial<WizardState['createdIds']>) => {
    dispatch({ type: 'UPDATE_CREATED_IDS', updates });
  }, []);

  const completeCurrentStep = useCallback(() => {
    dispatch({ type: 'COMPLETE_STEP', step: state.currentStep });
  }, [state.currentStep]);

  const value = useMemo<WizardContextValue>(() => ({
    state,
    dispatch,
    steps,
    currentStepIndex,
    canGoNext,
    canGoBack,
    goNext,
    goBack,
    goToStep,
    setMode,
    updateConfig,
    updateTriggers,
    updateApiConfig,
    toggleConnector,
    setScheduleInterval,
    updateTestResults,
    updateCreatedIds,
    completeCurrentStep,
  }), [state, steps, currentStepIndex, canGoNext, canGoBack, goNext, goBack, goToStep, setMode, updateConfig, updateTriggers, updateApiConfig, toggleConnector, setScheduleInterval, updateTestResults, updateCreatedIds, completeCurrentStep]);

  return <WizardCtx.Provider value={value}>{children}</WizardCtx.Provider>;
};

export { ALL_STEPS, STEPS_BY_MODE };
