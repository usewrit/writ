import { create } from 'zustand';
import type { Run, RunStatus } from '../api/runs';

/**
 * Client-side "live runs" coordination layer.
 *
 * Two jobs, both aimed at making run state feel INSTANT rather than poll-bound:
 *
 *  1. `epoch` — a monotonically bumped counter that non-react-query consumers
 *     (chiefly {@link useActivity}) watch to trigger an immediate refetch. It is
 *     bumped by the run-events SSE stream (a run started/ended server-side) and
 *     by a local launch. React-query views instead react through the query
 *     cache's stale-revalidate path (see `invalidateRunFeed`).
 *
 *  2. `optimistic` — runs inserted the moment they're launched, shown in Live
 *     activity and the runs feed BEFORE the server round-trips. Each entry
 *     auto-expires (so a lost launch can't get stuck) and is dropped as soon as
 *     the server feed reports the same run (`reconcile`).
 */

interface OptimisticEntry {
  run: Run;
  /** Wall-clock ms after which this optimistic row is dropped if the server
   *  feed still hasn't caught up (defensive — a real run always reconciles). */
  expiresAt: number;
}

interface LiveRunsState {
  epoch: number;
  optimistic: Record<string, OptimisticEntry>;
  /** Nudge epoch subscribers that run state changed (refetch now). */
  signal: () => void;
  /** Optimistically insert/replace a run on launch, then signal. */
  addOptimistic: (run: Run, ttlMs?: number) => void;
  /** Drop optimistic entries the server now reports (any status) or that expired. */
  reconcile: (serverIds: Iterable<string>) => void;
}

export const useLiveRuns = create<LiveRunsState>((set) => ({
  epoch: 0,
  optimistic: {},
  signal: () => set((s) => ({ epoch: s.epoch + 1 })),
  addOptimistic: (run, ttlMs = 25000) =>
    set((s) => ({
      epoch: s.epoch + 1,
      optimistic: { ...s.optimistic, [run.id]: { run, expiresAt: Date.now() + ttlMs } },
    })),
  reconcile: (serverIds) =>
    set((s) => {
      const known = serverIds instanceof Set ? serverIds : new Set(serverIds);
      const now = Date.now();
      let changed = false;
      const next: Record<string, OptimisticEntry> = {};
      for (const [id, e] of Object.entries(s.optimistic)) {
        if (known.has(id) || e.expiresAt <= now) {
          changed = true;
          continue;
        }
        next[id] = e;
      }
      return changed ? { optimistic: next } : {};
    }),
}));

/**
 * Merge optimistic runs into a server list: the server wins on id collision, and
 * any optimistic run not yet present (and not expired) is prepended (newest).
 * Pure read — call it in render; components that also read `optimistic` from the
 * store re-render when it changes.
 */
export function mergeOptimistic(serverRuns: Run[]): Run[] {
  const optimistic = useLiveRuns.getState().optimistic;
  const ids = Object.keys(optimistic);
  if (ids.length === 0) return serverRuns;
  const now = Date.now();
  const present = new Set(serverRuns.map((r) => r.id));
  const extra = ids
    .map((id) => optimistic[id])
    .filter((e) => e.expiresAt > now && !present.has(e.run.id))
    .map((e) => e.run);
  return extra.length ? [...extra, ...serverRuns] : serverRuns;
}

/** Build the optimistic Run shown the instant a workflow is launched. */
export function optimisticWorkflowRun(
  taskId: number | string,
  opts: { workflowId?: number | null; name?: string | null; status?: RunStatus } = {},
): Run {
  const wid = opts.workflowId ?? null;
  return {
    id: `workflow-${taskId}`,
    run_type: 'workflow',
    entity_id: wid,
    entity_name: opts.name ?? null,
    status: opts.status ?? 'pending',
    started_at: new Date().toISOString(),
    finished_at: null,
    duration_ms: null,
    trigger_source: 'manual',
    error: null,
    credits_used: null,
    detail_url_hint: wid != null ? `/workflows/${wid}` : null,
  };
}
