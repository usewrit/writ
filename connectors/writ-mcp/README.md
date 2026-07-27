# writ-mcp

**The official Writ MCP server connector — for Writ Cloud and self-hosted coordinators.**

It bridges a stdio MCP client (Claude Code, Claude Desktop, Cursor, Windsurf,
Codex, …) to a Writ MCP endpoint, so your saved browser workflows become callable
tools — run them, read their accumulated data, search past results, schedule
them, expose them as REST endpoints, and kick off site crawls.

The connector holds **no tool logic** — it's a transparent stdio↔HTTP proxy.
Every tool lives server-side, so what you get here always matches what the app
can do. (Clients that speak Streamable HTTP directly don't need this at all —
point them straight at the `/mcp` endpoint with an `Authorization: Bearer` header.)

## Prerequisites

- An **API key**: in the Writ app, go to **Settings → Developers → API keys**
  and create one — on Writ Cloud, or in your own instance for self-host.
- For self-host, the **URL of your coordinator** (`--url`). Without it the
  connector targets Writ Cloud at `https://api.usewrit.app`.
- Node 18+ (`npx` fetches the connector for you). A self-host install also
  bundles it under `connectors/writ-mcp` — run it by local path
  (`node path/to/connectors/writ-mcp --api-key …`) if you'd rather not use npm.

## Add to Claude Code

**Writ Cloud** (the default target — no `--url` needed):

```bash
claude mcp add writ-cloud -- npx -y writ-mcp --api-key <YOUR_API_KEY>
```

**Self-hosted coordinator**:

```bash
claude mcp add writ-selfhost -- npx -y writ-mcp --url https://writ.example.com --api-key <YOUR_API_KEY>
```

**A coordinator running locally**:

```bash
claude mcp add writ-selfhost -- npx -y writ-mcp --url http://localhost:8000 --api-key <YOUR_API_KEY>
```

**A published per-workflow MCP endpoint** (an "Expose as MCP" slug URL is used
verbatim — no path rewriting):

```bash
claude mcp add my-tools -- npx -y writ-mcp --url https://mcp.usewrit.app/mcp/my-tools --api-key <YOUR_API_KEY>
```

> **Prefer the environment variable for the key.** `--api-key` puts your key in
> the process's argument list, where any local process can read it via `ps` and
> your shell records it in history. `WRIT_API_KEY` avoids both.

Restart / reconnect and the `writ_*` tools appear (plus one `run_<name>` tool
per saved workflow). A running server also hands out these one-liners live at
`GET /api/mcp/connect-info`.

> **Coexists with the other Writ servers.** Each surface registers under its own
> slug on purpose: the desktop app is `writ`, Writ Cloud is `writ-cloud`, and a
> self-hosted coordinator is `writ-selfhost`. Keep any combination connected —
> each server identifies itself to the AI with a distinct title so they are
> never confused.

## Add to Claude Desktop / Cursor (config file)

Add to `claude_desktop_config.json` (Claude Desktop) or `~/.cursor/mcp.json`
(Cursor):

Passing config through `env` is the recommended form — it keeps the API key out
of the process argument list:

```json
{
  "mcpServers": {
    "writ-selfhost": {
      "command": "npx",
      "args": ["-y", "writ-mcp"],
      "env": { "WRIT_COORDINATOR_URL": "https://writ.example.com", "WRIT_API_KEY": "<YOUR_API_KEY>" }
    }
  }
}
```

The equivalent with flags — drop `--url` to target Writ Cloud instead:

```json
{
  "mcpServers": {
    "writ-selfhost": {
      "command": "npx",
      "args": ["-y", "writ-mcp", "--url", "https://writ.example.com", "--api-key", "<YOUR_API_KEY>"]
    }
  }
}
```

## Configuration

Flags take precedence over environment variables.

| Flag | Env | Default | Purpose |
|------|-----|---------|---------|
| `--url` | `WRIT_COORDINATOR_URL` / `WRIT_URL` | `https://api.usewrit.app` | Target base URL. A URL whose path is already `/mcp` or `/mcp/<slug>` is used verbatim. |
| `--api-key` | `WRIT_API_KEY` | — (**required**) | API key for `Authorization: Bearer`. Prefer the env var — see above. |
| `--insecure` | `WRIT_INSECURE_TLS=1` | off | Accept a self-signed local-CA HTTPS cert (self-host on a trusted private network only). |
| `--timeout` | `WRIT_MCP_TIMEOUT_MS` | `600000` | Per-request timeout (ms); covers long `writ_run_workflow` waits. |

> **HTTPS / local CA (self-host):** if your coordinator serves HTTPS with its
> own local CA, either trust the CA (recommended) and point `--url` at the
> `https://` address, or set `NODE_EXTRA_CA_CERTS=/path/to/ca.pem`. Use
> `--insecure` only for localhost testing.

## Security

This process exists to carry a credential, so everything that could expose one is
made loud rather than convenient.

- **The key is sent to `--url` and nowhere else.** No telemetry, no analytics, no
  update check, no third-party host — zero dependencies means there is nothing
  else in the process that could phone home.
- **Warnings you should never ignore.** The connector prints a `WARNING` to
  stderr (surfaced in your MCP client's logs) when TLS verification is disabled
  (`--insecure`) or when the target is plaintext `http://` on a non-loopback
  host. Both mean the key is interceptable.
- **Prefer `WRIT_API_KEY`.** `--api-key` is readable by any local process via
  `ps`; the connector says so at startup when you use it.
- **Scope the key.** Give it only `workflows:read` / `workflows:execute` unless a
  tool you actually use needs more.
- **Retries never double-run a workflow.** Only read-only methods
  (`initialize`, `ping`, `tools/list`, …) are retried on a transient failure.
  `tools/call` is sent exactly once, because a retry could re-execute a side
  effect the connector cannot see. The one exception is a connection that was
  refused outright — the request provably never arrived.
- **Responses are bounded** at 32 MB, so a broken or hostile endpoint cannot grow
  this process until the OS kills your MCP session.

Report vulnerabilities via the repository's [`SECURITY.md`](https://github.com/usewrit/writ/blob/main/SECURITY.md).

### Verifying what you install

Releases are published from CI with [npm provenance](https://docs.npmjs.com/generating-provenance-statements),
so the tarball is cryptographically linked to the commit and workflow that built it:

```bash
npm audit signatures
```

## Tools exposed

On both targets:

- `writ_list_workflows` — your saved workflows + a `run_<name>` tool per workflow
- `writ_run_workflow` — run one and wait for the extracted data
- `writ_workflow_data` — read a workflow's accumulated data table
- `writ_search_data` — search across everything already collected
- `writ_export_data` — export a workflow's data as CSV/JSON
- `writ_workflow_runs` — run history / status
- `writ_set_schedule` — schedule a workflow (interval / daily / weekly)
- `writ_expose_workflow_api` — expose a workflow as a callable REST endpoint
- `writ_crawl_site` / `writ_crawl_status` — start / poll a distributed site crawl
- `writ_create_automation` — event → run-workflow / notify chains

Self-host additionally exposes `writ_browser_use` / `writ_record_*` — drive a
live browser on your own fleet agent and save the session as a reusable
workflow. On Writ Cloud, build new workflows in the Writ app; this server
operates what you saved.

### Reusing a recent result (`max_age`)

Every workflow tool takes an optional **`max_age`** (seconds). Running a workflow
drives a real browser, so asking the same question twice in one session costs two
full runs and two waits. Pass `max_age` to say a recent answer is good enough:

```jsonc
{ "name": "run_price_check", "arguments": { "sku": "B0C123", "max_age": 300 } }
```

- **omitted or `0`** — always runs fresh (the default; nothing gets staler on its own).
- **`N`** — reuse a result younger than `N` seconds, otherwise run.

A reused answer carries `_cache: {hit: true, age_seconds: N}` so you can tell how
current it is.

### If a tool times out

A tool call that outruns its timeout returns `status: "running"` with
`retryable: true`. The run was **not** cancelled — calling the tool again starts a
*second* run. Wait, then retry with a `max_age` wide enough to pick up the first
run's result once it lands.

## Troubleshooting

- **`Unauthorized`** — the API key is wrong, disabled, or lacks scope. Recreate it
  under Settings → Developers and ensure it has `workflows:read` / `workflows:execute`.
- **`Cannot reach …`** — check `--url` and that the target is up. For
  self-signed HTTPS, see the local-CA note above.
- **No tools listed** — you have no saved workflows yet, or the key can't read them.
  Save one in the Writ app; the static `writ_*` tools still appear regardless.

## Development

```bash
npm test
```

The suite has **no dev dependencies** — it uses `node:test` and drives the real
`index.js` as a subprocess against a mock MCP server, so it exercises the same
stdio path an MCP client uses. `npm publish` runs it automatically via
`prepublishOnly`.

## License

**MIT** — see [`LICENSE`](./LICENSE) in this directory. The connector is
deliberately zero-dependency (Node core `http`/`https` only).

This one directory is intentionally permissive: it runs *inside your MCP client*,
not inside the coordinator, so it must be embeddable anywhere. The coordinator
that ships alongside it is **AGPL-3.0-only** (see the repository's top-level
`LICENSE`) — the two licenses are not interchangeable.
