# Production deployment

The compose file binds the coordinator to `127.0.0.1:8000` — loopback only — so
a fresh install is never accidentally exposed to the network. To serve real
traffic, put a TLS reverse proxy in front and keep the container on loopback.

## 1. Reverse proxy + TLS

Terminate HTTPS at nginx, Caddy, or Traefik on the same host and forward to
`127.0.0.1:8000`. The proxy must also forward WebSocket upgrades — the agent
fleet connects over `/ws/…`.

Caddy example (automatic TLS):

```caddyfile
writ.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

nginx essentials:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;      # WebSocket upgrade
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 300s;                     # don't cut long-lived agent sockets
}
```

Because the proxy terminates TLS, the container keeps listening on plain HTTP
`:8000` on loopback — that is expected. Only change the port mapping to
`"8000:8000"` (all interfaces) on a trusted private network where you accept
plaintext traffic.

## 2. Production environment

Set in your `.env`:

- `ENVIRONMENT=production` — enforces strong secrets, requires a Host
  allowlist, refuses wildcard CORS.
- `WRIT_PUBLIC_URL=https://writ.example.com` — agents dial back to this URL, so
  it must be reachable from wherever your agents run.
- `ALLOWED_HOSTS=writ.example.com` — hostname(s) this coordinator answers on
  (comma-separated, no scheme/port).
- `CORS_ORIGINS=https://writ.example.com` — explicit origin(s); `*` is refused.
- `FORWARDED_ALLOW_IPS=127.0.0.1` — trust only your proxy for forwarded IPs so
  per-IP rate limiting sees real client addresses.

Generate the secrets with `./scripts/gen-env.sh`, and back up
`SECRET_ENCRYPTION_KEY` separately from the data volume — see
[SECURITY.md](../SECURITY.md).

## 3. Backups

Everything lives on the `writ-data` volume (SQLite DB + files). Back it up
regularly, and store `SECRET_ENCRYPTION_KEY` (from `.env`) in a separate secure
location: a database backup without the key cannot decrypt stored secrets.

## 4. Document extraction across a network

The `doc-extract` service ships in this bundle and `docker compose up` starts
it, so PDFs, office documents and scanned pages work with no configuration —
**as long as your agents run on the same host as the coordinator.**

The reason is worth understanding, because the failure is silent: agents call
doc-extract *directly*, with bytes they already fetched. The coordinator never
calls it. The address it hands each agent at connect time defaults to
`http://127.0.0.1:8092`, which is right for a co-located agent and unreachable
for one on another machine. An agent that cannot reach the service does not
error — it skips non-HTML content exactly as if the service were absent.

So if any agent runs elsewhere:

1. Give the service a route those agents can reach. It is published on
   `127.0.0.1:8092`, so the usual answer is a second `server` block on the same
   reverse proxy — a separate hostname (`docs.writ.example.com`) or a path you
   forward — with TLS terminated there.
2. Set `DOC_EXTRACT_URL` in `.env` to that address. Every connect command the
   coordinator generates from then on carries it, so agents pick it up when they
   connect.
3. Keep `DOC_EXTRACT_SECRET` as generated. It is the *only* thing in front of
   the service once it is reachable over a network, so never expose it without
   one, and never publish port `8092` directly on a public interface.

To confirm the lane is live, `GET /api/fleet/connect-info` reports
`doc_extract.enabled` and the URL agents are being handed.

To turn it off entirely, set `DOC_EXTRACT_URL=` (empty) and run
`docker compose up -d coordinator` on its own.
