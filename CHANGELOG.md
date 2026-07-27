# Changelog

All notable changes to the self-hosted Writ coordinator are documented in this
file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - Unreleased

Initial public release.

### Added

- **Coordinator** — FastAPI + a single SQLite file; no external database or
  cache services. Serves the built web UI and the REST API from one process.
- **Document + OCR extraction** — a `doc-extract` service ships alongside the
  coordinator and `docker compose up` starts it, so crawls read PDFs,
  Word/Excel/PowerPoint files, images and scanned pages instead of skipping
  them. CPU-only and fully offline: the OCR weights ship in the wheel, so it
  works air-gapped, and the service makes no outbound network calls at all.
  The coordinator hands its address and secret to each agent at connect time,
  so there is nothing to configure — and `GET /api/fleet/connect-info` reports
  whether the lane is live.
- **Fleet agent dispatch** — connect any number of `writ-agent-fleet` workers
  over WebSocket; mint and revoke connect tokens from the Fleet page.
- **Workflows** — record browser tasks once, replay them on the fleet, on a
  schedule, or exposed as REST endpoints.
- **Crawl (Harvest)** — distributed site crawls across the agent fleet.
- **Personas & vault** — stored credentials, cookies, and TOTP seeds in
  Fernet-encrypted columns keyed by `SECRET_ENCRYPTION_KEY`.
- **MCP connector** — the coordinator speaks the Model Context Protocol; the
  bundled `writ-mcp` bridge connects Claude Code, Claude Desktop, Cursor, and
  other MCP clients. Workflow tools accept `wait` / `timeout_seconds` (block for
  the result) and `max_age` (reuse a result younger than N seconds instead of
  re-driving the browser — omitted or `0` always runs fresh).
- **Choose how a run answers you** — `POST /api/automation/workflows/{id}/run`
  returns a `task_id` immediately by default; add `?wait=true` (and optionally
  `&timeout=120`) to block until the run finishes and get its result inline. A
  run that FAILS is reported with `200` and `status: "failed"`; exceeding the
  timeout answers `504` with the `task_id` still valid, so a slow run is
  collected rather than started a second time.
- **First-run onboarding** — create the single admin account in the browser on
  first visit, or bootstrap it from environment variables.
- **AGPL-3.0 §13 source offer, surfaced in the app** — a public
  `GET /api/about` reports the running version, the license, and the repository
  where its corresponding source lives; the login screen and
  **Settings → General** link it. Operators of a modified build point
  `WRIT_SOURCE_URL` at their own fork.
- **Documentation opens on the public site** — the coordinator embeds no docs.
  Every "read the docs" affordance links out to
  [usewrit.app/docs](https://usewrit.app/docs), so guidance can't go stale
  against your install.
