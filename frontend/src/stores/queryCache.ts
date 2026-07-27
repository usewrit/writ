import { create } from 'zustand';

interface CacheEntry<T = any> {
  data: T;
  timestamp: number;
  error: string | null;
  /**
   * Flagged by invalidate()/invalidateMatching() to request a background
   * refetch. The entry keeps its data so subscribers can keep showing it
   * while the refetch is in flight (stale-while-revalidate), then swap the
   * fresh result in place — no empty flash / skeleton flicker.
   */
  stale?: boolean;
}

const MAX_CACHE_ENTRIES = 200;

interface QueryCacheState {
  cache: Record<string, CacheEntry>;
  inflight: Record<string, Promise<any>>;

  get: <T>(key: string) => CacheEntry<T> | undefined;
  set: <T>(key: string, data: T, error?: string | null) => void;
  invalidate: (...keys: string[]) => void;
  invalidateMatching: (pattern: string) => void;
  clear: () => void;
  isFresh: (key: string, staleTime: number) => boolean;

  getInflight: (key: string) => Promise<any> | undefined;
  setInflight: (key: string, promise: Promise<any>) => void;
  clearInflight: (key: string) => void;
}

function evictOldest(cache: Record<string, CacheEntry>, maxSize: number): Record<string, CacheEntry> {
  const keys = Object.keys(cache);
  if (keys.length <= maxSize) return cache;

  // Sort by timestamp ascending (oldest first), evict the oldest
  const sorted = keys.sort((a, b) => cache[a].timestamp - cache[b].timestamp);
  const toEvict = sorted.slice(0, keys.length - maxSize);
  const next = { ...cache };
  for (const key of toEvict) delete next[key];
  return next;
}

export const useQueryCache = create<QueryCacheState>((set, get) => ({
  cache: {},
  inflight: {},

  get: <T,>(key: string) => {
    return get().cache[key] as CacheEntry<T> | undefined;
  },

  set: <T,>(key: string, data: T, error: string | null = null) => {
    set((state) => ({
      cache: evictOldest(
        // A fresh write clears the stale flag.
        { ...state.cache, [key]: { data, timestamp: Date.now(), error, stale: false } },
        MAX_CACHE_ENTRIES,
      ),
    }));
  },

  invalidate: (...keys: string[]) => {
    set((state) => {
      let changed = false;
      const next = { ...state.cache };
      for (const key of keys) {
        const entry = next[key];
        // Keep the data visible; just flag it so mounted queries refetch in
        // the background instead of the entry vanishing (which would blank
        // the UI until the refetch resolves).
        if (entry && !entry.stale) {
          next[key] = { ...entry, stale: true };
          changed = true;
        }
      }
      return changed ? { cache: next } : {};
    });
  },

  invalidateMatching: (pattern: string) => {
    set((state) => {
      let changed = false;
      const next = { ...state.cache };
      for (const [key, entry] of Object.entries(state.cache)) {
        if (key.includes(pattern) && !entry.stale) {
          next[key] = { ...entry, stale: true };
          changed = true;
        }
      }
      return changed ? { cache: next } : {};
    });
  },

  clear: () => set({ cache: {}, inflight: {} }),

  isFresh: (key: string, staleTime: number) => {
    const entry = get().cache[key];
    if (!entry || entry.stale) return false;
    return Date.now() - entry.timestamp < staleTime;
  },

  getInflight: (key: string) => get().inflight[key],

  setInflight: (key: string, promise: Promise<any>) => {
    set((state) => ({ inflight: { ...state.inflight, [key]: promise } }));
  },

  clearInflight: (key: string) => {
    set((state) => {
      const next = { ...state.inflight };
      delete next[key];
      return { inflight: next };
    });
  },
}));
