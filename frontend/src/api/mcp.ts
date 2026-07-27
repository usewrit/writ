import client from './client';

// ── Types — GET /api/mcp/overview ────────────────────────────────────────────

export interface McpOverviewTool {
  tool_name: string;
  description: string | null;
  workflow_id: number;
  /** Null if the workflow was deleted or belongs elsewhere. */
  workflow_name: string | null;
}

export interface McpOverviewAuth {
  header: string;
  scheme: string;
  credential_type: string;
  /** Null when any active org API key is accepted. */
  required_api_key_id: number | null;
  instructions: string;
}

export interface McpOverviewServer {
  id: number;
  name: string;
  slug: string;
  description: string | null;
  enabled: boolean;
  connection_url: string;
  transport: string;
  auth: McpOverviewAuth;
  tools: McpOverviewTool[];
}

export interface McpOverview {
  servers: McpOverviewServer[];
  total: number;
}

// ── API ──────────────────────────────────────────────────────────────────────

export const mcpOverviewApi = {
  getOverview: async (): Promise<McpOverview> => {
    const response = await client.get('/mcp/overview');
    return response.data;
  },
};

// ── Types — GET /api/mcp/connect-info ─────────────────────────────────────────
// How an MCP client (Claude Code, Claude Desktop, Cursor, …) attaches to THIS
// coordinator's native /mcp server — the same operate tools the desktop daemon
// exposes. Powers the Connect page. No secrets: snippets carry a placeholder key.

export interface McpConnectNodeConnector {
  /** npm package name of the stdio bridge. */
  package: string;
  /** One-line `claude mcp add …` command. */
  claude_code: string;
  /** mcpServers JSON block for Claude Desktop. */
  claude_desktop: Record<string, unknown>;
  /** mcpServers JSON block for Cursor. */
  cursor: Record<string, unknown>;
  /** Env-var form (WRIT_COORDINATOR_URL / WRIT_API_KEY). */
  env: Record<string, string>;
}

export interface McpConnectInfo {
  /** Registration slug (e.g. "writ-selfhost") — distinct from the desktop app's "writ". */
  server_name: string;
  /** Human-facing name MCP clients show (e.g. "Writ Self-Host Coordinator"). */
  server_title?: string;
  /** Streamable-HTTP endpoint, e.g. https://host/mcp */
  endpoint: string;
  transport: string;
  auth: {
    scheme: string;
    credential: string;
    instructions: string;
  };
  node_connector: McpConnectNodeConnector;
  /** mcpServers JSON for clients that speak Streamable HTTP directly (no Node). */
  streamable_http: Record<string, unknown>;
  /** Static tool names the server exposes. */
  tools: string[];
  /** How this coexists with the official Writ desktop app's MCP. */
  coexists_with_desktop?: string;
}

export const mcpConnectApi = {
  getConnectInfo: async (): Promise<McpConnectInfo> => {
    const response = await client.get('/mcp/connect-info');
    return response.data;
  },
};
