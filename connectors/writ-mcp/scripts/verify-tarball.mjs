#!/usr/bin/env node
/*
 * Asserts that the publish tarball contains EXACTLY the files package.json
 * promises — no more, no less.
 *
 * This matters more than it looks. The package runs inside the user's MCP client
 * holding their API key, so a stray file in the tarball (a .env, a test fixture,
 * an editor backup) is a security event, not an aesthetic one. An over-broad
 * `files` list is the usual way that happens, and it is invisible until someone
 * unpacks what was published.
 *
 * Usage:  node scripts/verify-tarball.mjs [path/to/pack.json]
 *         npm pack --dry-run --json > /tmp/pack.json && node scripts/verify-tarball.mjs /tmp/pack.json
 *
 * With no argument it runs `npm pack --dry-run --json` itself.
 *
 * WHY THIS IS A SCRIPT AND NOT AN INLINE `node -e` IN EACH WORKFLOW:
 * it used to be inlined in three places (ci.yml, publish.yml, publish-mcp.sh),
 * which is exactly how they came to disagree — see the npm-12 note below. One
 * copy cannot drift from itself.
 */
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';

const WANT = ['CHANGELOG.md', 'LICENSE', 'README.md', 'index.js', 'package.json'];

const raw = process.argv[2]
  ? readFileSync(process.argv[2], 'utf8')
  : execFileSync('npm', ['pack', '--dry-run', '--json'], { encoding: 'utf8' });

let parsed;
try {
  parsed = JSON.parse(raw);
} catch (e) {
  console.error('could not parse npm pack output as JSON: ' + e.message);
  console.error(raw.slice(0, 400));
  process.exit(1);
}

// npm CHANGED THIS SHAPE. Up to npm 11 the payload is an array of package
// objects; from npm 12 it is an object keyed by package name. Reading `[0]`
// against npm 12 yields undefined, and the check dies with an unrelated
// TypeError about `.files` rather than saying anything useful — which is how it
// actually failed, on the first real publish dry-run. Accept both.
const entry = Array.isArray(parsed) ? parsed[0] : Object.values(parsed)[0];

if (!entry || !Array.isArray(entry.files)) {
  console.error('unrecognised `npm pack --json` output — no package entry with a files[] array.');
  console.error('npm version: ' + execFileSync('npm', ['--version'], { encoding: 'utf8' }).trim());
  console.error(JSON.stringify(parsed, null, 2).slice(0, 800));
  process.exit(1);
}

const files = entry.files.map((f) => f.path).sort();
const missing = WANT.filter((f) => !files.includes(f));
const extra = files.filter((f) => !WANT.includes(f));

if (missing.length) console.error('MISSING from tarball: ' + missing.join(', '));
if (extra.length) console.error('UNEXPECTED in tarball: ' + extra.join(', '));
if (missing.length || extra.length) process.exit(1);

console.log(`tarball ok (${entry.filename}, ${files.length} files): ${files.join(', ')}`);
