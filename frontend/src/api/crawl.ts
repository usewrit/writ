import client from './client';
import { useQueryCache } from '../stores/queryCache';
import { Q } from '../stores/queryKeys';

/**
 * Dragnet — the self-hosted whole-site distributed crawl API (`/api/crawl`).
 *
 * A crawl maps a seed URL, fans the discovered pages out across the agent fleet
 * as shards, and lands the collected pages into a synthetic per-crawl workflow
 * whose extracted-data grid the app already renders (Workflow Data API).
 *
 * Two orthogonal axes: `executor` decides WHO reads each page ('regular' =
 * deterministic, or 'ai' = a per-page AI read against `extract_prompt`, running on
 * the provider configured in Settings → AI), and `extract_mode` the output SHAPE
 * (markdown | schema). Both fields stay optional on the wire so a response from an
 * older coordinator still parses.
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
 * WHO reads each page: 'regular' (deterministic crawl — free, fast) or 'ai' (every
 * fetched page is read against `extract_prompt` by the owner's OWN AI provider).
 * A missing value reads as 'regular', so a response from an older coordinator —
 * which had no AI executor at all — still renders as the deterministic crawl.
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
  /** The login identity this crawl runs as (id only — never the session). */
  persona_id?: number | null;
  /** For the ai executor: the per-page extraction instruction. */
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
  /** For the ai executor: what every page should yield. Required when executor='ai'. */
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


/**
 * A SAVED crawl — a stored configuration with a stable slug.
 *
 * A CrawlView is one RUN; its id dies with that run, so a crawl had no stable
 * handle to expose as an API. A definition owns the settings, so it can be
 * called with a minted key and re-run with exactly those settings — and, with
 * `max_age`, answered from the data it already collected.
 */
export interface CrawlDefinition {
  id: number;
  slug: string;
  name: string;
  description?: string | null;
  seed_url: string;
  /** The saved StartCrawlRequest payload. PATCH it back verbatim to edit. */
  config: Partial<StartCrawlRequest> & { url?: string };
  /** Freshness applied when a caller omits max_age (null = always re-crawl). */
  default_max_age_seconds?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
  last_run_at?: string | null;
  run_url?: string;
  data_url?: string;
}

/** The canonical freshness stamp every surface returns (body, not headers — an
 *  MCP tool or SDK client never sees a header). */
export interface CacheStamp {
  hit: boolean;
  age_seconds?: number;
  source_crawl_id?: number;
}

/** One page of a crawl's collected rows, in the Workflow Data API's shape. */
export interface CrawlDataTable {
  columns?: string[];
  rows?: Array<Record<string, unknown>>;
  total?: number;
  truncated?: boolean;
}

export interface RunSavedCrawlResponse {
  /** True when this answered from already-collected data — nothing was crawled. */
  cached: boolean;
  _cache?: CacheStamp;
  definition: CrawlDefinition;
  crawl: CrawlView;
  status_url?: string | null;
  data_url?: string | null;
  /** Present on a freshness hit (and after wait=true); absent on a cold dispatch. */
  data?: CrawlDataTable | null;
}

export interface SavedCrawlDataResponse {
  definition: CrawlDefinition;
  crawl: CrawlView | null;
  age_seconds?: number | null;
  data_url?: string | null;
  data?: CrawlDataTable | null;
}

/** Delivery controls for a saved-crawl run. Never crawl settings — those are saved. */
export interface RunSavedCrawlOptions {
  max_age?: number;
  wait?: boolean;
  timeout?: number;
  limit?: number;
}

/** Human label for a freshness window, for the picker + the copy snippets. */
export const FRESHNESS_PRESETS: ReadonlyArray<{ seconds: number; label: string }> = [
  { seconds: 0, label: 'Always re-crawl' },
  { seconds: 3600, label: '1 hour' },
  { seconds: 86400, label: '24 hours' },
  { seconds: 604800, label: '7 days' },
];

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
  map: async (req: { url: string; search?: string; limit?: number; persona_id?: number | null }): Promise<MapCrawlResponse> => {
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

  // ── Saved crawls (the callable crawl API) ─────────────────────────────────

  /** Every saved crawl, newest first. */
  listDefinitions: async (limit = 50): Promise<CrawlDefinition[]> => {
    const response = await client.get('/crawl/definitions', { params: { limit } });
    return response.data?.definitions ?? [];
  },

  getDefinition: async (ref: number | string): Promise<CrawlDefinition> => {
    const response = await client.get(`/crawl/definitions/${ref}`);
    return response.data;
  },

  /**
   * Save a crawl configuration so it becomes callable by API and re-runnable.
   *
   * Pass EITHER `config` or `from_crawl_id`. Prefer `from_crawl_id` when saving an
   * existing crawl: a crawl's status view does not echo every knob it ran with
   * (politeness, shard sizing, path filters), so a config rebuilt on the client
   * would silently substitute defaults and save a crawl that behaves differently.
   */
  saveDefinition: async (body: {
    name?: string;
    slug?: string;
    description?: string;
    default_max_age_seconds?: number | null;
    config?: StartCrawlRequest;
    from_crawl_id?: number;
  }): Promise<CrawlDefinition> => {
    const response = await client.post('/crawl/definitions', body);
    return response.data;
  },

  updateDefinition: async (
    ref: number | string,
    body: {
      name?: string;
      description?: string;
      default_max_age_seconds?: number | null;
      config?: StartCrawlRequest;
    },
  ): Promise<CrawlDefinition> => {
    const response = await client.patch(`/crawl/definitions/${ref}`, body);
    return response.data;
  },

  removeDefinition: async (ref: number | string): Promise<void> => {
    await client.delete(`/crawl/definitions/${ref}`);
  },

  /**
   * Run a saved crawl. With `max_age`, a recent completed run's data comes back
   * immediately (`cached: true`) and nothing is crawled; otherwise the saved
   * settings are re-crawled and a handle is returned (HTTP 202).
   */
  runDefinition: async (
    ref: number | string,
    opts: RunSavedCrawlOptions = {},
  ): Promise<RunSavedCrawlResponse> => {
    const response = await client.post(`/crawl/definitions/${ref}/run`, opts);
    return response.data;
  },

  /** Data already collected by a saved crawl's latest completed run. Never crawls. */
  definitionData: async (
    ref: number | string,
    limit = 50,
  ): Promise<SavedCrawlDataResponse> => {
    const response = await client.get(`/crawl/definitions/${ref}/data`, { params: { limit } });
    return response.data;
  },
};
