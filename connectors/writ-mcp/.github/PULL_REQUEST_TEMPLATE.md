## What this changes

## Why

<!-- The comments in index.js justify decisions rather than restate code. Please
     do the same here: what failure mode does this fix, or what does it enable? -->

## Checklist

- [ ] `npm test` passes
- [ ] A test covers the change — one that **fails before it**
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`
- [ ] No new runtime, dev, optional or peer dependencies <!-- CI fails on any -->
- [ ] No tool logic added — the connector stays a transparent proxy
- [ ] If `package.json`'s `files` list changed, that was deliberate

## Verified against

<!-- Delete what does not apply. A real target is worth more than the mock. -->

- [ ] The mock server in `test/`
- [ ] A self-hosted coordinator
- [ ] Writ Cloud
- [ ] A published `/mcp/<slug>` endpoint

MCP client(s) tested with:
