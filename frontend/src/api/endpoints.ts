import client from './client';
import { useQueryCache } from '../stores/queryCache';
import { Q } from '../stores/queryKeys';
import { useLiveRuns, optimisticWorkflowRun } from '../stores/liveRuns';
import i18n from '../i18n';
import {
  Agent,
  Target,
  TargetChange,
  RecentChange,
  ApiKey,
  ScheduleConfig,
  TimeSlot,
  LogEntry,
  LogFilter,
  ApiResponse,
  PaginatedResponse,
  PushoverRecipient,
  TargetNotificationsResponse,
  AutomationWorkflow,
  AutomationTask,
  TargetAutomation,
  WorkflowStep,
  AIWorkflowSession,
  AISessionUpdate,
  StreamingSession,
  Persona,
  PersonaCreate,
  PersonaSignInResult,
  PersonaUpdate,
  PersonaRun,
  ImapConfig,
  AuthImportParseResult,
  AuthImportSelection,
  AuthImportCommitResult,
} from '../types/api';

/**
 * Payload for `automationApi.createAISession` — mirrors the coordinator's
 * `StartAISessionRequest` (routers/ai_sessions.py). `available_data` carries
 * NON-secret hints in plaintext; `credentials` carries secret fill values that the
 * coordinator re-seals under the agent's channel key (never stored). Everything but
 * `goal` is optional.
 */
export interface AISessionStartInput {
  name?: string;
  goal: string;
  entry_url?: string;
  /** Non-secret hints (e.g. {"email": "me@x.com"}), sent in plaintext. */
  available_data?: Record<string, string>;
  /** Secret fill values, re-sealed under the agent channel key server-side. */
  credentials?: Record<string, string>;
  max_steps?: number;
  /** Optional persona already deployed to the agent. */
  persona_id?: number;
  /** Record + save a reusable workflow when the session finishes. */
  generate_workflow?: boolean;
  /** Explicit target agent; else an online agent is chosen. */
  agent_id?: string;
}

// Auth endpoints
export const authApi = {
  login: async (apiKey: string): Promise<ApiResponse<{ role: string }>> => {
    const response = await client.post('/auth/login', { apiKey });
    return response.data;
  },

  verify: async (): Promise<ApiResponse<{ role: string }>> => {
    const response = await client.get('/auth/verify');
    return response.data;
  },
};

// The Home dashboard is built entirely from local data (targets / workflows /
// triggers / tasks / runs) — see pages/Dashboard.tsx and api/homeHealth.ts.

// Agents endpoints
export const agentsApi = {
  getAll: async (): Promise<Agent[]> => {
    const response = await client.get('/agents');
    return response.data;
  },

  // Per-agent error history for a user-hosted agent.
  getErrors: async (agentId: string, days = 7): Promise<{
    agent_id: string;
    user_hosted: boolean;
    days: number;
    count: number;
    errors: Array<{
      source: 'check' | 'workflow' | 'streaming';
      agent_id: string;
      error_type: string;
      error_message: string;
      http_status: number | null;
      occurred_at: string | null;
      target_url: string | null;
      workflow_id: number | null;
      task_id: number | null;
    }>;
  }> => {
    const response = await client.get(`/agents/${agentId}/errors?days=${days}`);
    return response.data;
  },

  revoke: async (id: string): Promise<ApiResponse<void>> => {
    const response = await client.post(`/agents/${id}/revoke`);
    return response.data;
  },

  rotateSecret: async (id: string): Promise<ApiResponse<{ secret: string }>> => {
    const response = await client.post(`/agents/${id}/rotate-secret`);
    return response.data;
  },

  forceCheck: async (id: string): Promise<ApiResponse<void>> => {
    const response = await client.post(`/agents/${id}/force-check`);
    return response.data;
  },

  setWeight: async (id: string, weight: number): Promise<ApiResponse<void>> => {
    const response = await client.patch(`/agents/${id}/weight?weight=${weight}`);
    return response.data;
  },

  setCaptchaTrusted: async (id: string, trusted: boolean): Promise<ApiResponse<void>> => {
    const response = await client.patch(`/agents/${id}/captcha-trusted?trusted=${trusted}`);
    return response.data;
  },

  // Trust an agent to run sensitive/isolated workflows (trusted_agents_only=True).
  setTrusted: async (id: string, trusted: boolean): Promise<ApiResponse<void>> => {
    const response = await client.patch(`/agents/${id}/trusted?trusted=${trusted}`);
    return response.data;
  },
};

// Targets endpoints
// Monitor (target) mutations are reached directly (list toggle, bulk toggle,
// CheckDetailPage saveSetting) rather than through `useMutation({ invalidate })`,
// so each view refreshed only its OWN query and a pause/resume in one surface
// left the other showing the pre-mutation status until a hard reload. Invalidate
// the shared list + this monitor's detail keys in the API layer so every mounted
// view re-reads on the next tick (`invalidate` flags the entry stale → the shared
// `useQuery` refetches in the background without blanking the UI).
function invalidateMonitorLists() {
  useQueryCache.getState().invalidate(Q.targets(), Q.targets('content'), Q.targets('uptime'));
}
function invalidateMonitor(id: string | number) {
  invalidateMonitorLists();
  // Exact keys (not a substring match) so `target:5` never bleeds into `target:50`.
  useQueryCache.getState().invalidate(Q.target(id), Q.targetChanges(id));
}

/**
 * Mark every run-derived view stale so it background-revalidates immediately —
 * called both when a run is launched locally and on each realtime run-event.
 * Keeps the shown rows visible while the fresh feed swaps in (no blank flash).
 */
export function invalidateRunFeed(workflowId?: number) {
  const qc = useQueryCache.getState();
  qc.invalidate(Q.key('runs:recent'), Q.workflows(), Q.recentTasks());
  if (workflowId != null) {
    qc.invalidate(Q.workflowTasks(workflowId), Q.workflow(workflowId));
    qc.invalidateMatching(`workflow:${workflowId}:data`);
  }
}

/**
 * After a workflow run is dispatched, make it appear instantly: insert an
 * optimistic row (shown in Live activity / the runs feed until the server feed
 * catches up) and mark the run feed stale so mounted views revalidate now.
 */
function reflectLaunchedRun(workflowId: number, data: any) {
  try {
    const taskId = data?.task_id;
    if (taskId != null) {
      useLiveRuns.getState().addOptimistic(
        optimisticWorkflowRun(taskId, { workflowId, status: data?.status }),
      );
    }
  } catch {
    /* optimistic insert is best-effort */
  }
  invalidateRunFeed(workflowId);
}

export const targetsApi = {
  getAll: async (checkType?: 'content' | 'uptime'): Promise<Target[]> => {
    const params = checkType ? `?check_type=${checkType}` : '';
    const response = await client.get(`/targets${params}`);
    return response.data;
  },

  getById: async (id: string): Promise<Target> => {
    const response = await client.get(`/targets/${id}`);
    return response.data;
  },

  create: async (target: Partial<Target>): Promise<ApiResponse<Target>> => {
    const response = await client.post('/targets', target);
    invalidateMonitorLists();
    return response.data;
  },

  update: async (id: string, target: Partial<Target>): Promise<ApiResponse<Target>> => {
    const response = await client.patch(`/targets/${id}`, target);
    invalidateMonitor(id);
    return response.data;
  },

  delete: async (id: string): Promise<ApiResponse<void>> => {
    const response = await client.delete(`/targets/${id}`);
    invalidateMonitor(id);
    return response.data;
  },

  setPersona: async (id: string | number, personaId: number | null): Promise<{ persona_id: number | null; auth_seeded?: boolean; detail?: string }> => {
    const response = await client.put(`/targets/${id}/persona`, { persona_id: personaId });
    invalidateMonitor(id);
    return response.data;
  },

  toggle: async (id: string, enabled: boolean): Promise<ApiResponse<void>> => {
    const response = await client.patch(`/targets/${id}/toggle?enabled=${enabled}`);
    invalidateMonitor(id);
    return response.data;
  },

  getChanges: async (id: string): Promise<TargetChange[]> => {
    const response = await client.get(`/targets/${id}/changes`);
    return response.data;
  },

  /**
   * Newest-first detected content changes across ALL monitors,
   * enriched with the monitor URL + selector name + a truncated diff snippet.
   * Powers the Monitors-list "what changed" preview + the recently-changed group.
   */
  getRecentChanges: async (limit = 50): Promise<RecentChange[]> => {
    const response = await client.get(`/targets/changes/recent?limit=${limit}`);
    return Array.isArray(response.data) ? response.data : (response.data?.data ?? []);
  },

  getErrors: async (id: string): Promise<{
    target_id: number;
    target_url: string;
    count: number;
    errors: Array<{
      id: number;
      error_type: string;
      error_message: string;
      http_status: number | null;
      occurred_at: string | null;
    }>;
  }> => {
    const response = await client.get(`/targets/${id}/errors`);
    return response.data;
  },

  getPreview: async (id: string): Promise<any> => {
    const response = await client.get(`/targets/${id}/preview`);
    return response.data;
  },

  getNotifications: async (id: string): Promise<TargetNotificationsResponse> => {
    const response = await client.get(`/targets/${id}/notifications`);
    return response.data;
  },

  updateNotifications: async (
    id: string,
    payload: {
      recipient_ids: number[];
      notification_title?: string | null;
      notification_message?: string | null;
      notification_priority?: number | null;
      notification_sound?: string | null;
    }
  ): Promise<ApiResponse<void>> => {
    const response = await client.post(`/targets/${id}/notifications`, payload);
    return response.data;
  },
};

// Schedule endpoints
export const scheduleApi = {
  getConfig: async (): Promise<ScheduleConfig> => {
    const response = await client.get('/schedule');
    return response.data;
  },

  updateConfig: async (config: Partial<ScheduleConfig>): Promise<ApiResponse<ScheduleConfig>> => {
    const response = await client.post('/schedule/config', config);
    return response.data;
  },

  getDistribution: async (): Promise<TimeSlot[]> => {
    const response = await client.get('/schedule/distribution');
    return response.data.timeSlots || [];
  },

  rebalance: async (): Promise<ApiResponse<void>> => {
    const response = await client.post('/schedule/rebalance');
    return response.data;
  },

  redistribute: async (): Promise<ApiResponse<any>> => {
    const response = await client.post('/schedule/redistribute');
    return response.data;
  },

  setTimeSlotMode: async (mode: 'distributed' | 'rolling'): Promise<ApiResponse<any>> => {
    const response = await client.post('/schedule/time-slot-mode', null, {
      params: { mode }
    });
    return response.data;
  },
};

// API Keys endpoints
export const apiKeysApi = {
  getAll: async (): Promise<ApiKey[]> => {
    const response = await client.get('/auth/api-keys');
    return response.data;
  },

  /** The scope vocabulary the coordinator enforces — never hardcode it client-side. */
  getCatalog: async (): Promise<any> => {
    const response = await client.get('/auth/api-keys/catalog');
    return response.data;
  },

  create: async (data: {
    label: string;
    /** Scope strings; `resource:*` is expanded server-side at grant time. */
    scopes?: string[];
    /** Named preset — REPLACES `scopes` when present, never merges with it. */
    preset?: string;
    resource_ids?: Record<string, number[]>;
    expires_at?: string;
  }): Promise<any> => {
    const response = await client.post('/auth/api-keys', data);
    return response.data;
  },

  update: async (id: number, data: {
    label?: string;
    scopes?: string[];
    preset?: string;
    resource_ids?: Record<string, number[]>;
  }): Promise<any> => {
    const response = await client.patch(`/auth/api-keys/${id}`, data);
    return response.data;
  },

  revoke: async (id: number): Promise<ApiResponse<void>> => {
    const response = await client.delete(`/auth/api-keys/${id}`);
    return response.data;
  },
};

// Logs endpoints
export const logsApi = {
  getLogs: async (filter?: LogFilter, page = 1, pageSize = 50): Promise<PaginatedResponse<LogEntry>> => {
    const params = new URLSearchParams();
    params.append('page', page.toString());
    params.append('limit', pageSize.toString());

    if (filter?.level) params.append('level', filter.level);
    if (filter?.actor) params.append('actor', filter.actor);
    if (filter?.action) params.append('action', filter.action);
    if (filter?.search) params.append('search', filter.search);
    if (filter?.startDate) params.append('start_date', filter.startDate);
    if (filter?.endDate) params.append('end_date', filter.endDate);

    const response = await client.get(`/logs?${params.toString()}`);

    return {
      data: response.data.logs,
      total: response.data.total,
      page: response.data.page,
      pageSize: response.data.limit,
      totalPages: response.data.pages,
    };
  },

  exportLogs: async (filter?: LogFilter): Promise<Blob> => {
    const params = new URLSearchParams();

    if (filter?.level) params.append('level', filter.level);
    if (filter?.actor) params.append('actor', filter.actor);
    if (filter?.action) params.append('action', filter.action);
    if (filter?.search) params.append('search', filter.search);
    if (filter?.startDate) params.append('start_date', filter.startDate);
    if (filter?.endDate) params.append('end_date', filter.endDate);

    const response = await client.get(`/logs/export?${params.toString()}`, {
      responseType: 'blob',
    });

    return response.data;
  },

  tail: async (callback: (log: LogEntry) => void): Promise<() => void> => {
    // Implement polling-based tail (fetch latest logs every 2 seconds)
    let isActive = true;
    let lastId = 0;

    const poll = async () => {
      if (!isActive) return;

      try {
        const response = await client.get(`/logs?limit=10&page=1`);
        const logs = response.data.logs || [];

        // Only call callback for new logs
        logs.forEach((log: LogEntry) => {
          if (log.id > lastId) {
            callback(log);
            lastId = log.id;
          }
        });
      } catch (error) {
        console.error('Error polling logs:', error);
      }

      if (isActive) {
        setTimeout(poll, 2000); // Poll every 2 seconds
      }
    };

    poll();

    // Return cleanup function
    return () => {
      isActive = false;
    };
  },
};

// Notifications endpoints
export const notificationsApi = {
  getPushoverRecipients: async (): Promise<PushoverRecipient[]> => {
    const response = await client.get('/notifications/pushover/recipients');
    return response.data;
  },

  addPushoverRecipient: async (name: string, userKey: string): Promise<ApiResponse<any>> => {
    const response = await client.post('/notifications/pushover/recipients', { name, user_key: userKey });
    return response.data;
  },

  deletePushoverRecipient: async (id: number): Promise<ApiResponse<void>> => {
    const response = await client.delete(`/notifications/pushover/recipients/${id}`);
    return response.data;
  },

  togglePushoverRecipient: async (id: number): Promise<ApiResponse<any>> => {
    const response = await client.patch(`/notifications/pushover/recipients/${id}/toggle`);
    return response.data;
  },
};

// Automation endpoints
export const automationApi = {
  // Workflows
  listWorkflows: async (type?: string, search?: string): Promise<AutomationWorkflow[]> => {
    const params = new URLSearchParams();
    if (type) params.append('workflow_type', type);
    if (search) params.append('search', search);
    const response = await client.get(`/automation/workflows?${params}`);
    return response.data;
  },

  getWorkflow: async (id: number): Promise<AutomationWorkflow> => {
    const response = await client.get(`/automation/workflows/${id}`);
    return response.data;
  },

  createWorkflow: async (workflow: {
    name: string;
    description?: string;
    workflow_type: string;
    steps: WorkflowStep[];
    raw_replay?: Array<Record<string, any>>;
    form_data?: Record<string, string>;
    credentials?: Record<string, string>;
    entry_url?: string;
    timeout_ms?: number;
    retry_count?: number;
    headless?: boolean;
    fast_mode?: boolean;
    schedule_enabled?: boolean;
    schedule_interval_ms?: number | null;
    // Precise recurrence (interval | daily | weekly).
    schedule_kind?: 'interval' | 'daily' | 'weekly';
    schedule_time?: string | null;
    schedule_days?: number[] | null;
    schedule_tz?: string | null;
    ai_session_id?: number;  // Link to AI session that generated this workflow
    execution_target?: 'auto' | 'local' | 'cloud';
    functions?: Array<Record<string, any>>;  // Step-group / script / extraction functions
    default_persona_id?: number | null;  // Cloud persona (auth identity) attached by default
  }): Promise<AutomationWorkflow> => {
    const response = await client.post('/automation/workflows', workflow);
    return response.data;
  },

  updateWorkflow: async (id: number, workflow: Partial<{
    name: string;
    description: string;
    workflow_type: string;
    steps: WorkflowStep[];
    form_data: Record<string, string>;
    credentials: Record<string, string>;
    timeout_ms: number;
    retry_count: number;
    headless: boolean;
    is_active: boolean;
    schedule_enabled: boolean;
    schedule_interval_ms: number | null;
    // Precise recurrence (interval | daily | weekly).
    schedule_kind: 'interval' | 'daily' | 'weekly';
    schedule_time: string | null;
    schedule_days: number[] | null;
    schedule_tz: string | null;
    ai_repair_enabled: boolean;
    execution_target: 'auto' | 'local' | 'cloud';
    streaming_config: Record<string, any>;
    functions: Array<Record<string, any>>;  // Step-group / script / extraction functions
    default_persona_id: number | null;  // Cloud persona (auth identity) attached by default
    auth_config: Record<string, any> | null;  // Browserless HTTP-lane AuthRecipe
  }>): Promise<AutomationWorkflow> => {
    const response = await client.put(`/automation/workflows/${id}`, workflow);
    return response.data;
  },

  /** Browserless HTTP-lane session status: { has_session, session_persistence, expires_at?, is_expired? }. */
  getWorkflowSession: async (id: number): Promise<{
    workflow_id: number; has_session: boolean; session_persistence: boolean;
    expires_at?: string | null; last_used_at?: string | null; is_expired?: boolean;
  }> => {
    const response = await client.get(`/automation/workflows/${id}/session`);
    return response.data;
  },

  /** Clear the workflow's persisted HTTP-lane session so the next run logs in fresh. */
  clearWorkflowSession: async (id: number): Promise<void> => {
    await client.delete(`/automation/workflows/${id}/session`);
  },

  repairWorkflow: async (workflowId: number, taskId: number, functionName?: string): Promise<any> => {
    // Step workflows re-run with AI repair forced on; streaming workflows repair the
    // advanced_script (or, when functionName is given, that functions[] script entry).
    const qs = new URLSearchParams({ task_id: String(taskId) });
    if (functionName) qs.set('function_name', functionName);
    const response = await client.post(`/automation/workflows/${workflowId}/repair?${qs.toString()}`);
    return response.data;
  },

  createApiRecordedWorkflow: async (data: {
    name: string;
    description?: string;
    functions: Record<string, any>;
    credentials?: Record<string, string>;
    custom_path_prefix?: string;
    create_webhooks?: boolean;
  }): Promise<any> => {
    const response = await client.post('/automation/workflows/api-recorded', data);
    return response.data;
  },

  deleteWorkflow: async (id: number): Promise<void> => {
    await client.delete(`/automation/workflows/${id}`);
  },

  /**
   * 'Unpublish & remove' fast action, retained for interface compatibility.
   * There is no marketplace in this build, so a workflow can never have
   * marketplace dependents and the coordinator has no unpublish-and-remove route.
   * Its only caller sits behind a 409 branch that cannot occur here, so this is a
   * local no-op that never reaches the coordinator.
   */
  unpublishAndRemove: async (
    id: number,
  ): Promise<{ archived: boolean; listing_id: number | null; slug: string | null; workflow_id: number }> => {
    return { archived: false, listing_id: null, slug: null, workflow_id: id };
  },

  duplicateWorkflow: async (id: number): Promise<AutomationWorkflow> => {
    const response = await client.post(`/automation/workflows/${id}/duplicate`);
    return response.data;
  },

  clearCaptchaBlock: async (id: number): Promise<ApiResponse<void>> => {
    const response = await client.post(`/automation/workflows/${id}/clear-captcha-block`);
    return response.data;
  },

  // Tasks
  listTasks: async (params?: {
    target_id?: number;
    workflow_id?: number;
    status?: string;
    limit?: number;
    offset?: number;
    /** Omit heavy result_data/screenshots — use for list views. */
    summary?: boolean;
  }): Promise<AutomationTask[]> => {
    const searchParams = new URLSearchParams();
    if (params?.target_id) searchParams.append('target_id', params.target_id.toString());
    if (params?.workflow_id) searchParams.append('workflow_id', params.workflow_id.toString());
    if (params?.status) searchParams.append('status', params.status);
    if (params?.limit) searchParams.append('limit', params.limit.toString());
    if (params?.offset) searchParams.append('offset', params.offset.toString());
    if (params?.summary) searchParams.append('summary', 'true');
    const response = await client.get(`/automation/tasks?${searchParams}`);
    return response.data;
  },

  retryTask: async (id: number): Promise<AutomationTask> => {
    const response = await client.post(`/automation/tasks/${id}/retry`);
    return response.data;
  },

  cancelTask: async (id: number): Promise<void> => {
    await client.delete(`/automation/tasks/${id}`);
  },

  // Approve a held auto-buy (status awaiting_approval) → dispatches the real purchase.
  approveTask: async (id: number): Promise<AutomationTask> => {
    const response = await client.post(`/automation/tasks/${id}/approve`);
    return response.data;
  },

  // Reject a held auto-buy → no purchase is made.
  rejectTask: async (id: number): Promise<AutomationTask> => {
    const response = await client.post(`/automation/tasks/${id}/reject`);
    return response.data;
  },

  getTaskResults: async (id: number) => {
    const response = await client.get(`/automation/tasks/${id}/results`);
    return response.data;
  },

  runWorkflow: async (
    workflowId: number,
    executionTarget?: 'auto' | 'local' | 'cloud',
    personaId?: number | null,
    // Optional LAST arg — pin the run to a SPECIFIC user-hosted device. The
    // backend already accepts `agent_id` to pin a device (never body-asserts
    // identity beyond it). Kept optional/last so existing callers are unchanged.
    agentId?: string,
  ) => {
    const response = await client.post(`/automation/workflows/${workflowId}/run`, {
      execution_target: executionTarget || undefined,
      persona_id: personaId ?? undefined,
      agent_id: agentId || undefined,
    });
    reflectLaunchedRun(workflowId, response.data);
    return response.data;
  },

  runWorkflowWithData: async (
    workflowId: number,
    executionTarget?: 'auto' | 'local' | 'cloud',
    formData?: Record<string, string>,
    personaId?: number | null,
    // FILE ASSETS (§4.5/§7.3): { slot: file_id } bindings of the runner's OWN
    // stored files to the workflow's declared file slots. Each file_id is
    // ownership-checked server-side (resolve_for_run fail-closes 404).
    files?: Record<string, string>,
    // Optional LAST arg — pin the run to a SPECIFIC user-hosted device (see runWorkflow).
    agentId?: string,
  ) => {
    const response = await client.post(`/automation/workflows/${workflowId}/run`, {
      execution_target: executionTarget || undefined,
      form_data: formData || undefined,
      persona_id: personaId ?? undefined,
      files: files && Object.keys(files).length > 0 ? files : undefined,
      agent_id: agentId || undefined,
    });
    reflectLaunchedRun(workflowId, response.data);
    return response.data;
  },

  dispatchAndWait: async (workflowId: number, targetId?: number, timeoutSeconds?: number) => {
    const params = new URLSearchParams();
    if (timeoutSeconds) params.append('timeout_seconds', timeoutSeconds.toString());
    const response = await client.post(`/automation/dispatch-and-wait?${params}`, {
      workflow_id: workflowId,
      target_id: targetId,
    });
    return response.data;
  },

  // Target automation
  getTargetAutomation: async (targetId: number): Promise<TargetAutomation> => {
    const response = await client.get(`/automation/targets/${targetId}/automation`);
    return response.data;
  },

  updateTargetAutomation: async (targetId: number, config: {
    pre_check_workflow_id?: number | null;
    on_change_workflow_id?: number | null;
    on_change_enabled?: boolean;
    on_change_conditions?: {
      contains?: string;
      not_contains?: string;
      regex?: string;
    };
  }): Promise<TargetAutomation> => {
    const response = await client.post(`/automation/targets/${targetId}/automation`, config);
    return response.data;
  },

  removeTargetAutomation: async (targetId: number): Promise<void> => {
    await client.delete(`/automation/targets/${targetId}/automation`);
  },

  clearAuthSession: async (targetId: number): Promise<void> => {
    await client.delete(`/automation/targets/${targetId}/auth-session`);
  },

  // AI Autonomous Navigation
  createAINavigation: async (request: {
    url: string;
    goal: string;
    site_description?: string;
    available_data?: Record<string, string>;
    max_steps?: number;
    timeout_ms?: number;
  }): Promise<{ task_id: number; status: string; message: string }> => {
    const response = await client.post('/automation/ai-navigate', request);
    return response.data;
  },

  getAINavigationTask: async (taskId: number): Promise<{
    task_id: number;
    status: string;
    result?: {
      status: string;
      steps_executed: Array<{
        action: string;
        description?: string;
        selector?: string;
        value?: string;
        success?: boolean;
        error?: string;
      }>;
      form_data_used?: Record<string, string>;
      missing_data?: string[];
      extracted_data?: Record<string, any>;
      final_url?: string;
      message?: string;
    };
    error_message?: string;
    screenshots?: Array<{
      step: number;
      action: string;
      timestamp: string;
      data_base64: string;
    }>;
  }> => {
    // Get specific task by ID
    const response = await client.get(`/automation/tasks/${taskId}`);
    const task = response.data;

    // Handle different result_data structures:
    // - ai_navigation tasks: result_data.ai_navigation.steps_executed
    // - ai_session tasks: result_data.steps or result_data.result.steps_executed
    let result = task.result_data?.ai_navigation;
    if (!result && task.result_data) {
      // AI session format - convert to expected format
      const steps = task.result_data.steps || task.result_data.result?.steps_executed || [];
      result = {
        status: task.status === 'success' ? 'completed' : 'failed',
        steps_executed: steps.map((s: any) => ({
          action: s.type,
          description: s.description,
          selector: s.selector,
          value: s.value,
          success: true,
          // Include coordinates for vision-based steps
          x: s.coordinates?.x,
          y: s.coordinates?.y,
        })),
        form_data_used: task.result_data.ai_config?.available_data || {},
        message: task.result_data.message,
      };
    }

    return {
      task_id: task.id,
      status: task.status,
      result,
      error_message: task.error_message,
      screenshots: task.screenshots,
    };
  },

  // AI Workflow Sessions.
  //
  // The self-host coordinator is a DISPATCH PROXY for AI sessions: `POST
  // /ai-sessions/start` fires one autonomous session at a connected fleet agent
  // (built --features fleet,local) and returns immediately with a `running`
  // AiSession ROW (the agent runs the whole loop locally and reports its terminal
  // state later; the recorded workflow surfaces in the workflow list via
  // local_catalog). `GET /ai-sessions` + `GET /ai-sessions/{id}` list/read the rows.
  //
  // The returned row is the coordinator's lightweight `AiSession.to_dict()` shape
  // (session_id / agent_id / status / workflow_id / …), NOT the rich cloud
  // `AIWorkflowSession`, so these are typed loosely — the wizard only needs the row
  // id + status to toast and navigate.
  listAISessions: async (_params?: {
    status?: string;
    search?: string;
    limit?: number;
  }): Promise<any[]> => {
    const response = await client.get('/ai-sessions');
    return response.data?.sessions ?? [];
  },

  getAISession: async (id: number): Promise<any> => {
    const response = await client.get(`/ai-sessions/${id}`);
    return response.data;
  },

  /**
   * Launch an autonomous AI session on a connected fleet agent. Returns quickly
   * with the persisted `running` row (the agent runs the whole loop; its terminal
   * state lands later). On no online agent the coordinator replies 409 — surfaced
   * to the caller as a rejected promise so the wizard can show a clear toast.
   *
   * The payload matches the coordinator's `StartAISessionRequest` (goal + optional
   * entry_url / non-secret available_data / secret credentials / persona / budget /
   * generate_workflow), NOT the rich cloud `AISessionCreate`.
   */
  createAISession: async (session: AISessionStartInput): Promise<any> => {
    const response = await client.post('/ai-sessions/start', session);
    return response.data;
  },

  updateAISession: async (_id: number, _session: AISessionUpdate): Promise<AIWorkflowSession> => {
    throw new Error(i18n.t('AI sessions are not available in the self-host build.'));
  },

  deleteAISession: async (_id: number): Promise<void> => {
    /* no-op: nothing persisted to delete */
  },

  cancelAISession: async (_id: number): Promise<AIWorkflowSession> => {
    throw new Error(i18n.t('AI sessions are not available in the self-host build.'));
  },

  linkWorkflowToSession: async (_sessionId: number, _workflowId: number): Promise<AIWorkflowSession> => {
    throw new Error(i18n.t('AI sessions are not available in the self-host build.'));
  },

  unlinkWorkflowFromSession: async (_sessionId: number, _workflowId: number): Promise<AIWorkflowSession> => {
    throw new Error(i18n.t('AI sessions are not available in the self-host build.'));
  },

  runAISession: async (_sessionId: number, _formDataOverride?: Record<string, string>, _personaId?: number, _executionTarget?: string): Promise<{
    task_id: number;
    session_id: number;
    status: string;
    message: string;
  }> => {
    throw new Error(i18n.t('AI sessions are not available in the self-host build.'));
  },
};

// AI Sessions helper endpoints (see above).
//
// This returned a hardcoded `[]`, which left every AI-session picker in the flow
// builder permanently empty — the `ai_session_started` / `ai_session_completed`
// TRIGGER blocks filter on a session id, so they could be added but never scoped.
// Self-host does have sessions (`GET /ai-sessions`), so list them for real and
// degrade to `[]` only on error, the way the other reference loaders do.
//
// NOTE the shape difference: these rows are RUN RECORDS, not saved recipes. The
// `ai_session` ACTION block is goal-shaped and does NOT read this list.
export const aiSessionsApi = {
  listAll: async (): Promise<any[]> => {
    try {
      const response = await client.get('/ai-sessions');
      const rows = response.data;
      return Array.isArray(rows) ? rows : (rows?.sessions ?? []);
    } catch {
      return [];
    }
  },
};

// Target Selectors endpoints
export const selectorsApi = {
  listForTarget: async (targetId: number, enabledOnly: boolean = false): Promise<any[]> => {
    const response = await client.get(`/targets/${targetId}/selectors?enabled_only=${enabledOnly}`);
    return response.data;
  },

  get: async (targetId: number, selectorId: number): Promise<any> => {
    const response = await client.get(`/targets/${targetId}/selectors/${selectorId}`);
    return response.data;
  },

  create: async (targetId: number, selector: any): Promise<any> => {
    const response = await client.post(`/targets/${targetId}/selectors`, selector);
    return response.data;
  },

  update: async (targetId: number, selectorId: number, selector: any): Promise<any> => {
    const response = await client.put(`/targets/${targetId}/selectors/${selectorId}`, selector);
    return response.data;
  },

  delete: async (targetId: number, selectorId: number): Promise<void> => {
    await client.delete(`/targets/${targetId}/selectors/${selectorId}`);
  },

  toggle: async (targetId: number, selectorId: number): Promise<{ selector_id: number; enabled: boolean }> => {
    const response = await client.post(`/targets/${targetId}/selectors/${selectorId}/toggle`);
    return response.data;
  },

  test: async (targetId: number, selectorId: number): Promise<any> => {
    const response = await client.post(`/targets/${targetId}/selectors/${selectorId}/test`);
    return response.data;
  },

  setBaseline: async (targetId: number, selectorId: number): Promise<any> => {
    const response = await client.post(`/targets/${targetId}/selectors/${selectorId}/set-baseline`);
    return response.data;
  },

  clearBaseline: async (targetId: number, selectorId: number): Promise<void> => {
    await client.post(`/targets/${targetId}/selectors/${selectorId}/clear-baseline`);
  },

  setAllBaselines: async (targetId: number): Promise<any> => {
    const response = await client.post(`/targets/${targetId}/selectors/set-all-baselines`);
    return response.data;
  },
};

// Unified Trigger Rules API
export type FlowReferenceMap = {
  workflow: Record<string, { id: number; name: string }[]>;
  target: Record<string, { id: number; name: string }[]>;
  ai_session: Record<string, { id: number; name: string }[]>;
  webhook: Record<string, { id: number; name: string }[]>;
};

export const triggersApi = {
  listAll: async (params?: { enabled_only?: boolean; event_type?: string; workflow_id?: number }): Promise<any[]> => {
    const searchParams = new URLSearchParams();
    if (params?.enabled_only) searchParams.append('enabled_only', 'true');
    if (params?.event_type) searchParams.append('event_type', params.event_type);
    if (params?.workflow_id) searchParams.append('workflow_id', String(params.workflow_id));
    const response = await client.get(`/triggers/all?${searchParams}`);
    return response.data;
  },

  // Compact cross-reference map: which flows reference each entity. Replaces
  // fetching the full /triggers/all payload (with blocks JSONB) on list pages.
  references: async (): Promise<FlowReferenceMap> => {
    const response = await client.get('/triggers/references');
    return response.data;
  },

  listForTarget: async (targetId: number, enabledOnly: boolean = false): Promise<any[]> => {
    const response = await client.get(`/triggers/target/${targetId}?enabled_only=${enabledOnly}`);
    return response.data;
  },

  get: async (triggerId: number): Promise<any> => {
    const response = await client.get(`/triggers/${triggerId}`);
    return response.data;
  },

  create: async (trigger: {
    target_id?: number;
    target_selector_id?: number;
    name: string;
    description?: string;
    enabled?: boolean;
    priority?: number;
    conditions?: any;
    actions: Array<{ type: string; config: any }>;
    [key: string]: any;
  }): Promise<any> => {
    const response = await client.post('/triggers', trigger);
    return response.data;
  },

  update: async (triggerId: number, trigger: {
    name?: string;
    description?: string;
    target_selector_id?: number;
    enabled?: boolean;
    priority?: number;
    conditions?: any;
    actions?: Array<{ type: string; config: any }>;
  }): Promise<any> => {
    const response = await client.patch(`/triggers/${triggerId}`, trigger);
    return response.data;
  },

  delete: async (triggerId: number): Promise<void> => {
    await client.delete(`/triggers/${triggerId}`);
  },

  toggle: async (triggerId: number): Promise<any> => {
    const response = await client.patch(`/triggers/${triggerId}/toggle`);
    return response.data;
  },

  test: async (triggerId: number, testData: { test_content: string; test_extracted?: any }): Promise<any> => {
    const response = await client.post(`/triggers/${triggerId}/test`, testData);
    return response.data;
  },

  getExecutions: async (triggerId: number, limit: number = 50): Promise<any[]> => {
    const response = await client.get(`/triggers/${triggerId}/executions?limit=${limit}`);
    return response.data;
  },
};

// Selector Extractors API
export const extractorsApi = {
  listForSelector: async (selectorId: number, enabledOnly: boolean = false): Promise<any[]> => {
    const response = await client.get(`/extractors/selector/${selectorId}?enabled_only=${enabledOnly}`);
    return response.data;
  },

  get: async (extractorId: number): Promise<any> => {
    const response = await client.get(`/extractors/${extractorId}`);
    return response.data;
  },

  create: async (extractor: {
    target_selector_id: number;
    name: string;
    output_name: string;
    enabled?: boolean;
    extract_type: string;
    config?: any;
    is_array?: boolean;
    default_value?: string;
  }): Promise<any> => {
    const response = await client.post('/extractors', extractor);
    return response.data;
  },

  update: async (extractorId: number, extractor: {
    name?: string;
    output_name?: string;
    enabled?: boolean;
    extract_type?: string;
    config?: any;
    is_array?: boolean;
    default_value?: string;
  }): Promise<any> => {
    const response = await client.patch(`/extractors/${extractorId}`, extractor);
    return response.data;
  },

  delete: async (extractorId: number): Promise<void> => {
    await client.delete(`/extractors/${extractorId}`);
  },

  toggle: async (extractorId: number): Promise<any> => {
    const response = await client.patch(`/extractors/${extractorId}/toggle`);
    return response.data;
  },

  test: async (extractorId: number, testData: { content: string; content_type?: string }): Promise<any> => {
    const response = await client.post(`/extractors/${extractorId}/test`, testData);
    return response.data;
  },

  testContent: async (params: {
    content: string;
    content_type?: string;
    extract_type: string;
    config?: any;
    is_array?: boolean;
  }): Promise<any> => {
    const response = await client.post('/extractors/test-content', null, { params });
    return response.data;
  },
};

// Notification Recipients API (unified)
export const recipientsApi = {
  getAll: async (enabledOnly: boolean = false): Promise<Array<{
    id: number;
    provider: string;
    name: string;
    identifier_preview: string;
    enabled: boolean;
  }>> => {
    const response = await client.get(`/notifications/recipients/all?enabled_only=${enabledOnly}`);
    return response.data;
  },
};

// Personas API (authenticated identities with 2FA)
export const personasApi = {
  list: async (domain?: string): Promise<Persona[]> => {
    const params = new URLSearchParams();
    if (domain) params.append('domain', domain);
    const response = await client.get(`/personas?${params}`);
    return response.data;
  },
  get: async (id: number): Promise<Persona> => {
    const response = await client.get(`/personas/${id}`);
    return response.data;
  },
  create: async (data: PersonaCreate): Promise<Persona> => {
    const response = await client.post('/personas', data);
    return response.data;
  },
  update: async (id: number, data: PersonaUpdate): Promise<Persona> => {
    const response = await client.patch(`/personas/${id}`, data);
    return response.data;
  },
  delete: async (id: number): Promise<void> => {
    await client.delete(`/personas/${id}`);
  },
  test2fa: async (id: number): Promise<{ ok: boolean; method: string; kind?: string; message?: string }> => {
    const response = await client.post(`/personas/${id}/test-2fa`);
    return response.data;
  },
  // Run the persona's login workflow to establish (or refresh) its warm session.
  // Resolves with ok:false + a human-readable error for login FAILURES — only
  // transport/authorization problems reject, so callers render `error` inline.
  // Long-running by nature (a login with an email-OTP hop polls a mailbox), so the
  // client timeout is raised well past the coordinator's own 240s ceiling.
  signIn: async (id: number, force = false): Promise<PersonaSignInResult> => {
    const response = await client.post(`/personas/${id}/sign-in`, { force }, { timeout: 300_000 });
    return response.data;
  },
  runs: async (id: number, limit = 20): Promise<PersonaRun[]> => {
    const response = await client.get(`/personas/${id}/runs?limit=${limit}`);
    return response.data;
  },
  // Capture a workflow's login (creds + warm session + fingerprint) as a persona.
  createFromWorkflow: async (workflowId: number, name?: string, attachAsDefault = true): Promise<Persona> => {
    const response = await client.post('/personas/from-workflow', {
      workflow_id: workflowId, name, attach_as_default: attachAsDefault,
    });
    return response.data;
  },
  // Capture the identity a specific RUN produced (e.g. an account-creation run).
  createFromTask: async (taskId: number, name?: string): Promise<Persona> => {
    const response = await client.post('/personas/from-task', { task_id: taskId, name });
    return response.data;
  },
  // Capture the identity an AI session established (signed up / logged in).
  createFromAiSession: async (sessionId: number, name?: string): Promise<Persona> => {
    const response = await client.post('/personas/from-ai-session', { session_id: sessionId, name });
    return response.data;
  },
  // Email-OTP 2FA reads codes from a connected IMAP mailbox (BYO app password),
  // one mailbox per persona. Verified server-side before saving; stored encrypted.
  setImap: async (personaId: number, config: ImapConfig): Promise<Persona> => {
    const response = await client.post(`/personas/${personaId}/imap`, config);
    return response.data;
  },

  // --- Import EXISTING authenticator seeds (no per-account re-enrollment) ---
  // Parse otpauth:// URIs (QR/paste) and/or a Google Authenticator export into a
  // secret-free preview + a single-use token; commit only the chosen accounts.
  parseAuthenticatorImport: async (
    payload: { otpauth_uris?: string[]; migration_payload?: string },
  ): Promise<AuthImportParseResult> => {
    const response = await client.post('/personas/import/authenticator/parse', payload);
    return response.data;
  },
  commitAuthenticatorImport: async (
    importToken: string, selections: AuthImportSelection[],
  ): Promise<AuthImportCommitResult> => {
    const response = await client.post('/personas/import/authenticator/commit', {
      import_token: importToken, selections,
    });
    return response.data;
  },
  // Confirm a pasted TOTP seed is valid base32 and (optionally) reproduces the
  // code the user just used — for the recorder's "verified" check. Code never logged.
  validateTotpSeed: async (
    body: { totp_seed: string; code?: string; algorithm?: string; digits?: number; period?: number },
  ): Promise<{ valid_base32: boolean; matches_code: boolean | null }> => {
    const response = await client.post('/personas/validate-totp', body);
    return response.data;
  },
};

// Webhook Triggers API (incoming webhooks that trigger workflows)
export const webhookTriggersApi = {
  list: async (): Promise<any[]> => {
    const response = await client.get('/webhooks/triggers');
    // Backend returns {triggers: [...]}
    return response.data?.triggers || response.data || [];
  },

  listForTarget: async (targetId: number): Promise<any[]> => {
    const all = await webhookTriggersApi.list();
    return all.filter((t: any) => t.target_id === targetId);
  },

  create: async (data: {
    name: string;
    target_id?: number;
    workflow_id?: number;
    action: string;
    secret?: string;
    enabled?: boolean;
    payload_mapping?: Record<string, string>;
    conditions?: Record<string, any>;
  }): Promise<any> => {
    const response = await client.post('/webhooks/triggers', data);
    return response.data;
  },

  update: async (id: number, data: {
    name?: string;
    enabled?: boolean;
    secret?: string;
    workflow_id?: number;
    target_id?: number;
    action?: string;
    payload_mapping?: Record<string, string>;
    conditions?: Record<string, any>;
  }): Promise<any> => {
    const response = await client.put(`/webhooks/triggers/${id}`, data);
    return response.data;
  },

  delete: async (id: number): Promise<void> => {
    await client.delete(`/webhooks/triggers/${id}`);
  },

  getWebhookUrl: (token: string): string => {
    // Returns the URL that external systems should call (using secure token)
    const baseUrl = import.meta.env.VITE_API_URL || window.location.origin;
    return `${baseUrl}/api/webhooks/hook/${token}`;
  },

  // Legacy method for ID-based URL (deprecated)
  getWebhookUrlById: (triggerId: number): string => {
    const baseUrl = import.meta.env.VITE_API_URL || window.location.origin;
    return `${baseUrl}/api/webhooks/trigger/${triggerId}`;
  },
};


// ============================================================================
// Scraping API
// ============================================================================

export const scrapingApi = {
  listJobs: async (params?: { scrape_type?: string; is_active?: boolean }) => {
    const searchParams = new URLSearchParams();
    if (params?.scrape_type) searchParams.append('scrape_type', params.scrape_type);
    if (params?.is_active !== undefined) searchParams.append('is_active', String(params.is_active));
    const qs = searchParams.toString();
    const response = await client.get(`/scraping/jobs${qs ? '?' + qs : ''}`);
    return response.data;
  },

  getJob: async (id: number) => {
    const response = await client.get(`/scraping/jobs/${id}`);
    return response.data;
  },

  createJob: async (job: Record<string, any>) => {
    const response = await client.post('/scraping/jobs', job);
    return response.data;
  },

  updateJob: async (id: number, updates: Record<string, any>) => {
    const response = await client.patch(`/scraping/jobs/${id}`, updates);
    return response.data;
  },

  deleteJob: async (id: number) => {
    const response = await client.delete(`/scraping/jobs/${id}`);
    return response.data;
  },

  runJob: async (id: number) => {
    const response = await client.post(`/scraping/jobs/${id}/run`);
    return response.data;
  },

  getResults: async (jobId: number, limit = 50) => {
    const response = await client.get(`/scraping/jobs/${jobId}/results?limit=${limit}`);
    return response.data;
  },

  getLatestResult: async (jobId: number) => {
    const response = await client.get(`/scraping/jobs/${jobId}/results/latest`);
    return response.data;
  },
};

// User-hosted recorder agents
export const userRecorderApi = {
  getAgents: async () => {
    const response = await client.get('/user-recorder/agents');
    return response.data;
  },

  getCapability: async () => {
    const response = await client.get('/user-recorder/capability');
    return response.data;
  },

  renameAgent: async (agentId: string, name: string) => {
    const response = await client.patch(`/user-recorder/agents/${agentId}`, { name });
    return response.data;
  },

  disconnectAgent: async (agentId: string) => {
    const response = await client.delete(`/user-recorder/agents/${agentId}`);
    return response.data;
  },
};

// Secrets Vault API
export const vaultApi = {
  listSecrets: async (params?: { search?: string; category?: string }) => {
    const response = await client.get('/vault/secrets', { params });
    return response.data;
  },
  createSecret: async (data: { name: string; value?: string; username?: string; password?: string; card?: Record<string, string>; description?: string; category?: string }) => {
    const response = await client.post('/vault/secrets', data);
    return response.data;
  },
  updateSecret: async (id: number, data: { value?: string; username?: string; password?: string; card?: Record<string, string>; description?: string; category?: string }) => {
    const response = await client.patch(`/vault/secrets/${id}`, data);
    return response.data;
  },
  deleteSecret: async (id: number) => {
    await client.delete(`/vault/secrets/${id}`);
  },
  // Secrets live in the built-in vault; there are no external secret-store
  // providers (AWS/Vault/Azure/GCP) in this build.
};

// ── Streaming Mode ────────────────────────────────────────────────────────

export const streamingApi = {
  // Session lifecycle
  startSession: async (data: {
    workflow_id: number;
    target_url: string;
    max_duration_seconds?: number;
    headless?: boolean;
    execution_target?: string;
    // Inputs resolved against the session's bindings by the backend at start.
    form_data?: Record<string, unknown>;
  }): Promise<StreamingSession> => {
    const response = await client.post('/streaming/sessions/start', data);
    return response.data;
  },

  listSessions: async (status?: string, limit?: number): Promise<StreamingSession[]> => {
    const params = new URLSearchParams();
    if (status) params.append('status', status);
    if (limit) params.append('limit', String(limit));
    const response = await client.get(`/streaming/sessions?${params}`);
    return response.data;
  },

  getSession: async (sessionKey: string): Promise<StreamingSession> => {
    const response = await client.get(`/streaming/sessions/${sessionKey}`);
    return response.data;
  },

  endSession: async (sessionKey: string): Promise<{ status: string }> => {
    const response = await client.post(`/streaming/sessions/${sessionKey}/end`);
    return response.data;
  },

  // Handler invocation
  invokeHandler: async (
    sessionKey: string,
    handlerName: string,
    data: Record<string, any> = {},
    timeout?: number,
  ): Promise<{ session_key: string; handler: string; result: any }> => {
    const response = await client.post(
      `/streaming/sessions/${sessionKey}/invoke/${handlerName}`,
      { data, timeout: timeout || 30 },
    );
    return response.data;
  },

  // Dynamic handler management
  addHandler: async (sessionKey: string, handler: {
    name: string;
    type: string;
    code?: string;
    step_range?: number[];
    input_variables?: string[];
    extract_fields?: string[];
  }): Promise<{ status: string }> => {
    const response = await client.post(`/streaming/sessions/${sessionKey}/handlers`, handler);
    return response.data;
  },

  removeHandler: async (sessionKey: string, handlerName: string): Promise<{ status: string }> => {
    const response = await client.delete(`/streaming/sessions/${sessionKey}/handlers/${handlerName}`);
    return response.data;
  },

  // OpenAI-compatible
  getModels: async (sessionKey: string): Promise<{ data: Array<{ id: string }> }> => {
    const response = await client.get(`/streaming/sessions/${sessionKey}/v1/models`);
    return response.data;
  },
};

// ── MCP Endpoints ─────────────────────────────────────────────────────

export interface McpToolConfig {
  workflow_id: number;
  tool_name: string;
  tool_description: string;
  input_schema?: Record<string, any> | null;
  handler_name?: string | null;
  function_name?: string;
  auto_start: boolean;
  timeout_seconds: number;
  // For recorded workflows: linked automation webhook
  webhook_token?: string;
  webhook_url?: string;
  trigger_id?: number;
}

export interface McpEndpoint {
  id: number;
  name: string;
  slug: string;
  description?: string;
  tools_config: McpToolConfig[];
  api_key_id?: number | null;
  enabled: boolean;
  auto_start_sessions: boolean;
  server_version: string;
  connection_url: string;
  created_at?: string;
  updated_at?: string;
}

export const mcpApi = {
  list: async (): Promise<McpEndpoint[]> => {
    const response = await client.get('/mcp-endpoints');
    return response.data;
  },

  get: async (id: number): Promise<McpEndpoint> => {
    const response = await client.get(`/mcp-endpoints/${id}`);
    return response.data;
  },

  create: async (data: {
    name: string;
    slug: string;
    description?: string;
    tools_config: McpToolConfig[];
    api_key_id?: number | null;
    auto_start_sessions?: boolean;
  }): Promise<McpEndpoint> => {
    const response = await client.post('/mcp-endpoints', data);
    return response.data;
  },

  update: async (id: number, data: Partial<{
    name: string;
    description: string;
    tools_config: McpToolConfig[];
    api_key_id: number | null;
    auto_start_sessions: boolean;
    enabled: boolean;
  }>): Promise<McpEndpoint> => {
    const response = await client.patch(`/mcp-endpoints/${id}`, data);
    return response.data;
  },

  delete: async (id: number): Promise<void> => {
    await client.delete(`/mcp-endpoints/${id}`);
  },
};
