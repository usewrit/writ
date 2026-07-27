/**
 * Route prefetching — warms both the lazy JS chunk AND the page's primary data
 * query on link hover/focus (and the common routes on idle), so navigating to a
 * page renders instantly instead of: blank → Suspense spinner (chunk download)
 * → mount → data fetch.
 *
 * Chunk prefetch: the import specifiers MUST match App.tsx so the bundler maps
 * them to the same chunk (Rollup/Vite dedupe by resolved module id).
 *
 * Data prefetch: each entry primes the SAME queryCache key + fetcher the page
 * uses via useQuery, so when the page mounts it sees a fresh cache entry (or
 * piggybacks on the in-flight request) and skips its own fetch. Keep these in
 * sync with the page's primary useQuery call.
 */
import { useQueryCache } from './stores/queryCache';
import { Q } from './stores/queryKeys';
import { targetsApi, automationApi, triggersApi } from './api/endpoints';
import { runsApi } from './api/runs';
import { workflowDataApi } from './api/workflowData';

// Mirror useQuery's default staleTime so a freshly-warmed entry isn't re-fetched
// by the page on mount.
const STALE_TIME = 30_000;

/**
 * Prime a queryCache entry the same way useQuery would, with the same inflight
 * dedup protocol so a concurrent page mount piggybacks instead of double-fetching.
 * No-op when the entry is already fresh or a request is already in flight.
 */
function prime<T>(key: string, fetcher: () => Promise<T>): void {
  const state = useQueryCache.getState();
  if (state.isFresh(key, STALE_TIME)) return;
  if (state.getInflight(key)) return;
  const p = fetcher()
    .then((data) => {
      useQueryCache.getState().set(key, data);
      return data;
    })
    .catch(() => {
      // Swallow: the page's own useQuery will retry and report the error
      // properly. Never let a speculative prefetch surface a toast.
    })
    .finally(() => {
      useQueryCache.getState().clearInflight(key);
    });
  state.setInflight(key, p);
}

interface RouteEntry {
  /** Dynamic import of the page chunk (same specifier as App.tsx). */
  load: () => Promise<unknown>;
  /** Warm the page's primary data query/queries into the shared cache. */
  data?: () => void;
}

// Keyed by sidebar nav href. Only sidebar destinations need entries — those are
// what a user hovers before clicking.
const ROUTES: Record<string, RouteEntry> = {
  '/': { load: () => import('./pages/Dashboard') },
  '/runs': {
    load: () => import('./pages/RunsPage'),
    // Mirrors RunsPage (key 'runs:recent', LOAD_LIMIT = 200).
    data: () => prime(Q.key('runs:recent'), () => runsApi.list({ limit: 200 })),
  },
  '/data': {
    load: () => import('./pages/data/DataExplorerPage'),
    data: () => prime(Q.dataWorkflows(), () => workflowDataApi.listWorkflows()),
  },
  '/checks': {
    load: () => import('./pages/checks/ChecksListPage'),
    data: () => {
      prime(Q.targets('content'), () => targetsApi.getAll('content'));
    },
  },
  '/workflows': {
    load: () => import('./pages/workflows/WorkflowsListPage'),
    data: () => prime(Q.workflows(), () => automationApi.listWorkflows()),
  },
  '/automations': {
    load: () => import('./pages/automations/AutomationsListPage'),
    data: () => prime(Q.triggers(), () => triggersApi.listAll()),
  },
  // Site crawl (Dragnet) — a first-class Build surface reachable from the sidebar.
  '/crawls': { load: () => import('./pages/crawls/CrawlsPage') },
  '/streaming': {
    load: () => import('./pages/streaming/StreamingListPage'),
    // Shares the workflows list key — warming it here also warms /workflows.
    data: () => prime(Q.workflows(), () => automationApi.listWorkflows()),
  },
  '/developers': { load: () => import('./pages/developers/DevelopersPage') },
  '/connect': { load: () => import('./pages/connect/ConnectPage') },
  '/fleet': { load: () => import('./pages/fleet/FleetPage') },
  '/secrets': { load: () => import('./pages/SecretsPage') },
  '/personas': { load: () => import('./pages/PersonasPage') },
  '/files': { load: () => import('./pages/Files') },
  '/settings': { load: () => import('./pages/Settings') },
};

// Don't re-trigger a chunk import we've already kicked off this session.
const chunkPrefetched = new Set<string>();

/**
 * Begin warming a sidebar destination (chunk + primary data). Safe to call on
 * every hover — deduped, and a no-op for unknown/external hrefs. The chunk is
 * fetched at most once; data warming self-dedupes via cache freshness.
 */
export function prefetchRoute(href: string): void {
  const entry = ROUTES[href];
  if (!entry) return;
  if (!chunkPrefetched.has(href)) {
    chunkPrefetched.add(href);
    entry.load().catch(() => {
      // Allow a later hover/click to retry the chunk download.
      chunkPrefetched.delete(href);
    });
  }
  // Data warming is cheap to attempt repeatedly (freshness-gated), so it's not
  // tied to the chunk dedup marker.
  entry.data?.();
}

// The nav destinations a logged-in user is most likely to visit next. Warmed
// first (chunk + data) so the landing → first-click path is instant.
const COMMON_ROUTES = ['/checks', '/workflows', '/runs', '/data', '/automations'];

/**
 * Pages reached by a row click or deep link rather than the sidebar (detail /
 * create sub-pages), so they have no ROUTES hover entry. Warmed chunk-only after
 * login so the first click into them is instant too. Same import specifiers as
 * App.tsx → the bundler maps them to the same chunks.
 */
const EXTRA_CHUNKS: Array<() => Promise<unknown>> = [
  () => import('./pages/checks/CheckCreatePage'),
  () => import('./pages/checks/CheckDetailPage'),
  () => import('./pages/workflows/WorkflowCreatePage'),
  () => import('./pages/workflows/WorkflowDetailPage'),
  () => import('./pages/streaming/StreamingSessionPage'),
  () => import('./pages/automations/AutomationBuilderPage'),
  () => import('./pages/automations/AutomationRunsPage'),
  () => import('./pages/crawls/CrawlCreatePage'),
  () => import('./pages/dragnet/DragnetDetailPage'),
  () => import('./components/notifications/NotificationsPage'),
];

/**
 * After login, warm EVERY route chunk in the background so navigating anywhere in
 * the app is instant — no blank → Suspense spinner while a JS chunk downloads.
 * This is the "it's a tool, not a website" tradeoff: spend one idle-time download
 * pass right after sign-in to buy zero-latency navigation for the whole session.
 *
 * Order: most-likely nav destinations first (they also prime their data), then the
 * remaining sidebar pages, then row/detail/create pages. Everything runs on
 * requestIdleCallback in small batches so it never contends with the landing
 * page's own above-the-fold work; the browser's per-host connection cap naturally
 * throttles the chunk downloads. Every step self-dedupes (prefetchRoute guards the
 * chunk; the module system caches import()), so a later mount call is cheap.
 */
export function prefetchAllRoutes(): void {
  const tasks: Array<() => void> = [];

  const navHrefs = [
    ...COMMON_ROUTES,
    ...Object.keys(ROUTES).filter((h) => !COMMON_ROUTES.includes(h)),
  ];
  for (const href of navHrefs) tasks.push(() => prefetchRoute(href));
  for (const load of EXTRA_CHUNKS) tasks.push(() => void load().catch(() => {}));

  const ric: typeof window.requestIdleCallback | undefined =
    typeof window !== 'undefined' ? window.requestIdleCallback : undefined;
  const BATCH = 4;
  const run = (i: number) => {
    if (i >= tasks.length) return;
    for (let j = i; j < Math.min(i + BATCH, tasks.length); j++) tasks[j]();
    const next = () => run(i + BATCH);
    if (ric) ric(next, { timeout: 2000 });
    else setTimeout(next, 150);
  };
  if (ric) ric(() => run(0), { timeout: 2000 });
  else setTimeout(() => run(0), 600);
}
