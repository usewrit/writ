import client from './client';

/**
 * Home Health dashboard API.
 *
 * Wraps three existing, org-scoped coordinator endpoints used by the
 * health-first Home overview:
 *   - GET /runs/summary   (24h / 7d aggregates + top failing entities)
 *   - GET /runs           (recent run feed — used for "Needs attention")
 *   - GET /user-recorder/agents  (connected recorder agents online/offline)
 *
 * Types mirror the API contract verbatim. The runs types intentionally
 * duplicate the shapes in ./runs.ts so this feature file is self-contained.
 */

export type RunType = 'workflow' | 'check' | 'ai_session' | 'automation' | 'crawl';

export type RunStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'failed'
  | 'cancelled'
  | 'skipped';

export interface HealthRun {
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
  /** Frontend route to the entity detail page, e.g. "/workflows/176" */
  detail_url_hint: string | null;
  /** AI self-repair fixed this run's workflow in-session (workflow runs only). */
  repaired?: boolean | null;
  /** AI self-repair was attempted for this run and gave up — this needs a human. */
  repair_failed?: boolean | null;
  /** The parent workflow was AI-repaired after this run failed, so it's already resolved. */
  repaired_since?: boolean | null;
}

export interface RunsSummaryWindow {
  total: number;
  /** Always contains all six status keys (zeros included). */
  by_status: Record<RunStatus, number>;
  /** Always contains all four run-type keys (zeros included). */
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
  /** Up to 5 entities, last 7 days, desc by failure_count. Empty when nothing failed. */
  top_failing: TopFailingEntity[];
}

export interface HealthAgent {
  agent_id: string;
  name?: string | null;
  status: string; // 'online' | 'offline'
  platform?: string | null;
  ai_provider?: string | null;
  active_sessions?: number;
  max_sessions?: number;
  last_seen_at?: string | null;
}

export interface AgentsResponse {
  agents: HealthAgent[];
  limit: number;
  active_count: number;
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

export const homeHealthApi = {
  getSummary: async (): Promise<RunsSummary> => {
    const response = await client.get('/runs/summary');
    return response.data;
  },

  listRuns: async (params: ListRunsParams = {}): Promise<HealthRun[]> => {
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

  /** Recent failed runs across all sources (for the "Needs attention" list). */
  listRecentFailures: async (limit = 10): Promise<HealthRun[]> => {
    const response = await client.get(`/runs?status=failed&limit=${limit}`);
    return response.data;
  },

  getAgents: async (): Promise<AgentsResponse> => {
    const response = await client.get('/user-recorder/agents');
    return response.data;
  },
};
