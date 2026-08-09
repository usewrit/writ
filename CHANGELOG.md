# Changelog

All notable changes to the self-hosted Writ coordinator are documented in this
file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.1] - 2026-08-09

### Fixed

- **The onboarding tour's buttons could sit outside its own card.** The tour
  panel was a fixed 312px, and its footer laid out a per-step progress dot beside
  Skip / Back / Next. Dots don't shrink — twelve of them hold about 44px of the
  row open no matter what — so the primary button was pushed roughly 35px past
  the card's edge in English, and considerably further in French, where
  "Ignorer / Retour / Suivant" runs about half again as wide as
  "Skip / Back / Next". A first-time operator could reach the last step of
  onboarding and not be able to click **Done**.

  The dot strip is now a single progress track that flexes down to nothing, the
  three actions travel together as one non-shrinking group, and the panel is
  340px — sized for the longest language rather than for English. The track
  carries `role="progressbar"` with the step number, so the progress is now
  announced rather than being decorative markup a screen reader skipped.

- **Two invisible elements caused by an invalid utility class.** The tour's
  progress dots and the automation builder's loading indicator both asked for
  `bg-border-border-strong`, which is not a class — the token is `border-strong`,
  so the correct utility is `bg-border-strong`. Tailwind emits nothing for a
  class it cannot resolve, so both elements rendered with no background at all
  and simply were not visible. No warning is produced for this at build time,
  which is why it survived: the only way to catch it is to build and diff the
  emitted CSS.

## [1.0.0] - 2026-08-06

Initial public release, tagged `v1.0.0`.

The version matches [`writ-agent`](https://github.com/usewrit/writ-agent) v1.0.0
and [`writ-mcp`](https://www.npmjs.com/package/writ-mcp) 1.0.0: the coordinator,
the agent it drives and the connector that talks to it are one product and are
versioned together, so "which agent goes with which coordinator" is never a
question you have to research.

Every release is gated by `scripts/release-e2e.sh`, which brings the stack up
with Docker Compose, creates the owner, installs the *published* agent release
through the same one-liner the UI prints, records a workflow on a real browser,
replays it, and calls it over both REST and MCP. A tag is not cut until that
passes.

### Added

- **Custom-path webhook URLs now work.** `WebhookTrigger.custom_path` had always
  documented `POST /api/v1/webhooks/{custom_path}` and the API-recorder minted one path
  per recorded function — but **no route served it**, so every custom-path URL this
  coordinator produced 404'd. Three defects stacked in one path:

  1. the route did not exist (now added, self-prefixed alongside the other `/api/v1`
     routers);
  2. the recorder created those triggers with **no signing secret**, so they were also
     refused on `/api/webhooks/hook/{token}` — `_process_webhook` fails closed on a
     secret-less trigger — meaning the endpoints it reported as created were callable by
     no means at all (it now mints a secret like every other trigger-creating path);
  3. the list of created endpoints was assembled and then **discarded**, so the caller
     was never told they existed (now returned as an additive `endpoints` field carrying
     the full callable URL).

  Credential model: a custom path is human-chosen and therefore guessable, so unlike the
  unguessable token it cannot be its own credential. The route requires an API key with
  `triggers:execute` — the same scope as its sibling `POST /api/webhooks/trigger/{id}` —
  re-checks that the key is scoped to the target workflow, and enforces the key's run
  budgets exactly as the direct run endpoint does. An HMAC signature is optional there
  (the key is the authentication) but is still verified when presented. The
  unauthenticated token route is unchanged and still demands one.

  Also: `custom_path` validation accepted only a single segment (max 64 chars) while the
  column is 100 and the recorder writes `{prefix}/{function}` — so a user could not
  create, or repair, the paths the coordinator mints itself. Widened to slash-joined
  segments with traversal shapes (`..`, `.`, empty segments, edge slashes) still refused,
  and `custom_webhook_path` is now returned from the trigger API so the URL is
  discoverable.
- **Saved crawls — call a crawl like a workflow, and reuse its data.** A crawl row is
  one RUN whose id dies with that run, so a crawl had no stable handle to expose as an
  API. New `crawl_definitions` (migration `0014_crawl_definitions`) stores the
  configuration under a slug; `crawl_jobs.definition_id` makes runs its history.

  `POST /api/crawl/definitions/{ref}/run` takes **`max_age`** — a freshness contract,
  not a cache flag: within the window the pages that crawl already collected come back
  with nothing crawled, otherwise the saved settings are re-crawled. Accepts
  `Cache-Control: max-age=N`, `?max_age=N`, or a body field, and stamps every answer
  with `_cache.hit` / `_cache.age_seconds`. Only a `completed` run with at least one
  fetched page qualifies, so a fully-blocked host cannot pin an empty answer.

  Scoped with the existing `crawl:execute` / `crawl:read` / `crawl:delete` key scopes,
  exposed to assistants as `writ_saved_crawls` / `writ_run_saved_crawl` /
  `writ_saved_crawl_data` (plus `save_as` on `writ_crawl_site`), and surfaced in the app
  as a *Call this crawl* panel on the crawl page.

### Fixed

- **A crawl is no longer mistaken for a workflow.** A crawl stores its pages under a
  synthetic workflow row (`workflow_type='crawl'`, one task per shard), and nothing kept
  that row out of the workflow surfaces. Three consequences, all fixed:

  1. **The workflows page crashed.** `WorkflowResponse.last_run_extracted_data` was typed
     `dict`, but a shard's result is a LIST of pages — so opening a crawl returned
     `ValidationError: Input should be a valid dictionary`. This also took down any
     ordinary workflow that extracts a list of rows (a scraped listing page), which had
     nothing to do with crawls. The field now accepts both shapes.
  2. **Crawls were listed as workflows**, and could be opened, run, edited, duplicated or
     deleted as one. Crawl datasets are now excluded from the workflow library and 404 on
     the recipe surfaces; their data stays reachable from Outputs and `/crawls/{id}`.
  3. **A removed crawl's pages could reappear inside a new workflow.**
     `automation_workflows.id` is a bare SQLite rowid alias, so the id freed by a deleted
     crawl is handed to the next workflow created; any shard row that outlived its crawl
     re-attached to that workflow and showed up as its extracted data and its last run.
     Dataset reads are now scoped by subsystem (a workflow never serves shard rows, and
     vice versa), and startup purges crawl rows whose crawl is gone.

  Crawl shards also no longer inflate a workflow's run counters, fire a
  `workflow_completed` trigger per shard, or raise a `run_failed` notification per page.

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
