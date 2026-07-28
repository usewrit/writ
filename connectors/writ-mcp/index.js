#!/usr/bin/env node
/*
 * writ-mcp — the official Writ MCP server connector.
 *
 * A thin stdio <-> HTTP bridge that works against BOTH Writ Cloud and a
 * self-hosted Writ coordinator. MCP clients that speak stdio (Claude Code,
 * Claude Desktop, Cursor, Windsurf, Codex, …) launch this process and exchange
 * newline-delimited JSON-RPC 2.0 over stdin/stdout. We forward each message
 * verbatim to the target's Streamable-HTTP MCP endpoint with an
 * Authorization: Bearer header, and relay the response back. ALL tools and
 * business logic live server-side — this connector holds none, so it can never
 * drift from the app.
 *
 * Targets (pick one). The key rides in the environment: `claude mcp add` takes
 * `-e KEY=value` before the `--`, so it is stored in the client's config file
 * instead of the process table.
 *   Writ Cloud (default — no --url needed):
 *     claude mcp add writ-cloud -e WRIT_API_KEY=wt_... -- npx -y writ-mcp
 *   Self-hosted coordinator:
 *     claude mcp add writ-selfhost -e WRIT_API_KEY=wt_... -- npx -y writ-mcp --url https://writ.example.com
 *   A published per-workflow MCP endpoint (an "Expose as MCP" slug URL) is used
 *   verbatim:
 *     claude mcp add my-tools -e WRIT_API_KEY=wt_... -- npx -y writ-mcp --url https://mcp.usewrit.app/mcp/my-tools
 *
 * Config (flags take precedence over env):
 *   --url <URL>        WRIT_COORDINATOR_URL | WRIT_URL   (default: Writ Cloud)
 *   --api-key <KEY>    WRIT_API_KEY  (prefer the env var — see below)
 *   --insecure         WRIT_INSECURE_TLS=1   (accept a self-signed local-CA cert)
 *   --timeout <ms>     WRIT_MCP_TIMEOUT_MS   (per-request; default 600000)
 *
 * Prefer WRIT_API_KEY over --api-key: a key on the command line is readable by
 * any local process through `ps` and is written to your shell history.
 *
 * Zero dependencies, built on Node's core http/https.
 * The API key is created in the Writ app: Settings -> Developers -> API keys.
 */
'use strict';

const http = require('http');
const https = require('https');
const readline = require('readline');
const { URL } = require('url');

// ── Constants ────────────────────────────────────────────────────────────────

// Writ Cloud's REST base. The account-wide MCP endpoint is POST <base>/mcp;
// published per-workflow endpoints live on their own host (mcp.usewrit.app) and
// are passed to --url verbatim.
const CLOUD_BASE = 'https://api.usewrit.app';

// Cap on a single response body. A broken or hostile server must not be able to
// grow this process until the OS kills it — the client would just see the MCP
// server vanish mid-session with no explanation.
const MAX_RESPONSE_BYTES = 32 * 1024 * 1024;

// Methods that are safe to re-send after a transient failure. Everything else —
// above all tools/call, which RUNS a workflow — is sent exactly once, because a
// retry could double-execute a side effect we cannot see.
const RETRY_SAFE_METHODS = new Set([
  'initialize', 'ping', 'tools/list', 'resources/list', 'prompts/list',
]);

// Errors that prove the request never reached the app: no connection was ever
// established. These are safe to retry for ANY method.
const NEVER_DELIVERED = new Set([
  'ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'EHOSTUNREACH', 'ENETUNREACH',
]);

const RETRY_STATUS = new Set([429, 502, 503, 504]);
const MAX_ATTEMPTS = 3;

const LOOPBACK = new Set(['localhost', '127.0.0.1', '::1', '[::1]', '0.0.0.0']);

// ── Config ───────────────────────────────────────────────────────────────────

function parseArgs(argv) {
  const out = { unknown: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--url') out.url = argv[++i];
    else if (a === '--api-key' || a === '--key') { out.apiKey = argv[++i]; out.keyOnArgv = true; }
    else if (a === '--insecure' || a === '--insecure-tls') out.insecure = true;
    else if (a === '--timeout') out.timeout = argv[++i];
    else if (a === '--help' || a === '-h') out.help = true;
    else if (a === '--version' || a === '-v') out.version = true;
    else if (a.startsWith('--url=')) out.url = a.slice(6);
    else if (a.startsWith('--api-key=')) { out.apiKey = a.slice(10); out.keyOnArgv = true; }
    else if (a.startsWith('--timeout=')) out.timeout = a.slice(10);
    // A mistyped flag must not fail silently as "missing api key" — name it.
    else out.unknown.push(a);
  }
  return out;
}

const args = parseArgs(process.argv.slice(2));

const HELP =
  'writ-mcp — the official Writ MCP server connector (Writ Cloud + self-host)\n\n' +
  // --api-key is bracketed, not required: the key normally arrives as
  // WRIT_API_KEY, and exactly one of the two has to be present.
  'Usage: writ-mcp [--url <url>] [--api-key <key>] [--insecure] [--timeout <ms>]\n\n' +
  'Without --url it targets Writ Cloud (' + CLOUD_BASE + '). Pass --url for a\n' +
  'self-hosted coordinator, or a published /mcp/<slug> endpoint URL to use it\n' +
  'verbatim.\n\n' +
  'Env:  WRIT_COORDINATOR_URL | WRIT_URL, WRIT_API_KEY, WRIT_INSECURE_TLS, WRIT_MCP_TIMEOUT_MS\n' +
  'The key also accepts --api-key, but prefer WRIT_API_KEY: a key on the command\n' +
  'line is visible in `ps` and recorded in your shell history.\n\n' +
  'Add to Claude Code (-e sets the env var, and must precede the --):\n' +
  '  claude mcp add writ-cloud    -e WRIT_API_KEY=<YOUR_KEY> -- npx -y writ-mcp\n' +
  '  claude mcp add writ-selfhost -e WRIT_API_KEY=<YOUR_KEY> -- npx -y writ-mcp --url https://writ.example.com\n';

// --help / --version are explicit queries that exit immediately, so stdout is
// not yet the JSON-RPC channel and is the right place for the answer.
if (args.help) {
  process.stdout.write(HELP);
  process.exit(0);
}
if (args.version) {
  process.stdout.write((require('./package.json').version || '0.0.0') + '\n');
  process.exit(0);
}

function die(msg) {
  process.stderr.write('[writ-mcp] ' + msg + '\n');
  process.exit(1);
}

function warn(msg) {
  process.stderr.write('[writ-mcp] ' + msg + '\n');
}

if (args.unknown.length) {
  die('Unknown option(s): ' + args.unknown.join(' ') + '\n\n' + HELP);
}

const RAW_URL =
  args.url ||
  process.env.WRIT_COORDINATOR_URL ||
  process.env.WRIT_URL ||
  CLOUD_BASE;
// Trimmed, because the overwhelmingly common way to supply a key is a paste from
// the app's UI or `WRIT_API_KEY=$(cat key.txt)` — both of which bring surrounding
// whitespace, and a newline in a header value is rejected by Node deep inside the
// transport, surfacing as a per-request "Cannot reach <target>" that blames the
// network for a typo. `Bearer ` is stripped too so either form normalizes.
const API_KEY = String(args.apiKey || process.env.WRIT_API_KEY || '')
  .trim()
  .replace(/^bearer\s+/i, '')
  .trim();
const INSECURE =
  args.insecure ||
  ['1', 'true', 'yes'].includes(String(process.env.WRIT_INSECURE_TLS || '').toLowerCase());

let TIMEOUT_MS = 600000;
if (args.timeout !== undefined || process.env.WRIT_MCP_TIMEOUT_MS) {
  const raw = args.timeout !== undefined ? args.timeout : process.env.WRIT_MCP_TIMEOUT_MS;
  const n = Number(raw);
  if (!Number.isFinite(n) || n <= 0) {
    die('Invalid timeout: ' + raw + ' (expected a positive number of milliseconds).');
  }
  TIMEOUT_MS = n;
}

if (!API_KEY) {
  die(
    'Missing API key. Set WRIT_API_KEY (preferred — a key passed as --api-key is ' +
    'visible to any local process via `ps` and is recorded in your shell history). ' +
    'Create one in the Writ app under Settings -> Developers -> API keys.'
  );
}

// Reject a key that cannot legally go in an HTTP header, HERE, where we can name
// the cause. Otherwise Node throws ERR_INVALID_CHAR inside every request and the
// user sees "Cannot reach <target> (Invalid character in header content)" — a
// message that points at the network for what is really a malformed credential.
// Writ keys are `wt_` + URL-safe base64, so printable-non-space is the right set.
if (!/^[\x21-\x7e]+$/.test(API_KEY)) {
  die(
    'The API key contains characters that cannot be sent in an HTTP header ' +
    '(whitespace, a newline, or a control character). Surrounding whitespace is ' +
    'already trimmed, so this is something inside the key itself — re-copy it from ' +
    'Settings -> Developers -> API keys. A Writ key looks like wt_ followed by ' +
    'letters, digits, - and _.'
  );
}

// Build the MCP endpoint URL. A base URL gets '/mcp' appended; a URL whose path
// already IS an MCP endpoint — '/mcp' exact (any server's native endpoint) or
// '/mcp/<slug>' (a published per-workflow endpoint) — is used verbatim, so
// copy-pasted endpoint URLs work without double-appending.
let ENDPOINT;
try {
  const trimmed = RAW_URL.replace(/\/+$/, '');
  const probe = new URL(trimmed);
  if (probe.protocol !== 'http:' && probe.protocol !== 'https:') {
    die('Invalid --url: ' + RAW_URL + ' (expected an http:// or https:// URL).');
  }
  const isMcpPath = probe.pathname === '/mcp' || probe.pathname.startsWith('/mcp/');
  ENDPOINT = isMcpPath ? probe : new URL(trimmed + '/mcp');
} catch (_) {
  die('Invalid --url: ' + RAW_URL);
}
const TRANSPORT = ENDPOINT.protocol === 'https:' ? https : http;
const AGENT = new TRANSPORT.Agent({ keepAlive: true, maxSockets: 8 });
const AUTH = 'Bearer ' + API_KEY;

// The URL as it is safe to PRINT. `https://user:pass@host/…` is a legal URL, and
// every diagnostic below interpolates the endpoint — but MCP clients persist a
// server's stderr to a log file on disk (Claude Code writes `mcp-logs-<name>/*.jsonl`),
// so echoing the href verbatim would write that password to disk in plaintext.
// The request itself never carries the userinfo: opts below are built from
// hostname/port/path only, so it is dropped either way. Say so rather than
// letting it look like it was applied.
const SAFE_HREF = (() => {
  const u = new URL(ENDPOINT.href);
  if (!u.username && !u.password) return u.href;
  u.username = '';
  u.password = '';
  return u.href;
})();
if (ENDPOINT.username || ENDPOINT.password) {
  warn(
    'NOTE: credentials embedded in the URL are ignored — Writ authenticates with ' +
    'the API key in the Authorization header. Remove them from --url; they are not ' +
    'sent, and a password in a URL tends to end up in logs and shell history.'
  );
}

// Which kind of target we resolved — used in messages so errors point users at
// the right place for keys/URLs.
const TARGET =
  ENDPOINT.hostname === new URL(CLOUD_BASE).hostname
    ? 'Writ Cloud'
    : ENDPOINT.pathname.startsWith('/mcp/')
      ? 'published MCP endpoint'
      : 'self-hosted coordinator';

// ── Credential-exposure warnings ─────────────────────────────────────────────
// Every one of these is a way the Bearer key leaks. Say so loudly at startup —
// stderr is surfaced in MCP client logs.

// SECURITY (audit #40): insecure TLS disables certificate verification, exposing
// the Bearer API key to a man-in-the-middle. Only for a trusted local self-signed
// CA on a private network — never against a remote/public coordinator.
if (INSECURE) {
  warn(
    'WARNING: TLS certificate verification is DISABLED (--insecure / WRIT_INSECURE_TLS). ' +
    'Your API key can be intercepted by a man-in-the-middle. Use only on a trusted ' +
    'private network with a self-signed local CA.'
  );
}

// Plaintext HTTP to anything but loopback ships the key in the clear. This is
// the quiet sibling of --insecure and deserves the same shout.
if (ENDPOINT.protocol === 'http:' && !LOOPBACK.has(ENDPOINT.hostname)) {
  warn(
    'WARNING: ' + ENDPOINT.origin + ' is plaintext HTTP. Your API key is sent ' +
    'UNENCRYPTED over the network and is readable by anything on the path. Use ' +
    'https:// for any non-loopback target.'
  );
}

if (args.keyOnArgv) {
  warn(
    'NOTE: --api-key puts your key in this process\'s command line, where any local ' +
    'process can read it via `ps`. Prefer the WRIT_API_KEY environment variable.'
  );
}

warn(
  'target: ' + TARGET + ' — proxying stdio -> ' + SAFE_HREF +
  (INSECURE ? ' (insecure TLS)' : '')
);

// ── Session state negotiated with the server ─────────────────────────────────
// Streamable HTTP lets a server pin a session (Mcp-Session-Id) at initialize and
// require it on every later request, and the 2025-06-18 spec has clients echo the
// negotiated MCP-Protocol-Version. Writ's own endpoints are stateless and ignore
// both, but honouring them keeps this connector correct against any compliant
// MCP server — including future Writ surfaces.
let SESSION_ID = null;
let PROTOCOL_VERSION = null;

// ── HTTP request (core http/https, no deps) ──────────────────────────────────

function httpPost(bodyStr) {
  return new Promise((resolve, reject) => {
    const body = Buffer.from(bodyStr, 'utf8');
    const headers = {
      'Content-Type': 'application/json',
      // The Streamable HTTP spec requires clients to accept both; we parse
      // either shape (see extractJsonRpc).
      Accept: 'application/json, text/event-stream',
      Authorization: AUTH,
      'Content-Length': body.length,
    };
    if (SESSION_ID) headers['Mcp-Session-Id'] = SESSION_ID;
    if (PROTOCOL_VERSION) headers['MCP-Protocol-Version'] = PROTOCOL_VERSION;

    const opts = {
      method: 'POST',
      protocol: ENDPOINT.protocol,
      hostname: ENDPOINT.hostname,
      port: ENDPOINT.port || (ENDPOINT.protocol === 'https:' ? 443 : 80),
      path: ENDPOINT.pathname + ENDPOINT.search,
      headers: headers,
      agent: AGENT,
      timeout: TIMEOUT_MS,
    };
    if (ENDPOINT.protocol === 'https:' && INSECURE) opts.rejectUnauthorized = false;

    const req = TRANSPORT.request(opts, (res) => {
      const chunks = [];
      let bytes = 0;
      let aborted = false;
      res.on('data', (c) => {
        if (aborted) return;
        bytes += c.length;
        if (bytes > MAX_RESPONSE_BYTES) {
          aborted = true;
          const e = new Error('response exceeded ' + MAX_RESPONSE_BYTES + ' bytes');
          e.tooLarge = true;
          res.destroy();
          req.destroy();
          reject(e);
          return;
        }
        chunks.push(c);
      });
      res.on('end', () => {
        if (aborted) return;
        resolve({
          status: res.statusCode,
          headers: res.headers,
          text: Buffer.concat(chunks).toString('utf8'),
        });
      });
      res.on('error', (e) => { if (!aborted) reject(e); });
    });
    req.on('error', reject);
    req.on('timeout', () => {
      const e = new Error('timeout');
      e.timedOut = true;
      req.destroy(e);
    });
    req.write(body);
    req.end();
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Re-send a request only when doing so cannot double-apply a side effect:
// either the method is read-only, or the connection was never established.
function mayRetry(msg, err, status) {
  if (err && err.timedOut) return false;  // may be running server-side — never resend
  if (err && err.tooLarge) return false;
  if (err && NEVER_DELIVERED.has(err.code)) return true;
  const method = !Array.isArray(msg) && msg ? msg.method : null;
  if (!method || !RETRY_SAFE_METHODS.has(method)) return false;
  if (err) return true;                    // transport-level failure on a safe method
  return RETRY_STATUS.has(status);
}

async function postWithRetry(msg, bodyStr) {
  let lastErr = null;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    try {
      const resp = await httpPost(bodyStr);
      if (attempt < MAX_ATTEMPTS && mayRetry(msg, null, resp.status)) {
        await sleep(200 * Math.pow(3, attempt - 1));
        continue;
      }
      return resp;
    } catch (err) {
      lastErr = err;
      if (attempt < MAX_ATTEMPTS && mayRetry(msg, err, null)) {
        await sleep(200 * Math.pow(3, attempt - 1));
        continue;
      }
      throw err;
    }
  }
  throw lastErr;
}

// ── Response decoding ────────────────────────────────────────────────────────

// A Streamable-HTTP server may answer a POST with either a JSON body or an SSE
// stream carrying the same JSON-RPC payload in `data:` frames. Writ answers JSON,
// but we advertise both in Accept, so we must read both.
function extractJsonRpc(contentType, text) {
  if (String(contentType || '').indexOf('text/event-stream') !== -1) {
    const payloads = [];
    for (const line of text.split('\n')) {
      const t = line.trim();
      if (t.slice(0, 5) === 'data:') {
        const d = t.slice(5).trim();
        if (d && d !== '[DONE]') payloads.push(d);
      }
    }
    if (!payloads.length) return undefined;
    // Concatenated frames: relay each as its own message.
    return payloads.map((p) => JSON.parse(p));
  }
  return JSON.parse(text);
}

// ── stdout is the JSON-RPC channel — one message per line ────────────────────

let writing = Promise.resolve();
function send(obj) {
  // Serialize writes so concurrent responses never interleave on the wire.
  writing = writing.then(() =>
    new Promise((resolve) => {
      process.stdout.write(JSON.stringify(obj) + '\n', () => resolve());
    })
  );
  return writing;
}

function rpcError(id, code, message) {
  return { jsonrpc: '2.0', id: id === undefined ? null : id, error: { code, message } };
}

// Is this actually a JSON-RPC response, or just *some* JSON?
//
// An error page from a proxy, or a framework's own `{"detail": "..."}` /
// `{"error": "busy"}` body on a 4xx/5xx, parses perfectly well as JSON. Relaying
// it verbatim puts a line on stdout that carries no id, so the client never
// learns its request failed and waits forever. Anything that is not addressable
// back to a pending id has to become a real JSON-RPC error instead.
function looksLikeJsonRpc(v) {
  if (Array.isArray(v)) return v.length > 0 && v.every(looksLikeJsonRpc);
  if (!v || typeof v !== 'object') return false;
  if (v.jsonrpc === '2.0') return true;
  return 'id' in v && ('result' in v || 'error' in v);
}

// Relay one decoded payload — a single response, or an array (SSE frames, or a
// JSON-RPC batch response) that must be emitted as separate stdio lines.
async function relay(decoded) {
  if (Array.isArray(decoded)) {
    for (const item of decoded) await send(item);
  } else if (decoded !== undefined) {
    await send(decoded);
  }
}

// Capture anything the server pinned on the way past. Only `initialize` can open
// a session, and only its result carries the negotiated protocol version.
function captureSession(msg, resp, decoded) {
  if (Array.isArray(msg) || !msg || msg.method !== 'initialize') return;
  const sid = resp.headers && resp.headers['mcp-session-id'];
  if (sid) SESSION_ID = sid;
  const one = Array.isArray(decoded) ? decoded[0] : decoded;
  const v = one && one.result && one.result.protocolVersion;
  if (v) PROTOCOL_VERSION = v;
}

// ── Forward one JSON-RPC message to the coordinator ──────────────────────────

// Every id the client is now blocked on, waiting for us to answer.
//
// A JSON-RPC BATCH is an array — and an array's `.id` is undefined, so treating
// `msg.id === undefined` as "notification" silently swallows a whole batch and
// hangs the client on every id inside it. Enumerating the ids instead means each
// failure path can unblock ALL of them, batch or not.
function expectedIds(msg) {
  if (Array.isArray(msg)) {
    return msg
      .filter((m) => m && typeof m === 'object' && m.id !== undefined)
      .map((m) => m.id);
  }
  if (msg && typeof msg === 'object' && msg.id !== undefined) return [msg.id];
  return [];  // notification, or something we can't address a reply to
}

// Every id the server actually ANSWERED in this response.
//
// A response may legitimately carry more than the answers we asked for — the
// Streamable HTTP spec lets a server put its own requests and notifications in
// the body of a POST response — so we relay everything and only use this to find
// what is MISSING. An id we sent that comes back unaddressed (a gateway serving a
// cached body, a server that answers a batch with one object, a proxy that
// rewrites the payload) leaves the client blocked on it forever, which is exactly
// the failure the batch handling above exists to prevent.
function answeredIds(decoded) {
  const list = Array.isArray(decoded) ? decoded : [decoded];
  const out = new Set();
  for (const m of list) {
    if (m && typeof m === 'object' && m.id !== undefined && ('result' in m || 'error' in m)) {
      out.add(m.id);
    }
  }
  return out;
}

async function forward(msg) {
  const ids = expectedIds(msg);

  // Answer every blocked id with the same failure, so nothing is left waiting.
  const failAll = async (code, message) => {
    for (const id of ids) await send(rpcError(id, code, message));
  };

  try {
    const resp = await postWithRetry(msg, JSON.stringify(msg));

    // 202 Accepted / 204 No Content — the server took it and has nothing to say.
    // (Writ's backend answers 202, mcp-service answers 204; both are valid for a
    // notification.) With ids outstanding it is a server bug: report, don't hang.
    if (resp.status === 202 || resp.status === 204) {
      await failAll(-32603,
        TARGET + ' accepted the request (HTTP ' + resp.status + ') but returned no response.');
      return;
    }
    if (!ids.length) return;  // notification — whatever came back, nobody is waiting

    if (resp.status === 401 || resp.status === 403) {
      await failAll(-32001,
        'Unauthorized: ' + TARGET + ' rejected the API key. Check WRIT_API_KEY / --api-key' +
        (TARGET === 'Writ Cloud'
          ? ' (create one at usewrit.app: Settings -> Developers -> API keys).'
          : ' (create one in your instance: Settings -> Developers -> API keys).'));
      return;
    }

    // A redirect is NOT followed, deliberately: replaying the Authorization
    // header to whatever origin the Location names would hand the API key to a
    // host the user never chose. Node doesn't follow them either, so without
    // this the user gets "Empty response" — a 301 from a reverse proxy doing
    // http -> https is a routine setup, and that message explains none of it.
    if (resp.status >= 300 && resp.status < 400) {
      const loc = resp.headers && resp.headers.location;
      await failAll(-32002,
        TARGET + ' redirected (HTTP ' + resp.status + ')' + (loc ? ' to ' + loc : '') +
        '. Redirects are not followed, because resending your API key to another ' +
        'origin would leak it. Point --url at the final URL instead.');
      return;
    }

    if (!resp.text) {
      await failAll(-32603, 'Empty response from ' + TARGET + '.');
      return;
    }

    let decoded;
    try {
      decoded = extractJsonRpc(resp.headers && resp.headers['content-type'], resp.text);
    } catch (_) {
      await failAll(-32603,
        TARGET + ' returned a non-JSON response (HTTP ' + resp.status + ').');
      return;
    }
    if (decoded === undefined) {
      await failAll(-32603, 'Empty response from ' + TARGET + '.');
      return;
    }
    if (!looksLikeJsonRpc(decoded)) {
      await failAll(resp.status >= 400 ? -32002 : -32603,
        TARGET + ' returned HTTP ' + resp.status + ' with a body that is not a ' +
        'JSON-RPC response.');
      return;
    }

    captureSession(msg, resp, decoded);
    await relay(decoded);

    // Backfill anything the server left unaddressed. Relaying first keeps us
    // spec-compliant (server-initiated messages pass through untouched); this
    // only guarantees the client is never left waiting on an id we sent.
    const answered = answeredIds(decoded);
    for (const id of ids) {
      if (!answered.has(id)) {
        await send(rpcError(id, -32603,
          TARGET + ' returned HTTP ' + resp.status + ' but no response for request id ' +
          JSON.stringify(id) + '.'));
      }
    }
  } catch (err) {
    if (err && err.timedOut) {
      await failAll(-32002, 'Request to ' + TARGET + ' timed out after ' + TIMEOUT_MS + 'ms.');
    } else if (err && err.tooLarge) {
      await failAll(-32002, TARGET + ' returned a response larger than ' +
        Math.round(MAX_RESPONSE_BYTES / 1024 / 1024) + 'MB — refused.');
    } else {
      await failAll(-32002, 'Cannot reach ' + TARGET + ' at ' + SAFE_HREF +
        ' (' + (err && err.message) + ').');
    }
  }
}

// ── Pump stdin lines ─────────────────────────────────────────────────────────

const rl = readline.createInterface({ input: process.stdin, terminal: false });

// Track in-flight forwards so an stdin EOF doesn't kill pending HTTP calls mid-
// request. Only exit once stdin has closed AND every forward has drained.
let pending = 0;
let closing = false;
function maybeExit() {
  if (closing && pending === 0) process.exit(0);
}

rl.on('line', (line) => {
  const s = line.trim();
  if (!s) return;
  let msg;
  try {
    msg = JSON.parse(s);
  } catch (_) {
    // Unparseable input — can't recover an id, so drop it (per JSON-RPC, a parse
    // error with no id has no addressable response).
    warn('dropped unparseable stdin line');
    return;
  }
  // Fire-and-forget: responses correlate by id, so overlap is safe.
  pending++;
  forward(msg).finally(() => {
    pending--;
    maybeExit();
  });
});

rl.on('close', () => { closing = true; maybeExit(); });
process.stdin.on('error', () => { closing = true; maybeExit(); });
process.on('SIGINT', () => process.exit(0));
process.on('SIGTERM', () => process.exit(0));
