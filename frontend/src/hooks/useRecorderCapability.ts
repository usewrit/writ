import { useQuery } from './useQuery';
import { Q } from '../stores/queryKeys';
import { userRecorderApi } from '../api/endpoints';

/**
 * Plan-aware recording capability + live local-agent state, polled together.
 *
 * Drives the creation wizard's pre-flight gate: free users get a limited amount
 * of cloud recording, after which they must connect their own local agent; paid
 * users record on cloud infra automatically (and auto-wait when it's busy).
 *
 * Polls every 5s so the gate auto-resolves the moment a local agent connects.
 */
export interface RecorderCapability {
  plan: string;
  cloud_recording_allowed: boolean;
  cloud_recording_unlimited: boolean;
  cloud_quota_limit: number | null;
  cloud_quota_used: number;
  cloud_quota_remaining: number | null;
  requires_local_agent_when_exhausted: boolean;
  max_user_recorders: number;
  online_local_agents: number;
  cloud_available: boolean;
}

const POLL_MS = 5000;

export function useRecorderCapability(enabled: boolean = true) {
  const { data, loading, refresh } = useQuery<RecorderCapability>(
    Q.recorderCapability(),
    () => userRecorderApi.getCapability(),
    { enabled, pollInterval: POLL_MS, staleTime: POLL_MS, silent: true },
  );

  const capability = data ?? null;
  const onlineLocalAgents = capability?.online_local_agents ?? 0;
  const cloudAvailable = !!capability?.cloud_available;
  const isPaidCloud = !!capability?.cloud_recording_unlimited;

  // Can we even attempt a recording right now? Either a local agent is online,
  // or cloud recording is available (quota remaining / unlimited).
  const canAttempt = onlineLocalAgents > 0 || cloudAvailable;

  return {
    capability,
    loading,
    refresh,
    onlineLocalAgents,
    cloudAvailable,
    isPaidCloud,
    canAttempt,
  };
}
