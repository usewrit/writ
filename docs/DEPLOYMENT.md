# Production deployment

Put this coordinator on a public domain, with a real certificate, in one
command.

```bash
./scripts/deploy.sh writ.example.com you@example.com
```

That is the whole thing. Point your domain's A record at the server first; the
script does the rest and tells you what it changed.

## What that command does

1. **Checks the ground.** Docker is installed and running, ports 80 and 443 are
   free, and `writ.example.com` already resolves here. The DNS check matters:
   Let's Encrypt rate-limits failed issuance, so it is cheaper to catch a missing
   record now than to burn attempts on it.
2. **Creates `.env` with fresh secrets** if you have not run `gen-env.sh` yet.
   Existing secrets are never rotated, so re-running is safe.
3. **Writes every domain-derived setting consistently.** These four used to be
   four separate things to remember, and getting any one of them wrong produced a
   coordinator that looked healthy and did not work:

   | Setting | Value | Why |
   |---|---|---|
   | `WRIT_PUBLIC_URL` | `https://writ.example.com` | agents dial it, `/agent.sh` embeds it, the Host allowlist derives from it |
   | `WRIT_DOMAIN` | `writ.example.com` | the site address Caddy requests a certificate for |
   | `CORS_ORIGINS` | `https://writ.example.com` | `*` is refused in production |
   | `FORWARDED_ALLOW_IPS` | `127.0.0.1,172.16.0.0/12` | so per-IP limits see the real caller, not Caddy |

4. **Starts the `tls` profile**, which brings up Caddy beside the coordinator.
5. **Waits and verifies** — the coordinator's health endpoint, then the live
   `https://` URL. If the certificate does not arrive it tells you the three
   things that actually cause that (DNS, firewall, a web server already on :80).

Re-run it any time: to change domain, to repair a half-finished deploy, or after
`docker compose down`.

## TLS

Caddy obtains the certificate over ACME and **renews it by itself**. There is no
certbot, no renewal cron, and no reload hook to forget — the single most common
way a self-hosted deployment breaks three months later.

Certificates and the ACME account key live on the `caddy-data` volume. Back it
up with the rest of your data. Losing it is survivable — Caddy re-issues on the
next start — but Let's Encrypt allows only 5 duplicate certificates per domain
per week, so repeated loss during a rebuild loop can lock you out of issuance for
a few days.

Debugging a deploy? `--staging` switches to Let's Encrypt's staging CA, which
issues untrusted certificates but has far looser limits:

```bash
./scripts/deploy.sh writ.example.com you@example.com --staging
```

Re-run without the flag once it works; the script switches back automatically.

## Bring your own proxy

If you already run nginx, Traefik, or a cloud load balancer, skip the `tls`
profile entirely — `docker compose up -d` keeps the coordinator on
`127.0.0.1:8000` and you point your proxy at it.

Two things your proxy must do:

- **forward WebSocket upgrades.** The agent fleet holds a long-lived socket on
  `/ws/…`. On nginx that means `proxy_set_header Upgrade`/`Connection`, and
  `proxy_read_timeout 300s` — without the timeout, nginx silently drops healthy
  agents after 60 seconds.
- **set `X-Forwarded-For` and `X-Forwarded-Proto`.**

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 300s;
}
```

Then set in `.env`, by hand:

```ini
ENVIRONMENT=production
WRIT_PUBLIC_URL=https://writ.example.com
CORS_ORIGINS=https://writ.example.com
FORWARDED_ALLOW_IPS=127.0.0.1
```

`FORWARDED_ALLOW_IPS` must name the address your proxy connects **from**. Get it
wrong and every request looks like it came from the proxy: all clients share one
rate-limit bucket, one attacker's failed logins lock out everyone, and your audit
log records the proxy's IP instead of the caller's.

## Hostnames

You do **not** need `ALLOWED_HOSTS`. `WRIT_PUBLIC_URL`'s hostname is trusted
automatically, and so is loopback (the container healthcheck curls
`localhost:8000` from inside the container).

Set `ALLOWED_HOSTS` only for *extra* names this coordinator also answers on — a
vanity alias, a second hostname, a wildcard like `*.team.example.com`. You can
also edit them under **Settings → Network → Trusted hosts**, which applies
without a restart and shows you the effective list.

A request whose `Host` header is not on the list gets a 400 naming the rejected
hostname and how to fix it.

## Rate limiting

Three layers, on by default:

- **Per-IP ceiling across everything.** `GLOBAL_RATE_LIMIT_REQUESTS` requests per
  `GLOBAL_RATE_LIMIT_WINDOW` seconds — 600/60 by default. Static assets, the
  healthcheck and CORS preflights are exempt. Generous on purpose: it is a DoS
  backstop, not a quota, and one browser tab loading the app already spends tens
  of requests a minute. Set `GLOBAL_RATE_LIMIT_ENABLED=false` if a WAF in front
  already does this.
- **Per-path limits** on the paths that matter: login and password reset have
  brute-force lockout on both the IP and the account, agent pairing codes are
  single-use and rate-limited, inbound webhooks are capped.
- **Automatic IP bans** for hosts that keep failing authentication.

Behind a proxy, all three depend on `FORWARDED_ALLOW_IPS` being right.

## Backups

Everything lives on the `writ-data` volume: the SQLite database and your files.

Back up **`SECRET_ENCRYPTION_KEY` from `.env` separately, and somewhere other
than this server.** It encrypts stored credentials at rest — a database backup
without it cannot decrypt them, and there is no recovery path.

## Document extraction across a network

The `doc-extract` service ships in this bundle and starts with everything else,
so PDFs, office documents and scanned pages work with no configuration — **as
long as your agents run on the same host as the coordinator.**

The reason is worth understanding, because the failure is silent: agents call
doc-extract *directly*, with bytes they already fetched. The coordinator never
calls it. The address handed to each agent defaults to `http://127.0.0.1:8092`,
which is right for a co-located agent and unreachable for one elsewhere. An agent
that cannot reach the service does not error — it skips non-HTML content exactly
as if the service were absent.

So if any agent runs on another machine:

1. Set `WRIT_DOC_EXTRACT_DOMAIN=docs.writ.example.com` in `.env` and point that
   name at this server. The bundled Caddy already has a site block for it and
   will get it a certificate.
2. Set `DOC_EXTRACT_URL=https://docs.writ.example.com` so agents are handed the
   routable address.
3. Leave `DOC_EXTRACT_SECRET` as generated. Once the service is reachable over a
   network that secret is the only thing in front of it, so never expose it
   without one and never publish port 8092 on a public interface.

`GET /api/fleet/connect-info` reports `doc_extract.enabled` and the URL agents
are being handed. To turn the lane off entirely, set `DOC_EXTRACT_URL=` (empty)
and run `docker compose up -d coordinator` on its own.

## Checklist

- [ ] DNS A record points at this server
- [ ] `./scripts/deploy.sh <domain> <email>` completed and `https://` verified
- [ ] `SECRET_ENCRYPTION_KEY` backed up off this machine
- [ ] `writ-data` and `caddy-data` volumes in your backup rotation
- [ ] An admin has enrolled a second factor, then `REQUIRE_ADMIN_MFA=true`
- [ ] Agents connect and appear in **Fleet**
- [ ] Firewall allows inbound 80 and 443, and nothing else
