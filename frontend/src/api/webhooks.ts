import client from './client';

// ============================================================================
// Webhooks API — outgoing notification recipients + incoming triggers
// Backend: /api/webhooks/* (dict envelopes, org-scoped)
// ============================================================================

export interface WebhookRecipient {
  id: number;
  name: string;
  enabled: boolean;
  url: string;
  method: string;
  has_secret: boolean;
  headers: Record<string, string> | null;
  event_types: string[] | null;
  timeout: number | null;
  max_retries: number | null;
  include_diff: boolean;
  include_content: boolean;
  last_success_at: string | null;
  last_failure_at: string | null;
  success_count: number;
  failure_count: number;
  created_at: string | null;
}

export interface WebhookTrigger {
  id: number;
  token: string;
  name: string;
  enabled: boolean;
  has_secret: boolean;
  workflow_id: number | null;
  target_id: number | null;
  action: string;
  payload_mapping: Record<string, string> | null;
  conditions: Record<string, any> | null;
  wait_for_result: boolean;
  wait_timeout: number;
  custom_path: string | null;
  function_name: string | null;
  last_triggered_at: string | null;
  trigger_count: number;
  created_at: string | null;
  webhook_path: string;
}

export interface CreateRecipientInput {
  name: string;
  url: string;
  enabled?: boolean;
  method?: string;
  secret?: string;
  headers?: Record<string, string>;
  event_types?: string[];
  timeout?: number;
  max_retries?: number;
  include_diff?: boolean;
  include_content?: boolean;
}

export interface CreateTriggerInput {
  name: string;
  enabled?: boolean;
  secret?: string;
  workflow_id?: number;
  target_id?: number;
  action?: string;
  payload_mapping?: Record<string, string>;
  conditions?: Record<string, any>;
  wait_for_result?: boolean;
  wait_timeout?: number;
  custom_path?: string;
}

export const webhooksApi = {
  // --- Outgoing notification webhooks (recipients) ---

  listRecipients: async (): Promise<WebhookRecipient[]> => {
    const response = await client.get('/webhooks/recipients');
    return response.data?.recipients || [];
  },

  createRecipient: async (data: CreateRecipientInput): Promise<WebhookRecipient> => {
    const response = await client.post('/webhooks/recipients', data);
    return response.data?.recipient;
  },

  updateRecipient: async (id: number, data: Partial<CreateRecipientInput>): Promise<WebhookRecipient> => {
    const response = await client.put(`/webhooks/recipients/${id}`, data);
    return response.data?.recipient;
  },

  deleteRecipient: async (id: number): Promise<void> => {
    await client.delete(`/webhooks/recipients/${id}`);
  },

  testRecipient: async (id: number): Promise<any> => {
    const response = await client.post(`/webhooks/recipients/${id}/test`);
    return response.data;
  },

  // --- Incoming webhook triggers ---

  listTriggers: async (): Promise<WebhookTrigger[]> => {
    const response = await client.get('/webhooks/triggers');
    return response.data?.triggers || [];
  },

  createTrigger: async (data: CreateTriggerInput): Promise<WebhookTrigger> => {
    const response = await client.post('/webhooks/triggers', data);
    return response.data?.trigger;
  },

  updateTrigger: async (id: number, data: Partial<CreateTriggerInput>): Promise<WebhookTrigger> => {
    const response = await client.put(`/webhooks/triggers/${id}`, data);
    return response.data?.trigger;
  },

  deleteTrigger: async (id: number): Promise<void> => {
    await client.delete(`/webhooks/triggers/${id}`);
  },

  /** Absolute URL external systems should call for an incoming trigger. */
  getTriggerUrl: (webhookPath: string): string => {
    const baseUrl = import.meta.env.VITE_API_URL || window.location.origin;
    return `${baseUrl}${webhookPath}`;
  },
};
