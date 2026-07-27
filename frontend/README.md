# Writ self-host frontend

The web UI for the Writ self-host coordinator. A single-page app built with
Vite, React 18, and TypeScript, styled with Tailwind CSS.

## How it is served

- **Production**: the SPA is built inside the coordinator Docker image (see
  `docker/Dockerfile.coordinator` at the repo root) and served by the FastAPI
  coordinator itself at `/`. There is no separate frontend container, no
  nginx, and no standalone deployment of this directory.
- **API**: everything is same-origin — axios uses `baseURL: '/api'`
  (`src/api/client.ts`), and WebSockets (`/ws`, `/api/recorder`) share the
  app's origin.

## Development

```bash
npm install
npm run dev
```

The Vite dev server listens on `http://0.0.0.0:5173` and proxies `/api` and
`/ws` to a locally running coordinator. By default it targets
`http://127.0.0.1:8000`; point it elsewhere with:

```bash
VITE_COORDINATOR_URL=http://other-host:8000 npm run dev
```

Note that `VITE_API_URL` is **not** the dev proxy target — it only overrides
the origin displayed in copyable webhook/snippet URLs and falls back to
`window.location.origin` (see `.env.example`).

## Auth model

Sign-in is email + password against the coordinator. The access token lives
**in memory only** (never localStorage); an httpOnly refresh cookie re-mints
it on reload and on 401 via a single-flight refresh in the axios interceptor
(`src/utils/auth.ts`, `src/api/client.ts`).

## i18n

English, French, and Spanish via `i18next` / `react-i18next`. English source
strings are the translation keys themselves; `src/i18n/locales/fr.json` and
`es.json` are lazily loaded dictionaries.

## Checks and builds

```bash
npx tsc --noEmit   # typecheck
npm run lint       # eslint
npm run build      # production bundle into dist/
```
