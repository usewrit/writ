#!/usr/bin/env node
/**
 * check-ui-radius — fail the build on a segmented control whose selected pill
 * does not nest inside its container.
 *
 * WHY THIS EXISTS
 *
 * `tailwind.config.js` overrides the radius scale (sm 8, DEFAULT 12, lg 16,
 * xl 24) but never defines `md`, so `rounded-md` silently falls through to
 * Tailwind's stock 6px — a value that belongs to no other element in the
 * design. Pair it with a `rounded-lg` container and you get a 6px pill inside a
 * 16px shell: a rectangle floating in a stadium. It shipped that way in the
 * secret-creation modal and in four other switchers, and nothing caught it,
 * because each line is individually plausible.
 *
 * THE RULE
 *
 * A segmented control — a flex row with small padding (`p-0.5`/`p-1`) acting as
 * a track for selectable pills — must use the SAME radius token on the track
 * and the pill. In practice that means `rounded-full` on both: a stadium inside
 * a stadium nests correctly at any height and any padding, so it cannot drift
 * when someone later changes `py-*`. Matching any other token is accepted too,
 * since equal radii with small padding read as concentric.
 *
 * Run: node scripts/check-ui-radius.mjs [srcDir]
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const SRC = process.argv[2] || 'frontend/src';
const RADIUS = /rounded-(full|xl|lg|md|sm)\b/;
const RADIUS_G = /rounded-(full|xl|lg|md|sm)\b/g;
// A track: a flex row that carries its own small padding. Anything with a fixed
// size is an icon button, not a track.
const TRACK = (line) =>
  RADIUS.test(line) &&
  /\bp-(0\.5|1)\b/.test(line) &&
  /\bflex\b/.test(line) &&
  !/\bw-\d|\bh-\d/.test(line);

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (name.endsWith('.tsx')) out.push(p);
  }
  return out;
}

const problems = [];
for (const file of walk(SRC)) {
  const lines = readFileSync(file, 'utf8').split('\n');
  lines.forEach((line, i) => {
    if (!TRACK(line)) return;
    const track = line.match(RADIUS)[1];
    // The pill markup follows the track within a few lines.
    const window = lines.slice(i, i + 12).join('\n');
    const children = [...window.matchAll(RADIUS_G)].slice(1).map((m) => m[1]);
    const mismatched = [...new Set(children)].filter((c) => c !== track);
    if (mismatched.length) {
      problems.push(
        `  ${relative(process.cwd(), file)}:${i + 1}\n` +
          `      track is rounded-${track}, pill is ${mismatched
            .map((c) => `rounded-${c}`)
            .join(' / ')} — use rounded-full on both`
      );
    }
  });
}

if (problems.length) {
  console.error(
    `\n✗ segmented controls whose pill does not nest in its track (${problems.length})\n`
  );
  console.error(problems.join('\n'));
  console.error(
    '\nA selected pill must share its track\'s radius token. `rounded-md` is not\n' +
      'in this theme\'s scale (it falls back to 6px), so it is never the right\n' +
      'answer next to a rounded-lg/xl track.\n'
  );
  process.exit(1);
}
console.log('check-ui-radius OK — every segmented control nests correctly');
