'use strict';
/* Config resolution, target selection, and the credential-exposure warnings. */

const { test } = require('node:test');
const assert = require('node:assert');
const { spawn } = require('node:child_process');
const path = require('node:path');
const { startServer, runConnector } = require('./helpers');

const BIN = path.join(__dirname, '..', 'index.js');
const PKG = require('../package.json');

// Run to completion and capture everything, including a non-zero exit. stdin is
// closed immediately: a healthy connector exits as soon as its input ends, which
// is also what keeps this suite fast.
function cli(args, env) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [BIN].concat(args || []), {
      env: Object.assign(
        {}, process.env,
        { WRIT_API_KEY: '', WRIT_COORDINATOR_URL: '', WRIT_URL: '', WRIT_INSECURE_TLS: '' },
        env || {}
      ),
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => { stdout += d; });
    child.stderr.on('data', (d) => { stderr += d; });
    child.stdin.end();
    const kill = setTimeout(() => child.kill(), 8000);
    child.on('exit', (code) => { clearTimeout(kill); resolve({ code, stdout, stderr }); });
  });
}

test('--version prints the package version on stdout', async () => {
  const r = await cli(['--version']);
  assert.equal(r.code, 0);
  assert.equal(r.stdout.trim(), PKG.version);
});

test('--help prints usage on stdout and exits 0', async () => {
  const r = await cli(['--help']);
  assert.equal(r.code, 0);
  assert.match(r.stdout, /Usage: writ-mcp/);
  assert.match(r.stdout, /api\.usewrit\.app/);
});

test('a missing API key fails fast with an actionable message', async () => {
  const r = await cli([]);
  assert.notEqual(r.code, 0);
  assert.match(r.stderr, /Missing API key/);
  assert.match(r.stderr, /WRIT_API_KEY/);
});

// A typo'd flag used to be swallowed and resurface as a confusing "missing key".
test('an unknown flag is named, not silently ignored', async () => {
  const r = await cli(['--api_key', 'wt_x']);
  assert.notEqual(r.code, 0);
  assert.match(r.stderr, /Unknown option/);
  assert.match(r.stderr, /--api_key/);
});

test('an invalid timeout is rejected instead of silently defaulting', async () => {
  const r = await cli(['--timeout', 'soon'], { WRIT_API_KEY: 'wt_x' });
  assert.notEqual(r.code, 0);
  assert.match(r.stderr, /Invalid timeout/);
});

test('a non-http URL is rejected', async () => {
  const r = await cli(['--url', 'ftp://example.com'], { WRIT_API_KEY: 'wt_x' });
  assert.notEqual(r.code, 0);
  assert.match(r.stderr, /Invalid --url/);
});

test('with no --url the target is Writ Cloud at api.usewrit.app/mcp', async () => {
  const r = await cli([], { WRIT_API_KEY: 'wt_x' });
  assert.match(r.stderr, /target: Writ Cloud/);
  assert.match(r.stderr, /https:\/\/api\.usewrit\.app\/mcp/);
});

test('a base URL gets /mcp appended; an /mcp URL is used verbatim', async () => {
  const base = await cli([], { WRIT_API_KEY: 'wt_x', WRIT_COORDINATOR_URL: 'https://writ.example.com' });
  assert.match(base.stderr, /https:\/\/writ\.example\.com\/mcp\b/);

  const exact = await cli([], { WRIT_API_KEY: 'wt_x', WRIT_COORDINATOR_URL: 'https://writ.example.com/mcp' });
  assert.match(exact.stderr, /https:\/\/writ\.example\.com\/mcp\b/);
  assert.doesNotMatch(exact.stderr, /\/mcp\/mcp/);

  const slug = await cli([], { WRIT_API_KEY: 'wt_x', WRIT_COORDINATOR_URL: 'https://mcp.usewrit.app/mcp/my-tools' });
  assert.match(slug.stderr, /target: published MCP endpoint/);
  assert.match(slug.stderr, /\/mcp\/my-tools/);
});

test('a trailing slash does not produce a doubled path', async () => {
  const r = await cli([], { WRIT_API_KEY: 'wt_x', WRIT_COORDINATOR_URL: 'https://writ.example.com/' });
  assert.match(r.stderr, /https:\/\/writ\.example\.com\/mcp\b/);
});

test('flags beat environment variables', async () => {
  const r = await cli(['--url', 'https://from-flag.example.com'], {
    WRIT_API_KEY: 'wt_x', WRIT_COORDINATOR_URL: 'https://from-env.example.com',
  });
  assert.match(r.stderr, /from-flag\.example\.com/);
});

// Each of these is a distinct way the Bearer key can leak. All must be loud.
test('plaintext HTTP to a non-loopback host warns about the key in the clear', async () => {
  const r = await cli([], { WRIT_API_KEY: 'wt_x', WRIT_COORDINATOR_URL: 'http://writ.example.com' });
  assert.match(r.stderr, /UNENCRYPTED/);
});

test('plaintext HTTP to localhost does NOT warn', async () => {
  const r = await cli([], { WRIT_API_KEY: 'wt_x', WRIT_COORDINATOR_URL: 'http://localhost:8000' });
  assert.doesNotMatch(r.stderr, /UNENCRYPTED/);
});

test('--insecure warns that certificate verification is off', async () => {
  const r = await cli(['--insecure'], { WRIT_API_KEY: 'wt_x', WRIT_COORDINATOR_URL: 'https://writ.example.com' });
  assert.match(r.stderr, /certificate verification is DISABLED/);
});

test('--api-key warns that the key is visible in ps', async () => {
  const r = await cli(['--api-key', 'wt_x', '--url', 'https://writ.example.com']);
  assert.match(r.stderr, /`ps`/);
});

test('WRIT_API_KEY alone produces no ps warning', async () => {
  const r = await cli([], { WRIT_API_KEY: 'wt_x', WRIT_COORDINATOR_URL: 'https://writ.example.com' });
  assert.doesNotMatch(r.stderr, /`ps`/);
});

test('the package ships zero runtime dependencies', () => {
  assert.equal(PKG.dependencies, undefined, 'writ-mcp must stay dependency-free');
  assert.equal(PKG.devDependencies, undefined, 'tests use node:test only');
});

// REGRESSION: `WRIT_API_KEY=$(cat key.txt)` and pasting from the app's UI both
// bring whitespace. Untrimmed, the newline reaches Node's header writer, which
// throws ERR_INVALID_CHAR inside EVERY request — surfacing to the user as
// "Cannot reach <target> (Invalid character in header content)", i.e. blaming
// the network for a credential that is one character off.
test('a key with surrounding whitespace is trimmed, not sent verbatim', async () => {
  const srv = await startServer((msg) => ({ body: { jsonrpc: '2.0', id: msg.id, result: { ok: true } } }));
  const r = await runConnector({
    args: ['--url', srv.url], env: { WRIT_API_KEY: '  wt_padded_key\n' }, expect: 1,
    send: [{ jsonrpc: '2.0', id: 1, method: 'ping' }],
  });
  await srv.close();

  assert.equal(srv.requests[0].headers.authorization, 'Bearer wt_padded_key');
  assert.ok(r.lines[0].result, 'the request must succeed, not fail as unreachable');
});

test('a "Bearer <key>" value is accepted without doubling the scheme', async () => {
  const srv = await startServer((msg) => ({ body: { jsonrpc: '2.0', id: msg.id, result: { ok: true } } }));
  const r = await runConnector({
    args: ['--url', srv.url], env: { WRIT_API_KEY: 'Bearer wt_prefixed' }, expect: 1,
    send: [{ jsonrpc: '2.0', id: 1, method: 'ping' }],
  });
  await srv.close();
  assert.equal(srv.requests[0].headers.authorization, 'Bearer wt_prefixed');
});

test('a key with an interior control character is rejected at startup, with the cause named', async () => {
  const r = await cli([], { WRIT_API_KEY: 'wt_bad\tkey', WRIT_COORDINATOR_URL: 'https://writ.example.com' });
  assert.equal(r.code, 1);
  assert.match(r.stderr, /cannot be sent in an HTTP header/);
  assert.doesNotMatch(r.stderr, /Cannot reach/);
});

// MCP clients persist a server's stderr to a log file on disk (Claude Code
// writes mcp-logs-<name>/*.jsonl), so anything echoed here is written down.
test('credentials embedded in --url are never echoed into the client log', async () => {
  const r = await cli([], {
    WRIT_API_KEY: 'wt_x',
    WRIT_COORDINATOR_URL: 'https://someone:hunter2@writ.example.com',
  });
  assert.doesNotMatch(r.stderr, /hunter2/, 'the password must not reach stderr');
  assert.match(r.stderr, /credentials embedded in the URL are ignored/);
});
