#!/usr/bin/env node
/**
 * i18n guard for the self-host web UI.
 *
 * The UI uses natural-language keys (the English string IS the key), so a missing
 * translation degrades silently to English instead of throwing. Nothing in `vite
 * build` or `tsc` catches that, which is how ~500 strings drifted out of fr/es.
 * This script is the gate.
 *
 * Checks:
 *   1. COVERAGE   — every string the UI can render has an fr and an es entry.
 *   2. FROZEN      — no `i18n.t()` at module top level. Those resolve once, when the
 *                    chunk is first imported, so they keep the language that was
 *                    active at import time and never follow a later switch. Data
 *                    catalogs must store the bare English key and let the render
 *                    site call `t(x.label)`.
 *   3. INTEGRITY   — fr and es share one key set, no blank values, and every
 *                    {{placeholder}} in a key survives into both translations.
 *
 * Usage:  node scripts/check-i18n.mjs [--frontend <dir>]
 * Exit 0 clean, 1 on any violation.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const argIdx = process.argv.indexOf('--frontend');
const FRONTEND = path.resolve(argIdx > -1 ? process.argv[argIdx + 1] : path.join(HERE, '..', 'frontend'));
const SRC = path.join(FRONTEND, 'src');
const LOCALES = path.join(SRC, 'i18n', 'locales');

if (!fs.existsSync(SRC)) {
  console.error(`check-i18n: no frontend source at ${SRC}`);
  process.exit(1);
}

let ts;
try {
  ts = createRequire(path.join(FRONTEND, 'package.json'))('typescript');
} catch {
  console.error('check-i18n: typescript not found — run `npm ci` in the frontend first.');
  process.exit(1);
}

// ---------------------------------------------------------------------------
// Data catalogs: modules that store bare English strings which a render site
// translates via t(x.label). Their labelish values are dictionary keys too.
// ---------------------------------------------------------------------------
const CATALOGS = new Set([
  'components/flows/blockCatalog.ts', 'components/toolrail/configs.tsx', 'components/steps/stepMeta.tsx',
  'components/flows/types.ts', 'components/Layout.tsx', 'components/flows/templates.ts',
  'components/flows/blocks/SourceBlockPicker.tsx', 'components/UnifiedTriggersModal.tsx',
  'components/wizard/steps/ModeSelectionStep.tsx', 'components/home/runLabel.ts',
  'pages/streaming/StreamingSessionPage.tsx', 'components/CommandPalette.tsx', 'pages/ApiKeys.tsx',
  'pages/automations/AutomationsListPage.tsx', 'pages/Files.tsx', 'components/StatusBadge.tsx',
  'components/wizard/panels/ApiWorkflowPanel.tsx', 'pages/workflows/detail/meta.ts',
  'components/wizard/steps/FinalizeStep.tsx', 'components/workflows/ExecutionTargetPicker.tsx',
  'pages/workflows/detail/ConnectTab.tsx', 'components/wizard/panels/ContentMonitorPanel.tsx',
  'components/wizard/panels/ExtractScrapePanel.tsx', 'components/workflows/PersonaWizard.tsx',
  'pages/RunsPage.tsx', 'pages/SecretsPage.tsx', 'components/library/PersonaDetailPane.tsx',
  'components/checks/CheckFastActions.tsx', 'components/library/NotificationList.tsx',
  'components/wizard/MonitorTargetsPanel.tsx', 'components/wizard/shared/WizardStepIndicator.tsx',
  'pages/developers/EndpointsPage.tsx', 'components/flows/presets.ts',
  'components/workflows/WorkflowFastActions.tsx', 'components/PlatformIcon.tsx',
  'components/settings/GeneralSection.tsx', 'pages/Settings.tsx', 'pages/developers/DevelopersPage.tsx',
  'utils/schedule.ts', 'components/schedule/SchedulePicker.tsx', 'pages/checks/CheckDetailPage.tsx',
  'onboarding/hints.ts', 'components/wizard/panels/SiteCrawlPanel.tsx', 'components/steps/StepTypePalette.tsx',
  'components/settings/NotificationsSection.tsx', 'pages/Setup.tsx', 'components/flows/blocks/FieldRef.tsx',
  'components/wizard/shared/StudioAppBar.tsx', 'components/flows/AiAutomationBar.tsx',
  'components/common/ActionMenu.tsx', 'components/library/WorkflowDetailPane.tsx',
]);

// `when` is deliberately absent: blockCatalog's `when` only feeds the AI prompt.
const LABELISH = new Set(['label', 'description', 'summary', 'help', 'hint', 'title', 'subtitle',
  'detail', 'blurb', 'badge', 'desc', 'body', 'name', 'group', 'activityTitle', 'shortLabel',
  'kindLabel', 'cloudOnlyReason', 'groupLabel', 'emptyText']);

function walk(dir, out = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, out);
    else if (/\.(ts|tsx)$/.test(e.name)) out.push(p);
  }
  return out;
}

/** Identifiers/CSS/urls and placeholder-only strings are never dictionary keys. */
function isTechnical(v) {
  const s = String(v).trim();
  if (!s || !/[A-Za-z]/.test(s)) return true;
  if (/^[a-z0-9_\-.:/#?=&%+]+$/.test(s)) return true;
  if (/^[a-z]+([A-Z][a-z0-9]*)+$/.test(s)) return true;
  if (/^(https?:)?\/\//.test(s)) return true;
  if (/^[A-Z]{2,6}$/.test(s)) return true;
  const CLS = /^(bg|text|border|ring|from|to|via|animate|rounded|opacity|shadow|hover|focus|dark)[-:]/;
  const toks = s.split(/\s+/);
  if (toks.length && toks.every((w) => CLS.test(w) || w === 'border')) return true;
  if (!/[A-Za-z]/.test(s.replace(/\{\{[^}]*\}\}/g, ''))) return true;
  return false;
}

/**
 * Extra rejects for bare strings in catalog ARRAYS / RETURNS, where there is no
 * property name to tell a label from a config value. Search-keyword lists, IANA
 * timezones, BCP-47 tags and user-agent strings all live in arrays like these.
 */
function isTechnicalBare(v) {
  if (isTechnical(v)) return true;
  const s = String(v).trim();
  if (/^Mozilla\/\d/.test(s)) return true;                      // user agents
  if (/^[A-Za-z]+\/[A-Za-z_]+(\/[A-Za-z_]+)?$/.test(s)) return true; // America/New_York
  if (/^[a-z]{2}(-[A-Za-z0-9]{2,4})+$/.test(s)) return true;    // en-US, zh-Hans
  if (s === s.toLowerCase()) return true;                       // lowercase search keywords
  return false;
}

function atModuleTopLevel(node) {
  let n = node.parent;
  while (n) {
    if (
      ts.isFunctionDeclaration(n) || ts.isFunctionExpression(n) || ts.isArrowFunction(n) ||
      ts.isMethodDeclaration(n) || ts.isGetAccessorDeclaration(n) || ts.isConstructorDeclaration(n)
    ) return false;
    if (ts.isSourceFile(n)) return true;
    n = n.parent;
  }
  return true;
}

const keys = new Set();
const frozen = [];

for (const file of walk(SRC)) {
  const rel = path.relative(SRC, file).split(path.sep).join('/');
  if (rel.startsWith('i18n/')) continue;
  const src = ts.createSourceFile(file, fs.readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const isCatalog = CATALOGS.has(rel);

  const visit = (node) => {
    if (ts.isCallExpression(node)) {
      const ex = node.expression;
      const isI18nDotT =
        ts.isPropertyAccessExpression(ex) && ex.name.text === 't' &&
        ts.isIdentifier(ex.expression) && /^i18n$/i.test(ex.expression.text);
      const name = ts.isIdentifier(ex) ? ex.text : ts.isPropertyAccessExpression(ex) ? ex.name.text : '';
      if (name === 't' && node.arguments.length) {
        const a = node.arguments[0];
        if (ts.isStringLiteral(a) || ts.isNoSubstitutionTemplateLiteral(a)) keys.add(a.text);
      }
      if (isI18nDotT && atModuleTopLevel(node)) {
        const { line } = src.getLineAndCharacterOfPosition(node.getStart());
        frozen.push(`${rel}:${line + 1}  ${node.getText().slice(0, 70)}`);
      }
    }
    // <Trans i18nKey="…"> — the key carries inline markup (<1>…</1>) and never
    // reaches a t() call, so it has to be picked up from the attribute.
    if (ts.isJsxAttribute(node) && node.name.getText() === 'i18nKey' && node.initializer) {
      const init = node.initializer;
      const lit = ts.isStringLiteral(init) ? init
        : ts.isJsxExpression(init) && init.expression && ts.isStringLiteral(init.expression) ? init.expression
        : null;
      if (lit) keys.add(lit.text);
    }
    if (isCatalog && ts.isPropertyAssignment(node)) {
      const k = ts.isIdentifier(node.name) || ts.isStringLiteral(node.name) ? node.name.text : '';
      const v = node.initializer;
      if (LABELISH.has(k) && (ts.isStringLiteral(v) || ts.isNoSubstitutionTemplateLiteral(v)) && !isTechnical(v.text)) {
        keys.add(v.text);
      }
    }
    // Catalog arrays of bare label strings (`const EXAMPLE_GOALS = ['Alert me…']`)
    // and label strings returned from catalog helpers (`scheduleError`), both of
    // which reach the UI through t(value).
    if (isCatalog && (ts.isArrayLiteralExpression(node) || ts.isReturnStatement(node))) {
      const lits = ts.isArrayLiteralExpression(node) ? node.elements : [node.expression];
      for (const e of lits) {
        if (e && (ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e)) && !isTechnicalBare(e.text)) {
          keys.add(e.text);
        }
      }
    }
    // Top-level `const X: Record<..., string> = { a: 'Label' }` catalogs.
    if (isCatalog && ts.isVariableDeclaration(node) && node.type && node.initializer &&
        node.parent?.parent?.parent && ts.isSourceFile(node.parent.parent.parent)) {
      if (/Record<[^>]*,\s*string\s*>/.test(node.type.getText()) && ts.isObjectLiteralExpression(node.initializer)) {
        for (const p of node.initializer.properties) {
          if (ts.isPropertyAssignment(p)) {
            const v = p.initializer;
            if ((ts.isStringLiteral(v) || ts.isNoSubstitutionTemplateLiteral(v)) && !isTechnical(v.text)) keys.add(v.text);
          }
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(src);
}

const fr = JSON.parse(fs.readFileSync(path.join(LOCALES, 'fr.json'), 'utf8'));
const es = JSON.parse(fs.readFileSync(path.join(LOCALES, 'es.json'), 'utf8'));
const placeholders = (s) => (String(s).match(/\{\{[^}]+\}\}/g) || []).sort().join(',');

const problems = [];
const report = (title, items) => {
  if (!items.length) return;
  problems.push(items.length);
  console.error(`\n✗ ${title} (${items.length})`);
  for (const i of items.slice(0, 25)) console.error(`    ${i}`);
  if (items.length > 25) console.error(`    … and ${items.length - 25} more`);
};

const missing = [...keys].sort().filter((k) => !(k in fr) || !(k in es));
const dumpIdx = process.argv.indexOf('--report-missing');
if (dumpIdx > -1) fs.writeFileSync(process.argv[dumpIdx + 1], JSON.stringify(missing, null, 2));
report('strings the UI renders with no fr/es translation', missing.map((k) => JSON.stringify(k)));
report(
  'i18n.t() at module top level — frozen at import, ignores a language switch',
  frozen,
);
report(
  'keys present in only one dictionary',
  [...Object.keys(fr).filter((k) => !(k in es)), ...Object.keys(es).filter((k) => !(k in fr))],
);
report(
  'blank translations',
  [...Object.entries(fr).filter(([, v]) => typeof v !== 'string' || !v.trim()).map(([k]) => `fr ${k}`),
   ...Object.entries(es).filter(([, v]) => typeof v !== 'string' || !v.trim()).map(([k]) => `es ${k}`)],
);
report(
  'placeholder mismatch between key and translation',
  [...Object.entries(fr).filter(([k, v]) => placeholders(k) !== placeholders(v)).map(([k]) => `fr ${JSON.stringify(k)}`),
   ...Object.entries(es).filter(([k, v]) => placeholders(k) !== placeholders(v)).map(([k]) => `es ${JSON.stringify(k)}`)],
);

if (problems.length) {
  console.error(`\ncheck-i18n FAILED — ${problems.reduce((a, b) => a + b, 0)} problem(s)\n`);
  process.exit(1);
}
console.log(`check-i18n OK — ${keys.size} UI strings, fr/es ${Object.keys(fr).length} entries each`);
