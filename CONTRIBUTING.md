# Contributing

Thanks for your interest in improving the self-hosted **writ** coordinator! This
is the single-container, self-hostable edition — SQLite + an in-process cache, a
FastAPI coordinator, a React SPA, and a fleet of `writ-agent` workers that connect
over WebSocket. There is no cloud dependency.

## Development setup

Requirements: Python 3.11+, Node 18+, and (optionally) Docker.

```bash
# From the repository root:
bash run-local.sh            # creates a venv, builds the SPA, migrates SQLite, runs the coordinator
# → open http://localhost:8000 and complete the first-run setup
```

`run-local.sh` persists a dev database and generated secrets under `.local/`
(git-ignored). Delete that directory for a clean slate.

To iterate on the frontend:

```bash
cd frontend
npm install
npm run dev                  # Vite dev server, proxies /api + /ws to the coordinator
```

## Project layout

| Path            | What it is                                                        |
| --------------- | ----------------------------------------------------------------- |
| `coordinator/`  | FastAPI app — REST API, agent WebSocket ingress, APScheduler jobs |
| `frontend/`     | React + Vite single-page app (served by the coordinator at `/`)   |
| `docker/`       | Single-container image + `docker-compose.yml`                     |
| `docs/`         | Operator guides (agent connect, production deployment)            |
| `scripts/`      | `gen-env.sh` — generates a filled-in `.env` with fresh secrets    |

## Testing & CI

CI (`.github/workflows/ci.yml`) runs on every PR:

**Required checks:**

- **Backend (compile + migrate):** installs `coordinator/requirements.txt`,
  byte-compiles every module (`python -m compileall coordinator`), and applies
  all Alembic migrations against a fresh scratch SQLite database.
- **Backend (pytest):** the suite in `coordinator/tests/`. It is
  dependency-light and green on a bare checkout — the DB-backed cases skip
  loudly (with the reason printed) unless `DATABASE_URL` points at a reachable
  server-backed database.
- **Backend (ruff):** the *correctness* rule subset — undefined names, syntax
  errors, mutable default arguments. The full `ruff.toml` ruleset is a ratchet,
  not a gate; see the comments in that file.
- **Frontend:** `npm ci`, then `npx tsc --noEmit` (`vite build` does **not**
  type-check), then `npm run build`.

- **Quality + SAST:** `bandit` (MEDIUM+ severity *and* confidence; justified
  exceptions live in `coordinator/bandit.yaml`) and `pip-audit` against
  `requirements.txt` (the ignore-list, with a written rationale per entry, is
  `coordinator/pip-audit-ignore.txt`). Both are clean today, so both block.

**Advisory (not blocking yet):** `mypy` in the same job, and `eslint` on the SPA.
Their boards are non-empty — mypy reports ~541 findings across 89 files, almost
all a consequence of the codebase being largely unannotated, and eslint reports
30 errors plus ~1k `any` warnings. Both are `continue-on-error` so the required
checks stay meaningful; shrinking either board and flipping the flag is a
welcome contribution. mypy is set up to ratchet per-module — annotate a package,
then add a `[mypy-<pkg>.*] disallow_untyped_defs = True` section to `mypy.ini`.

Run the same checks locally before opening a PR:

```bash
cd coordinator
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
ruff check --select E9,F6,F7,F81,F82,B002,B006,B012 .
bandit -q -c bandit.yaml -r . -x ./tests,./alembic/versions -ll -ii
pip-audit -r requirements.txt $(grep -vE '^\s*(#|$)' pip-audit-ignore.txt | sed 's/^/--ignore-vuln /')
```

For hands-on verification, `bash run-local.sh` starts the whole coordinator
against a local SQLite DB (state persists under `.local/`; delete it for a clean
slate).

## Before you open a PR

- Run the CI checks above locally (pytest + ruff always; migrations if you
  touched the schema; `tsc --noEmit` + build if you touched the SPA).
- Keep changes self-host-appropriate: **no cloud services, no external
  database or cache servers, no multi-tenant / billing / marketplace code.**
  The coordinator runs as one process against SQLite with an in-process cache.
- Match the surrounding code style (naming, comment density, idioms).

## Reporting bugs / requesting features

Open a GitHub issue with steps to reproduce (for bugs) or a clear use case (for
features). For security issues, see [SECURITY.md](SECURITY.md) — please do **not**
open a public issue.

## License

By contributing you agree that your contributions are licensed under the license
that already covers the file you touched: [AGPL-3.0](LICENSE) for the repository,
and [MIT](connectors/writ-mcp/LICENSE) for `connectors/writ-mcp/` — the one
directory that is deliberately permissive so it stays embeddable inside
third-party MCP clients. The full map is in the README's License section.

If your change modifies what a running coordinator does, remember that AGPL-3.0
§13 obliges anyone deploying a modified build to offer its source to their
network users. The app already makes that offer — keep `GET /api/about`, its
`WRIT_SOURCE_URL` setting, and the links the UI renders from them intact.
`scripts/export.sh` fails the build if any of them go missing.
