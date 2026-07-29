import client from './client';

// Fleet — the coordinator's view of its connected agent fleet. Backs the
// /fleet page. Served by coordinator/routers/fleet.py (prefix "/api/fleet").

export interface FleetAgentCapacity {
  /** The EFFECTIVE cap the scheduler uses, resolved from the three inputs below. */
  max_sessions: number | null;
  active_sessions: number | null;
  free_slots: number | null;
  /**
   * What the machine says it can handle, from its heartbeat. Null until the
   * agent's first heartbeat. The stock writ-agent reports 2, which is why a
   * freshly enrolled agent looks capped at two sessions.
   */
  agent_reported?: number | null;
  /** The ceiling issued in this agent's token — the most it may claim for itself. */
  token_ceiling?: number | null;
  /** The operator's pin. Null = follow whatever the agent reports. */
  operator_override?: number | null;
  /** Upper bound accepted by the capacity endpoint. */
  limit?: number;
}

export interface FleetAgent {
  id: string;
  name: string;
  platform: string;
  online: boolean;
  last_seen: string | null;
  capacity: FleetAgentCapacity;
  status: string | null;
  is_trusted: boolean;
  /** When this agent's durable row was first created (ISO). */
  created_at?: string | null;
  /**
   * Whether this agent can host local workflows (advertises the
   * `local_workflows=1` connect param, captured into the coordinator's
   * `_agent_meta`). Added by the P1/coordinator layer; may be undefined until
   * that lands, so treat an ABSENT value as capable (see `local_capable !== false`
   * filtering in SendToAgentModal).
   */
  local_capable?: boolean;
}

export interface FleetAgentsResponse {
  agents: FleetAgent[];
  online_count: number;
  total: number;
}

export interface AgentInstallCommands {
  /** macOS/Linux: resolve the release asset for this platform, download, chmod. */
  unix: string;
  /** Windows PowerShell equivalent. */
  windows: string;
  /** git clone + cargo build, for platforms with no published asset. */
  source: string;
}

export interface FleetConnectInfo {
  ws_url: string | null;
  public_url: string | null;
  github_url: string;
  /** `owner/name` of the agent source repo. */
  repo: string;
  repo_url: string;
  install_commands: AgentInstallCommands;
  docker_image: string;
  saas_url: string | null;
}

/** GET/POST /api/fleet/local-agent — an agent run on the coordinator's own host. */
export interface LocalAgentStatus {
  /**
   * False when this host can't run one: no published build, a containerised
   * coordinator, a missing installer dependency, or Windows (the installer is a
   * POSIX shell script — `blockers` points at the PowerShell route instead).
   */
  supported: boolean;
  blockers: string[];
  /** Release-asset infix for this host, e.g. `macos-arm64`. Null when unsupported. */
  target: string | null;
  platform: string;
  running: boolean;
  pid: number | null;
  agent_name: string | null;
  binary_installed: boolean;
  installed_version: string | null;
  log_path: string;
  /** Present on POST: whether an already-running agent was adopted. */
  adopted?: boolean;
  status?: 'started' | 'already_running';
  /** True only when the release published a checksum that we verified. */
  checksum_verified?: boolean;
}

export interface FleetTokenMeta {
  token_id: string;
  name: string;
  agent_id: string;
  token_prefix: string;
  created_at: string;
  revoked_at: string | null;
}

export interface FleetCapacityPreset {
  interval_ms: number;
  agents_required: number;
  feasible: boolean;
}

/** Advisory of how the connected-agent count bounds the min check interval. */
export interface FleetCapacity {
  agents_online: number;
  per_agent_floor_ms: number;
  /** Fastest interval a single monitor can effectively run at (= 60000/agents). */
  min_interval_ms: number;
  active_monitors: number;
  under_provisioned_monitors: number;
  presets: FleetCapacityPreset[];
  fleet_stats: Record<string, unknown> | null;
  explanation: string;
}

export interface PairCode {
  /** Human-facing, e.g. `WRIT-4K2P-9XQ`. Single use, expires. */
  code: string;
  /** Seconds until it stops working. */
  expires_in: number;
  /** The whole thing to copy: `curl -fsSL <base>/agent.sh | sh -s -- <code>`. */
  install_command: string;
  /** The manual fallbacks, from the SAME mint — never mint a second token
   *  just to fill the Binary/Docker tabs. */
  token: string;
  agent_id: string;
  connect_command: string;
  docker_command: string;
  install_commands: AgentInstallCommands;
}

export interface MintedFleetToken {
  token_id: string;
  name: string;
  agent_id: string;
  /** Raw token — returned ONCE at mint time. */
  token: string;
  channel_key: string | null;
  /**
   * Runnable invocation for the `writ-agent-fleet` binary the installer above
   * produces. ONE line: that binary is configured ENTIRELY BY ENVIRONMENT —
   *   WRIT_SERVICE_TOKEN=<raw> WRIT_COORDINATOR_URL=<coordinator> ~/.writ/writ-agent-fleet
   * with WRIT_FLEET_ALLOW_INSECURE=1 added only for a plaintext, non-loopback
   * coordinator. There is no `--token` flag, and `config set` / `start
   * --headless` belong to the DESKTOP `writ-agent` binary, which is a different
   * program — issuing them against this one is why the pasted command used to be
   * unrunnable.
   */
  connect_command: string;
  /** Docker variant using the published image's WRIT_SERVICE_TOKEN env. */
  docker_command: string;
  /** How to GET the binary in the first place — the connect command assumes it. */
  install_commands: AgentInstallCommands;
  repo_url: string;
  created_at: string;
}

// ---- Send-to-agent (deploy) ------------------------------------------------
// A workflow / secret / persona is sealed under the target agent's channel key
// and pushed over the persistent WS. `mode` decides whether the coordinator
// keeps its copy (mirror) or transfers it fully (move).

export interface DeployRequest {
  kind: 'workflow' | 'secret' | 'persona';
  id: number;
  /** workflow only: bundle its referenced secrets + persona. */
  include_deps?: boolean;
  mode?: 'mirror' | 'move';
}

export interface DeployResponse {
  local_id: string;
  mode: 'mirror' | 'move';
}

/** Lightweight preview of what a workflow deploy would carry along. */
export interface WorkflowDepsPreview {
  secrets: string[];
  persona: string | null;
}

export const fleetApi = {
  async listAgents(): Promise<FleetAgentsResponse> {
    const r = await client.get('/fleet/agents');
    return r.data;
  },
  /**
   * Send a workflow/secret/persona to a connected fleet agent. Backed by the
   * coordinator's `POST /api/fleet/agents/{agent_id}/deploy` (P1 layer) — will
   * 404 until that endpoint lands.
   */
  async deploy(agentId: string, body: DeployRequest): Promise<DeployResponse> {
    const r = await client.post(`/fleet/agents/${agentId}/deploy`, body);
    return r.data;
  },
  /**
   * Preview a workflow's referenced secrets + persona. Backed by the
   * lightweight `GET /api/automation/workflows/{id}/deps` (P1 layer). Callers
   * should try/catch so a 404 (before P1 lands) degrades to "no preview".
   */
  async workflowDeps(workflowId: number): Promise<WorkflowDepsPreview> {
    const r = await client.get(`/automation/workflows/${workflowId}/deps`);
    // Normalize to the promised shape — some backends omit `secrets` (or send it
    // null) when a workflow references none, and the raw body would then break
    // every `secrets.length`/`.map` consumer. Make the contract honest here.
    return {
      secrets: Array.isArray(r.data?.secrets) ? r.data.secrets : [],
      persona: r.data?.persona ?? null,
    };
  },
  async connectInfo(): Promise<FleetConnectInfo> {
    const r = await client.get('/fleet/connect-info');
    return r.data;
  },
  async listTokens(): Promise<FleetTokenMeta[]> {
    const r = await client.get('/fleet/tokens');
    return r.data?.tokens ?? [];
  },
  async mintToken(name: string): Promise<MintedFleetToken> {
    const r = await client.post('/fleet/tokens', { name });
    return r.data;
  },

  /**
   * Mint a short single-use pairing code for one agent.
   *
   * This is the path the UI should lead with. The alternative — a raw fleet
   * token pasted into a ~450-character `docker run` carrying the coordinator
   * URL, the document-extractor address and its secret — exists because a
   * containerised coordinator cannot launch an agent on the host itself. The
   * code moves all of that server-side: the installer fetches it by exchanging
   * the code, so the operator handles one short line.
   *
   * `name` is the operator's label for the machine. The agent never learns it —
   * nothing on the wire carries a name — so the coordinator keeps it against the
   * minted token and resolves it back when listing the fleet. Omit it and the
   * agent lists under its own id rather than a generated placeholder.
   */
  async mintPairCode(name?: string): Promise<PairCode> {
    const r = await client.post('/fleet/pair-code', name ? { name } : {});
    return r.data;
  },
  /** Can this coordinator run an agent on its own host, and is one running? */
  async localAgentStatus(): Promise<LocalAgentStatus> {
    const r = await client.get('/fleet/local-agent');
    return r.data;
  },
  /**
   * Mint a token, download the agent for this host, configure and launch it.
   * The token never reaches the browser — nothing has to be pasted anywhere.
   */
  async startLocalAgent(name: string): Promise<LocalAgentStatus> {
    const r = await client.post('/fleet/local-agent', { name });
    return r.data;
  },
  async stopLocalAgent(): Promise<{ stopped: boolean }> {
    const r = await client.delete('/fleet/local-agent');
    return r.data;
  },
  async capacity(): Promise<FleetCapacity> {
    const r = await client.get('/fleet/capacity');
    return r.data;
  },
  async revokeToken(tokenId: string): Promise<void> {
    await client.delete(`/fleet/tokens/${tokenId}`);
  },
  /**
   * Remove a single agent from the fleet. Durable: revokes any bound fleet
   * token, evicts the live socket, drops Redis state, and deletes the DB row.
   * Backed by `DELETE /api/fleet/agents/{agent_id}`.
   */
  async removeAgent(agentId: string): Promise<void> {
    await client.delete(`/fleet/agents/${agentId}`);
  },
  /**
   * Pin how many concurrent sessions this agent may be given, or pass null to
   * clear the pin and follow the agent's own report again.
   *
   * Applies live when the agent is connected; when it is offline the override is
   * persisted on its row and takes effect on the next connect (`applied` says
   * which happened).
   */
  async setAgentCapacity(
    agentId: string,
    maxSessions: number | null,
  ): Promise<{ agent_id: string; capacity: FleetAgentCapacity; applied: 'live' | 'on_next_connect' }> {
    const r = await client.patch(`/fleet/agents/${agentId}/capacity`, { max_sessions: maxSessions });
    return r.data;
  },
  /**
   * Bulk-remove agents in one call (the ids the operator selected, or every
   * offline machine). Backed by `POST /api/fleet/agents/prune`.
   */
  async pruneAgents(agentIds: string[]): Promise<{ removed: number; requested: number }> {
    const r = await client.post('/fleet/agents/prune', { agent_ids: agentIds });
    return r.data;
  },
};
