import { cleanTitle } from '../../utils/format';
import type { HealthRun } from '../../api/homeHealth';

/** Human label for a run's type ("Workflow run", "Monitor check"). */
export const RUN_TYPE_LABEL: Record<string, string> = {
  workflow: 'Workflow run',
  check: 'Monitor check',
  ai_session: 'AI session',
  automation: 'Automation',
  crawl: 'Crawl',
};

/** Short label for a run type ("Workflow", "Monitor") — for the type pill. */
export const RUN_TYPE_SHORT: Record<string, string> = {
  workflow: 'Workflow',
  check: 'Monitor',
  ai_session: 'AI session',
  automation: 'Automation',
  crawl: 'Crawl',
};

// Noun used to name a URL-only entity ("Amazon monitor", "ChatGPT session").
const RUN_TYPE_NOUN: Record<string, string> = {
  workflow: 'workflow',
  check: 'monitor',
  ai_session: 'session',
  automation: 'automation',
  crawl: 'crawl',
};

// Brand casing for common hosts so a URL becomes a proper name, not "Chatgpt".
const BRAND: Record<string, string> = {
  chatgpt: 'ChatGPT', openai: 'OpenAI', github: 'GitHub', gitlab: 'GitLab',
  youtube: 'YouTube', linkedin: 'LinkedIn', amazon: 'Amazon', ebay: 'eBay',
  paypal: 'PayPal', tiktok: 'TikTok', facebook: 'Facebook', instagram: 'Instagram',
  reddit: 'Reddit', twitter: 'Twitter', walmart: 'Walmart', shopify: 'Shopify',
};

/** Bare host for a URL (drops scheme + www.), else null. */
export function hostOf(value: string): string | null {
  try {
    return new URL(value).hostname.replace(/^www\./, '');
  } catch {
    return null;
  }
}

function brandCase(host: string): string {
  const seg = host.split('.')[0];
  return BRAND[seg.toLowerCase()] || seg.replace(/^./, (c) => c.toUpperCase());
}

/**
 * Turn a run into a human-readable name + optional host metadata + optional lead. A
 * URL anywhere in the entity name is NEVER shown as the title: it becomes a brand name
 * ("Amazon monitor" for a bare-URL monitor, "Amazon page" for a "Concierge browse:
 * https://amazon.ca/…" session) with the full host surfaced separately as muted
 * metadata. When there's descriptive text before the URL ("Concierge browse"), it's
 * returned as `lead` so the caller can use it as the meta prefix instead of the raw
 * type label. Named entities keep their cleaned title.
 */
export function readableRunName(run: HealthRun): { name: string; host: string | null; lead: string | null } {
  const raw = (run.entity_name || '').trim();
  const m = raw.match(/https?:\/\/[^\s"'()<>]+/i);
  if (m) {
    const host = hostOf(m[0]);
    const brand = host ? brandCase(host) : null;
    const before = raw.slice(0, m.index).replace(/[\s:–—-]+$/, '').trim();
    const isBare = m.index === 0;
    const noun = isBare ? (RUN_TYPE_NOUN[run.run_type] || 'page') : 'page';
    const name = brand ? `${brand} ${noun}`.trim() : (before || RUN_TYPE_LABEL[run.run_type] || 'Run');
    return { name, host, lead: before || null };
  }
  if (!raw) return { name: RUN_TYPE_LABEL[run.run_type] || 'Run', host: null, lead: null };
  return { name: cleanTitle(raw), host: null, lead: null };
}
