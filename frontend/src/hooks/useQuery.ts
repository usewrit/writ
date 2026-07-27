import { useEffect, useRef, useCallback } from 'react';
import toast from 'react-hot-toast';
import { useQueryCache } from '../stores/queryCache';
import { apiErrorMessage } from '../api/client';
import i18n from '../i18n';

interface UseQueryOptions {
  /** Time in ms before cached data is considered stale (default: 30s) */
  staleTime?: number;
  /** Set to false to disable automatic fetching */
  enabled?: boolean;
  /** If set, re-fetches on this interval (ms) */
  pollInterval?: number;
  /** Set to true to suppress the automatic error toast for this query */
  silent?: boolean;
}

const DEFAULT_STALE_TIME = 30_000;

// One toast per query key per minute, and only when the query *transitions*
// into the error state — a 5s polling query failing repeatedly toasts once,
// not every poll. 401/402 are handled globally (login redirect / upgrade
// prompt), so they never toast here.
const ERROR_TOAST_COOLDOWN_MS = 60_000;
const lastErrorToastAt: Record<string, number> = {};

function notifyQueryError(cacheKey: string, err: any, hadError: boolean, silent?: boolean) {
  if (silent || hadError) return;
  const status = err?.response?.status;
  if (status === 401 || status === 402) return;
  const now = Date.now();
  if (lastErrorToastAt[cacheKey] && now - lastErrorToastAt[cacheKey] < ERROR_TOAST_COOLDOWN_MS) return;
  lastErrorToastAt[cacheKey] = now;
  toast.error(apiErrorMessage(err, i18n.t('Failed to load data')));
}

export function useQuery<T>(
  key: string | string[],
  fetcher: () => Promise<T>,
  options: UseQueryOptions = {}
) {
  const {
    staleTime = DEFAULT_STALE_TIME,
    enabled = true,
    pollInterval,
    silent,
  } = options;
  const silentRef = useRef(silent);

  // Stable cache key — avoid useMemo with computed deps
  const rawKey = Array.isArray(key) ? key.join(':') : key;
  const cacheKeyRef = useRef(rawKey);

  const isMounted = useRef(true);
  const fetcherRef = useRef(fetcher);

  // The "latest value" mirrors are refreshed AFTER commit, never during render —
  // writing a ref in the render path is a render-time mutation React may tear
  // on. Declared FIRST so it runs before the fetch/poll effects below (React
  // runs a commit's effects in declaration order), which means every doFetch()
  // they trigger already sees this render's key, fetcher and silent flag.
  useEffect(() => {
    silentRef.current = silent;
    cacheKeyRef.current = rawKey;
    fetcherRef.current = fetcher;
  });

  // Subscribe only to the specific cache entry, not the whole store
  const currentCache = useQueryCache((s) => s.cache[rawKey]);

  const doFetch = useCallback(async () => {
    const cacheKey = cacheKeyRef.current;
    const state = useQueryCache.getState();

    // Dedup: if there's already an inflight request for this key, piggyback on it
    const existing = state.getInflight(cacheKey);
    if (existing) return existing;

    // Register inflight BEFORE starting the fetch to prevent races
    const promise = (async () => {
      try {
        const data = await fetcherRef.current();
        // Cache the data even if unmounted / re-keyed — another component may use it.
        useQueryCache.getState().set(cacheKey, data);
        return data;
      } catch (err: any) {
        if (isMounted.current && cacheKeyRef.current === cacheKey) {
          const msg = apiErrorMessage(err, i18n.t('An error occurred'));
          // On error: preserve existing cached data, only update the error field
          // Don't reset the timestamp — stale data is still valid
          const existing = useQueryCache.getState().get(cacheKey);
          notifyQueryError(cacheKey, err, Boolean(existing?.error), silentRef.current);
          if (existing) {
            // Keep old data + old timestamp, just add error
            useQueryCache.getState().set(cacheKey, existing.data, msg);
          } else {
            useQueryCache.getState().set(cacheKey, null, msg);
          }
        }
      } finally {
        useQueryCache.getState().clearInflight(cacheKey);
      }
    })();

    state.setInflight(cacheKey, promise);
    return promise;
  }, []); // No deps — uses refs internally

  // Main effect: fetch on mount + set up polling
  useEffect(() => {
    isMounted.current = true;
    if (!enabled) return;

    const cacheKey = rawKey;
    if (!useQueryCache.getState().isFresh(cacheKey, staleTime)) doFetch();

    // Polling
    let timer: ReturnType<typeof setInterval> | null = null;
    const startPolling = () => {
      if (timer) clearInterval(timer);
      if (pollInterval && pollInterval > 0) {
        timer = setInterval(() => {
          if (isMounted.current) doFetch();
        }, pollInterval);
      }
    };
    const stopPolling = () => {
      if (timer) { clearInterval(timer); timer = null; }
    };

    startPolling();

    // Pause polling when tab is hidden, resume when visible
    const handleVisibility = () => {
      if (document.hidden) {
        stopPolling();
      } else {
        if (isMounted.current) doFetch(); // Catch up immediately
        startPolling();
      }
    };

    if (pollInterval && pollInterval > 0) {
      document.addEventListener('visibilitychange', handleVisibility);
    }

    return () => {
      isMounted.current = false;
      stopPolling();
      if (pollInterval && pollInterval > 0) {
        document.removeEventListener('visibilitychange', handleVisibility);
      }
    };
  }, [rawKey, enabled, staleTime, pollInterval, doFetch]);

  // Background revalidation: when a mutation marks this entry stale (via
  // invalidate/invalidateMatching), refetch WITHOUT clearing the visible data
  // or flipping `loading`. `invalidate` only flags the entry, so the list keeps
  // showing the current rows and the fresh result swaps in place — no empty
  // flash / skeleton flicker.
  useEffect(() => {
    if (!enabled) return;
    if (currentCache?.stale && !useQueryCache.getState().getInflight(rawKey)) {
      doFetch();
    }
  }, [currentCache?.stale, enabled, rawKey, doFetch]);

  const refresh = useCallback(() => {
    useQueryCache.getState().invalidate(cacheKeyRef.current);
    return doFetch();
  }, [doFetch]);

  return {
    data: (currentCache?.data ?? null) as T | null,
    // `loading` is DERIVED from the cache entry rather than stored: an entry
    // exists for this key exactly when there is something to render — data, or
    // the null+message entry a failed first load writes. That is the same
    // condition the old `localLoading` state tracked (it was seeded with
    // `!currentCache` and only ever cleared once an entry landed), minus the
    // state write from inside the fetch effect. A stale-while-revalidate
    // refetch keeps its entry, so it still never flips loading back to true.
    loading: !currentCache,
    error: currentCache?.error ?? null,
    refresh,
  };
}
