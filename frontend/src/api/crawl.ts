import client from './client';
import { useQueryCache } from '../stores/queryCache';
import { Q } from '../stores/queryKeys';

/**
 * Dragnet — the self-hosted whole-site distributed crawl API (`/api/crawl`).
 *
 * A crawl maps a seed URL, fans the discovered pages out across the agent fleet
 * as shards, and lands the collected pages into a synthetic per-crawl workflow
 * whose extracted-data grid the app already renders (Workflow Data API). The
 * self-hosted coordinator does DETERMINISTIC crawls only — its one axis is the
 * output shape (markdown | schema); there is no AI-agent (`ai`) executor, so the
 * `executor` / `extract_prompt` fields below are OPTIONAL: the shared cloud UI
 * reads them defensively, but the coordinator response never carries them (and it
 * ignores them on the start request).
 *
 * Three read-only endpoints scope a crawl BEFORE it starts — none creates a crawl
 * or dispatches an agent: `preview` (the scope it would use + a kept/dropped URL
 * sample), `map` (the site's URLs ranked against a query, to hand-pick seeds), and
 * `scrape` (one page to markdown).
 *
 * The row is the thing the UI polls: list on an interval, poll one crawl every
 * ~2.5s while not terminal, then stop. Statuses: queued → mapping → crawling →
 * completed | failed | cancelled (+ `stopping` = a cancel draining in-flight shards).
 */

export type CrawlStatus =
  | 'queued'
  | 'mapping'
  | 'crawling'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'stopping';

/**
 * WHO reads each page: 'regular' (deterministic crawl) or 'ai' (an agent fleet
 * reads each page). The self-hosted coordinator only runs the 'regular'
 * executor, so the field is optional and absent on its responses — the UI
 * treats a missing/`regular` value as the deterministic crawl.
 */
export type CrawlExecutor = 'regular' | 'ai';
/** The SHAPE the collected pages land in. */
export type CrawlExtractMode = 'markdown' | 'schema';

/**
 * How a page is fetched (render_mode) and how non-HTML docs are read (ocr_mode):
 *   render_mode — 'auto' (HTTP-first, browser only for JS/thin pages), 'http'
 *     (never render — fast/static), or 'browser' (always render in a warm browser).
 *   ocr_mode — how PDF/office/image docs + DOM-empty renders are read: 'auto'
 *     (text layer direct, OCR only pixels w/o text), 'off', or 'force'.
 */
export type CrawlRenderMode = 'auto' | 'http' | 'browser';
export type CrawlOcrMode = 'auto' | 'off' | 'force';

/**
 * Display branding stamped on a crawl. The cloud backend sends a
 * `{ crawl, agent }` object; the self-hosted coordinator stamps a plain string
 * (e.g. "Dragnet"). The helpers below tolerate both and always fall back to the
 * default names, so the UI renders regardless of shape.
 */
export interface CrawlBrand {
  /** Capability name, e.g. "Dragnet". */
  crawl?: string | null;
  /** Agent/concierge name, e.g. "Scribe". */
  agent?: string | null;
}

export interface CrawlView {
  id: number;
  name: string;
  seed_url: string;
  /** Same-origin site-favicon proxy path (/crawl/{id}/favicon); 404→globe. */
  favicon?: string | null;
  status: CrawlStatus;
  /** Deterministic crawl vs AI-agent extraction (absent on self-host = regular). */
  executor?: CrawlExecutor;
  /** Output shape the collected pages land in. */
  extract_mode: CrawlExtractMode;
  /** Page render strategy (warm browser for JS/SPA sites). */
  render_mode?: CrawlRenderMode;
  /** OCR policy for PDF/office/image docs + DOM-empty renders. */
  ocr_mode?: CrawlOcrMode;
  /** For the ai executor: the per-page extraction instruction (unused on self-host). */
  extract_prompt?: string | null;
  /** Plain-English goal: derives the scope and ranks the frontier by relevance. */
  intent?: string | null;
  /** Audit of what the goal was translated into, for the detail page. */
  derived_scope?: (CrawlScope & { reason?: string }) | null;
  /** Drop-below relevance score applied to discovered URLs (0 = keep everything). */
  relevance_threshold?: number | null;
  /** The crawl workflow itself. */
  workflow_id: number | null;
  /** The workflow the collected pages land in — its data grid IS the dataset. */
  data_workflow_id: number | null;
  pages_discovered: number;
  pages_done: number;
  pages_failed: number;
  pages_skipped: number;
  shards_dispatched: number;
  shards_done: number;
  current_depth: number;
  max_depth: number;
  page_budget: number;
  /** Unique records after the end-of-crawl reconciliation pass deduped every shard's slice. */
  records_total?: number | null;
  duplicates_removed?: number | null;
  reconciled_at?: string | null;
  error: string | null;
  created_at: string;
  completed_at: string | null;
  /** Capability display name/branding the coordinator stamps ("Dragnet"). */
  brand?: CrawlBrand | string | null;
}

/** Content-selection spec — which page ELEMENTS a scrape keeps. Omitted ⇒ default (main-content
 *  isolation, comments kept). preset "full" = whole page incl. discussion/comment threads. */
export interface ContentSpec {
  preset?: 'main' | 'full' | 'readable';
  include_comments?: boolean;
  exclude_selectors?: string[];
  include_selectors?: string[];
  keep?: { images?: boolean; tables?: boolean; links?: boolean };
}

export interface StartCrawlRequest {
  url: string;
  name?: string;
  /** Deterministic vs AI-agent extraction — IGNORED by the self-hosted coordinator. */
  executor?: CrawlExecutor;
  /** Output shape. */
  extract_mode: CrawlExtractMode;
  /** Page render strategy: 'auto' | 'http' | 'browser' (warm browser for JS sites). */
  render_mode?: CrawlRenderMode;
  /** OCR policy for PDF/office/image docs + DOM-empty renders: 'auto' | 'off' | 'force'. */
  ocr_mode?: CrawlOcrMode;
  /** For the ai executor — IGNORED by the self-hosted coordinator. */
  extract_prompt?: string;
  /** Plain-English goal; derives scope + ranks the frontier by relevance. */
  intent?: string;
  /** Hand-picked seed URLs from the map step (auto-discover when empty). */
  seed_urls?: string[];
  /** Drop URLs scoring below this against the goal (0–1; 0 = keep all). */
  relevance_threshold?: number;
  /** 0–20. Unset lets the goal-derived depth win. */
  max_depth?: number;
  page_budget: number;
  max_concurrent_shards: number;
  shard_size: number;
  delay_ms: number;
  respect_robots: boolean;
  same_domain: boolean;
  allow_subdomains: boolean;
  include_paths?: string[];
  exclude_paths?: string[];
  persona_id?: number;
  /** Which page elements the scrape keeps (preset + include/exclude CSS selectors + toggles). */
  content_spec?: ContentSpec;
}

/** The concrete scope a crawl WOULD use, from `POST /crawl/preview` — costs nothing. */
export interface CrawlScope {
  include_paths: string[];
  exclude_paths: string[];
  max_depth: number;
}

export interface PreviewSampleItem {
  url: string;
  /** Would this URL be crawled under the effective scope + relevance threshold? */
  kept: boolean;
  /** Relevance of the URL to the goal (~[-0.25, 1]); the frontier's sort key. */
  score: number;
}

export interface PreviewCrawlRequest {
  url: string;
  intent?: string;
  include_paths?: string[];
  exclude_paths?: string[];
  max_depth?: number;
  relevance_threshold?: number;
  same_domain?: boolean;
  allow_subdomains?: boolean;
  /** How many of the site's real URLs to sample (1–200). */
  sample_limit?: number;
}

export interface PreviewCrawlResponse {
  /** What the goal was translated into (empty arrays when no goal / no AI configured). */
  derived: CrawlScope & { reason?: string };
  /** Derived scope with any explicit include/exclude/depth override applied. */
  effective: CrawlScope;
  sample: PreviewSampleItem[];
  counts: { kept: number; dropped: number; total: number };
  brand?: CrawlBrand | string | null;
}

export interface MapUrlItem {
  url: string;
  score: number;
  /** Anchor text, else the URL's last path segment. */
  title?: string | null;
}

export interface MapCrawlResponse {
  urls: MapUrlItem[];
  count: number;
  brand?: CrawlBrand | string | null;
}

export interface ScrapePageResponse {
  verb: 'scrape';
  url: string;
  title?: string | null;
  format: 'markdown';
  markdown: string;
  counts: { chars: number; raw_tokens_est: number; clean_tokens_est: number };
  brand?: CrawlBrand | string | null;
}

/** Statuses at which a crawl has stopped moving (no more polling / cancel). */
export const TERMINAL_CRAWL_STATUSES: ReadonlySet<CrawlStatus> = new Set<CrawlStatus>([
  'completed',
  'failed',
  'cancelled',
]);

export const isCrawlTerminal = (status: CrawlStatus): boolean =>
  TERMINAL_CRAWL_STATUSES.has(status);

/** A crawl that can be cancelled (in-flight, not already stopping/terminal). */
export const isCrawlCancellable = (status: CrawlStatus): boolean =>
  status === 'queued' || status === 'mapping' || status === 'crawling';

/** Live agents currently working this crawl (dispatched shards not yet done). */
export const crawlAgentsWorking = (c: CrawlView): number =>
  Math.max(0, (c.shards_dispatched ?? 0) - (c.shards_done ?? 0));

/** The crawl's agent/concierge name — falls back to "Scribe" for a string/absent brand. */
export const crawlAgentName = (c?: CrawlView | null): string => {
  const b = c?.brand;
  return (b && typeof b === 'object' ? b.agent : undefined) || 'Scribe';
};

/** The crawl's capability name — falls back to "Dragnet" (string brand = its own value). */
export const crawlBrandName = (c?: CrawlView | null): string => {
  const b = c?.brand;
  if (typeof b === 'string' && b) return b;
  return (b && typeof b === 'object' ? b.crawl : undefined) || 'Dragnet';
};

function invalidateCrawls() {
  useQueryCache.getState().invalidate(Q.crawls());
}

export const crawlApi = {
  /** Every crawl, newest first (coordinator ordering). Wrapped as `{ crawls: [...] }`. */
  list: async (limit = 50): Promise<CrawlView[]> => {
    const response = await client.get(`/crawl?limit=${limit}`);
    return response.data?.crawls ?? [];
  },

  get: async (id: number | string): Promise<CrawlView> => {
    const response = await client.get(`/crawl/${id}`);
    return response.data;
  },

  start: async (req: StartCrawlRequest): Promise<CrawlView> => {
    const response = await client.post('/crawl', req);
    invalidateCrawls();
    return response.data;
  },

  /** Preview the scope a crawl would use (derived paths + a kept/dropped sample)
   *  without creating a crawl or dispatching an agent. */
  preview: async (req: PreviewCrawlRequest): Promise<PreviewCrawlResponse> => {
    const response = await client.post('/crawl/preview', req);
    return response.data;
  },

  /** List a site's URLs ranked by relevance to `search`, so the operator can pick
   *  which to crawl. Creates nothing. */
  map: async (req: { url: string; search?: string; limit?: number }): Promise<MapCrawlResponse> => {
    const response = await client.post('/crawl/map', req);
    return response.data;
  },

  /** Scrape ONE page to clean markdown — no crawl, no fleet dispatch. */
  scrape: async (url: string): Promise<ScrapePageResponse> => {
    const response = await client.post('/crawl/scrape', { url });
    return response.data;
  },

  cancel: async (id: number | string): Promise<CrawlView> => {
    const response = await client.post(`/crawl/${id}/cancel`);
    invalidateCrawls();
    useQueryCache.getState().invalidate(Q.crawl(id));
    return response.data;
  },

  /** Remove a crawl and its collected dataset. Only terminal crawls can be
   *  removed (a running crawl must be stopped first). */
  remove: async (id: number | string): Promise<void> => {
    await client.delete(`/crawl/${id}`);
    invalidateCrawls();
  },
};
