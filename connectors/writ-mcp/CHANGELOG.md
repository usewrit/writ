# Changelog

All notable changes to `writ-mcp` are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] — 2026-08-14

### Added
- **Saved crawls are callable, with freshness.** New relayed tools `writ_saved_crawls`,
  `writ_run_saved_crawl` and `writ_saved_crawl_data`, plus `save_as` on
  `writ_crawl_site`. A saved crawl is a stored crawl configuration with a stable slug,
  so `max_age` now answers "give me this site's pages unless what you have is older
  than N" instead of forcing a full re-crawl. Answers carry
  `_cache: {hit, age_seconds, source_crawl_id}`.
- **`max_age` on Writ Cloud.** The freshness argument previously worked only against a
  self-hosted coordinator and was silently ignored by Writ Cloud — the worst outcome
  for a connector meant to be interchangeable. Cloud now honours it on
  `writ_run_workflow` and on every generated per-workflow tool, with the same argument
  name, the same `_cache` stamp and the same success-only rule.

_No connector changes were needed for any of this: the bridge holds no tool
definitions, so server-side tools reach it for free._

## [1.0.0] — 2026-07-27

First public release on npm.

Development of this connector predates the release: it shipped bundled inside
self-host installs before it was ever published to the registry. The entries
below therefore cover everything in 1.0.0, including fixes made to code that was
only ever distributed that way. Nothing was published under an earlier version.

### Fixed — credential handling and hangs

- **A request the server never addressed left the client hanging forever.** The
  connector relayed whatever came back and stopped there, so a response carrying
  no answer for an id we sent — a gateway serving a cached body, a proxy that
  rewrites the payload, a server answering a batch with a single object — blocked
  that id permanently. On `initialize` this presents as an MCP server that simply
  never finishes starting. Every id the client is waiting on is now guaranteed a
  response; the server's own payload is still relayed untouched, so
  server-initiated messages keep working.
- **The API key is now trimmed, and rejected at startup if it cannot go in a
  header.** `WRIT_API_KEY=$(cat key.txt)` and pasting from the app both bring
  whitespace, and a newline made Node throw inside *every* request — surfacing as
  `Cannot reach <target> (Invalid character in header content)`, which blames the
  network for a credential that is one character off. A `Bearer …`-prefixed value
  is accepted without doubling the scheme.
- **Credentials embedded in `--url` are no longer echoed to stderr.**
  `https://user:pass@host/` is a legal URL, and MCP clients persist a server's
  stderr to a log file on disk, so the password was written down in plaintext. It
  is redacted from every diagnostic, and the connector now says the credentials
  were ignored (they were never sent — Writ authenticates with the key header).
- **An HTTP redirect is named instead of reported as `Empty response`.** A 301
  from a reverse proxy doing `http` → `https` is a routine setup and gave a
  message that explained none of it. Redirects are still deliberately not
  followed: resending the `Authorization` header to whatever origin `Location`
  names would leak the key.
- Documentation used a `wk_` API-key prefix that no Writ surface has ever issued;
  real keys are `wt_`.

### Added — relayed server capabilities

- Workflow tools now accept an optional `max_age` (seconds) so an agent can reuse a
  recent result instead of re-driving the browser. Omitted or `0` still always runs
  fresh. A reused answer reports `_cache: {hit, age_seconds}`.
- A timed-out tool call now answers `status: "running"` with `retryable: true` and
  says the run was not cancelled, instead of a dead-end "may still be executing".

Both are server-side capabilities relayed verbatim — this connector holds no tool
logic, so no client update is needed to pick them up.

### Changed

- The publish-tarball check moved into `scripts/verify-tarball.mjs`, called by CI,
  the release workflow and the monorepo's assembly script alike. It had been
  inlined in all three, which is how they came to disagree: the release workflow
  installs `npm@latest`, and **npm 12 changed `npm pack --json` from an array to
  an object keyed by package name** — so the check died there with an unrelated
  `TypeError` while CI stayed green on the runner's npm 11. It now accepts both
  shapes, says so plainly when it recognises neither, and CI runs it a second
  time under `npm@latest` so the next such change is caught before a release.
- The connector now lives in its own repository,
  [`usewrit/writ-mcp`](https://github.com/usewrit/writ-mcp), and is released from
  there. `repository`, `bugs` and `homepage` point at it. It continues to ship
  bundled inside a self-host install at `connectors/writ-mcp`.

### Added — the connector itself

- stdio ↔ Streamable-HTTP bridge for Writ Cloud, self-hosted coordinators, and
  published per-workflow `/mcp/<slug>` endpoints. Zero runtime dependencies.
- Session continuity: an `Mcp-Session-Id` pinned by the server at `initialize`
  and the negotiated `MCP-Protocol-Version` are echoed on every later request,
  so the connector is correct against any spec-compliant MCP server.
- SSE-framed responses are decoded, matching the `Accept` header the Streamable
  HTTP spec requires clients to send.
- Bounded retries with backoff for read-only methods, and for any method whose
  connection was refused outright. `tools/call` is never retried — a retry could
  re-run a workflow.
- Startup warnings for every way the API key can leak: disabled TLS verification,
  plaintext HTTP to a non-loopback host, and a key passed on the command line.
- 32 MB response cap, so a broken endpoint cannot exhaust memory.
- Test suite (`npm test`, `node:test`, no dev dependencies) driving the real
  binary over stdio against a mock server.

### Fixed — protocol correctness

- **JSON-RPC batches were silently dropped.** A batch is an array, and an array's
  `.id` is `undefined`, so batches were misread as notifications and their
  responses discarded — hanging the client on every id in the batch.
- **Failures on a batch left every id unanswered.** Errors now answer each
  outstanding id individually, so nothing waits forever.
- **A non-JSON-RPC JSON body was relayed verbatim.** A proxy error page or a
  framework's `{"detail": "…"}` on a 4xx/5xx parses as JSON but carries no id;
  relaying it left the caller hanging. Such bodies now become addressed
  JSON-RPC errors.
- `204 No Content` is accepted alongside `202` (mcp-service answers 204,
  the backend answers 202).
- `202`/`204` with an id still outstanding is reported instead of swallowed.
- Mistyped flags are named instead of being silently ignored and resurfacing as
  a confusing "missing API key".
- An invalid `--timeout` is rejected instead of silently falling back to the
  default.
- `--version` and `--help` print to stdout instead of stderr.
