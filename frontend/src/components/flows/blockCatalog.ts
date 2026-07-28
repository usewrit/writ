// Canonical block catalog — the single source of truth for every automation block.
//
// This drives four things that used to be defined ad-hoc in separate places:
//   1. The AI automation generator's prompt (`catalogPromptDigest`) — the "clear
//      explanation so the AI knows what block to generate".
//   2. The add-block menu / source picker (capability-aware — desktop hides or gates
//      cloud-only blocks).
//   3. Save-time validation + AI-spec repair (`specValidator.ts`).
//   4. Runtime capability gating on the desktop app.
//
// Keep this in sync with the desktop daemon interpreter (`src/local/flow.rs`) and the
// cloud trigger service. The daemon/backend expose the same shape via `/…/schema`.

export type Platform = 'cloud' | 'desktop';

export type RefKind =
  | 'workflow'
  | 'target'
  | 'selector'
  | 'ai_session'
  | 'persona'
  | 'file'
  | 'recipient'
  | 'data_collection';

export interface BlockField {
  key: string;
  type: 'string' | 'number' | 'boolean' | 'enum' | 'ref' | 'template' | 'string[]' | 'object' | 'regex';
  required?: boolean;
  ref?: RefKind;
  enumValues?: string[];
  help?: string;
}

export interface BlockDef {
  category: 'event' | 'condition' | 'action';
  blockType: string;
  /** i18n natural-language label. */
  label: string;
  /** One-line summary — shown in the add-menu AND fed to the AI. */
  summary: string;
  /** "Use this when…" guidance for the AI generator. */
  when: string;
  platforms: Platform[];
  /**
   * 'ga' (default) = renders + executes today. 'planned' = defined for the roadmap
   * but not yet wired into the editor/interpreter, so it is hidden from the add-menu,
   * excluded from the AI prompt digest, and rejected by the spec validator. Flip a
   * type to 'ga' as its editor + execution land.
   */
  status?: 'ga' | 'planned';
  /** Shown when the block is gated on a platform that doesn't support it. */
  cloudOnlyReason?: string;
  /** Can this block be the root event of an automation? */
  isSource?: boolean;
  config: BlockField[];
  /** Which reference lists this block's config points at (for linking + validation). */
  linksTo?: RefKind[];
  /** Output keys downstream blocks may reference via {{...}} placeholders. */
  produces?: string[];
}

// --- Condition operators -----------------------------------------------------

export interface OperatorDef {
  value: string;
  label: string;
  needsValue: boolean;
  numeric?: boolean;
  help: string;
}

export const CONDITION_OPERATORS: OperatorDef[] = [
  { value: 'exists', label: 'exists', needsValue: false, help: 'Field is present and non-empty' },
  { value: 'changed', label: 'has changed', needsValue: false, help: 'Value differs from the previous run' },
  { value: 'contains', label: 'contains', needsValue: true, help: 'Text contains the value' },
  { value: 'not_contains', label: 'does not contain', needsValue: true, help: 'Text does not contain the value' },
  { value: 'equals', label: 'equals', needsValue: true, help: 'Exact match (numeric when both sides are numbers)' },
  { value: 'not_equals', label: 'not equals', needsValue: true, help: 'Not an exact match' },
  { value: 'matches', label: 'matches regex', needsValue: true, help: 'Matches the regular expression' },
  { value: 'gt', label: 'greater than', needsValue: true, numeric: true, help: 'Numeric greater-than' },
  { value: 'gte', label: 'greater or equal', needsValue: true, numeric: true, help: 'Numeric greater-or-equal' },
  { value: 'lt', label: 'less than', needsValue: true, numeric: true, help: 'Numeric less-than' },
  { value: 'lte', label: 'less or equal', needsValue: true, numeric: true, help: 'Numeric less-or-equal' },
];

// --- Notification channels (channel-level capability) ------------------------

export interface ChannelDef {
  value: string;
  label: string;
  platforms: Platform[];
  cloudOnlyReason?: string;
  /** For local delivery, does it need a target URL/config field? */
  needsWebhookUrl?: boolean;
}

export const NOTIFICATION_CHANNELS: ChannelDef[] = [
  { value: 'webhook', label: 'Outbound webhook', platforms: ['cloud', 'desktop'], needsWebhookUrl: true },
  { value: 'desktop', label: 'Desktop notification', platforms: ['desktop', 'cloud'] },
  { value: 'in_app', label: 'In-app alert', platforms: ['desktop', 'cloud'] },
  { value: 'email', label: 'Email', platforms: ['cloud'], cloudOnlyReason: 'Email delivery needs the cloud relay' },
  { value: 'pushover', label: 'Pushover', platforms: ['cloud'], cloudOnlyReason: 'Push delivery needs the cloud relay' },
  { value: 'twilio', label: 'SMS', platforms: ['cloud'], cloudOnlyReason: 'SMS delivery needs the cloud relay' },
  { value: 'whatsapp', label: 'WhatsApp', platforms: ['cloud'], cloudOnlyReason: 'WhatsApp delivery needs the cloud relay' },
  { value: 'signal', label: 'Signal', platforms: ['cloud'], cloudOnlyReason: 'Signal delivery needs the cloud relay' },
];

// --- The catalog -------------------------------------------------------------

const BOTH: Platform[] = ['cloud', 'desktop'];

export const BLOCK_CATALOG: Record<string, BlockDef> = {
  // ---- Event blocks (triggers / "how it starts") ----
  change_detected: {
    category: 'event',
    blockType: 'change_detected',
    label: 'Content Change',
    summary: 'Runs when a watched page or selector changes.',
    when: 'The user wants to react to a website changing — price drops, stock, new posts, status pages. Bind to an existing monitor via target_id, or create a new one with url + wizardSelectors.',
    platforms: BOTH,
    isSource: true,
    config: [
      { key: 'url', type: 'string', help: 'Page to watch (when creating a new monitor)' },
      { key: 'target_id', type: 'ref', ref: 'target', help: 'Existing monitor to bind to' },
      { key: 'selector_id', type: 'ref', ref: 'selector', help: 'Specific selector on the target' },
      { key: 'wizardSelectors', type: 'object', help: 'New selectors [{name, selector}] when creating a monitor' },
      { key: 'check_period_ms', type: 'number', help: 'How often to check' },
      { key: 'cooldown_minutes', type: 'number', help: 'Minimum minutes between fires' },
      { key: 'max_fires', type: 'number', help: 'Stop after N fires' },
    ],
    linksTo: ['target', 'selector'],
    produces: ['extracted', 'change_id', 'target_name'],
  },
  monitor_down: {
    category: 'event',
    blockType: 'monitor_down',
    label: 'Monitor Down',
    summary: 'Runs when a monitored page becomes unreachable.',
    when: 'React to a site going DOWN — the uptime probe fails or the page can’t be reached. Bind an existing monitor via target_id (health is whole-monitor, no selector).',
    platforms: BOTH,
    isSource: true,
    config: [
      { key: 'target_id', type: 'ref', ref: 'target', required: true, help: 'Existing monitor to watch' },
      { key: 'cooldown_minutes', type: 'number', help: 'Minimum minutes between fires' },
      { key: 'max_fires', type: 'number', help: 'Stop after N fires' },
    ],
    linksTo: ['target'],
    produces: ['target_name', 'url', 'state'],
  },
  monitor_stale: {
    category: 'event',
    blockType: 'monitor_stale',
    label: 'Monitor Stale',
    summary: 'Runs when a monitor stops updating within its expected cadence.',
    when: 'React to a monitor going quiet — no fresh check for well past its interval (an agent stopped reporting, the page hangs). Bind an existing monitor via target_id.',
    platforms: BOTH,
    isSource: true,
    config: [
      { key: 'target_id', type: 'ref', ref: 'target', required: true, help: 'Existing monitor to watch' },
      { key: 'cooldown_minutes', type: 'number', help: 'Minimum minutes between fires' },
      { key: 'max_fires', type: 'number', help: 'Stop after N fires' },
    ],
    linksTo: ['target'],
    produces: ['target_name', 'url', 'state'],
  },
  monitor_recovered: {
    category: 'event',
    blockType: 'monitor_recovered',
    label: 'Monitor Recovered',
    summary: 'Runs when a monitor returns to healthy after being down or stale.',
    when: 'React to a monitor coming back — a fresh successful check after it was down or stale. Pair with monitor_down/monitor_stale for up/down alerting. Bind target_id.',
    platforms: BOTH,
    isSource: true,
    config: [
      { key: 'target_id', type: 'ref', ref: 'target', required: true, help: 'Existing monitor to watch' },
      { key: 'cooldown_minutes', type: 'number', help: 'Minimum minutes between fires' },
      { key: 'max_fires', type: 'number', help: 'Stop after N fires' },
    ],
    linksTo: ['target'],
    produces: ['target_name', 'url', 'state'],
  },
  webhook_received: {
    category: 'event',
    blockType: 'webhook_received',
    label: 'Webhook Received',
    summary: 'Runs when an external system POSTs to a generated URL.',
    when: 'The user wants an external app/service to trigger the automation via HTTP. Generates a URL + token on save; downstream blocks read the JSON body via {{payload.*}}.',
    platforms: BOTH,
    isSource: true,
    config: [
      { key: 'webhook_trigger_token', type: 'string', help: 'Generated on save' },
      { key: 'custom_path', type: 'string', help: 'Optional friendly URL alias' },
    ],
    produces: ['payload'],
  },
  scheduled: {
    category: 'event',
    blockType: 'scheduled',
    label: 'Schedule',
    summary: 'Runs on a time interval or cron schedule.',
    when: 'The user wants a time-based trigger — "every morning", "every 15 minutes", "weekdays at 9am". Prefer this over a monitor when there is no page to watch.',
    platforms: BOTH,
    isSource: true,
    config: [
      { key: 'interval_ms', type: 'number', help: 'Fixed interval in ms (alternative to cron)' },
      { key: 'cron', type: 'string', help: 'Cron expression' },
      { key: 'timezone', type: 'string', help: 'IANA timezone, e.g. Europe/Paris' },
    ],
    produces: ['now_time'],
  },
  workflow_started: {
    category: 'event',
    blockType: 'workflow_started',
    label: 'Workflow Started',
    summary: 'Runs when a chosen workflow begins.',
    when: 'The user wants to react to one of their workflows starting. Bind workflow_id.',
    platforms: BOTH,
    isSource: true,
    config: [{ key: 'workflow_id', type: 'ref', ref: 'workflow', required: true }],
    linksTo: ['workflow'],
    produces: ['workflow_status'],
  },
  workflow_completed: {
    category: 'event',
    blockType: 'workflow_completed',
    label: 'Workflow Completed',
    summary: 'Runs when a chosen workflow finishes (success or error).',
    when: 'Chain off a workflow result. Bind workflow_id; set status_condition="error" for failure-only, "success" for success-only.',
    platforms: BOTH,
    isSource: true,
    config: [
      { key: 'workflow_id', type: 'ref', ref: 'workflow', required: true },
      { key: 'status_condition', type: 'enum', enumValues: ['any', 'success', 'error'] },
    ],
    linksTo: ['workflow'],
    produces: ['result', 'success', 'status', 'error', 'workflow_duration_seconds'],
  },
  ai_session_started: {
    category: 'event',
    blockType: 'ai_session_started',
    label: 'AI Session Started',
    summary: 'Runs when an autonomous AI goal session starts working.',
    when: 'React to an autonomous AI goal session starting (the browser agent that works toward a goal and records a workflow — not the Scribe chat concierge). Runs on the desktop daemon or in the cloud.',
    platforms: BOTH,
    isSource: true,
    config: [{ key: 'ai_session_id', type: 'ref', ref: 'ai_session' }],
    linksTo: ['ai_session'],
  },
  ai_session_completed: {
    category: 'event',
    blockType: 'ai_session_completed',
    label: 'AI Session Completed',
    summary: 'Runs when an autonomous AI goal session finishes (its recorded workflow is ready).',
    when: 'React to an autonomous AI goal session finishing — success or error. On success its steps are saved as a replayable workflow (this is not the Scribe chat concierge). Runs on the desktop daemon or in the cloud.',
    platforms: BOTH,
    isSource: true,
    config: [
      { key: 'ai_session_id', type: 'ref', ref: 'ai_session' },
      { key: 'status_condition', type: 'enum', enumValues: ['any', 'success', 'error'] },
    ],
    linksTo: ['ai_session'],
    produces: ['ai_result'],
  },
  streaming_session_started: {
    category: 'event',
    blockType: 'streaming_session_started',
    label: 'Streaming Session Started',
    summary: 'Runs when a live streaming session for a workflow starts.',
    when: 'React to a long-lived streaming browser session starting. Desktop-native analogue of an AI session.',
    platforms: ['desktop'], // cloud backend has no handler/emitter yet — daemon-only
    isSource: true,
    config: [{ key: 'workflow_id', type: 'ref', ref: 'workflow' }],
    linksTo: ['workflow'],
    produces: ['session_key', 'target_url', 'status'],
  },
  streaming_session_ended: {
    category: 'event',
    blockType: 'streaming_session_ended',
    label: 'Streaming Session Ended',
    summary: 'Runs when a live streaming session for a workflow ends.',
    when: 'React to a streaming session finishing.',
    platforms: ['desktop'], // cloud backend has no handler/emitter yet — daemon-only
    isSource: true,
    config: [{ key: 'workflow_id', type: 'ref', ref: 'workflow' }],
    linksTo: ['workflow'],
    produces: ['session_key', 'target_url', 'status'],
  },
  data_extracted: {
    category: 'event',
    blockType: 'data_extracted',
    label: 'Data Extracted',
    summary: 'Runs when a workflow produces new extracted rows.',
    when: 'The user wants to react to fresh scraped/extracted data arriving. Bind the source workflow_id; downstream can save/filter the rows.',
    platforms: ['desktop'], // cloud backend has no handler/emitter yet — daemon-only
    isSource: true,
    config: [
      { key: 'workflow_id', type: 'ref', ref: 'workflow', required: true },
      { key: 'min_rows', type: 'number', help: 'Only fire when at least N rows were produced' },
    ],
    linksTo: ['workflow'],
    produces: ['row_count', 'columns', 'last_data_at'],
  },
  file_uploaded: {
    category: 'event',
    blockType: 'file_uploaded',
    label: 'File Received',
    summary: 'Runs when a file is uploaded or arrives via the file webhook.',
    when: 'The user wants to react to a new file — an upload, or an external POST to the file webhook.',
    platforms: ['desktop'], // cloud backend has no handler/emitter yet — daemon-only
    isSource: true,
    config: [{ key: 'source', type: 'string', help: 'Optional source filter (upload | api | workflow_output)' }],
    produces: ['file_id', 'filename', 'bytes', 'source'],
  },
  crawl_completed: {
    category: 'event',
    blockType: 'crawl_completed',
    label: 'Crawl Finished',
    summary: 'Runs when a Dragnet whole-site crawl converges (all pages collected).',
    when: 'Chain off a finished crawl — notify, or run a workflow over the collected pages (read them via the Workflow Data API keyed on {{data_workflow_id}}). Fires ONCE per crawl on convergence, not per page. Filter with conditions on {{seed_host}} / {{crawl_name}}.',
    platforms: BOTH,
    isSource: true,
    config: [
      { key: 'seed_host', type: 'string', help: 'Only fire for crawls of this host (optional)' },
      { key: 'min_pages', type: 'number', help: 'Only fire when at least N pages were collected' },
    ],
    produces: ['crawl_id', 'data_workflow_id', 'seed_url', 'seed_host', 'crawl_name', 'pages_done', 'pages_failed', 'pages_discovered', 'status'],
  },
  crawl_started: {
    category: 'event',
    blockType: 'crawl_started',
    label: 'Crawl Started',
    summary: 'Runs when a Dragnet crawl begins mapping a site.',
    when: 'React to a crawl kicking off — e.g. log it or notify a channel. Filter with conditions on {{seed_host}}.',
    platforms: BOTH,
    isSource: true,
    config: [
      { key: 'seed_host', type: 'string', help: 'Only fire for crawls of this host (optional)' },
    ],
    produces: ['crawl_id', 'data_workflow_id', 'seed_url', 'seed_host', 'crawl_name', 'status'],
  },
  crawl_failed: {
    category: 'event',
    blockType: 'crawl_failed',
    label: 'Crawl Failed',
    summary: 'Runs when a Dragnet crawl ends in failure.',
    when: 'React to a crawl that could not complete — alert someone or retry a narrower crawl.',
    platforms: BOTH,
    isSource: true,
    config: [
      { key: 'seed_host', type: 'string', help: 'Only fire for crawls of this host (optional)' },
    ],
    produces: ['crawl_id', 'data_workflow_id', 'seed_url', 'seed_host', 'crawl_name', 'error', 'status'],
  },

  // ---- Condition block ----
  condition: {
    category: 'condition',
    blockType: 'condition',
    label: 'Condition',
    summary: 'Continues only when a field matches a rule.',
    when: 'Filter the flow — only proceed when an upstream value matches. Reference upstream outputs by dot-path (e.g. result.price, payload.status, extracted.title).',
    platforms: BOTH,
    config: [
      { key: 'field', type: 'string', required: true, help: 'Dot-path into an upstream output' },
      { key: 'operator', type: 'enum', required: true, enumValues: CONDITION_OPERATORS.map((o) => o.value) },
      { key: 'value', type: 'string', help: 'Comparison value (not needed for exists/changed)' },
    ],
  },

  // ---- Control-flow block ----
  for_each: {
    category: 'action',
    blockType: 'for_each',
    label: 'For Each',
    summary: 'Runs its child blocks once per item of an upstream list.',
    when: 'Iterate over a list from an upstream output — e.g. extracted rows or result items. Set source to the dot-path of the array (result.rows, extracted.items). Inside the loop, reference the current element as {{item}} (or {{item.field}}) and its position as {{item_index}}. Bounded by max_items.',
    platforms: BOTH,
    config: [
      { key: 'source', type: 'string', required: true, help: 'Dot-path to the array to iterate (e.g. result.rows, extracted.items)' },
      { key: 'max_items', type: 'number', help: 'Safety cap on iterations (default 1000)' },
    ],
    produces: ['item', 'item_index'],
  },

  // ---- Action blocks ----
  workflow: {
    category: 'action',
    blockType: 'workflow',
    label: 'Run Workflow',
    summary: 'Runs one of the user’s recorded workflows.',
    when: 'Do something on a site — the core action. Bind workflow_id; map inputs from upstream data via input_mapping; attach persona_id for a logged-in run. For flaky targets set retry (+ retry_backoff_ms); set on_error="stop" to halt the rest of this chain when the run fails.',
    platforms: BOTH,
    config: [
      { key: 'workflow_id', type: 'ref', ref: 'workflow', required: true },
      { key: 'input_mapping', type: 'object', help: 'Map workflow inputs to {{placeholders}}' },
      { key: 'persona_id', type: 'ref', ref: 'persona', help: 'Logged-in identity to run as' },
      { key: 'retry', type: 'number', help: 'Retry this many extra times if the run fails' },
      { key: 'retry_backoff_ms', type: 'number', help: 'Pause between retries (ms)' },
      { key: 'on_error', type: 'enum', enumValues: ['continue', 'stop'], help: 'continue (default) or stop the chain on failure' },
    ],
    linksTo: ['workflow', 'persona'],
    produces: ['result', 'success', 'status', 'error'],
  },
  crawl: {
    category: 'action',
    blockType: 'crawl',
    label: 'Crawl Site',
    summary: 'Kicks off a Dragnet whole-site crawl and collects every page.',
    when: 'Sweep an entire site — e.g. when a monitor changes, re-crawl its site; or crawl on a schedule. Leave seed_url blank to crawl the monitor that triggered this flow; or set an explicit URL / {{placeholder}} (e.g. {{extracted.link}}). Runs async — collected pages land under a dataset read via the Workflow Data API; use a "Crawl Finished" trigger to chain what happens next. A crawl of the same site already running is skipped.',
    platforms: BOTH,
    config: [
      { key: 'seed_url', type: 'template', help: 'URL to crawl. Blank = the monitor that fired this flow. Supports {{placeholders}}.' },
      { key: 'name', type: 'template', help: 'Optional label for the crawl' },
      { key: 'intent', type: 'template', help: 'Plain-English goal — scopes the crawl and crawls matching pages first' },
      { key: 'extract_mode', type: 'enum', enumValues: ['markdown', 'schema'], help: 'markdown = full page text; schema = structured extraction' },
      { key: 'extract_schema', type: 'object', help: 'Field schema when extract_mode=schema' },
      { key: 'persona_id', type: 'ref', ref: 'persona', help: 'Logged-in identity for authenticated crawls' },
      { key: 'max_depth', type: 'number', help: 'How many links deep to follow (default 4)' },
      { key: 'page_budget', type: 'number', help: 'Max pages to collect (default 1000)' },
      { key: 'relevance_threshold', type: 'number', help: 'With a goal: skip pages scoring below this (0–1; 0 = keep all)' },
      { key: 'include_paths', type: 'string[]', help: 'Only crawl URLs matching these path regexes' },
      { key: 'exclude_paths', type: 'string[]', help: 'Skip URLs matching these path regexes' },
      { key: 'same_domain', type: 'boolean', help: 'Stay on the seed domain (default on)' },
      { key: 'allow_subdomains', type: 'boolean', help: 'Follow subdomains of the seed (default on)' },
      { key: 'respect_robots', type: 'boolean', help: 'Honor robots.txt (default on)' },
    ],
    linksTo: ['persona'],
    produces: ['crawl_id', 'crawl_workflow_id', 'crawl_seed_url', 'crawl_name', 'crawl_status'],
  },
  return_data: {
    category: 'action',
    blockType: 'return_data',
    label: 'Return Data',
    summary: 'Returns collected data to the webhook/API caller.',
    when: 'Only in webhook-triggered flows — hand the collected result back in the HTTP response.',
    platforms: BOTH,
    config: [],
  },
  notification: {
    category: 'action',
    blockType: 'notification',
    label: 'Send Notification',
    summary: 'Alerts via webhook, desktop, in-app, email, SMS, or push.',
    when: 'Tell the user (or another system) something happened. On desktop, webhook / desktop / in-app work offline; email/SMS/push need the cloud link.',
    platforms: BOTH,
    config: [
      { key: 'channels', type: 'string[]', required: true, help: 'One or more channel ids' },
      { key: 'recipients', type: 'string[]', help: 'provider:id recipient references' },
      { key: 'webhook_url', type: 'string', help: 'Target URL when channel=webhook' },
      { key: 'title', type: 'string' },
      { key: 'template', type: 'template', help: 'Message body with {{placeholders}}' },
    ],
    linksTo: ['recipient'],
  },
  ai_session: {
    category: 'action',
    blockType: 'ai_session',
    label: 'Run AI Agent',
    summary: 'Fires one autonomous AI browser session: the agent works toward your goal, then saves its steps as a replayable workflow.',
    when: 'One-shot autonomy, not a chat: the agent opens a browser, navigates/clicks/extracts toward the goal on its own, and records what it did as a replayable workflow (this is NOT Scribe, the chat concierge). Runs on the desktop daemon (using your configured AI provider) or in the cloud.',
    platforms: BOTH,
    // GOAL-shaped, unlike cloud's `session_ids`. Self-host has no saved AI-session
    // recipes to reference — `ai_sessions` rows are run records and the start
    // endpoint takes the goal inline. See _dispatch_ai_session in
    // services/unified_trigger_service.py.
    config: [
      { key: 'goal', type: 'string', help: 'What the agent should accomplish' },
      { key: 'entry_url', type: 'string', help: 'Where it starts (optional)' },
      { key: 'max_steps', type: 'number', help: 'Iteration budget' },
      { key: 'generate_workflow', type: 'boolean', help: 'Save the run as a replayable workflow' },
      { key: 'user_context', type: 'string' },
      { key: 'form_data', type: 'object' },
    ],
    linksTo: ['ai_session'],
    produces: ['ai_result'],
  },
  create_persona: {
    category: 'action',
    blockType: 'create_persona',
    label: 'Create Persona',
    summary: 'Saves a workflow login as a reusable persona.',
    when: 'Right after a workflow that logs in — capture the session as a persona for later runs.',
    platforms: BOTH,
    config: [{ key: 'workflow_id', type: 'ref', ref: 'workflow', required: true }],
    linksTo: ['workflow'],
    produces: ['persona_id'],
  },
  start_streaming_session: {
    category: 'action',
    blockType: 'start_streaming_session',
    label: 'Start Streaming Session',
    summary: 'Launches a live streaming session for a workflow.',
    when: 'Open a warm, long-lived browser session — for live monitoring or interactive follow-up.',
    platforms: ['desktop'], // cloud backend has no handler/emitter yet — daemon-only
    config: [{ key: 'workflow_id', type: 'ref', ref: 'workflow', required: true }],
    linksTo: ['workflow'],
    produces: ['session_key', 'target_url'],
  },
  stop_streaming_session: {
    category: 'action',
    blockType: 'stop_streaming_session',
    label: 'Stop Streaming Session',
    summary: 'Ends a live streaming session for a workflow.',
    when: 'Tear down a streaming session started earlier in the flow (or on a schedule).',
    platforms: ['desktop'], // cloud backend has no handler/emitter yet — daemon-only
    config: [{ key: 'workflow_id', type: 'ref', ref: 'workflow', required: true }],
    linksTo: ['workflow'],
  },
  save_data_to_file: {
    category: 'action',
    blockType: 'save_data_to_file',
    label: 'Save Data to File',
    summary: 'Exports a workflow’s extracted rows to a CSV/JSON file.',
    when: 'Persist scraped/extracted data as a reusable file asset — e.g. after data_extracted.',
    platforms: ['desktop'], // cloud backend has no handler/emitter yet — daemon-only
    config: [
      { key: 'source_workflow_id', type: 'ref', ref: 'workflow', required: true },
      { key: 'format', type: 'enum', enumValues: ['csv', 'json'] },
      { key: 'filters', type: 'object', help: 'Optional row filters' },
    ],
    linksTo: ['workflow'],
    produces: ['file_id', 'filename', 'bytes'],
  },
  query_and_export: {
    category: 'action',
    blockType: 'query_and_export',
    label: 'Query & Export Data',
    summary: 'Filters a data table and exports the matching rows.',
    when: 'Pull a filtered slice of a workflow’s data and export it to a file.',
    platforms: ['desktop'], // cloud backend has no handler/emitter yet — daemon-only
    config: [
      { key: 'source_workflow_id', type: 'ref', ref: 'workflow', required: true },
      { key: 'format', type: 'enum', enumValues: ['csv', 'json'] },
      { key: 'filters', type: 'object' },
      { key: 'limit', type: 'number' },
    ],
    linksTo: ['workflow'],
    produces: ['file_id', 'row_count'],
  },
  append_to_data: {
    category: 'action',
    blockType: 'append_to_data',
    label: 'Append to Data',
    summary: 'Copies matching rows from one data table into another.',
    when: 'Aggregate rows across workflows into a single collection.',
    platforms: BOTH,
    config: [
      { key: 'source_workflow_id', type: 'ref', ref: 'workflow', required: true },
      { key: 'target_workflow_id', type: 'ref', ref: 'workflow' },
      { key: 'filters', type: 'object' },
      { key: 'limit', type: 'number' },
    ],
    linksTo: ['workflow'],
    produces: ['rows_appended'],
  },
  send_file: {
    category: 'action',
    blockType: 'send_file',
    label: 'Send File',
    summary: 'Delivers a stored file to a recipient or webhook.',
    when: 'Hand off a produced file — e.g. after save_data_to_file, send it to a webhook or (cloud) email.',
    platforms: ['desktop'], // cloud backend has no handler/emitter yet — daemon-only
    config: [
      { key: 'file_id', type: 'ref', ref: 'file', required: true },
      { key: 'recipient_type', type: 'string' },
      { key: 'recipient_id', type: 'string' },
    ],
    linksTo: ['file', 'recipient'],
  },
};

// Blocks defined for the roadmap but not yet wired into the editor + interpreter.
// They stay out of the add-menu, the AI prompt, and the validator until their phase
// lands. Phase 6 (scheduled + streaming session start/end triggers) and Phase 7
// (data/file/streaming action families + data_extracted/file_uploaded sources) are
// now GA — the daemon executes them and BlockContent has editors — so the set is
// empty. Add a type here only when it is defined ahead of its editor/interpreter.
// append_to_data has NO working interpreter anywhere today: the cloud dispatcher
// skips it as an unknown action and the desktop daemon explicitly refuses it —
// keep it planned until one of them actually executes it.
const PLANNED_BLOCK_TYPES = new Set<string>(['append_to_data']);
for (const bt of PLANNED_BLOCK_TYPES) {
  if (BLOCK_CATALOG[bt]) BLOCK_CATALOG[bt].status = 'planned';
}

// --- Helpers -----------------------------------------------------------------

export function getBlockDef(blockType: string): BlockDef | undefined {
  return BLOCK_CATALOG[blockType];
}

/**
 * Produced keys that are objects (referenced with a dotted sub-path, e.g.
 * `{{extracted.title}}`, `{{result.price}}`) rather than a bare scalar token.
 * Used to expand a block's `produces` into insertable placeholder tokens.
 */
const OBJECT_OUTPUT_KEYS = new Set(['extracted', 'result', 'ai_result', 'payload', 'item']);

/**
 * The placeholder tokens a block of `blockType` makes available to downstream
 * blocks, derived from the catalog `produces`. Object-shaped outputs surface as
 * an editable `key.*` token (the user appends the field); scalars are bare.
 * Returns tokens WITHOUT the surrounding braces — callers wrap as `{{token}}`.
 */
export function blockOutputTokens(blockType: string): string[] {
  const def = BLOCK_CATALOG[blockType];
  if (!def?.produces?.length) return [];
  return def.produces.map((k) => (OBJECT_OUTPUT_KEYS.has(k) ? `${k}.*` : k));
}

// Same placeholder grammar as steps/stepMeta.collectPlaceholders — kept local so
// the flow catalog stays decoupled from step UI. Lazy `[^{}]+?` never crosses an
// intervening brace, so `{{a}}{{b}}` yields two captures and a whitespace-only
// `{{ }}` is dropped by the trim below.
const PLACEHOLDER_RE = /\{\{\s*([^{}]+?)\s*\}\}/g;

/**
 * The upstream placeholder tokens a block CONSUMES, auto-detected by scanning its
 * config for `{{...}}` references (recursively through objects/arrays) — the
 * block's inputs, the mirror of `blockOutputTokens`. A filter tail (`{{x|upper}}`)
 * is stripped to the bare data path. Returns tokens WITHOUT braces, deduped and
 * in first-seen order.
 */
export function blockInputTokens(config: unknown): string[] {
  const out = new Set<string>();
  const walk = (x: unknown): void => {
    if (typeof x === 'string') {
      for (const m of x.matchAll(PLACEHOLDER_RE)) {
        let name = m[1].trim();
        const pipe = name.indexOf('|'); // drop transform filter: `price|round` -> `price`
        if (pipe !== -1) name = name.slice(0, pipe).trim();
        if (name) out.add(name);
      }
      return;
    }
    if (Array.isArray(x)) { x.forEach(walk); return; }
    if (x && typeof x === 'object') { Object.values(x).forEach(walk); }
  };
  walk(config);
  return [...out];
}

/** Blocks a platform can use today (GA only) — drives the add-menu + AI prompt. */
export function catalogForPlatform(platform: Platform): BlockDef[] {
  return Object.values(BLOCK_CATALOG).filter(
    (d) => d.status !== 'planned' && d.platforms.includes(platform),
  );
}

export function isBlockAvailable(blockType: string, platform: Platform): boolean {
  const def = BLOCK_CATALOG[blockType];
  return !!def && def.status !== 'planned' && def.platforms.includes(platform);
}

/** Whether the platform supports the block at all (ignoring GA/planned status). */
export function isBlockSupported(blockType: string, platform: Platform): boolean {
  const def = BLOCK_CATALOG[blockType];
  return !!def && def.platforms.includes(platform);
}

export function channelsForPlatform(platform: Platform): ChannelDef[] {
  return NOTIFICATION_CHANNELS.filter((c) => c.platforms.includes(platform));
}

export function isChannelSupported(channel: string, platform: Platform): boolean {
  const def = NOTIFICATION_CHANNELS.find((c) => c.value === channel);
  return !!def && def.platforms.includes(platform);
}

/**
 * Compact, machine-readable description of every block available on a platform,
 * injected verbatim into the AI generator's prompt. This is the "explanation so
 * the AI knows what block to generate".
 */
export function catalogPromptDigest(platform: Platform): string {
  const lines: string[] = [];
  const cats: Array<BlockDef['category']> = ['event', 'condition', 'action'];
  const heading: Record<BlockDef['category'], string> = {
    event: 'EVENT BLOCKS (a flow has exactly one, as its root — how it starts)',
    condition: 'CONDITION BLOCKS (gate the flow on an upstream value)',
    action: 'ACTION BLOCKS (do something)',
  };
  for (const cat of cats) {
    lines.push(`\n## ${heading[cat]}`);
    for (const def of catalogForPlatform(platform).filter((d) => d.category === cat)) {
      const cfg = def.config
        .map((f) => `${f.key}${f.required ? '*' : ''}:${f.type}${f.ref ? `<${f.ref}>` : ''}`)
        .join(', ');
      const links = def.linksTo?.length ? ` links:[${def.linksTo.join(',')}]` : '';
      const outs = def.produces?.length ? ` outputs:{{${def.produces.join('}} {{')}}}` : '';
      lines.push(`- ${def.blockType}: ${def.summary} WHEN: ${def.when} CONFIG: {${cfg}}${links}${outs}`);
    }
  }
  lines.push(
    `\nCONDITION OPERATORS: ${CONDITION_OPERATORS.map((o) => o.value).join(', ')}.`,
    `NOTIFICATION CHANNELS (${platform}): ${channelsForPlatform(platform).map((c) => c.value).join(', ')}.`,
    `Reference an upstream block's output with {{path}} — e.g. {{result.price}}, {{payload.email}}, {{extracted.title}}.`,
  );
  return lines.join('\n');
}
