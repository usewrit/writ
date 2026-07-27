# Connecting a fleet agent

The coordinator never launches a browser — all browser work runs on
**`writ-agent-fleet`** workers you connect to it. This guide takes you from a
running coordinator to an agent showing **online** on the Fleet page.

The agent lives in its own repository:
[github.com/usewrit/writ-agent](https://github.com/usewrit/writ-agent).

## 1. Prerequisites

- A running coordinator you can log in to (see the [root README](../README.md)).
- `WRIT_PUBLIC_URL` set to a URL your agents can reach. For agents on the same
  machine, `http://localhost:8000` works; for remote agents, use your public
  `https://` URL behind a TLS proxy (see [DEPLOYMENT.md](DEPLOYMENT.md)).
- `RECORDER_AUTH_SECRET` set in the coordinator's environment — it signs fleet
  tokens, and minting fails without it.
- On the agent host: a Chromium driver. The agent uses an installed
  Playwright/Patchright Chromium, or attempts `patchright install chromium` on
  first run. To pre-install:

  ```bash
  pip install patchright && patchright install chromium
  # or: pip install playwright && playwright install chromium
  ```

## 2. Mint a fleet token

**In the UI:** open **Fleet → Connect a new agent**. It shows this
coordinator's connect URL and mints a token — the raw token is shown **once**;
copy it now.

**Via the API** (equivalent):

```bash
# Log in → admin JWT
TOKEN=$(curl -s -X POST https://your-coordinator/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"..."}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# Mint a long-lived agent token (raw token returned once)
curl -s -X POST https://your-coordinator/api/fleet/tokens \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"my-first-agent"}'
```

Treat the token as a secret. Revoke unused tokens from the Fleet page.

## 3. Install and run the agent

Pick one of three install paths, then run it with two environment variables:

```bash
WRIT_SERVICE_TOKEN=<token> WRIT_COORDINATOR_URL=https://your-coordinator writ-agent-fleet
```

### a) Release binary

Download `writ-agent-fleet` for your platform from the
[writ-agent Releases](https://github.com/usewrit/writ-agent/releases) page,
make it executable, and run it as above.

### b) Docker

```bash
docker run -d --name writ-agent \
  -e WRIT_SERVICE_TOKEN=<token> \
  -e WRIT_COORDINATOR_URL=https://your-coordinator \
  ghcr.io/usewrit/writ-agent:latest
```

### c) Build from source

```bash
git clone https://github.com/usewrit/writ-agent
cd writ-agent
cargo build --release --no-default-features --features local,fleet,openai --bin writ-agent-fleet
./target/release/writ-agent-fleet   # with the env vars above
```

A successful dial-in logs the warm browser launch, then
`Connecting to https://your-coordinator...` and `Connected — waiting for tasks`.

## 4. Environment reference

| Variable | Meaning |
| --- | --- |
| `WRIT_SERVICE_TOKEN` | **Required.** The fleet token minted in step 2. |
| `WRIT_COORDINATOR_URL` | **Required.** Base URL of your coordinator (`https://…`, or `http://localhost:8000` for loopback). |
| `WRIT_HOME` | Agent data directory (default `~/.writ`). Holds the SQLCipher-encrypted local DB and a `0600` `vault.key`. |
| `WRIT_USE_KEYRING` | Store the vault key in the OS keyring instead of the `vault.key` file. |
| `WRIT_FLEET_ALLOW_INSECURE` | Allow plaintext `http://` to a **non-loopback** coordinator. Refused by default; enable only on a trusted private network. |
| `WRIT_FLEET_STATUS_PORT` | Serve a loopback-only `GET /healthz` on this port for healthchecks. Returns `503` while disconnected from the coordinator. |
| `WRIT_AI_KEYS_CONFIGURED` | Signal that AI provider keys are configured on this agent (enables AI-assisted tasks). |

## 5. Healthchecks and supervision

`WRIT_FLEET_STATUS_PORT` gives supervisors a real liveness signal: `200` only
while the agent holds its coordinator connection.

**docker-compose sidecar** (add next to the coordinator service, or run on any
other host):

```yaml
  writ-agent:
    image: ghcr.io/usewrit/writ-agent:latest
    environment:
      WRIT_SERVICE_TOKEN: ${WRIT_FLEET_TOKEN}
      WRIT_COORDINATOR_URL: ${WRIT_PUBLIC_URL:-http://coordinator:8000}
      WRIT_FLEET_STATUS_PORT: "8500"
      # WRIT_FLEET_ALLOW_INSECURE: "true"   # only if the coordinator URL is plain http on a trusted LAN
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://127.0.0.1:8500/healthz"]
      interval: 30s
      timeout: 5s
      start_period: 30s
      retries: 3
```

**systemd** unit:

```ini
[Unit]
Description=Writ fleet agent
After=network-online.target

[Service]
Environment=WRIT_SERVICE_TOKEN=<token>
Environment=WRIT_COORDINATOR_URL=https://your-coordinator
Environment=WRIT_FLEET_STATUS_PORT=8500
ExecStart=/usr/local/bin/writ-agent-fleet
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Check liveness with `curl -f http://127.0.0.1:8500/healthz`.

## 6. Verify it on the Fleet page

Open **Fleet** in the sidebar — the agent appears under **Connected agents**
with an online dot, platform, and capacity (max sessions / free slots).

API equivalent:

```bash
curl -s https://your-coordinator/api/fleet/agents -H "Authorization: Bearer $TOKEN"
# → {"agents":[{"id":"writ-…","online":true,"capacity":{…}}],"online_count":1,…}
```

`online` reflects the live WebSocket: it is `true` only while the agent holds
its connection. A previously connected agent stays listed with `last_seen` set
after it disconnects.

## 7. Troubleshooting

- **Token minting returns HTTP 500** — `RECORDER_AUTH_SECRET` is not set on the
  coordinator. Set it and restart.
- **`invalid token` / auth error on connect** — the token was mistyped,
  truncated, revoked, or minted on a different coordinator (tokens are signed
  per-install). Mint a fresh one from the Fleet page.
- **Agent refuses to connect over plain `http://`** — by design: plaintext to a
  non-loopback coordinator is refused. Either front the coordinator with TLS and
  use `https://`, or (trusted private networks only) set
  `WRIT_FLEET_ALLOW_INSECURE=true`.
- **Agent connects, then no tasks arrive** — check the Fleet page shows it
  **online** with free slots. If it flaps offline, check for a proxy or LB
  idle-timeout shorter than the agent's heartbeat, and make sure the coordinator
  was started via `serve.py` / the shipped Docker image (which keep agent
  WebSockets alive).
- **Browser fails to launch on the agent host** — install a driver manually
  (`pip install patchright && patchright install chromium`) and check the
  agent's logs for the `Warm browser launched` line.
- **`/healthz` returns 503** — the agent process is up but not connected to the
  coordinator; check `WRIT_COORDINATOR_URL`, DNS/firewall reachability, and the
  token.
