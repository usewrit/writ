/**
 * API key scope vocabulary — the client half of the coordinator's
 * `security/api_scopes.py`.
 *
 * The catalogue is FETCHED (`GET /auth/api-keys/catalog`) rather than hardcoded,
 * so this screen can never offer a permission the coordinator does not enforce —
 * or miss one it does. The constant below is a last-resort fallback for when that
 * request fails; it is not the source of truth.
 */
import i18n from '../i18n';

export type ScopeAction = 'read' | 'write' | 'execute' | 'delete';

export interface CatalogResource {
  key: string;
  label: string;
  description: string;
  actions: ScopeAction[];
  /** Whether this resource supports "only these specific items". */
  pinnable: boolean;
}

export interface CatalogPreset {
  key: string;
  label: string;
  description: string;
  scopes: string[];
}

export interface ScopeCatalog {
  resources: CatalogResource[];
  actions: Array<{ key: ScopeAction; label: string }>;
  presets: CatalogPreset[];
}

/** Shown while the catalogue is in flight, and if it never arrives. */
export const FALLBACK_CATALOG: ScopeCatalog = {
  resources: [
    { key: 'workflows', label: 'Workflows', description: 'Automations you have built, and running them', actions: ['read', 'write', 'execute', 'delete'], pinnable: true },
    { key: 'runs', label: 'Runs', description: 'Execution history, results and live run state', actions: ['read'], pinnable: false },
    { key: 'monitors', label: 'Monitors', description: 'Watched pages, their selectors and detected changes', actions: ['read', 'write', 'execute', 'delete'], pinnable: true },
    { key: 'datasets', label: 'Datasets', description: 'Extracted records, search and exports', actions: ['read', 'delete'], pinnable: true },
    { key: 'crawl', label: 'Crawls', description: 'Site-wide crawls', actions: ['read', 'execute', 'delete'], pinnable: false },
    { key: 'scrape', label: 'Scrape & map', description: 'One-off page scrapes and site maps', actions: ['execute'], pinnable: false },
    { key: 'files', label: 'Files', description: 'Uploaded and produced file assets', actions: ['read', 'write', 'delete'], pinnable: false },
    { key: 'personas', label: 'Personas', description: 'Saved browser identities', actions: ['read', 'write', 'delete'], pinnable: false },
    { key: 'secrets', label: 'Secrets', description: 'Vault entries (names and metadata only)', actions: ['read', 'write', 'delete'], pinnable: false },
    { key: 'agents', label: 'Agents', description: 'Your own devices and fleet agents', actions: ['read', 'write', 'execute'], pinnable: false },
    { key: 'triggers', label: 'Triggers & webhooks', description: 'Trigger rules and webhook endpoints', actions: ['read', 'write', 'execute', 'delete'], pinnable: false },
    { key: 'recorder', label: 'Recorder', description: 'Live record and browse sessions', actions: ['read', 'execute'], pinnable: false },
    { key: 'streaming', label: 'Streaming', description: 'Streaming sessions and OpenAI-compatible endpoints', actions: ['read', 'execute', 'delete'], pinnable: false },
    { key: 'mcp', label: 'MCP', description: 'The MCP endpoint and its tool catalogue', actions: ['read', 'write', 'execute', 'delete'], pinnable: false },
    { key: 'account', label: 'Account', description: 'Read-only account and usage summary', actions: ['read'], pinnable: false },
  ],
  actions: [
    { key: 'read', label: 'View' },
    { key: 'write', label: 'Create and modify' },
    { key: 'execute', label: 'Run' },
    { key: 'delete', label: 'Delete' },
  ],
  presets: [],
};

/** Verb shown on an action chip. Short, because it sits next to three siblings. */
export function actionLabel(action: ScopeAction): string {
  switch (action) {
    case 'read': return i18n.t('View');
    case 'write': return i18n.t('Edit');
    case 'execute': return i18n.t('Run');
    case 'delete': return i18n.t('Delete');
    default: return action;
  }
}

export function scopeString(resource: string, action: ScopeAction): string {
  return `${resource}:${action}`;
}

export function splitScope(scope: string): { resource: string; action: ScopeAction } {
  const [resource, action] = scope.split(':');
  return { resource, action: action as ScopeAction };
}

/** Scopes grouped by resource, in catalogue order. */
export function groupByResource(
  scopes: string[],
  catalog: ScopeCatalog,
): Array<{ resource: CatalogResource; actions: ScopeAction[] }> {
  const held = new Set(scopes);
  const out: Array<{ resource: CatalogResource; actions: ScopeAction[] }> = [];
  for (const resource of catalog.resources) {
    const actions = resource.actions.filter((a) => held.has(scopeString(resource.key, a)));
    if (actions.length) out.push({ resource, actions });
  }
  return out;
}

/**
 * Plain-language capability lines — "Run and view workflows", not
 * "workflows: read,execute". This is the whole point of the rewrite: the old
 * screen showed the storage format and called it a summary, so a key labelled
 * "Full access" told you nothing about what it could actually reach.
 */
export function describeGrant(
  scopes: string[],
  catalog: ScopeCatalog,
  resourceIds?: Record<string, number[]>,
): string[] {
  return groupByResource(scopes, catalog).map(({ resource, actions }) => {
    const verbs = actions.map(actionLabel).map((v) => v.toLowerCase());
    const joined =
      verbs.length === 1
        ? verbs[0]
        : `${verbs.slice(0, -1).join(', ')} ${i18n.t('and')} ${verbs[verbs.length - 1]}`;
    const pins = resourceIds?.[resource.key];
    const target = pins?.length
      ? i18n.t('{{count}} selected {{resource}}', { count: pins.length, resource: resource.label.toLowerCase() })
      : resource.label.toLowerCase();
    return `${joined.charAt(0).toUpperCase()}${joined.slice(1)} ${target}`;
  });
}

/** Which preset a scope set is exactly equal to, or null for a custom grant. */
export function matchPreset(scopes: string[], catalog: ScopeCatalog): string | null {
  const held = new Set(scopes);
  for (const preset of catalog.presets) {
    if (preset.scopes.length === held.size && preset.scopes.every((s) => held.has(s))) {
      return preset.key;
    }
  }
  return null;
}

export function presetLabel(key: string | null, catalog: ScopeCatalog): string {
  if (!key) return i18n.t('Custom access');
  return catalog.presets.find((p) => p.key === key)?.label ?? i18n.t('Custom access');
}

/** Expiry choices offered at creation. A key that never expires is the exception. */
export const EXPIRY_OPTIONS = [
  { value: '30', label: () => i18n.t('30 days') },
  { value: '90', label: () => i18n.t('90 days') },
  { value: '365', label: () => i18n.t('1 year') },
  { value: 'never', label: () => i18n.t('No expiry') },
] as const;

export function expiryToIso(value: string): string | undefined {
  if (value === 'never') return undefined;
  const days = Number(value);
  if (!Number.isFinite(days)) return undefined;
  const at = new Date();
  at.setDate(at.getDate() + days);
  return at.toISOString();
}
