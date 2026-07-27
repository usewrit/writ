import { mcpApi, triggersApi, webhookTriggersApi } from './endpoints';
import { buildExposeAsMcp } from '../utils/blockChainBuilder';
import type { McpEndpoint, McpToolConfig } from './endpoints';

/**
 * MCP publish / call-this helpers for the workflow detail "Connect" surface
 * (self-host build).
 *
 * The cloud managed-API (REST gateway / branded domains / consumer keys) is
 * gone — the coordinator serves REST directly at `/api/v1` with a scoped
 * `wlk_` key, so there is nothing to "publish". What remains is the MCP tool
 * exposure, which is built entirely on the KEPT mcp/trigger/webhook APIs.
 */

const baseOrigin = (): string =>
  typeof window !== 'undefined' ? window.location.origin : '';

// ── MCP tool ────────────────────────────────────────────────────────────────

export const deriveMcpSlug = (workflow: any): string =>
  workflow?.name
    ?.toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '') || `wf-${workflow?.id}`;

/** Tool names a workflow exposes over MCP (mirrors the MCP server modal). */
export const deriveToolNames = (workflow: any): string[] => {
  const names: string[] = [];
  for (const fn of workflow?.functions || []) {
    if (fn?.name && !names.includes(fn.name)) names.push(fn.name);
  }
  if (workflow?.workflow_type === 'streaming') {
    const sc = workflow.streaming_config || {};
    for (const h of sc.handlers || []) {
      if (h?.name && !names.includes(h.name)) names.push(h.name);
    }
    const chatName = (sc.openai_compat?.default_handler || '').trim() || 'chat';
    if (
      (sc.openai_compat?.enabled || sc.advanced_script?.enabled) &&
      !names.includes(chatName)
    ) {
      names.push(chatName);
    }
  }
  if (names.length === 0) names.push('run_workflow');
  return names;
};

const buildToolConfigs = (workflow: any): McpToolConfig[] => {
  const isStreaming = workflow?.workflow_type === 'streaming';
  const sc = workflow?.streaming_config || {};

  const fns: { name: string; description: string; type: string; inputs: string[] }[] = [];
  const seen = new Set<string>();

  for (const fn of workflow?.functions || []) {
    if (seen.has(fn.name)) continue;
    seen.add(fn.name);
    fns.push({
      name: fn.name,
      description: fn.description || fn.name,
      type: fn.type || 'steps',
      inputs: (fn.input_variables || []).map((v: any) =>
        typeof v === 'string' ? v : v.name,
      ),
    });
  }

  if (isStreaming) {
    for (const h of sc.handlers || []) {
      if (seen.has(h.name)) continue;
      seen.add(h.name);
      fns.push({
        name: h.name,
        description: `Handler: ${h.name}`,
        type: 'handler',
        inputs: (h.input_variables || []) as string[],
      });
    }
    const chatName = (sc.openai_compat?.default_handler || '').trim() || 'chat';
    if (
      (sc.openai_compat?.enabled || sc.advanced_script?.enabled) &&
      !seen.has(chatName)
    ) {
      fns.push({ name: chatName, description: 'Send a message to the AI chat session', type: 'chat', inputs: ['message'] });
    }
  } else if (fns.length === 0) {
    fns.push({
      name: 'run_workflow',
      description: workflow?.description || `Run ${workflow?.name}`,
      type: 'workflow',
      inputs: Object.keys(workflow?.form_data || {}),
    });
  }

  const buildSchema = (inputs: string[]): Record<string, any> | null => {
    if (inputs.length === 0) return null;
    const properties: Record<string, any> = {};
    for (const name of inputs) {
      properties[name] = { type: 'string', description: `Input: ${name}` };
    }
    return { type: 'object', properties, required: inputs };
  };

  return fns.map((fn) => ({
    workflow_id: workflow.id,
    tool_name: fn.name,
    tool_description: fn.description,
    input_schema: buildSchema(fn.inputs),
    function_name: fn.name,
    handler_name: fn.type === 'chat' ? undefined : fn.name,
    auto_start: false,
    timeout_seconds: 30,
  })) as McpToolConfig[];
};

export const mcpPublishApi = {
  list: mcpApi.list,

  /** Find the MCP endpoint exposing this workflow (if any). */
  findForWorkflow: (
    endpoints: McpEndpoint[] | null | undefined,
    workflowId: number,
  ): McpEndpoint | undefined =>
    (endpoints || []).find((ep) =>
      ep.tools_config?.some((tc) => tc.workflow_id === workflowId),
    ),

  /**
   * Expose a workflow as an MCP tool.
   * For non-streaming (recorded/api/ai) workflows we first auto-create the
   * linked webhook-trigger automation per tool so the MCP server can actually
   * run the workflow and wait for results — exactly as the MCP server modal does.
   */
  expose: async (workflow: any): Promise<McpEndpoint> => {
    const isStreaming = workflow.workflow_type === 'streaming';
    let tools = buildToolConfigs(workflow);

    if (!isStreaming) {
      tools = await Promise.all(
        tools.map(async (tool) => {
          try {
            const { payload } = buildExposeAsMcp(tool.workflow_id, tool.tool_name);
            const trigger = await triggersApi.create({
              name: `MCP: ${tool.tool_name} (${workflow.name})`,
              enabled: true,
              ...payload,
            });
            if (trigger?.webhook_trigger_token) {
              return {
                ...tool,
                webhook_token: trigger.webhook_trigger_token,
                webhook_url: webhookTriggersApi.getWebhookUrl(trigger.webhook_trigger_token),
                trigger_id: trigger.id,
              } as McpToolConfig;
            }
          } catch (e) {
            // Non-fatal: tool still registers, just without a linked runner.
            // eslint-disable-next-line no-console
            console.error(`Failed to create automation for tool ${tool.tool_name}:`, e);
          }
          return tool;
        }),
      );
    }

    return mcpApi.create({
      name: `${workflow.name} MCP`,
      slug: deriveMcpSlug(workflow),
      description: `MCP server for ${workflow.name}`,
      tools_config: tools,
      auto_start_sessions: false,
    });
  },

  /** Remove the MCP exposure. */
  remove: async (endpointId: number): Promise<void> => {
    await mcpApi.delete(endpointId);
  },
};

/** Build the MCP connection URL ({origin}/mcp/{slug}). */
export const buildMcpUrl = (slug: string): string => `${baseOrigin()}/mcp/${slug}`;
