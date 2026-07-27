## What & why

Describe the change and the motivation. Link related issues (`Fixes #123`).

## How it was verified

- [ ] Backend: `python -m compileall coordinator` passes
- [ ] Backend (schema changes): `alembic upgrade head` applies to a fresh SQLite DB
- [ ] Frontend: `cd frontend && npm run build` passes
- [ ] Manually exercised the change (describe how below)

## Notes for reviewers

Anything that needs special attention — migrations, config changes, follow-ups.

<!-- Keep changes self-host-appropriate: no cloud services, no external
     database or cache servers, no multi-tenant code. See CONTRIBUTING.md. -->
