import { useQuery } from './useQuery';
import { Q } from '../stores/queryKeys';
import { userRecorderApi } from '../api/endpoints';

/**
 * Connected user-hosted recorder agents (GET /user-recorder/agents). The
 * endpoint returns { agents, limit, active_count }; callers that need the
 * richer per-agent rows (management page) read additional fields off `agents`,
 * so the value stays loosely typed to preserve the prior untyped semantics.
 */
export interface UserAgentsData {
  agents?: any[];
  limit?: number;
  active_count?: number;
  [key: string]: any;
}

/**
 * Shared subscription to the caller's connected user-hosted recorder agents.
 * Every consumer reads the SAME Q.userAgents() cache entry, so the response is
 * identical everywhere and a single fetch per stale window fans out to all
 * subscribers.
 *
 * Cadence is centralized here so we don't have N different poll intervals
 * hammering the same endpoint. Most call sites just need the cached value and
 * pass no interval (relying on the shared cache + staleTime refetch); pages
 * that surface live agent connectivity may pass an explicit `pollInterval`.
 */
export function useUserAgents(opts: { pollInterval?: number } = {}) {
  return useQuery<UserAgentsData>(
    Q.userAgents(),
    () => userRecorderApi.getAgents(),
    { pollInterval: opts.pollInterval },
  );
}
