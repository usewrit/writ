import { useCallback, useMemo, useState } from 'react';

// Client-side dismissal for "Needs attention" rows. A failed run is a transient
// record with no server-side "acknowledged" concept, so we persist the set of
// dismissed run ids in localStorage and filter them out of the feed. The list is
// capped so it can't grow without bound (failed runs come from a recent window,
// so old ids age out of the feed regardless).

const KEY = 'writ:needsAttention:dismissed';
const CAP = 300;

function load(): string[] {
  try {
    const raw = localStorage.getItem(KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr.filter((x): x is string => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

export function useDismissedFailures() {
  const [ids, setIds] = useState<string[]>(load);
  const dismissed = useMemo(() => new Set(ids), [ids]);

  const dismiss = useCallback((id: string) => {
    setIds(prev => {
      if (prev.includes(id)) return prev;
      const next = [...prev, id].slice(-CAP);
      try {
        localStorage.setItem(KEY, JSON.stringify(next));
      } catch {
        /* quota / unavailable — dismissal still holds for this session */
      }
      return next;
    });
  }, []);

  return { dismissed, dismiss };
}
