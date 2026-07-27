import { useCallback, useEffect, useRef, useState } from 'react';
import { runsApi, type Run } from '../api/runs';
import { streamingApi, targetsApi } from '../api/endpoints';
import type { StreamingSession, Target } from '../types/api';
import { useLiveRuns, mergeOptimistic } from '../stores/liveRuns';

/** Run statuses that count as "in progress" (queued or executing). */
const ACTIVE_RUN_STATUSES = new Set(['pending', 'running']);
/** Streaming statuses that still count as "live" (not ended / failed). */
const ACTIVE_SESSION_STATUSES = new Set(['starting', 'queued', 'running', 'ending']);
/** Idle cadence when nothing transient is live. A run starting/ending still
 *  surfaces immediately via the run-events push (or a launch's optimistic
 *  insert), so idling never adds latency — it only stops the fast poll while the
 *  app sits with no in-flight runs/sessions. Enabled monitors don't force the
 *  fast cadence (they tick on their own minutes-scale schedule). */
const IDLE_POLL_MS = 15000;

async function safeList<T>(fetcher: () => Promise<T[]>): Promise<T[]> {
  try {
    const v = await fetcher();
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

export interface ActivitySnapshot {
  /** Workflow / AI-session runs in progress right now. */
  runs: Run[];
  /** Live streaming (watch-and-act) sessions. */
  sessions: StreamingSession[];
  /**
   * Enabled monitors — shown in the indicator as live, scheduled work with a per-row countdown to
   * the next check (mirrors the monitor list page's interval display).
   */
  monitors: Target[];
  /** runs.length + sessions.length + monitors.length. */
  total: number;
  loading: boolean;
  /** Force an immediate re-poll (e.g. right after a stop action). */
  refresh: () => void;
}

/**
 * A monitor is "active" (worth surfacing as live, scheduled work) when it's enabled. We list these
 * with a live next-check countdown rather than only when momentarily "due" — an enabled monitor IS
 * live work (it's watching on a cadence), and the countdown mirrors the list page's interval UI.
 */
function monitorIsActive(t: any): boolean {
  return !(t.enabled === false || t.enabled === 0);
}

/**
 * Aggregates the three "live work" surfaces — in-progress runs, live streaming
 * sessions, and active monitors — on a single gentle poll for the always-mounted
 * chrome indicator. Every source fails soft to empty so the indicator can never
 * throw, and the poll pauses while the tab is hidden.
 */
export function useActivity(pollMs = 2000): ActivitySnapshot {
  const [runs, setRuns] = useState<Run[]>([]);
  const [sessions, setSessions] = useState<StreamingSession[]>([]);
  const [monitors, setMonitors] = useState<Target[]>([]);
  const [loading, setLoading] = useState(true);
  // Latest tick, so refresh() can re-poll on demand without re-running the effect.
  const tickRef = useRef<() => void>(() => {});
  // Re-render when an optimistic launch is added/removed, or a push bumps epoch.
  const optimistic = useLiveRuns((s) => s.optimistic);
  const epoch = useLiveRuns((s) => s.epoch);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    // Adaptive cadence: fast while there's transient live work, gentle when idle.
    let hasLiveWork = false;

    const schedule = () => {
      if (timer) clearTimeout(timer);
      if (cancelled) return;
      timer = setTimeout(() => void tick(), hasLiveWork ? pollMs : IDLE_POLL_MS);
    };

    const tick = async () => {
      if (document.hidden) {
        // Don't fetch while hidden, but keep the loop alive so it resumes.
        schedule();
        return;
      }
      const [runList, sessionList, targetList] = await Promise.all([
        safeList<Run>(() => runsApi.list({ limit: 50 })),
        safeList<StreamingSession>(() => streamingApi.listSessions()),
        safeList<Target>(() => targetsApi.getAll()),
      ]);
      if (cancelled) return;
      // Drop optimistic launches the server now reports.
      useLiveRuns.getState().reconcile(runList.map((r) => r.id));
      const activeServer = runList.filter((r) => ACTIVE_RUN_STATUSES.has(r.status));
      const mergedActive = mergeOptimistic(activeServer).filter((r) => ACTIVE_RUN_STATUSES.has(r.status));
      const liveSessions = sessionList.filter((s) => ACTIVE_SESSION_STATUSES.has(s.status));
      setRuns(activeServer);
      setSessions(liveSessions);
      setMonitors(targetList.filter(monitorIsActive));
      setLoading(false);
      hasLiveWork = mergedActive.length + liveSessions.length > 0;
      schedule();
    };

    tickRef.current = () => void tick();
    void tick();
    const onVisible = () => {
      if (!document.hidden) void tick();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [pollMs]);

  // A run-events push (or a local launch) bumps epoch → refetch immediately.
  useEffect(() => {
    tickRef.current();
  }, [epoch]);

  const refresh = useCallback(() => tickRef.current(), []);

  // Merge optimistic launches into the returned list so a just-launched run
  // shows on the very next render, before the refetch lands. `optimistic` is
  // read above so this recomputes when it changes.
  void optimistic;
  const mergedRuns = mergeOptimistic(runs).filter((r) => ACTIVE_RUN_STATUSES.has(r.status));

  return {
    runs: mergedRuns,
    sessions,
    monitors,
    total: mergedRuns.length + sessions.length + monitors.length,
    loading,
    refresh,
  };
}

export default useActivity;
