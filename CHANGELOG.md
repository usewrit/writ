# Changelog

All notable changes to the self-hosted Writ coordinator are documented in this
file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.2] - 2026-08-10

Attaching a file while recording shipped in 1.0.1, but a file picked during a
recording did not survive to replay. These four defects were the gap between the
feature existing and the feature working.

### Security

- **The automation builder's hand-off draft now re-validates where it sends you.**
  Leaving the builder to create a workflow stashes the draft in `sessionStorage`
  along with a `returnTo` route, and finishing interpolates that value straight
  into a `navigate()` target. It was checked only for being a *string*.

  It is written as `location.pathname`, so it is same-origin by construction — but
  it then survives in storage and is re-read rather than re-derived, so anything
  able to write that key chose where "back to the builder" landed, including an
  absolute URL or the `/\evil.com` form that gets past React Router's own check
  (CVE-2026-53669). It is now resolved against this origin on the way **out**, via
  the same `safeInternalPath` guard the login redirect uses, and falls back to
  `/automations/new` when it does not point here. Validating on read rather than
  on write means the guarantee holds however the value got there.

  Defence in depth rather than a fix for a live hole: writing that key requires
  script execution on the page already. No configuration change, and legitimate
  builder routes — including query and fragment — are unaffected.

### Fixed

- **A recorded upload failed at replay with "no file is bound" — even though you
  had picked a file.** A saved step carries its binding in `config` when the
  *editor* wrote it and in `options` when the *recorder* did. The run's file map
  read only `config`, so every upload bound while recording was simply absent from
  it. Both shapes are canonical for a saved workflow — the UI already tolerates the
  `step.x` / `config.x` / `options.x` spread — so both are read now, with `config`
  winning as the explicit later edit.

- **An upload step with a file already pinned to it was invisible in the run
  dialog.** A pinned file was treated as "not a run-time slot" and excluded
  entirely, which meant a workflow that ran perfectly well offered no way to swap
  the file for one run without editing the workflow itself. Every upload step is now
  listed, and a pinned file comes through as that slot's **default** — the run works
  untouched, and the default is there to override.

- **Answering the recorder's file chooser could cancel itself.** `FilePicker` calls
  `onSelect` and then `onClose`, while answering is asynchronous — it mints a signed
  URL first. Both handlers therefore ran against the same React render, and the
  close handler sent `skip` before the answer had gone out, so the file you chose
  was discarded and the page's dialog was dismissed empty. The prompt is now claimed
  through a ref that updates synchronously: whichever handler takes it first wins,
  and the other finds nothing and does nothing. State could not fix this — a state
  update is not visible to a handler in the same render.

- **MCP callers could not tell which files a workflow needs.** Workflow tools now
  describe their `files` parameter per workflow: each slot, its label, and the
  filename of any pinned default, so a model can see what the call will use if it
  passes nothing and which slot it must supply when a step ships no file. The
  workflow-agnostic runner keeps a generic `{slot: file_id}` schema. **No `file_id`
  is ever exposed** — only slot names, labels and the default's filename.

## [1.0.1] - 2026-08-10

### Added

- **Attach a file while recording.** When a page opens its file chooser mid-recording,
  the recorder now asks you which stored file to hand it instead of dismissing the
  dialog empty. Pick one and recording continues; the step is saved bound to that
  file, so every replay uploads it again with no model in the loop.

  The agent never receives your credentials. It is handed a **short-lived signed URL**
  for that one file (`GET /files/{id}/signed-url`, TTL from
  `FILE_SIGNED_URL_TTL_SECONDS`), minted only after ownership is checked
  server-side — an agent cannot authenticate as you and so cannot read
  `/files/{id}/content` itself. Declining is always available and never blocks the
  page: the chooser is answered either way, and the step is simply recorded unbound
  rather than leaving the browser stuck on an open dialog.

### Changed

- **A goal-directed crawl now spends its page budget best-first instead of
  breadth-first.** `crawl_targeting` gained a lexical relevance score — token
  overlap between the goal and a link's URL path and anchor text, with
  discounted credit for near-matches such as plural and stem variants, a boost
  for include-path hits, and a mild depth penalty. It runs on the coordinator
  with no AI, no network call and no embeddings, so it costs nothing per link.
  The frontier is ranked *before* admission rather than filtered after, which is
  what makes the budget go to pages the goal actually names.

  With no goal supplied the score reduces to shallow-first, which reproduces the
  previous breadth-first sweep exactly — untargeted crawls behave as before.

  Links matching the goal not at all are still followed, because the page you
  want is often two hub pages away, but they draw from a bounded allowance
  (`max(8, page_budget // 10)` at seeding, 8 per completed shard) so they can
  walk toward the content without starving the on-topic links found later.

- **`/map` harvests a real candidate pool before ranking.** It now collects at
  least 200 seed-page links regardless of the caller's `limit`, then ranks and
  truncates. Previously a small `limit` also shrank what was gathered, so the
  ranking chose the best of whatever happened to be found first rather than the
  best on the page.

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

- **Every image in a crawled article rendered as a broken tile.** The markdown
  preview had no raw-HTML support, so an `<img>` tag arrived as literal text; and
  once it did render, the app's Content-Security-Policy (`img-src 'self' data:
  blob:`) refused the remote host anyway, leaving the browser's broken-image
  chrome on every picture.

  Both markdown `![](…)` and raw-HTML `<img>` now take one path, so the two can no
  longer diverge. An image is embedded only when its URL is one the CSP actually
  permits — inline `data:`/`blob:` bytes, or this origin — and anything else
  degrades to its **alt text**, which is the caption the page's author wrote and
  is real information rather than a broken tile. The CSP is deliberately *not*
  widened to arbitrary remote hosts: rendering a crawled page must not become a
  way to make your browser fetch from whatever that page names.

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
