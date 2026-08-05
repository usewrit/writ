import React from 'react';
import {
  Squares2X2Icon,
  BoltIcon,
  ExclamationTriangleIcon,
  ArrowPathIcon,
  MoonIcon,
  TableCellsIcon,
  ArrowTrendingUpIcon,
  PauseCircleIcon,
  EnvelopeIcon,
  LinkIcon,
  EyeIcon,
  CheckBadgeIcon,
  ShieldCheckIcon,
  LockClosedIcon,
  ClockIcon,
  StarIcon,
  UserIcon,
} from '@heroicons/react/24/outline';
import i18n from '../../i18n';

/**
 * Declarative per-page configuration for the contextual tool rail.
 * Each page describes its Views (filter segments), Facets (multi-select
 * filters), Overview stats, and an activity mapper. The rail renders entirely
 * from this — keeping the four rails visually and behaviourally consistent.
 *
 * All predicates / stats operate on the same array the list page already loads
 * via useQuery (shared cache — no extra requests).
 */

export interface RailView {
  id: string;
  label: string;
  icon: React.FC<any>;
  predicate: (item: any) => boolean;
}

export interface RailFacetOption {
  id: string;
  label: string;
  predicate: (item: any) => boolean;
}

export interface RailFacet {
  /** URL param key + persistence key, e.g. "type" */
  id: string;
  label: string;
  options: RailFacetOption[];
}

export interface RailStatResult {
  value: React.ReactNode;
  /** 0–100; when present renders a monochrome ratio bar under the value */
  ratio?: number;
}

export interface RailStat {
  id: string;
  label: string;
  compute: (items: any[]) => RailStatResult;
}

export interface RailActivityItem {
  id: string | number;
  title: string;
  subtitle?: string;
  time: string | null;
  /** true = good/idle, false = failed, null = neutral/running */
  ok?: boolean | null;
}

export interface RailConfig {
  /** persistence + URL namespace */
  page: string;
  title: string;
  views: RailView[];
  facets: RailFacet[];
  stats: RailStat[];
  activityTitle: string;
  activity: (items: any[]) => RailActivityItem[];
}

// ── shared helpers ──────────────────────────────────────────────────────────
const RUNNING = ['running', 'pending', 'assigned', 'queued'];
const isRunning = (w: any) => RUNNING.includes(w.last_run_status || '');
const pct = (n: number, d: number) => (d > 0 ? Math.round((n / d) * 100) : 0);

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}

function latest(items: any[], field: string, mapper: (i: any) => RailActivityItem, limit = 6): RailActivityItem[] {
  return [...items]
    .filter((i) => i[field])
    .sort((a, b) => new Date(b[field]).getTime() - new Date(a[field]).getTime())
    .slice(0, limit)
    .map(mapper);
}

// ── Workflows ────────────────────────────────────────────────────────────────
export const workflowsConfig: RailConfig = {
  page: 'workflows',
  title: 'Workflows',
  views: [
    { id: 'all', label: 'All', icon: Squares2X2Icon, predicate: () => true },
    { id: 'active', label: 'Active', icon: BoltIcon, predicate: (w) => !!w.is_active },
    { id: 'running', label: 'Running now', icon: ArrowPathIcon, predicate: isRunning },
    { id: 'failing', label: 'Failing', icon: ExclamationTriangleIcon, predicate: (w) => w.last_run_status === 'failed' },
    { id: 'unused', label: 'Never run', icon: MoonIcon, predicate: (w) => !w.usage_count },
    { id: 'data', label: 'Has data', icon: TableCellsIcon, predicate: (w) => w.last_run_has_extracted_data ?? (w.last_run_extracted_data && Object.keys(w.last_run_extracted_data).length > 0) },
  ],
  facets: [
    {
      id: 'type',
      label: 'Type',
      options: [
        { id: 'recorded', label: 'Recorded', predicate: (w) => w.workflow_type === 'recorded' },
        { id: 'ai_navigate', label: 'AI Navigate', predicate: (w) => w.workflow_type === 'ai_navigate' },
        { id: 'pre_check', label: 'Pre-Check', predicate: (w) => w.workflow_type === 'pre_check' },
        { id: 'on_change', label: 'On Change', predicate: (w) => w.workflow_type === 'on_change' },
        { id: 'scheduled', label: 'Scheduled', predicate: (w) => w.workflow_type === 'scheduled' },
        { id: 'api_recorded', label: 'API', predicate: (w) => w.workflow_type === 'api_recorded' },
        { id: 'streaming', label: 'Streaming', predicate: (w) => w.workflow_type === 'streaming' },
      ],
    },
  ],
  stats: [
    { id: 'total', label: 'Total', compute: (i) => ({ value: i.length }) },
    { id: 'active', label: 'Active', compute: (i) => ({ value: i.filter((w) => w.is_active).length }) },
    {
      id: 'success',
      label: 'Success rate',
      compute: (i) => {
        const ran = i.filter((w) => w.last_run_status && w.last_run_status !== 'never');
        const ok = ran.filter((w) => w.last_run_status === 'success' || w.last_run_status === 'completed').length;
        const r = pct(ok, ran.length);
        return { value: ran.length ? `${r}%` : '—', ratio: ran.length ? r : undefined };
      },
    },
    {
      id: 'avg',
      label: 'Avg run',
      compute: (i) => {
        const ds = i.map((w) => w.last_run_duration_ms).filter((d) => typeof d === 'number');
        if (!ds.length) return { value: '—' };
        return { value: fmtDuration(ds.reduce((a, b) => a + b, 0) / ds.length) };
      },
    },
  ],
  activityTitle: 'Recent runs',
  activity: (i) =>
    latest(i, 'last_run_at', (w) => ({
      id: w.id,
      title: w.name || i18n.t('Untitled'),
      time: w.last_run_at,
      ok: isRunning(w) ? null : w.last_run_status === 'failed' ? false : true,
    })),
};

// ── Checks ────────────────────────────────────────────────────────────────────
const PERIODS: Array<[number, string]> = [
  [10000, '10s'], [30000, '30s'], [60000, '1m'], [300000, '5m'],
  [900000, '15m'], [3600000, '1h'], [86400000, '24h'],
];

// The targets API serializes camelCase (changesCount); older paths use
// snake_case. Tolerate either so rail counts match the page header.
const checkChanges = (t: any): number => t.changesCount ?? t.changes_count ?? 0;

export const checksConfig: RailConfig = {
  page: 'checks',
  title: 'Monitors',
  views: [
    { id: 'all', label: 'All', icon: Squares2X2Icon, predicate: () => true },
    { id: 'active', label: 'Active', icon: EyeIcon, predicate: (t) => t.enabled !== false },
    { id: 'paused', label: 'Paused', icon: PauseCircleIcon, predicate: (t) => t.enabled === false },
    { id: 'changed', label: 'Has changes', icon: ArrowTrendingUpIcon, predicate: (t) => (checkChanges(t)) > 0 },
    { id: 'stale', label: 'Never checked', icon: MoonIcon, predicate: (t) => !(t.lastCheckedAt ?? t.last_checked_at) },
  ],
  facets: [
    {
      id: 'freq',
      label: 'Frequency',
      options: PERIODS.map(([ms, label]) => ({
        id: String(ms),
        label,
        predicate: (t: any) => (t.checkPeriodMs ?? t.check_period_ms) === ms,
      })),
    },
  ],
  stats: [
    { id: 'total', label: 'Total', compute: (i) => ({ value: i.length }) },
    { id: 'active', label: 'Active', compute: (i) => ({ value: i.filter((t) => t.enabled !== false).length }) },
    { id: 'changes', label: 'Total changes', compute: (i) => ({ value: i.reduce((s, t) => s + checkChanges(t), 0) }) },
    {
      id: 'cadence',
      label: 'Tightest',
      compute: (i) => {
        const ms = i.map((t) => t.checkPeriodMs ?? t.check_period_ms).filter((n) => typeof n === 'number');
        if (!ms.length) return { value: '—' };
        const min = Math.min(...ms);
        return { value: PERIODS.find(([m]) => m === min)?.[1] || `${Math.round(min / 1000)}s` };
      },
    },
  ],
  activityTitle: 'Recently checked',
  activity: (i) =>
    latest(i, 'lastCheckedAt', (t) => ({
      id: t.id,
      title: t.url || i18n.t('Untitled'),
      subtitle: checkChanges(t) > 0 ? i18n.t('{{n}} changes', { n: checkChanges(t) }) : undefined,
      time: t.lastCheckedAt ?? t.last_checked_at,
      ok: t.enabled !== false ? true : null,
    })),
};

// ── Automations ────────────────────────────────────────────────────────────────
export const automationsConfig: RailConfig = {
  page: 'automations',
  title: 'Automations',
  views: [
    { id: 'all', label: 'All', icon: Squares2X2Icon, predicate: () => true },
    { id: 'active', label: 'Active', icon: BoltIcon, predicate: (f) => !!f.enabled },
    { id: 'paused', label: 'Paused', icon: PauseCircleIcon, predicate: (f) => !f.enabled },
    { id: 'triggered', label: 'Most triggered', icon: ArrowTrendingUpIcon, predicate: (f) => (f.trigger_count || 0) > 0 },
    { id: 'never', label: 'Never run', icon: MoonIcon, predicate: (f) => !f.trigger_count },
    { id: 'webhook', label: 'Has webhook', icon: LinkIcon, predicate: (f) => !!(f.webhook_trigger_token || f.blocks?.find((b: any) => b.blockType === 'webhook_received')?.config?.webhook_trigger_token) },
  ],
  facets: [
    {
      id: 'source',
      label: 'Trigger source',
      options: [
        { id: 'change_detected', label: 'Monitor', predicate: (f) => f.event_type === 'change_detected' },
        { id: 'webhook_received', label: 'Webhook', predicate: (f) => f.event_type === 'webhook_received' },
        { id: 'ai_session_completed', label: 'AI Session', predicate: (f) => f.event_type === 'ai_session_completed' || f.event_type === 'ai_session_started' },
        { id: 'workflow_completed', label: 'Workflow', predicate: (f) => f.event_type === 'workflow_completed' || f.event_type === 'workflow_started' },
      ],
    },
  ],
  stats: [
    { id: 'total', label: 'Total', compute: (i) => ({ value: i.length }) },
    { id: 'active', label: 'Active', compute: (i) => ({ value: i.filter((f) => f.enabled).length }) },
    { id: 'runs', label: 'Total runs', compute: (i) => ({ value: i.reduce((s, f) => s + (f.trigger_count || 0), 0) }) },
    {
      id: 'enabled',
      label: 'Enabled',
      compute: (i) => {
        const r = pct(i.filter((f) => f.enabled).length, i.length);
        return { value: `${r}%`, ratio: r };
      },
    },
  ],
  activityTitle: 'Recently triggered',
  activity: (i) =>
    latest(i, 'last_triggered_at', (f) => ({
      id: f.id,
      title: f.name || i18n.t('Untitled'),
      subtitle: (f.trigger_count || 0) > 0 ? i18n.t('{{n}} runs', { n: f.trigger_count }) : undefined,
      time: f.last_triggered_at,
      ok: f.enabled ? true : null,
    })),
};

// ── Runs ────────────────────────────────────────────────────────────────────────
const RUN_TYPE_LABEL: Record<string, string> = {
  workflow: 'Workflow',
  check: 'Monitor',
  ai_session: 'AI session',
  automation: 'Automation',
  crawl: 'Harvest',
};
const runIsRunning = (r: any) => r.status === 'running' || r.status === 'pending';
const withinHours = (iso: string | null | undefined, h: number) =>
  !!iso && Date.now() - new Date(iso).getTime() <= h * 3600000;

export const runsConfig: RailConfig = {
  page: 'runs',
  title: 'Runs',
  views: [
    { id: 'all', label: 'All', icon: Squares2X2Icon, predicate: () => true },
    { id: 'running', label: 'Running now', icon: ArrowPathIcon, predicate: runIsRunning },
    { id: 'failed', label: 'Failed', icon: ExclamationTriangleIcon, predicate: (r) => r.status === 'failed' },
    { id: 'success', label: 'Succeeded', icon: CheckBadgeIcon, predicate: (r) => r.status === 'success' },
    { id: 'data', label: 'Has data', icon: TableCellsIcon, predicate: (r) => !!r.data_url_hint },
  ],
  facets: [
    {
      id: 'type',
      label: 'Type',
      options: [
        { id: 'workflow', label: 'Workflows', predicate: (r: any) => r.run_type === 'workflow' },
        { id: 'check', label: 'Monitors', predicate: (r: any) => r.run_type === 'check' },
        { id: 'ai_session', label: 'AI sessions', predicate: (r: any) => r.run_type === 'ai_session' },
        { id: 'automation', label: 'Automations', predicate: (r: any) => r.run_type === 'automation' },
        { id: 'crawl', label: 'Crawls', predicate: (r: any) => r.run_type === 'crawl' },
      ],
    },
    {
      id: 'time',
      label: 'Time',
      options: [
        { id: '1h', label: 'Last hour', predicate: (r: any) => withinHours(r.started_at, 1) },
        { id: '24h', label: 'Last 24 hours', predicate: (r: any) => withinHours(r.started_at, 24) },
        { id: '7d', label: 'Last 7 days', predicate: (r: any) => withinHours(r.started_at, 24 * 7) },
      ],
    },
  ],
  stats: [
    { id: 'total', label: 'Total', compute: (i) => ({ value: i.length }) },
    { id: 'failed', label: 'Failed', compute: (i) => ({ value: i.filter((r) => r.status === 'failed').length }) },
    {
      id: 'success',
      label: 'Success %',
      compute: (i) => {
        const done = i.filter((r) => r.status === 'success' || r.status === 'failed');
        const ok = i.filter((r) => r.status === 'success').length;
        const r = pct(ok, done.length);
        return { value: done.length ? `${r}%` : '—', ratio: done.length ? r : undefined };
      },
    },
    {
      id: 'avg',
      label: 'Avg run',
      compute: (i) => {
        const ds = i.map((r) => r.duration_ms).filter((d) => typeof d === 'number');
        if (!ds.length) return { value: '—' };
        return { value: fmtDuration(ds.reduce((a, b) => a + b, 0) / ds.length) };
      },
    },
  ],
  activityTitle: 'Recent runs',
  activity: (i) =>
    latest(i, 'started_at', (r) => ({
      id: r.id,
      title: r.entity_name || (RUN_TYPE_LABEL[r.run_type] ? i18n.t(RUN_TYPE_LABEL[r.run_type]) : i18n.t('Run')),
      subtitle: RUN_TYPE_LABEL[r.run_type] ? i18n.t(RUN_TYPE_LABEL[r.run_type]) : undefined,
      time: r.started_at,
      ok: runIsRunning(r) ? null : r.status === 'failed' ? false : true,
    })),
};

// ── Personas ────────────────────────────────────────────────────────────────────
const sessionValid = (p: any) => p.has_warm_session && p.validation_status !== 'expired';

export const personasConfig: RailConfig = {
  page: 'personas',
  title: 'Personas',
  views: [
    { id: 'all', label: 'All', icon: Squares2X2Icon, predicate: () => true },
    { id: 'valid', label: 'Warm & valid', icon: CheckBadgeIcon, predicate: sessionValid },
    { id: 'expired', label: 'Expired', icon: ExclamationTriangleIcon, predicate: (p) => p.has_warm_session && p.validation_status === 'expired' },
    { id: 'unused', label: 'Unused', icon: MoonIcon, predicate: (p) => !(p.linked_workflows || []).length },
    { id: 'needs_mailbox', label: 'Needs mailbox', icon: EnvelopeIcon, predicate: (p) => p.twofa_method === 'email_otp' && !p.connected_mailbox },
  ],
  facets: [
    {
      id: '2fa',
      label: '2FA method',
      options: [
        { id: 'none', label: 'No 2FA', predicate: (p) => p.twofa_method === 'none' },
        { id: 'totp', label: 'Authenticator', predicate: (p) => p.twofa_method === 'totp' },
        { id: 'email_otp', label: 'Email code', predicate: (p) => p.twofa_method === 'email_otp' },
        { id: 'sms', label: 'SMS', predicate: (p) => p.twofa_method === 'sms' },
      ],
    },
  ],
  stats: [
    { id: 'total', label: 'Total', compute: (i) => ({ value: i.length }) },
    {
      id: 'valid',
      label: 'Valid sessions',
      compute: (i) => {
        const v = i.filter(sessionValid).length;
        return { value: v, ratio: pct(v, i.length) };
      },
    },
    { id: 'expired', label: 'Expired', compute: (i) => ({ value: i.filter((p) => p.has_warm_session && p.validation_status === 'expired').length }) },
    { id: 'mailboxes', label: 'Mailboxes', compute: (i) => ({ value: i.filter((p) => p.connected_mailbox).length }) },
  ],
  activityTitle: 'Recently used',
  activity: (i) =>
    latest(i, 'last_used_at', (p) => ({
      id: p.id,
      title: p.name || i18n.t('Untitled'),
      subtitle: p.target_domain || undefined,
      time: p.last_used_at,
      ok: sessionValid(p) ? true : p.validation_status === 'expired' ? false : null,
    })),
};

// ── API Keys ────────────────────────────────────────────────────────────────────
// Scopes are a flat `resource:action` string list. These predicates used to
// index `k.scopes[resource].permissions`, which is now always undefined — the
// "Full access" / "Read-only" chips would have silently counted zero.
const keyHasScope = (k: any, res: string) =>
  ((k.scopes as string[] | undefined) || []).some((s) => s.startsWith(`${res}:`));
// The server names the preset a grant exactly matches, so the UI does not have to
// re-derive "is this full access?" from a scope set it may not know all of.
const keyIsFull = (k: any) => k.preset === 'full';
const keyIsReadonly = (k: any) => k.preset === 'read_only';
const usedWithin = (iso: string | null | undefined, days: number) =>
  !!iso && (Date.now() - new Date(iso).getTime()) < days * 86400000;

export const apiKeysConfig: RailConfig = {
  page: 'apikeys',
  title: 'API Keys',
  views: [
    { id: 'all', label: 'All', icon: Squares2X2Icon, predicate: () => true },
    { id: 'used', label: 'Recently used', icon: ClockIcon, predicate: (k) => usedWithin(k.lastUsed, 30) },
    { id: 'idle', label: 'Never used', icon: MoonIcon, predicate: (k) => !k.lastUsed },
    { id: 'full', label: 'Full access', icon: ShieldCheckIcon, predicate: keyIsFull },
    { id: 'readonly', label: 'Read-only', icon: LockClosedIcon, predicate: keyIsReadonly },
  ],
  facets: [
    {
      id: 'scope',
      label: 'Resource access',
      options: [
        { id: 'workflows', label: 'Workflows', predicate: (k) => keyHasScope(k, 'workflows') },
        { id: 'monitors', label: 'Monitors', predicate: (k) => keyHasScope(k, 'monitors') },
        { id: 'datasets', label: 'Datasets', predicate: (k) => keyHasScope(k, 'datasets') },
        { id: 'crawl', label: 'Crawls', predicate: (k) => keyHasScope(k, 'crawl') },
        { id: 'secrets', label: 'Secrets', predicate: (k) => keyHasScope(k, 'secrets') },
      ],
    },
  ],
  stats: [
    { id: 'total', label: 'Total', compute: (i) => ({ value: i.length }) },
    {
      id: 'used',
      label: 'In use',
      compute: (i) => {
        const u = i.filter((k) => k.lastUsed).length;
        return { value: u, ratio: pct(u, i.length) };
      },
    },
    { id: 'full', label: 'Full access', compute: (i) => ({ value: i.filter(keyIsFull).length }) },
    { id: 'idle', label: 'Never used', compute: (i) => ({ value: i.filter((k) => !k.lastUsed).length }) },
  ],
  activityTitle: 'Recently used',
  activity: (i) =>
    latest(i, 'lastUsed', (k) => ({
      id: k.id,
      title: k.label || i18n.t('Untitled key'),
      time: k.lastUsed,
      ok: true,
    })),
};

// ── Team ────────────────────────────────────────────────────────────────────────
export const teamConfig: RailConfig = {
  page: 'team',
  title: 'Team',
  views: [
    { id: 'all', label: 'All', icon: Squares2X2Icon, predicate: () => true },
    { id: 'owners', label: 'Owners', icon: StarIcon, predicate: (m) => m.role === 'owner' },
    { id: 'admins', label: 'Admins', icon: ShieldCheckIcon, predicate: (m) => m.role === 'admin' },
    { id: 'members', label: 'Members', icon: UserIcon, predicate: (m) => m.role === 'member' },
  ],
  facets: [],
  stats: [
    { id: 'total', label: 'Total', compute: (i) => ({ value: i.length }) },
    { id: 'admins', label: 'Admins+', compute: (i) => ({ value: i.filter((m) => m.role === 'owner' || m.role === 'admin').length }) },
    { id: 'members', label: 'Members', compute: (i) => ({ value: i.filter((m) => m.role === 'member').length }) },
    { id: 'recent', label: 'Joined 7d', compute: (i) => ({ value: i.filter((m) => usedWithin(m.joined_at, 7)).length }) },
  ],
  activityTitle: 'Recently joined',
  activity: (i) =>
    latest(i, 'joined_at', (m) => ({
      id: m.id,
      title: m.name || m.email || i18n.t('Member'),
      subtitle: m.role,
      time: m.joined_at,
      ok: null,
    })),
};
