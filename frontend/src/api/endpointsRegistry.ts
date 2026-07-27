// ============================================================================
// Endpoints Registry — read-only observability aggregation helpers.
//
// The Developers "Endpoints" page is a READ-ONLY registry. Configuration lives
// on the workflow (Publish panel) or in the automation builder. This module
// only adds light typing + a client-side join to resolve which automation a
// given inbound webhook trigger belongs to. It reuses existing endpoints:
//   - managedEndpointsApi / consumerKeysApi / managedApiUsageApi (api/managedApis)
//   - mcpOverviewApi (api/mcp)
//   - webhookTriggersApi / triggersApi / automationApi (api/endpoints)
// No new backend routes are introduced.
// ============================================================================

import { webhookTriggersApi, triggersApi } from './endpoints';

// ── Inbound webhook trigger (shape of /api/webhooks/triggers items) ──────────
// Mirrors WebhookTrigger.to_dict() (backend models/webhook_trigger.py). Note
// the raw HMAC secret is NEVER returned by the API — only `has_secret`. The
// `token` IS returned and is the credential embedded in the webhook URL.
export interface WebhookTriggerSummary {
  id: number;
  token: string;
  name: string;
  enabled: boolean;
  has_secret: boolean;
  workflow_id: number | null;
  target_id: number | null;
  action: string;
  payload_mapping: Record<string, string> | null;
  conditions: Record<string, unknown> | null;
  wait_for_result: boolean;
  wait_timeout: number;
  custom_path: string | null;
  function_name: string | null;
  last_triggered_at: string | null;
  trigger_count: number | null;
  created_at: string | null;
  webhook_path: string; // /api/webhooks/hook/{token}
}

// A webhook trigger paired with the automation (TriggerRule) it fires, resolved
// client-side. `automationId` deep-links to /automations/:id (the builder).
export interface WebhookTriggerWithAutomation {
  trigger: WebhookTriggerSummary;
  automationId: number | null;
  automationName: string | null;
}

// Minimal block shape we read off a TriggerRule's `blocks` JSONB.
interface FlowBlockLike {
  type?: string;
  blockType?: string;
  config?: {
    webhook_trigger_token?: string;
    webhook_trigger_id?: number;
    [k: string]: unknown;
  };
}

interface TriggerRuleLike {
  id: number;
  name?: string | null;
  event_type?: string;
  blocks?: FlowBlockLike[] | null;
}

/**
 * Resolve every inbound webhook_received endpoint and the automation it fires.
 *
 * Linkage (per backend recon): an automation is a TriggerRule whose `blocks`
 * contains a `webhook_received` block. That block's config carries the
 * `webhook_trigger_token` / `webhook_trigger_id` produced when the builder
 * created the WebhookTrigger. We match on token first (stable), then id.
 *
 * Returns every WebhookTrigger (the source of truth for URL/secret/deliveries)
 * annotated with its owning automation when one can be found. Triggers with no
 * matching automation block are still listed (automationId = null) so nothing
 * is silently hidden.
 */
export async function listInboundWebhooks(): Promise<WebhookTriggerWithAutomation[]> {
  // Fetch both in parallel. If automations fail to load we degrade gracefully
  // and still show the triggers (without the automation deep-link).
  const triggersPromise = webhookTriggersApi.list() as Promise<WebhookTriggerSummary[]>;
  const automationsPromise = triggersApi
    .listAll({ event_type: 'webhook_received' })
    .catch(() => [] as TriggerRuleLike[]);

  const [triggers, automations] = await Promise.all([triggersPromise, automationsPromise]);

  // Build token/id → automation lookup by scanning each automation's blocks.
  const byToken = new Map<string, TriggerRuleLike>();
  const byId = new Map<number, TriggerRuleLike>();
  for (const auto of automations as TriggerRuleLike[]) {
    const blocks = Array.isArray(auto.blocks) ? auto.blocks : [];
    for (const block of blocks) {
      if (block?.blockType !== 'webhook_received') continue;
      const tok = block.config?.webhook_trigger_token;
      const wid = block.config?.webhook_trigger_id;
      if (tok && !byToken.has(tok)) byToken.set(tok, auto);
      if (typeof wid === 'number' && !byId.has(wid)) byId.set(wid, auto);
    }
  }

  return (triggers || []).map((trigger) => {
    const auto = byToken.get(trigger.token) ?? byId.get(trigger.id) ?? null;
    return {
      trigger,
      automationId: auto?.id ?? null,
      automationName: auto?.name ?? null,
    };
  });
}
