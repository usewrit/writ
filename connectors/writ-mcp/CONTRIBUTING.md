# Contributing to writ-mcp

Thanks for your interest. This package is deliberately tiny — one file, no
dependencies, no build step — so the contribution loop is short.

## Setup

```bash
git clone https://github.com/usewrit/writ-mcp
cd writ-mcp
npm test
```

That is the whole setup. There is nothing to install: the package has zero
dependencies and the test suite uses Node's built-in `node:test` runner. You need
**Node 18 or newer**.

## Running the tests

```bash
npm test
```

The suite drives the real `index.js` as a **subprocess** against a mock MCP
server, speaking newline-delimited JSON-RPC over stdin/stdout — the same path a
real MCP client uses. No module internals are reached into, because the wire
behaviour *is* the contract. It runs in about six seconds.

`npm publish` runs the suite automatically via `prepublishOnly`.

### Testing against a real Writ instance

The mock server proves protocol behaviour; a real coordinator proves the whole
thing works. Start a [self-host coordinator](https://github.com/usewrit/writ),
create an API key under **Settings → Developers → API keys**, then:

```bash
claude mcp add writ-dev -e WRIT_API_KEY=<YOUR_API_KEY> -- node ./index.js --url http://localhost:8000
```

`claude mcp list` will tell you whether it connected.

## The two rules

**1. Zero dependencies. Permanently.**

This process runs inside the user's MCP client holding their API key. Every
dependency is code with access to that key, arriving through a supply chain
nobody in this repository controls. A PR that adds one will not be merged — the
test suite and CI both fail on a non-empty `dependencies`, `devDependencies`,
`optionalDependencies` or `peerDependencies`, and that is intentional friction.

If you need a helper, write it in `index.js`.

**2. No tool logic lives here.**

The connector is a transparent stdio↔HTTP proxy. Tools, schemas, validation and
business rules all live server-side, so the connector can never drift from the
app and an old install never has to be upgraded to see a new tool. If your change
teaches the connector what a tool *means*, it belongs in
[`usewrit/writ`](https://github.com/usewrit/writ) instead.

## What a good pull request looks like

- **A test that fails before your change.** Every behaviour in `test/` maps to a
  real failure mode; several are marked `REGRESSION` because they encode a bug
  that shipped once. Add yours in that style.
- **A comment explaining *why*, not *what*.** The existing comments justify
  decisions — why `tools/call` is never retried, why an array's `.id` cannot be
  used to detect a notification. Match that.
- **A `CHANGELOG.md` entry** under `## [Unreleased]`.
- **No new files in the published tarball** unless you mean it. `package.json`'s
  `files` list is asserted by CI.

## Reporting bugs

A connector bug usually looks like *the client hangs* or *the client shows an
unhelpful error*. Both are in scope and both are taken seriously — see the issue
template. Include:

- what MCP client, and what Node version (`node --version`);
- the connector's **stderr**, which your client writes to a log file (Claude Code:
  `~/Library/Caches/claude-cli-nodejs/<project>/mcp-logs-<name>/`);
- whether the target is Writ Cloud, a self-hosted coordinator, or a published
  `/mcp/<slug>` endpoint.

**Never paste an API key** into an issue. Redact it — the stderr banner already
does.

## Security issues

Do not open a public issue. See [`SECURITY.md`](./SECURITY.md).

## Releasing (maintainers)

1. Move `## [Unreleased]` entries into a new version section in `CHANGELOG.md`.
2. Bump `version` in `package.json`.
3. Tag it: `git tag v1.1.0 && git push origin v1.1.0`.

The tag triggers `.github/workflows/publish.yml`, which refuses to publish if the
tag and `package.json` disagree, verifies the tarball's file list, and publishes
with npm provenance.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](./LICENSE), the same terms that cover the rest of this package.
