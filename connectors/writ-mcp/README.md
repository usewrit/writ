<div align="center">
  <img src="./assets/banner.svg" alt="writ-mcp — the official Writ MCP server connector" width="100%">

  <br/>

  <p align="center">
    <a href="https://github.com/usewrit/writ-mcp/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/usewrit/writ-mcp/ci.yml?branch=main&style=flat-square&label=CI" alt="CI"></a>
    <a href="https://www.npmjs.com/package/writ-mcp"><img src="https://img.shields.io/npm/v/writ-mcp?style=flat-square&color=FF4A24" alt="npm version"></a>
    <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-FF4A24?style=flat-square" alt="License: MIT"></a>
    <img src="https://img.shields.io/badge/node-18%2B-339933?style=flat-square&logo=node.js&logoColor=white" alt="Node 18+">
    <img src="https://img.shields.io/badge/dependencies-0-16a34a?style=flat-square" alt="Zero dependencies">
    <img src="https://img.shields.io/badge/provenance-signed-16a34a?style=flat-square" alt="Published with npm provenance">
    <img src="https://img.shields.io/badge/PRs-welcome-16a34a?style=flat-square" alt="PRs welcome">
  </p>

  <h3 align="center">Your saved browser workflows, as tools your assistant can call.</h3>

  <p align="center">
    <a href="#quick-start"><b>Quick start</b></a> ·
    <a href="#tools-you-get"><b>Tools</b></a> ·
    <a href="#configuration"><b>Configuration</b></a> ·
    <a href="#security"><b>Security</b></a> ·
    <a href="#troubleshooting"><b>Troubleshooting</b></a> ·
    <a href="./CONTRIBUTING.md"><b>Contributing</b></a>
  </p>
</div>

---

**writ-mcp** connects a stdio MCP client — Claude Code, Claude Desktop, Cursor,
Windsurf, Codex — to [Writ](https://github.com/usewrit/writ), so the browser
workflows you already recorded become tools your assistant can call: run them,
read the data they collected, search past results, schedule them, expose them as
REST endpoints, and kick off site crawls.

<div align="center">
  <img src="./assets/connect.svg" alt="Terminal: claude mcp add writ-selfhost, then claude mcp list reporting Connected" width="100%">
</div>

It is a **transparent stdio↔HTTP proxy and nothing else**. Every tool, schema and
rule lives server-side, so what you get always matches what your instance can do —
and a new tool never requires upgrading this package. Zero dependencies, one file,
Node core only.

> Clients that speak Streamable HTTP natively don't need this at all — point them
> straight at the `/mcp` endpoint with an `Authorization: Bearer <key>` header.

## Quick start

**1. Get an API key.** In the Writ app: **Settings → Developers → API keys**. Keys
look like `wt_…`.

**2. Add the server.**

Against a self-hosted coordinator:

```bash
claude mcp add writ-selfhost -e WRIT_API_KEY=<YOUR_API_KEY> -- npx -y writ-mcp --url https://writ.example.com
```

Against a coordinator on this machine:

```bash
claude mcp add writ-selfhost -e WRIT_API_KEY=<YOUR_API_KEY> -- npx -y writ-mcp --url http://localhost:8000
```

Against a published per-workflow endpoint (an "Expose as MCP" slug URL is used
verbatim — no path rewriting):

```bash
claude mcp add my-tools -e WRIT_API_KEY=<YOUR_API_KEY> -- npx -y writ-mcp --url https://mcp.example.com/mcp/my-tools
```

**3. Check it.**

```bash
claude mcp list
```

> **Writ Cloud is the default target** when you pass no `--url`
> (`https://api.usewrit.app`). Pass `--url` to point the connector at your own
> coordinator instead. See [Status](#status).

> **Pass the key through the environment, not the command line.** `--api-key`
> works, but it puts your key in the process's argument list where any local
> process can read it via `ps`, and your shell records it in history. The
> connector prints a note when you use it.

Your running coordinator also hands out these one-liners, pre-filled, on its
**Connect** page and at `GET /api/mcp/connect-info`:

<div align="center">
  <img src="./assets/media/connect.png" alt="The Connect page of a self-hosted Writ coordinator, showing the writ-mcp one-liner and the tools it exposes" width="100%">
</div>

### Claude Desktop / Cursor (config file)

Add to `claude_desktop_config.json` (Claude Desktop) or `~/.cursor/mcp.json`
(Cursor). The `env` form is recommended — it keeps the key out of the argument
list:

```json
{
  "mcpServers": {
    "writ-selfhost": {
      "command": "npx",
      "args": ["-y", "writ-mcp"],
      "env": {
        "WRIT_COORDINATOR_URL": "https://writ.example.com",
        "WRIT_API_KEY": "<YOUR_API_KEY>"
      }
    }
  }
}
```

### Without npm

A self-host install bundles this connector at `connectors/writ-mcp`. There is no
build step, so you can run it straight from disk:

```bash
node /path/to/writ/connectors/writ-mcp/index.js --url https://writ.example.com
```

> **It coexists with the other Writ servers.** Each surface registers under its
> own slug on purpose — the desktop app is `writ`, Writ Cloud is `writ-cloud`, a
> self-hosted coordinator is `writ-selfhost` — and each identifies itself to the
> assistant with a distinct title. Keep any combination connected at once.

## Tools you get

Served by the coordinator, not by this package:

| Tool | What it does |
|---|---|
| `writ_list_workflows` | Your saved workflows — plus a `run_<name>` tool per workflow |
| `writ_run_workflow` | Run one and wait for the extracted data |
| `writ_workflow_data` | Read a workflow's accumulated data table |
| `writ_search_data` | Search across everything already collected |
| `writ_export_data` | Export a workflow's data as CSV/JSON |
| `writ_workflow_runs` | Run history and status |
| `writ_set_schedule` | Schedule a workflow (interval / daily / weekly) |
| `writ_expose_workflow_api` | Publish a workflow as a callable REST endpoint |
| `writ_crawl_site` / `writ_crawl_status` | Start and poll a distributed site crawl |
| `writ_create_automation` | Event → run-workflow / notify chains |
| `writ_create_monitor` / `writ_wire_monitor` | Watch a page and react to changes |

Every target additionally exposes a **build** family — `writ_browser_use`,
`writ_record_website`, `writ_build`, `writ_website_to_api`, then
`writ_browser_act` / `_context` / `_network` / `_save` / `_cancel`. Your
assistant opens a real browser, drives it turn by turn, and saves the session as
a reusable workflow that afterwards replays with no model in the loop. Writ Cloud
runs it on Writ's fleet; a self-hosted coordinator runs it on your own [fleet
agent](https://github.com/usewrit/writ-agent). Either way your assistant is the
brain — no second model key is involved.

### Reusing a recent result (`max_age`)

Running a workflow drives a real browser, so asking the same question twice in one
session costs two full runs and two waits. Every workflow tool takes an optional
**`max_age`** (seconds) meaning *a recent answer is good enough*:

```jsonc
{ "name": "run_price_check", "arguments": { "sku": "B0C123", "max_age": 300 } }
```

- **omitted or `0`** — always run fresh (the default; nothing goes stale on its own).
- **`N`** — reuse a result younger than `N` seconds, otherwise run.

A reused answer carries `_cache: {hit: true, age_seconds: N}` so the assistant can
tell how current it is.

`max_age` works identically against Writ Cloud and a self-hosted coordinator, and on
a workflow's own generated tool as well as `writ_run_workflow`.

### Calling a saved crawl (`writ_run_saved_crawl`)

A whole-site crawl is slow and metered, so re-crawling to answer the same question is
the most expensive mistake an assistant can make. A **saved crawl** is a stored crawl
configuration with a stable name, and the same `max_age` contract applies to it:

```jsonc
{ "name": "writ_run_saved_crawl", "arguments": { "crawl": "docs", "max_age": 86400 } }
```

- **hit** — the pages that crawl already collected come back inline, instantly, with
  nothing crawled and nothing metered.
- **miss** — the site is crawled again with the *saved* settings, and you get a crawl
  id to poll with `writ_crawl_status` (a crawl outlives a single tool call).

Three tools cover the surface:

| Tool | What it does |
|------|--------------|
| `writ_saved_crawls` | List saved crawls. **Check here before crawling a site again.** |
| `writ_run_saved_crawl` | Run one, reusing recent data when `max_age` allows. |
| `writ_saved_crawl_data` | Read what one already collected, at any age. Never crawls. |

To create one, pass `save_as` to `writ_crawl_site` — that saves the settings *and*
runs them, so the crawl becomes callable by REST as well. Re-using the same `save_as`
updates that saved crawl instead of piling up duplicates. Saving needs an
`admin`-scoped credential (it creates reusable, callable configuration); running one
needs only `run`.

### If a tool call times out

It comes back as `status: "running"` with `retryable: true`. The run was **not**
cancelled — calling the tool again starts a *second* run. Wait, then retry with a
`max_age` wide enough to pick up the first run's result once it lands.

## Configuration

Flags take precedence over environment variables.

| Flag | Env | Default | Purpose |
|------|-----|---------|---------|
| `--url` | `WRIT_COORDINATOR_URL` / `WRIT_URL` | `https://api.usewrit.app` | Target base URL. A URL whose path is already `/mcp` or `/mcp/<slug>` is used verbatim. |
| `--api-key` | `WRIT_API_KEY` | — (**required**) | Key for `Authorization: Bearer`. Prefer the env var. |
| `--insecure` | `WRIT_INSECURE_TLS=1` | off | Accept a self-signed local-CA cert. Private networks only. |
| `--timeout` | `WRIT_MCP_TIMEOUT_MS` | `600000` | Per-request timeout (ms); covers long `writ_run_workflow` waits. |
| `--help` / `--version` | — | — | Print usage or version and exit. |

> **HTTPS with a local CA (self-host):** trust the CA (recommended) and use the
> `https://` address, or set `NODE_EXTRA_CA_CERTS=/path/to/ca.pem`. Use
> `--insecure` only for localhost testing — it disables certificate verification
> entirely, which exposes your key to a man-in-the-middle.

## Security

This process exists to carry a credential, so everything that could expose one is
made loud rather than convenient. Full detail in [`SECURITY.md`](./SECURITY.md).

- **The key goes to `--url` and nowhere else.** No telemetry, no analytics, no
  update check. **Zero dependencies** means there is no transitive code in the
  process that could phone home — and CI fails the build if that ever changes.
- **Redirects are never followed.** Replaying your `Authorization` header to
  whatever origin a `Location` header names would hand your key to a host you
  didn't choose. A 3xx becomes an error telling you to point `--url` at the final URL.
- **Credentials in a URL are redacted from every diagnostic.** MCP clients write a
  server's stderr to a log file on disk; `https://user:pass@host/` would otherwise
  be written down in plaintext.
- **Loud warnings, on stderr, for every way a key leaks:** TLS verification
  disabled, plaintext `http://` to a non-loopback host, or a key passed on the
  command line.
- **Retries never double-run a workflow.** Only read-only methods are retried;
  `tools/call` is sent exactly once, because a retry could re-execute a side
  effect the connector cannot see.
- **Responses are bounded** at 32 MB, so a broken endpoint can't grow this process
  until the OS kills your session.
- **No request is ever left unanswered** — a hung MCP client is a denial of
  service on your assistant, and that is the failure this connector works hardest
  to make impossible.

**Scope your keys.** Give a key only `workflows:read` / `workflows:execute` unless
a tool you actually use needs more.

### Verifying what you install

Releases are published from CI with [npm
provenance](https://docs.npmjs.com/generating-provenance-statements), so the
tarball is cryptographically linked to the commit and workflow that built it:

```bash
npm audit signatures
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Unauthorized: … rejected the API key` | The key is wrong, disabled, or lacks scope. Recreate it under **Settings → Developers** with `workflows:read` / `workflows:execute`. |
| `Cannot reach …` | Wrong `--url`, or the target is down. For a self-signed cert see the local-CA note above. |
| `… redirected (HTTP 301)` | Your reverse proxy redirects (usually `http` → `https`). Point `--url` at the final URL. |
| `The API key contains characters that cannot be sent in an HTTP header` | A newline or control character got into the key — usually a copy-paste artifact. Re-copy it. |
| **No tools listed** | You have no saved workflows yet, or the key can't read them. The static `writ_*` tools appear regardless. |
| **Client won't connect, no error** | Read the connector's stderr — your client logs it. Claude Code: `~/Library/Caches/claude-cli-nodejs/<project>/mcp-logs-<name>/`. |

## Status

| | |
|---|---|
| **Self-hosted coordinator** | Supported and verified end to end. |
| **Published `/mcp/<slug>` endpoints** | Supported. |
| **Writ Cloud** (`https://api.usewrit.app`, the no-`--url` default) | Supported. Used automatically when no `--url` is passed. |

## Development

```bash
npm test
```

No dev dependencies — the suite uses Node's built-in `node:test` and drives the
real `index.js` as a subprocess against a mock MCP server, exercising the same
stdio path an MCP client uses. It runs in about six seconds. `npm publish` runs it
automatically via `prepublishOnly`.

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) — note the two hard rules: **zero
dependencies, permanently**, and **no tool logic here**.

## The rest of Writ

| | |
|---|---|
| [**usewrit/writ**](https://github.com/usewrit/writ) | The self-host coordinator — web UI, API, your data. Start here. |
| [**usewrit/writ-agent**](https://github.com/usewrit/writ-agent) | The Rust fleet worker that does the actual browsing. |
| **writ-mcp** (this repo) | The MCP connector. |

## License

**MIT** — see [`LICENSE`](./LICENSE).

This package is deliberately permissive because it runs *inside your MCP client*,
not inside the coordinator, so it has to be embeddable anywhere. The coordinator
it talks to is **AGPL-3.0-only**; the two licenses are not interchangeable.
