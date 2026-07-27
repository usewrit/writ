import { useEffect, useState } from 'react';
import { fleetApi, type FleetCapacity } from '../api/fleet';

// Small module-level cache so the interval picker + hint don't each refetch on
// every keystroke. Capacity changes only when agents connect/disconnect, so a
// short TTL is plenty and keeps the create form snappy.
let _cache: { data: FleetCapacity; at: number } | null = null;
const TTL_MS = 15000;

/**
 * Read the fleet's check-capacity advisory (how many agents are connected and
 * therefore the fastest supported check interval). Returns null until the first
 * fetch resolves; never throws (a failed fetch just yields null / stale cache).
 */
export function useFleetCapacity(): FleetCapacity | null {
  const [cap, setCap] = useState<FleetCapacity | null>(_cache?.data ?? null);

  useEffect(() => {
    // A fresh cache was already read by the useState initialiser above, so there
    // is nothing to write here — re-setting it would only cost a render pass.
    if (_cache && Date.now() - _cache.at < TTL_MS) return;
    let alive = true;
    fleetApi
      .capacity()
      .then((d) => {
        _cache = { data: d, at: Date.now() };
        if (alive) setCap(d);
      })
      .catch(() => {
        /* leave whatever we had (or null) */
      });
    return () => {
      alive = false;
    };
  }, []);

  return cap;
}

/** Human interval label (matches the preset vocabulary: 10s / 1m / 1h). */
export function formatInterval(ms: number): string {
  if (ms % 86400000 === 0) return `${ms / 86400000}d`;
  if (ms % 3600000 === 0) return `${ms / 3600000}h`;
  if (ms % 60000 === 0) return `${ms / 60000}m`;
  return `${Math.round(ms / 1000)}s`;
}
