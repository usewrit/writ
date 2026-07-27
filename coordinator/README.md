# Coordinator

The **coordinator** is the server side of the self-hosted Writ stack: a single
FastAPI process that stores everything in one SQLite file (no external database
or cache services — an in-process fakeredis backs queues, rate limits, and
pub/sub), serves the built web UI as static files, and dispatches all browser
work over WebSocket to your fleet of `writ-agent-fleet` workers. It never
launches a browser itself.

## Layout

| Path                | What it is                                                  |
| ------------------- | ----------------------------------------------------------- |
| `main.py`           | FastAPI app assembly (routers, middleware, static UI)       |
| `serve.py`          | The launcher — use this, not `uvicorn main:app` (see below) |
| `config.py`         | Settings: env vars, secret validation, environment posture  |
| `database.py`       | Async SQLAlchemy engine over the SQLite file                |
| `alembic/`          | Schema migrations (`alembic upgrade head`)                  |
| `routers/`          | REST API + agent WebSocket ingress (`/ws/ai-gateway`)       |
| `services/`         | Domain logic: workflows, fleet dispatch, crawls, schedules  |
| `models/`           | SQLAlchemy models                                           |
| `security/`, `middleware/`, `utils/` | Auth, rate limiting, helpers               |
| `notifications/`    | BYO notification providers (email, webhook, SMS)            |

## Run it for development

The easiest path is the repo-root helper, which creates a venv, builds the SPA,
migrates SQLite, and starts the server:

```bash
bash ../run-local.sh        # from this directory (or bash run-local.sh from the repo root)
```

Manual equivalent: install `requirements.txt` into a venv, export the secrets
listed in `../.env.example` (plus `ENVIRONMENT=development`), run
`alembic upgrade head`, then start with `python serve.py`. Always launch via
`serve.py` — it disables uvicorn's WS protocol keepalive, which would otherwise
drop healthy fleet agents after about a minute.

See the [root README](../README.md) for the full quickstart, Docker deployment,
and how to connect agents.
