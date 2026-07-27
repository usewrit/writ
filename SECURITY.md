# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities **privately** — do not open a public
GitHub issue.

Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
("Report a vulnerability" under this repository's **Security** tab).

> **Maintainers:** that tab only appears once *Private vulnerability reporting*
> is switched on in **Settings → Advanced Security**. Enable it before making the
> repository public, or this page describes a channel that does not exist.

If private reporting is unavailable to you for any reason, open a public issue
containing **only** "security issue, need a private channel" — no details, no
proof-of-concept — and a maintainer will open one.

Please include:

- A description of the issue and its impact.
- Steps to reproduce (a proof-of-concept if possible).
- The version / commit you tested.

We aim to acknowledge reports within a few days and will keep you updated on the
fix. Please give us a reasonable window to release a patch before any public
disclosure.

## Data at rest

Be clear about what is and is not encrypted on disk:

- **The SQLite database file is NOT encrypted.** Workflow definitions, run
  history, and scraped/extracted data are stored in plaintext inside
  `writ.db`. Use full-disk (or volume-level) encryption if you need
  content-level protection of that data.
- **Secrets are encrypted at the column level.** The vault, persona
  credentials and cookies, TOTP seeds, AI/notification provider keys, and
  webhook secrets are stored in Fernet-encrypted columns, keyed by
  `SECRET_ENCRYPTION_KEY` from your environment.
- **Back up `SECRET_ENCRYPTION_KEY` separately from the `writ-data` volume.**
  The two halves are only dangerous — or useful — together:
  - A DB backup **without** the key: the encrypted secrets are unrecoverable.
  - The key and the DB stolen **together**: every stored secret is exposed.

  Store the key in a password manager or secrets vault, not next to the
  database backups.

## Hardening notes for self-hosters

The coordinator is a **single-owner** application: the first account created is
the sole administrator. It is designed to run behind your own network boundary
— the shipped compose file binds to loopback only.

- **Set strong secrets.** In production (`ENVIRONMENT=production`) the app
  refuses to boot with default/placeholder `API_SECRET_KEY`, `HMAC_SECRET_KEY`,
  `JWT_SECRET_KEY`, and requires `SECRET_ENCRYPTION_KEY`.
  `./scripts/gen-env.sh` generates all of them for you.
- **Enable MFA on the admin account.** After enrolling a second factor in the
  app, set `REQUIRE_ADMIN_MFA=true` so admin logins always require it.
- **Terminate TLS** in front of the coordinator (reverse proxy) and set
  `ALLOWED_HOSTS` + `CORS_ORIGINS` to your real origin in production. See
  [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
- **Fleet tokens** authorize an agent to connect. Treat them as secrets; revoke
  unused ones from the Fleet page.
- Stored provider/AI keys are encrypted at rest and never returned to the
  client (only a masked hint).
