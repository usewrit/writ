'use strict';
/*
 * Test harness: a mock Streamable-HTTP MCP server plus a driver that runs the
 * REAL connector binary as a subprocess and speaks stdio JSON-RPC to it.
 *
 * Everything is exercised end to end through the same path an MCP client uses —
 * spawn, write newline-delimited JSON to stdin, read it back from stdout. No
 * module internals are reached into, because the wire behaviour IS the contract.
 */

const http = require('http');
const path = require('path');
const { spawn } = require('child_process');

const BIN = path.join(__dirname, '..', 'index.js');

/**
 * Start a mock server. `handler(msg, req)` returns:
 *   { status?, headers?, body? }   — body may be a string or a JSON value
 * or undefined for a default 200 JSON-RPC "method not found".
 * Resolves to { port, url, requests, close }.
 */
function startServer(handler) {
  const requests = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      let msg = null;
      try { msg = JSON.parse(raw); } catch (_) { /* leave null */ }
      requests.push({ method: req.method, url: req.url, headers: req.headers, raw, msg });

      const reply = (out) => {
        if (!out) {
          out = { status: 200, body: { jsonrpc: '2.0', id: msg && msg.id, error: { code: -32601, message: 'no handler' } } };
        }
        const status = out.status || 200;
        const headers = Object.assign(
          { 'Content-Type': out.contentType || 'application/json' },
          out.headers || {}
        );
        if (out.body === undefined || out.body === null) {
          res.writeHead(status, headers);
          return res.end();
        }
        const body = typeof out.body === 'string' ? out.body : JSON.stringify(out.body);
        res.writeHead(status, headers);
        res.end(body);
      };

      // Handlers may be async — the drain test needs a genuinely slow reply.
      Promise.resolve()
        .then(() => handler(msg, req, requests.length))
        .then(reply)
        .catch((err) => { res.writeHead(500); res.end(String(err && err.message)); });
    });
  });

  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      resolve({
        port,
        url: 'http://127.0.0.1:' + port,
        requests,
        close: () => new Promise((r) => server.close(r)),
      });
    });
  });
}

/**
 * Run the connector.
 *   opts.args      extra argv (array)
 *   opts.env       extra env vars
 *   opts.send      array of values written to stdin as JSON lines (strings are
 *                  written verbatim, so malformed input can be tested)
 *   opts.expect    resolve as soon as this many stdout lines have arrived
 *   opts.timeoutMs overall cap (default 5000)
 * Resolves to { out, err, lines, code }.
 */
function runConnector(opts) {
  const o = opts || {};
  const timeoutMs = o.timeoutMs || 5000;
  const expect = o.expect === undefined ? 0 : o.expect;

  return new Promise((resolve) => {
    const child = spawn(process.execPath, [BIN].concat(o.args || []), {
      env: Object.assign({}, process.env, { WRIT_API_KEY: '' }, o.env || {}),
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    let out = '';
    let err = '';
    let settled = false;
    const lines = () => out.split('\n').filter(Boolean).map((l) => JSON.parse(l));

    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { child.stdin.end(); } catch (_) { /* already closed */ }
      // Give the process a moment to exit cleanly, then report.
      const done = () => resolve({ out, err, lines: lines(), code: child.exitCode });
      if (child.exitCode !== null) return done();
      child.once('exit', done);
      setTimeout(() => { try { child.kill(); } catch (_) {} done(); }, 400);
    };

    const timer = setTimeout(finish, timeoutMs);

    const queue = (o.send || []).slice();
    const write = (m) => child.stdin.write(typeof m === 'string' ? m : JSON.stringify(m) + '\n');

    child.stdout.on('data', (d) => {
      out += d;
      const got = out.split('\n').filter(Boolean).length;
      // `staged` models a real MCP client, which waits for each reply before
      // sending the next request (notably: nothing goes out before initialize
      // returns, which is how session pinning can work at all).
      if (o.staged && queue.length) write(queue.shift());
      if (expect > 0 && got >= expect) {
        // Let any trailing write flush before we tear down.
        setTimeout(finish, 60);
      }
    });
    child.stderr.on('data', (d) => { err += d; });
    child.on('exit', () => { if (expect === 0) setTimeout(finish, 30); });

    if (o.staged) {
      if (queue.length) write(queue.shift());
    } else {
      while (queue.length) write(queue.shift());
    }
    if (!expect) child.stdin.end();
  });
}

const INIT = {
  jsonrpc: '2.0',
  id: 1,
  method: 'initialize',
  params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'test', version: '1' } },
};

const okResult = (id, result) => ({ jsonrpc: '2.0', id, result });

module.exports = { startServer, runConnector, BIN, INIT, okResult };
