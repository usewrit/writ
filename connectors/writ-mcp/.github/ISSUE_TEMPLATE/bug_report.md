---
name: Bug report
about: Something the connector does wrong — including a hang or an unhelpful error
title: ""
labels: bug
---

<!--
Please do NOT paste an API key. The connector's own startup banner redacts
credentials; redact anything you copy from elsewhere.

If this is a security issue, do not file it here — see SECURITY.md.
-->

## What happened

<!-- If your MCP client hangs, or shows an error you cannot act on, that IS a
     connector bug. Say so plainly. -->

## What you expected

## Reproduction

Steps, and the exact command or config block you used (with the key redacted):

```
```

## Connector stderr

Your MCP client writes the connector's stderr to a log file:

- **Claude Code** — `~/Library/Caches/claude-cli-nodejs/<project>/mcp-logs-<server-name>/` (macOS)
- **Claude Desktop** — `~/Library/Logs/Claude/mcp-server-<name>.log` (macOS)
- **Cursor** — the MCP panel's output pane

```
```

## Environment

- `writ-mcp` version (`npx writ-mcp --version`):
- Node version (`node --version`):
- MCP client and version:
- OS:
- Target: <!-- Writ Cloud / self-hosted coordinator / published /mcp/<slug> endpoint -->
- Self-host coordinator version, if applicable:
