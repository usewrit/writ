import client from './client';
import i18n from '../i18n';
import type { ExecutionTier } from '../components/TierBadge';

export type RunType = 'workflow' | 'check' | 'ai_session' | 'automation' | 'crawl';

export type RunStatus = 'pending' | 'running' | 'success' | 'failed' | 'cancelled' | 'skipped';

export interface Run {
  /** Unique across the whole feed: "<run_type>-<row id>" — use as React key */
  id: string;
  run_type: RunType;
  entity_id: number | null;
  entity_name: string | null;
  status: RunStatus;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  trigger_source: string | null;
  error: string | null;
  credits_used: number | null;
  /**
   * Real-money price charged for THIS run (micro-USD), when one applies — e.g.
   * a marketplace/installed per-success run. Null/0 for unbilled own runs.
   */
  cost_micros?: number | null;
  /**
   * Isolation tier the run executed on when its venue was Writ Cloud (Phase 9).
   * 'isolated' = sensitive run on the gVisor ephemeral-process pool; 'shared' =
   * non-sensitive run on the warm-browser pool. Absent for own-BYO / foreign-BYO
   * supply runs (those never tier) or when the backend has not stamped it yet —
   * the UI renders a neutral placeholder in that case.
   */
  execution_tier?: ExecutionTier | null;
  /** Frontend route to the entity detail page, e.g. "/workflows/176" */
  detail_url_hint: string | null;
  /**
   * Direct link to this run's extracted-data view, when one exists (non-streaming
   * workflow runs), e.g. "/workflows/176?tab=data". Null when there's no data view.
   */
  data_url_hint?: string | null;
}

export interface RunsSummaryWindow {
  total: number;
  by_status: Record<RunStatus, number>;
  by_run_type: Record<RunType, number>;
}

export interface TopFailingEntity {
  run_type: RunType;
  entity_id: number;
  entity_name: string | null;
  failure_count: number;
  detail_url_hint: string | null;
}

export interface RunsSummary {
  last_24h: RunsSummaryWindow;
  last_7d: RunsSummaryWindow;
  top_failing: TopFailingEntity[];
}

export interface ListRunsParams {
  run_type?: RunType;
  status?: RunStatus;
  /** ISO 8601 datetime — runs started at/after */
  since?: string;
  /** ISO 8601 datetime — runs started at/before */
  until?: string;
  entity_id?: number;
  /** 1–200, default 50 */
  limit?: number;
  offset?: number;
}

export const runsApi = {
  list: async (params: ListRunsParams = {}): Promise<Run[]> => {
    const qs = new URLSearchParams();
    if (params.run_type) qs.append('run_type', params.run_type);
    if (params.status) qs.append('status', params.status);
    if (params.since) qs.append('since', params.since);
    if (params.until) qs.append('until', params.until);
    if (params.entity_id != null) qs.append('entity_id', String(params.entity_id));
    if (params.limit != null) qs.append('limit', String(params.limit));
    if (params.offset != null) qs.append('offset', String(params.offset));
    const response = await client.get(`/runs?${qs}`);
    return response.data;
  },

  getSummary: async (): Promise<RunsSummary> => {
    const response = await client.get('/runs/summary');
    return response.data;
  },

  /** Whether this run can be cancelled from the runs feed. Only workflow runs. */
  isCancellable: (run: Run): boolean =>
    (run.status === 'pending' || run.status === 'running') &&
    run.run_type === 'workflow',

  /**
   * Cancel/abort a pending or running run. The feed id is "<run_type>-<row id>",
   * where the row id maps to AutomationTask (workflow). Check/automation rows
   * have no cancel API.
   */
  cancel: async (run: Run): Promise<void> => {
    const rowId = run.id.slice(run.id.lastIndexOf('-') + 1);
    if (run.run_type === 'workflow') {
      await client.delete(`/automation/tasks/${rowId}`);
    } else {
      throw new Error(i18n.t('This run type cannot be cancelled'));
    }
  },
};
