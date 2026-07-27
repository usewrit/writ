# Changelog

All notable changes to `writ-mcp` are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Workflow tools now accept an optional `max_age` (seconds) so an agent can reuse a
  recent result instead of re-driving the browser. Omitted or `0` still always runs
  fresh. A reused answer reports `_cache: {hit, age_seconds}`.
- A timed-out tool call now answers `status: "running"` with `retryable: true` and
  says the run was not cancelled, instead of a dead-end "may still be executing".

Both are server-side capabilities relayed verbatim — this connector holds no tool
logic, so no client update is needed to pick them up.

## [1.0.0] — 2026-07-25

First public release on npm.

### Added

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

### Fixed

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
