'use strict';
/* Protocol behaviour: what the connector puts on stdout for what the server says. */

const { test } = require('node:test');
const assert = require('node:assert');
const { startServer, runConnector, INIT, okResult } = require('./helpers');

const KEY = { WRIT_API_KEY: 'wt_test_key' };

test('relays initialize and tools/list, and sends a Bearer header to /mcp', async () => {
  const srv = await startServer((msg) => {
    if (msg.method === 'initialize') {
      return { body: okResult(msg.id, { protocolVersion: '2025-06-18', serverInfo: { name: 'Mock', version: '1' }, capabilities: { tools: {} } }) };
    }
    if (msg.method === 'tools/list') {
      return { body: okResult(msg.id, { tools: [{ name: 'writ_list_workflows', inputSchema: { type: 'object' } }] }) };
    }
  });

  const r = await runConnector({
    args: ['--url', srv.url], env: KEY, expect: 2,
    send: [INIT, { jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} }],
  });
  await srv.close();

  assert.equal(r.lines.length, 2);
  assert.equal(r.lines[0].id, 1);
  assert.equal(r.lines[0].result.serverInfo.name, 'Mock');
  assert.equal(r.lines[1].result.tools[0].name, 'writ_list_workflows');

  assert.equal(srv.requests[0].url, '/mcp');
  assert.equal(srv.requests[0].headers.authorization, 'Bearer wt_test_key');
  assert.match(srv.requests[0].headers.accept, /application\/json/);
  assert.match(srv.requests[0].headers.accept, /text\/event-stream/);
});

// REGRESSION: an array's `.id` is undefined too. Reading that as "notification"
// dropped every batch response on the floor and hung the client on each id.
test('relays every response in a JSON-RPC batch', async () => {
  const srv = await startServer((msg) => {
    assert.ok(Array.isArray(msg), 'server should receive the batch as an array');
    return { body: msg.filter((m) => m.id !== undefined).map((m) => okResult(m.id, { echo: m.method })) };
  });

  const r = await runConnector({
    args: ['--url', srv.url], env: KEY, expect: 2,
    send: [[{ jsonrpc: '2.0', id: 6, method: 'ping' }, { jsonrpc: '2.0', id: 7, method: 'ping' }]],
  });
  await srv.close();

  assert.equal(r.lines.length, 2, 'both batch responses must reach stdout');
  assert.deepEqual(r.lines.map((l) => l.id).sort(), [6, 7]);
});

test('a batch that fails answers every id in it, so nothing hangs', async () => {
  const srv = await startServer(() => ({ status: 502, contentType: 'text/html', body: '<html>bad gateway</html>' }));

  const r = await runConnector({
    args: ['--url', srv.url], env: KEY, expect: 2,
    send: [[{ jsonrpc: '2.0', id: 10, method: 'ping' }, { jsonrpc: '2.0', id: 11, method: 'ping' }]],
  });
  await srv.close();

  assert.equal(r.lines.length, 2);
  assert.deepEqual(r.lines.map((l) => l.id).sort(), [10, 11]);
  for (const l of r.lines) assert.equal(l.error.code, -32603);
});

test('notifications are forwarded but produce no stdout', async () => {
  const srv = await startServer(() => ({ status: 202, body: null }));

  const r = await runConnector({
    args: ['--url', srv.url], env: KEY,
    send: [{ jsonrpc: '2.0', method: 'notifications/initialized' }],
  });
  await srv.close();

  assert.equal(srv.requests.length, 1, 'the notification must still reach the server');
  assert.equal(r.lines.length, 0, 'a notification has no reply');
});

test('202 with an id outstanding is reported, not swallowed', async () => {
  const srv = await startServer(() => ({ status: 202, body: null }));
  const r = await runConnector({
    args: ['--url', srv.url], env: KEY, expect: 1,
    send: [{ jsonrpc: '2.0', id: 3, method: 'ping' }],
  });
  await srv.close();

  assert.equal(r.lines.length, 1);
  assert.equal(r.lines[0].id, 3);
  assert.equal(r.lines[0].error.code, -32603);
});

test('204 No Content is treated like 202 (mcp-service answers 204)', async () => {
  const srv = await startServer(() => ({ status: 204, body: null }));
  const r = await runConnector({
    args: ['--url', srv.url], env: KEY,
    send: [{ jsonrpc: '2.0', method: 'notifications/cancelled' }],
  });
  await srv.close();
  assert.equal(r.lines.length, 0);
});

test('401 becomes an actionable -32001', async () => {
  const srv = await startServer(() => ({ status: 401, body: { detail: 'nope' } }));
  const r = await runConnector({
    args: ['--url', srv.url], env: KEY, expect: 1,
    send: [{ jsonrpc: '2.0', id: 4, method: 'tools/list' }],
  });
  await srv.close();

  assert.equal(r.lines[0].error.code, -32001);
  assert.match(r.lines[0].error.message, /Unauthorized/);
  assert.match(r.lines[0].error.message, /API keys/);
});

test('a non-JSON body becomes -32603 rather than a crash', async () => {
  const srv = await startServer(() => ({ status: 502, contentType: 'text/html', body: '<html>nginx</html>' }));
  const r = await runConnector({
    args: ['--url', srv.url], env: KEY, expect: 1,
    send: [{ jsonrpc: '2.0', id: 5, method: 'ping' }],
  });
  await srv.close();

  assert.equal(r.lines[0].error.code, -32603);
  assert.match(r.lines[0].error.message, /non-JSON/);
});

test('an unreachable target becomes -32002, not a hang', async () => {
  // Port 1 is reserved and refuses instantly.
  const r = await runConnector({
    args: ['--url', 'http://127.0.0.1:1'], env: KEY, expect: 1,
    send: [{ jsonrpc: '2.0', id: 9, method: 'ping' }],
  });
  assert.equal(r.lines[0].error.code, -32002);
  assert.match(r.lines[0].error.message, /Cannot reach/);
});

test('an SSE-framed response is decoded (spec allows it; we advertise it)', async () => {
  const srv = await startServer((msg) => ({
    contentType: 'text/event-stream',
    body: 'event: message\ndata: ' + JSON.stringify(okResult(msg.id, { via: 'sse' })) + '\n\n',
  }));
  const r = await runConnector({
    args: ['--url', srv.url], env: KEY, expect: 1,
    send: [{ jsonrpc: '2.0', id: 12, method: 'ping' }],
  });
  await srv.close();

  assert.equal(r.lines[0].id, 12);
  assert.equal(r.lines[0].result.via, 'sse');
});

test('a pinned Mcp-Session-Id and protocol version are echoed on later requests', async () => {
  const srv = await startServer((msg) => {
    if (msg.method === 'initialize') {
      return {
        headers: { 'Mcp-Session-Id': 'sess-abc123' },
        body: okResult(msg.id, { protocolVersion: '2025-06-18', serverInfo: { name: 'Mock', version: '1' }, capabilities: {} }),
      };
    }
    return { body: okResult(msg.id, { ok: true }) };
  });

  const r = await runConnector({
    args: ['--url', srv.url], env: KEY, expect: 2, staged: true,
    send: [INIT, { jsonrpc: '2.0', id: 2, method: 'ping' }],
  });
  await srv.close();

  assert.equal(r.lines.length, 2);
  const second = srv.requests[1];
  assert.equal(second.headers['mcp-session-id'], 'sess-abc123');
  assert.equal(second.headers['mcp-protocol-version'], '2025-06-18');
  assert.equal(srv.requests[0].headers['mcp-session-id'], undefined, 'no session on the first call');
});

test('tools/call is never retried — a workflow must not run twice', async () => {
  let calls = 0;
  const srv = await startServer((msg) => {
    calls++;
    return { status: 503, body: { error: 'busy' } };
  });

  const r = await runConnector({
    args: ['--url', srv.url], env: KEY, expect: 1,
    send: [{ jsonrpc: '2.0', id: 20, method: 'tools/call', params: { name: 'run_x', arguments: {} } }],
  });
  await srv.close();

  assert.equal(calls, 1, 'a side-effecting call must be sent exactly once');
  // …and the caller still gets an addressable failure rather than the server's
  // bare `{"error": "busy"}` body, which carries no id and would hang the client.
  assert.equal(r.lines.length, 1);
  assert.equal(r.lines[0].id, 20);
  assert.equal(r.lines[0].error.code, -32002);
});

test('a JSON body that is not a JSON-RPC response never reaches stdout raw', async () => {
  const srv = await startServer(() => ({ status: 429, body: { detail: 'rate limited' } }));
  const r = await runConnector({
    args: ['--url', srv.url], env: KEY, expect: 1,
    send: [{ jsonrpc: '2.0', id: 22, method: 'ping' }],
  });
  await srv.close();

  assert.equal(r.lines[0].id, 22, 'the reply must be addressable to the pending id');
  assert.match(r.lines[0].error.message, /not a JSON-RPC response/);
});

test('tools/list IS retried on a transient 503 and then succeeds', async () => {
  let calls = 0;
  const srv = await startServer((msg) => {
    calls++;
    if (calls < 3) return { status: 503, body: { error: 'busy' } };
    return { body: okResult(msg.id, { tools: [] }) };
  });

  const r = await runConnector({
    args: ['--url', srv.url], env: KEY, expect: 1, timeoutMs: 8000,
    send: [{ jsonrpc: '2.0', id: 21, method: 'tools/list' }],
  });
  await srv.close();

  assert.equal(calls, 3, 'read-only method should have been retried');
  assert.deepEqual(r.lines[0].result.tools, []);
});

test('an unparseable stdin line is dropped without killing the session', async () => {
  const srv = await startServer((msg) => ({ body: okResult(msg.id, { ok: true }) }));
  const r = await runConnector({
    args: ['--url', srv.url], env: KEY, expect: 1,
    send: ['{{{ not json\n', { jsonrpc: '2.0', id: 30, method: 'ping' }],
  });
  await srv.close();

  assert.match(r.err, /unparseable/);
  assert.equal(r.lines[0].id, 30, 'the session survives bad input');
});

test('stdout carries only JSON-RPC — diagnostics go to stderr', async () => {
  const srv = await startServer((msg) => ({ body: okResult(msg.id, { ok: true }) }));
  const r = await runConnector({
    args: ['--url', srv.url], env: KEY, expect: 1,
    send: [{ jsonrpc: '2.0', id: 40, method: 'ping' }],
  });
  await srv.close();

  for (const line of r.out.split('\n').filter(Boolean)) {
    assert.doesNotThrow(() => JSON.parse(line), 'every stdout line must be valid JSON');
  }
  assert.match(r.err, /\[writ-mcp\] target:/);
});

// stdin EOF must not kill a request that is still in flight — a workflow run can
// legitimately take minutes, and the client is waiting on its id.
test('in-flight requests drain before exit when stdin closes', async () => {
  const srv = await startServer((msg) => new Promise((resolve) => {
    setTimeout(() => resolve({ body: okResult(msg.id, { slow: true }) }), 400);
  }));

  const r = await runConnector({
    args: ['--url', srv.url], env: KEY, expect: 1,
    send: [{ jsonrpc: '2.0', id: 50, method: 'ping' }],
  });
  await srv.close();

  assert.equal(r.lines.length, 1, 'the slow reply must still arrive');
  assert.equal(r.lines[0].result.slow, true);
});

// REGRESSION: the connector used to relay whatever the server sent and stop
// there. A response that does not address an id we sent — a gateway serving a
// cached body, a proxy that rewrites the payload, a server answering a batch
// with one object — left the client blocked on that id forever. On `initialize`
// that means the MCP server never finishes starting and the client just reports
// "failed to connect" with nothing to go on.
test('an id the server never answers is backfilled, not left hanging', async () => {
  const srv = await startServer(() => ({
    // Well-formed JSON-RPC, wrong id.
    body: { jsonrpc: '2.0', id: 999, result: { ok: true } },
  }));

  const r = await runConnector({
    args: ['--url', srv.url], env: KEY, expect: 2,
    send: [INIT],
  });
  await srv.close();

  const forOne = r.lines.find((l) => l.id === 1);
  assert.ok(forOne, 'id 1 must be answered even though the server addressed id 999');
  assert.equal(forOne.error.code, -32603);
  assert.match(forOne.error.message, /no response for request id 1/);
  // The server's own message is still relayed — a POST response may legitimately
  // carry server-initiated traffic, so we add, never filter.
  assert.ok(r.lines.some((l) => l.id === 999), 'the server payload is still relayed');
});

test('a batch where the server answers only some ids backfills the rest', async () => {
  const srv = await startServer((msg) => ({
    body: [okResult(msg[0].id, { echo: 1 })],  // second id silently dropped
  }));

  const r = await runConnector({
    args: ['--url', srv.url], env: KEY, expect: 2,
    send: [[
      { jsonrpc: '2.0', id: 11, method: 'tools/list', params: {} },
      { jsonrpc: '2.0', id: 12, method: 'ping', params: {} },
    ]],
  });
  await srv.close();

  assert.ok(r.lines.find((l) => l.id === 11 && l.result), 'id 11 answered normally');
  const missing = r.lines.find((l) => l.id === 12);
  assert.ok(missing, 'id 12 must not hang');
  assert.match(missing.error.message, /no response for request id 12/);
});

// A 301/302 is what a reverse proxy doing http -> https returns. Node does not
// follow redirects (correctly — replaying the Authorization header to a new
// origin would hand the API key to a host the user never chose), so the body is
// empty and this used to surface as the useless "Empty response from ...".
test('a redirect is named and explained rather than reported as an empty response', async () => {
  const srv = await startServer(() => ({
    status: 301,
    headers: { Location: 'https://writ.example.com/mcp' },
    body: null,
  }));

  const r = await runConnector({
    args: ['--url', srv.url], env: KEY, expect: 1,
    send: [{ jsonrpc: '2.0', id: 3, method: 'tools/list', params: {} }],
  });
  await srv.close();

  assert.equal(r.lines[0].id, 3);
  assert.match(r.lines[0].error.message, /redirected \(HTTP 301\)/);
  assert.match(r.lines[0].error.message, /https:\/\/writ\.example\.com\/mcp/);
  assert.match(r.lines[0].error.message, /would leak it/);
});
