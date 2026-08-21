// Agent types
export interface Agent {
  id: string;
  agentId: string;
  platform: string;
  status: string;
  connected: boolean;
  lastSeenAt?: string;
  createdAt: string;
  meta?: Record<string, any>;
  checkCount: number;
  assignedTargets: number;
  avgLatencyMs?: number;
  lastReportAt?: string;
  uptimeHours?: number;
  // Fleet capacity / speed-class fields (platform-admin /agents view).
  speedClass?: 'fast' | 'throughput' | 'balanced';
  perfScore?: number;
  maxSessions?: number;
  activeSessions?: number;
  cpuCores?: number;
  cpuThreads?: number;
  cpuClockMhz?: number;
  ramMb?: number;
  role?: 'infrastructure' | 'user-hosted' | null;
  /**
   * Isolation pool this agent serves on Writ Cloud (Phase 3a). 'isolated' = the
   * gVisor / ephemeral-process pool that may run SENSITIVE (credentialed) runs;
   * 'shared' = the warm-browser pool for non-sensitive cloud runs. BYO agents
   * carry no tier (own-BYO never tiers). Stamped server-side into `meta.tier`;
   * surfaced as a first-class field for the Fleet column + filter.
   */
  tier?: 'shared' | 'isolated' | null;
  /** Geographic region detected via IP geolocation at registration (e.g. us-east). */
  region?: string | null;
  // ── Capabilities (what this agent can actually run) ──
  /** Supported check types, e.g. ["content","uptime","playwright"]. */
  checkModes?: string[];
  /** Can render JS pages / run visual + browser checks (Playwright). */
  hasPlaywright?: boolean;
  /** Trusted to solve/bypass CAPTCHA (e.g. residential IP). */
  captchaTrusted?: boolean;
  /** Shared infrastructure (vs a user's own machine). */
  isTrusted?: boolean;
  /** Monitoring throughput: targets checked per time slot. */
  targetsPerSlot?: number;
  /** Concurrent check workers. */
  parallelWorkers?: number;
}

// Target types
export interface Target {
  id: string;
  url: string;
  selector: string;
  ignoreRegex?: string;
  enabled: boolean;
  checkPeriodMs?: number | null;
  // Precise recurrence (interval | daily | weekly). Interval kind uses checkPeriodMs;
  // daily/weekly use scheduleTime/scheduleDays/scheduleTz. Response is camelCase.
  scheduleKind?: 'interval' | 'daily' | 'weekly' | null;
  scheduleTime?: string | null; // 'HH:MM' local
  scheduleDays?: number[] | null; // 1..7 ISO
  scheduleTz?: string | null; // IANA
  requiresPlaywright?: boolean;
  createdAt: string;
  updatedAt?: string;
  lastChecked?: string;
  checkCount: number;
  changeCount: number;
  assignedAgents: number;
  selectorCount?: number;
  triggerCount?: number;
}

export interface TargetChange {
  id: string;
  targetId: string;
  timestamp: string;
  oldContent: string;
  newContent: string;
  diff: string;
  detectedBy: string;
  // Multi-selector support
  selectorId?: number;
  selectorName?: string;
  // Visual-zone snapshots: same-origin API paths the UI blob-fetches (with the
  // Bearer token) and renders via <AuthImage>. Null when there's no image of
  // that kind for this change (e.g. diff couldn't be computed).
  screenshotBefore?: string | null;
  screenshotAfter?: string | null;
  screenshotDiff?: string | null;
}

/**
 * One row of the global "recent changes" feed (`/targets/changes/recent`),
 * enriched with the monitor URL + selector name + a truncated diff snippet.
 * Snake-cased to mirror the daemon/cloud feed payload the cards read directly.
 */
export interface RecentChange {
  id: number;
  target_id: number;
  target_url: string;
  target_selector_id?: number | null;
  selector_name?: string | null;
  diff_snippet?: string | null;
  first_detected_at: string;
  last_detected_at: string;
}

// Dashboard types
export interface DashboardStats {
  agents_online: number;
  checks_per_second: number;
  last_change_detected?: string;
  avg_latency: number;
  total_targets: number;
  enabled_targets: number;
  errors_last_hour: number;
  cloudflare_blocks: number;
  rate_limit_hits: number;
}

export interface Alert {
  id: number;
  target_url: string;
  content_hash: string;
  diff_snippet: string | null;
  content_before: string | null;
  content_after: string | null;
  agent_count: number;
  received_at: string;
}

export interface AgentMonitoringInfo {
  agent_id: string;
  status: string;
  last_seen: string | null;
  assigned_targets: number;
  total_checks: number;
  error_count: number;
  success_rate: number;
  cloudflare_blocks: number;
  rate_limit_hits: number;
  avg_latency_ms: number | null;
}

// API Key types
export interface ApiKey {
  id: number;
  label: string;
  /** Scope strings, e.g. ["workflows:read", "workflows:execute"]. */
  scopes: string[];
  /** Per-resource object pinning: { workflows: [12, 15] }. Absent = all items. */
  resource_ids?: Record<string, number[]>;
  /** Server-rendered one-liner, used where a full breakdown does not fit. */
  scope_summary?: string;
  /** Preset the grant exactly matches ("read_only" | "run" | "full"), else null. */
  preset?: string | null;
  created: string;
  lastUsed?: string;
  status: 'active' | 'revoked';
  is_scoped: boolean;
  expires_at?: string | null;
  api_key?: string;
}

// Schedule types
export interface ScheduleConfig {
  globalPeriodMs: number;
  quorum: number;
  platformWeights: {
    cloudflare: number;
    vercel: number;
    lambda: number;
    gcp: number;
  };
  timeSlotMode?: 'distributed' | 'rolling';
  redistributionIntervalHours?: number;
}

export interface TimeSlot {
  agentId: string;
  platform: string;
  slotIndex: number;
  timestamp: number;
}

// Log types
export interface LogEntry {
  id: number;
  timestamp: string;
  level: 'debug' | 'info' | 'warn' | 'error';
  agentId?: string;
  targetId?: string;
  message: string;
  metadata?: Record<string, unknown>;
}

export interface LogFilter {
  agentId?: string;
  targetId?: string;
  level?: string;
  actor?: string;
  action?: string;
  startDate?: string;
  endDate?: string;
  search?: string;
}

// Auth types
export interface AuthState {
  apiKey: string | null;
  role: 'admin' | 'viewer' | null;
  isAuthenticated: boolean;
}

// API Response types
export interface ApiResponse<T> {
  success: boolean;
  data?: T;
  error?: string;
  message?: string;
}

export interface PaginatedResponse<T> {
  data: T[];
  total: number;
  page: number;
  pageSize: number;
  totalPages: number;
}

// Notification types
export interface PushoverRecipient {
  id: number;
  name: string;
  user_key_preview: string;
  enabled: boolean;
  created_at: string;
  last_notified_at: string | null;
}

export interface TargetNotificationsResponse {
  target_id: string;
  target_url: string;
  recipients: PushoverRecipient[];
  count: number;
  notification_title: string | null;
  notification_message: string | null;
  notification_priority: number | null;
  notification_sound: string | null;
}

// Automation types
export interface WorkflowStep {
  id?: string;
  type: 'navigate' | 'navigated_to' | 'click' | 'fill' | 'type' | 'select' | 'wait' | 'screenshot' | 'extract' | 'evaluate' | 'api_call' | 'login_post' | 'wait_for_tab' | 'open_tab' | 'tab_closed' | 'assert' | 'ai_fill' | 'ai_continue' | 'ai_navigate' | 'codegen' | 'press' | 'scroll' | 'scroll_into_view' | 'hover' | 'check' | 'uncheck' | 'end_point' | 'captcha' | 'wait_for_change' | 'upload' | 'wait_for_download' | 'twofa' | 'advanced_script';
  selector?: string;
  value?: string;
  url?: string;
  timeout_ms?: number;
  description?: string;
  optional?: boolean;
  enabled?: boolean;
  config?: Record<string, any>;
  options?: Record<string, any>;
  script?: string;
}

export interface ExitCondition {
  type: 'url_contains' | 'url_equals' | 'element_exists' | 'element_text';
  value: string;
}

export interface AutomationWorkflow {
  id: number;
  name: string;
  description?: string;
  workflow_type: 'pre_check' | 'on_change' | 'manual' | 'recorded' | 'scheduled' | 'streaming';
  steps: WorkflowStep[];
  // The polled list omits `steps` for payload size and sends this count instead;
  // the full `steps` come from GET /workflows/{id} (lazy-fetched on expand).
  step_count?: number;
  raw_replay?: Array<Record<string, any>>;  // Raw coordinate-based replay for fallback
  form_data?: Record<string, unknown>;
  // Entry and exit points
  entry_url?: string;
  exit_condition?: ExitCondition;
  timeout_ms: number;
  retry_count: number;
  headless: boolean;
  is_active: boolean;
  fast_mode: boolean;  // Fast execution vs human-like (anti-bot evasion)
  // Schedule settings
  schedule_enabled?: boolean;
  schedule_interval_ms?: number | null;
  // Precise recurrence (interval | daily | weekly). Interval kind uses
  // schedule_interval_ms; daily/weekly use schedule_time/schedule_days/schedule_tz.
  schedule_kind?: 'interval' | 'daily' | 'weekly' | null;
  schedule_time?: string | null; // 'HH:MM' local
  schedule_days?: number[] | null; // 1..7 ISO
  schedule_tz?: string | null; // IANA
  last_scheduled_at?: string | null;
  next_scheduled_at?: string | null;
  // AI Session link
  ai_session_id?: number | null;  // If workflow was generated by an AI session
  default_persona_id?: number | null;  // Default cloud persona (auth identity); run can override
  has_login?: boolean;  // True if the workflow authenticates (login/2FA detected) — gates persona UI
  has_twofa?: boolean;  // True if a step enters a one-time code — runs need a persona with a 2FA method
  // Reverse persona link: personas whose sign-in workflow IS this row.
  // Stamped by the list endpoint only; absent elsewhere.
  login_personas?: Array<{ id: number; name: string }>;
  // Metadata
  created_at: string;
  updated_at: string;
  usage_count?: number;
  // Last run details
  last_run_at?: string | null;
  last_run_duration_ms?: number | null;
  last_run_status?: string | null;
  last_run_task_id?: number | null;
  last_run_error?: string | null;
  /** One record, or a LIST of records when the run extracted multiple rows. */
  last_run_extracted_data?: Record<string, any> | unknown[] | null;
  // Presence flag from the polled list (the blob itself is lazy-fetched).
  last_run_has_extracted_data?: boolean;
  // Captcha handling
  captcha_blocked: boolean;
  last_captcha_at?: string | null;
  // Failure tracking
  consecutive_failures?: number;
  total_failure_count?: number;
  // Browserless HTTP execution lane
  auth_config?: {
    version?: number;
    kind?: 'http' | 'browser' | 'none';
    http?: boolean;
    login?: { steps?: Array<{ challenges?: Array<{ type?: string }> }> };
  } | null;
  http_capable?: boolean | null;
  // AI Repair
  ai_repair_enabled?: boolean;
  ai_repair_history?: Array<{
    repaired_at: string;
    // 'simple' | 'complex' = recorded-step repair. 'script' | 'function' =
    // streaming advanced_script / functions[] JS repair (old_code/new_code carry the
    // before/after; step arrays are empty for these).
    repair_type: 'simple' | 'complex' | 'script' | 'function';
    error_message: string;
    // Step repairs (simple/complex) populate these:
    failed_step_index?: number | null;
    old_steps?: WorkflowStep[];
    new_steps?: WorkflowStep[];
    task_id?: number;
    // Streaming script/function repairs (script/function) populate these:
    old_code?: string;
    new_code?: string;
    explanation?: string;
    target_name?: string | null;
  }>;
  last_repaired_at?: string | null;
  repair_count?: number;
  // Streaming mode config
  streaming_config?: Record<string, any> | null;
  // API recorder function definitions
  api_functions?: Record<string, any> | null;
  // Callable functions (step-groups / script / extraction) created from recorded steps
  functions?: WorkflowFunction[] | null;
  // ── Marketplace install PROXY (read-only mirror of an installed listing) ──
  /** True ⇒ this is a read-only proxy of an installed marketplace listing.
   *  Server omits steps/raw_replay and most edit affordances 403. */
  is_installed?: boolean;
  /** WorkflowListing this proxy was installed from (drives the marketplace link). */
  source_listing_id?: number | null;
  /** Display name of the creator (resolved from the source workflow's owner). */
  creator_name?: string | null;
  /** Mirrors InstalledWorkflow.status: 'active' | 'needs_attach' | 'disabled'. */
  installed_status?: 'active' | 'needs_attach' | 'disabled' | null;
  /** Slug of the source listing (for marketplace deep-links). */
  source_listing_slug?: string | null;
  /** Data-less required-data manifest (names/types only) for the attach-data UI. */
  data_manifest?: import('../api/marketplace').DataManifest | null;
  /** FILE ASSETS (§7.3): declared file input slots a runner binds their own stored
   *  file to before a run. Computed server-side from upload-step config.file_slot
   *  (present even when the polled list omits `steps`). The run modal renders a
   *  file picker per slot and sends `files: {slot: file_id}` in the run request. */
  file_slots?: Array<{ slot: string; label?: string | null; is_multiple?: boolean }>;
}

export interface WorkflowFunction {
  name: string;
  type: 'steps' | 'script' | 'extraction';
  description?: string;
  /** Contiguous [start, end) range over the workflow's steps */
  step_range?: [number, number];
  /** Exact step indices (may be non-contiguous) selected in the recorder */
  step_indices?: number[];
  depends_on?: string[];
  input_variables?: Array<{ name: string; type: string; description: string; required: boolean } | string>;
  output_fields?: Array<{ name: string; type: string; description: string; selector?: string }>;
  handler_event?: string;
  selector?: string;
  js_expression?: string;
}

/**
 * Config of a `type:'advanced_script'` WorkflowStep (Feature #2). The streaming
 * advanced script becomes a first-class step so it can declare TYPED callable
 * functions. On save the backend (`_sync_advanced_script_functions`) merges
 * `functions` into `workflow.functions` — the single source MCP / Managed-API /
 * the marketplace output-manifest read from. `persistent` keeps the injected
 * runtime across the whole session (defaults true).
 */
export interface AdvancedScriptStepConfig {
  code: string;
  persistent?: boolean;
  functions?: WorkflowFunction[];
}

export interface TaskResultData {
  steps_completed?: number;
  duration_ms?: number;
  extracted_data?: Record<string, any>;
  captcha_detected?: boolean;
  captcha_info?: {
    detected: boolean;
    type?: string;
    step_failed?: string;
    from_error_message?: boolean;
  };
}

export interface AutomationTask {
  id: number;
  target_id: number;
  workflow_id: number;
  detected_change_id?: number;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  trigger_type: 'pre_check' | 'on_change' | 'manual';
  executor_agent_id?: string;
  success?: boolean;
  result_data?: TaskResultData;
  error_message?: string;
  screenshots?: Array<{
    step?: string;
    type?: string;
    timestamp?: string;
    error?: string;
    data_base64?: string;
  }>;
  attempt_count: number;
  max_attempts: number;
  started_at?: string;
  completed_at?: string;
  created_at: string;
  duration_ms?: number;
  workflow?: AutomationWorkflow;
  target?: Target;
  // AI Repair
  ai_repair_attempted?: boolean;
  ai_repair_result?: {
    success: boolean;
    repair_type?: 'simple' | 'complex';
  };
  // Which lane ran this task: 'http' (browserless), 'browser', or 'hybrid'.
  engine?: 'http' | 'browser' | 'hybrid';
}

export interface TargetAutomation {
  target_id: number;
  pre_check_workflow_id?: number;
  on_change_workflow_id?: number;
  on_change_enabled: boolean;
  on_change_conditions?: {
    contains?: string;
    not_contains?: string;
    regex?: string;
  };
  pre_check_workflow?: AutomationWorkflow;
  on_change_workflow?: AutomationWorkflow;
  has_auth_session: boolean;
}

// AI Workflow Session types
export interface GeneratedWorkflowInfo {
  id: number;
  name: string;
  workflow_type: string;
  steps_count: number;
  created_at: string;
}

export interface AIWorkflowSession {
  id: number;
  name: string;
  description?: string;
  goal: string;
  entry_url: string;
  form_data: Record<string, unknown>;
  has_credentials: boolean;
  ai_model?: string;
  max_steps: number;
  // Intelligent mode settings
  mode: 'standard' | 'intelligent' | 'api_discovery';
  user_context?: string;
  max_actions: number;
  actions_taken?: number;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  error_message?: string;
  ai_conversation: Array<Record<string, any>>;
  screenshots: Array<Record<string, any>>;
  headless: boolean;
  timeout_ms: number;
  auto_validate: boolean;
  /** Default persona supplying auth identity for runs (null = manual login). */
  default_persona_id?: number | null;
  validation_status?: 'pending' | 'passed' | 'failed';
  created_at: string;
  started_at?: string;
  completed_at?: string;
  steps_taken: number;
  workflows_generated: number;
  generated_workflows: GeneratedWorkflowInfo[];
  // Progress tracking
  progress_message?: string;
  current_url?: string;
  run_count: number;
  last_run_at?: string;
  // Task id of the most recent run — used to abort an in-flight AI session.
  last_run_task_id?: number | null;
  // Delegated "Get a task done" scheduling (mirrors AutomationWorkflow).
  schedule_enabled?: boolean;
  schedule_interval_ms?: number | null;
  next_scheduled_at?: string | null;
  last_scheduled_at?: string | null;
}

export interface AISessionCreate {
  name: string;
  description?: string;
  goal: string;
  entry_url: string;
  form_data?: Record<string, unknown>;
  credentials?: Record<string, string>;
  ai_model?: string;
  max_steps?: number;
  // Intelligent mode settings
  mode?: 'standard' | 'intelligent' | 'api_discovery';
  user_context?: string;
  max_actions?: number;
  headless?: boolean;
  timeout_ms?: number;
  auto_validate?: boolean;
  /** When the autonomous AI session finishes, save the steps it took as a reusable
      workflow. Forwarded as `generate_workflow` to the coordinator's AI-session start. */
  generate_workflow?: boolean;
  /** Force this session's AI through the managed cloud gateway instead of the agent's local keys. */
  use_writ_ai?: boolean;
  /** Pin runs to an agent: 'auto'|'cloud'|an agent_id (BYO local agent → free AI). */
  execution_target?: string;
  /** Default persona supplying login credentials / 2FA for runs; a run can override. */
  persona_id?: number | null;
  // Phase 4 — delegated "Get a task done" scheduling. When schedule_enabled is set,
  // central_scheduler fires the session on schedule_interval_ms (mirrors workflows).
  schedule_enabled?: boolean;
  schedule_interval_ms?: number | null;
  /** First scheduled fire time; defaults to now + interval when omitted. */
  next_scheduled_at?: string | null;
}

export interface AISessionUpdate {
  name?: string;
  description?: string;
  goal?: string;
  entry_url?: string;
  form_data?: Record<string, unknown>;
  credentials?: Record<string, string>;
  // Intelligent mode settings
  mode?: 'standard' | 'intelligent' | 'api_discovery';
  user_context?: string;
  max_actions?: number;
  status?: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  error_message?: string;
  ai_conversation?: Array<Record<string, any>>;
  screenshots?: Array<Record<string, any>>;
  validation_status?: 'pending' | 'passed' | 'failed';
  /** Default persona for runs; pass 0/null to clear. */
  persona_id?: number | null;
  // Phase 4 — toggle/adjust delegated scheduling on an existing session.
  schedule_enabled?: boolean;
  schedule_interval_ms?: number | null;
}

// ── Streaming Mode Types ──────────────────────────────────────────────────

export interface StreamingHandler {
  name: string;
  type: 'steps' | 'script';
  input_variables?: string[];
  extract_fields?: string[];
  persistent?: boolean;
}

export interface StreamingSession {
  session_key: string;
  status: 'starting' | 'queued' | 'running' | 'ending' | 'ended' | 'failed';
  target_url: string;
  current_url?: string;
  agent_id?: string;
  workflow_id: number;
  handlers: StreamingHandler[];
  events_emitted: number;
  commands_received: number;
  started_at?: string;
  last_activity_at?: string;
  ended_at?: string;
  end_reason?: string;
  max_duration_seconds: number;
  error_message?: string;
}

export interface StreamingEvent {
  type: string;
  session_key: string;
  event_name?: string;
  data: any;
  timestamp: number;
}

export interface StreamingConfig {
  setup_steps_count: number;
  handlers: Array<{
    name: string;
    type: 'steps';
    step_range: [number, number];
    input_variables: string[];
    extract_fields: string[];
  }>;
  advanced_script?: {
    enabled: boolean;
    code: string;
    persistent: boolean;
    /** Typed callable functions declared on the advanced script (Feature #2).
     *  Authoritative declaration lives on the `advanced_script` STEP; mirrored
     *  here for back-compat with sessions that still read streaming_config. */
    functions?: WorkflowFunction[];
  };
  openai_compat?: {
    enabled: boolean;
    default_handler: string;
    model_name: string;
    response_field: string;
  };
  keepalive_interval_ms?: number;
  // Multi-conversation routing. When false (default), every message reuses the
  // single main tab. When true, conversations get their own tabs up to
  // max_concurrent_threads, sharing or isolating state per context_mode.
  multi_conversation?: boolean;
  context_mode?: 'shared' | 'isolated';
  max_concurrent_threads?: number;
  max_sessions?: number;
  distribute_ips?: boolean;
  max_duration_seconds?: number;
}

// ---------------------------------------------------------------------------
// Personas — authenticated identities (login + 2FA + warm session)
// ---------------------------------------------------------------------------
export type TwoFactorMethod = 'none' | 'totp' | 'email_otp' | 'sms';

export interface Persona {
  id: number;
  name: string;
  description?: string | null;
  target_domain?: string | null;
  login_username?: string | null;
  has_password: boolean;
  twofa_method: TwoFactorMethod;
  has_totp_seed: boolean;
  email_otp_mode?: 'oauth_mailbox' | 'relay' | null;
  mail_connection_id?: number | null;
  connected_mailbox?: string | null;
  relay_address?: string | null;
  has_fingerprint: boolean;
  /** True if a BYO/residential proxy is configured for this persona. */
  has_proxy: boolean;
  /** When the owner acknowledged lawful use of the configured proxy. */
  proxy_lawful_use_ack_at?: string | null;
  preferred_agent_id?: string | null;
  is_active: boolean;
  validation_status: string;
  has_warm_session: boolean;
  session_expires_at?: string | null;
  last_login_at?: string | null;
  last_used_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  linked_workflows?: { id: number; name: string }[];
  /** {credential_field: vault_secret_key} for fields linked to a vault secret. */
  linked_secrets?: Record<string, string>;
  /** Workflow that SIGNS THIS PERSONA IN. Without one the persona can only use a
   * session captured elsewhere, so an expired session is a dead end; with one it
   * re-logins on its own (on demand, and automatically when a crawl finds it stale). */
  login_workflow_id?: number | null;
  login_workflow_name?: string | null;
  /** Why the most recent sign-in attempt failed (cleared on success). */
  last_login_error?: string | null;
  /** True when the persona can sign itself in — i.e. has a login workflow. Gate
   * "needs setup" on THIS, not has_warm_session (which is only a point-in-time fact). */
  can_self_login?: boolean;
}

export interface PersonaSignInResult {
  ok: boolean;
  error?: string | null;
  has_warm_session: boolean;
  session_expires_at?: string | null;
}

export interface PersonaCreate {
  name: string;
  description?: string;
  target_domain?: string;
  login_username?: string;
  password?: string;
  extra_login_fields?: Record<string, string>;
  twofa_method?: TwoFactorMethod;
  totp_seed?: string;
  totp_digits?: number;
  totp_period_seconds?: number;
  totp_algorithm?: string;
  email_otp_mode?: 'oauth_mailbox' | 'relay';
  mail_connection_id?: number;
  relay_address?: string;
  otp_extract_config?: Record<string, any>;
  fingerprint?: Record<string, any>;
  preferred_agent_id?: string;
  // ── BYO/residential proxy ──
  /** Proxy URL or host:port, e.g. http://host:port. Write-only. */
  proxy_server?: string;
  /** Proxy auth username (write-only). */
  proxy_username?: string;
  /** Proxy auth password (write-only). */
  proxy_password?: string;
  /** Required (true) when proxy_server is set: acknowledges lawful use of the proxy. */
  proxy_lawful_use_ack?: boolean;
  /** Workflow that signs this persona in (establishes/refreshes its warm session). */
  login_workflow_id?: number | null;
}

export type PersonaUpdate = Partial<PersonaCreate & { is_active: boolean }>;

export interface MailConnection {
  id: number;
  provider: string;
  email: string;
  is_active: string;
  created_at?: string | null;
}

export interface PersonaRun {
  task_id: number;
  workflow_id?: number | null;
  workflow_name?: string | null;
  status?: string | null;
  success?: boolean | null;
  started_at?: string | null;
  completed_at?: string | null;
  error?: string | null;
}

export interface ImapConfig {
  host: string;
  port?: number | null;
  username: string;
  password: string;
  use_ssl?: boolean;
  mailbox?: string | null;
}

// ── Importing EXISTING authenticator seeds → TOTP personas ──
/** One parsed account in an authenticator import — secret-free preview. */
export interface AuthImportEntryPreview {
  idx: number;
  issuer?: string | null;
  label?: string | null;
  suggested_name: string;
  suggested_domain?: string | null;
  algorithm: string;
  digits: number;
  period: number;
}

export interface AuthImportParseResult {
  /** Single-use token referencing the staged seeds. */
  import_token: string;
  count: number;
  entries: AuthImportEntryPreview[];
}

/** A user-chosen account to turn into a persona, with optional overrides. */
export interface AuthImportSelection {
  idx: number;
  name?: string;
  target_domain?: string;
  login_username?: string;
}

export interface AuthImportCommitResult {
  created: Persona[];
  skipped: { idx: number; reason: string }[];
}
