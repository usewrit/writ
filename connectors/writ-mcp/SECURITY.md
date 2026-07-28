# Security

`writ-mcp` exists to carry a credential. It runs **inside your MCP client** —
Claude Code, Claude Desktop, Cursor — as a child process, holds your Writ API key
in memory, and puts it in an `Authorization` header on every request. That is its
entire job, so this document is mostly about the ways a key can escape and what
the connector does about each one.

## Reporting a vulnerability

Please report security issues privately through **GitHub Security Advisories**:
use the **"Report a vulnerability"** button under the
[Security tab of this repository](https://github.com/usewrit/writ-mcp/security/advisories/new).
Do **not** open a public issue or pull request for an undisclosed vulnerability.

What to expect:

- **Acknowledgement** within 3 business days.
- **Initial assessment** (severity + affected versions) within 7 business days.
- We coordinate a fix and disclosure timeline with you through the advisory
  thread, and credit reporters in the published advisory unless you prefer
  otherwise.

If the issue is in the **server** side — the tools themselves, the coordinator,
or Writ Cloud — report it against [`usewrit/writ`](https://github.com/usewrit/writ/security/advisories/new)
instead. This connector holds no tool logic.

### Supported versions

Security fixes are applied to the latest release line only.

| Version | Supported |
|---------|-----------|
| 1.x (latest release) | Yes |
| Older releases | No — please upgrade |

---

## Threat model

**What this process is trusted with:** one API key, and the contents of every
MCP request and response that passes through it.

**Who it talks to:** exactly one host — whatever `--url` names, defaulting to
Writ Cloud. There is no telemetry, no analytics, no update check, and no
third-party endpoint. The package has **zero dependencies**, so there is no
transitive code in the process that could add one. CI fails the build if
`dependencies`, `devDependencies`, `optionalDependencies` or `peerDependencies`
ever becomes non-empty.

**What it is not:** a security boundary between you and your own Writ instance.
The key is sent to the target you configured; anyone who can read that key can do
whatever its scopes allow. Scope your keys.

## How a key escapes, and what we do about it

| Exposure | Mitigation |
|---|---|
| **`--api-key` on the command line** | Readable by any local process via `ps`, and recorded in shell history. The connector prints a `NOTE` at startup whenever the key arrives this way. **Prefer `WRIT_API_KEY`.** |
| **Plaintext `http://` to a non-loopback host** | The key crosses the network unencrypted. The connector prints a `WARNING` naming the origin. Loopback targets do not warn — nothing leaves the machine. |
| **`--insecure` / `WRIT_INSECURE_TLS`** | Certificate verification is off, so a man-in-the-middle can read the key. The connector prints a `WARNING`. Use only against a trusted private network with a self-signed local CA; prefer `NODE_EXTRA_CA_CERTS`. |
| **Credentials embedded in `--url`** | `https://user:pass@host/` is a legal URL. They are **not** sent (Writ authenticates with the API key header), and they are **redacted from every diagnostic** — MCP clients persist a server's stderr to a log file on disk, so echoing the href verbatim would write the password down. The connector says the credentials were ignored. |
| **HTTP redirects** | **Never followed.** Resending the `Authorization` header to whatever origin a `Location` header names would hand your key to a host you did not choose. A 3xx becomes an actionable error telling you to point `--url` at the final URL. |
| **Diagnostics** | The key is never written to stdout or stderr. stdout carries JSON-RPC and nothing else. |

## Other hardening

- **Retries never double-run a workflow.** Only read-only methods (`initialize`,
  `ping`, `tools/list`, `resources/list`, `prompts/list`) are retried on a
  transient failure. `tools/call` is sent **exactly once**, because a retry could
  re-execute a side effect the connector cannot see. The single exception is a
  connection refused outright — the request provably never arrived.
- **Responses are bounded** at 32 MB. A broken or hostile endpoint cannot grow
  this process until the OS kills your MCP session.
- **A malformed key is rejected at startup**, with the cause named, rather than
  failing inside every request as a misleading "cannot reach the server".
- **No request is left unanswered.** Every JSON-RPC id the client is blocked on
  gets a response — including when the server returns a body that addresses none
  of them, answers only part of a batch, or hands back a proxy error page. A
  hung MCP client is a denial of service on your assistant, and it is the failure
  mode this connector works hardest to make impossible.

## Verifying what you install

Releases are published from CI with
[npm provenance](https://docs.npmjs.com/generating-provenance-statements), so the
tarball is cryptographically linked to the commit and workflow that built it:

```bash
npm audit signatures
```

The published tarball contains exactly five files — `index.js`, `package.json`,
`README.md`, `LICENSE`, `CHANGELOG.md`. CI asserts that list on every run, so
anything else appearing in it is a signal worth reporting.
