/**
 * Client-side serialization of extracted-data records (an array of flat
 * objects) into the formats the Data view offers for selected rows: CSV, TSV
 * (paste straight into Sheets/Excel), a Markdown table, and pretty JSON. Used
 * for both the "export selected" download and the "copy" actions, so a user's
 * selection stays usable outside the app.
 */

export type RecordRow = Record<string, unknown>;

/** Stringify one cell value: scalars as-is, objects/arrays as compact JSON. */
function cellToString(v: unknown): string {
  if (v === null || v === undefined) return '';
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

/** Column union across the rows, declared columns first (in order), then extras. */
export function unionColumns(rows: RecordRow[], declared: string[] = []): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const c of declared) {
    if (!seen.has(c)) {
      seen.add(c);
      out.push(c);
    }
  }
  for (const r of rows) {
    for (const k of Object.keys(r)) {
      if (!seen.has(k)) {
        seen.add(k);
        out.push(k);
      }
    }
  }
  return out;
}

function csvField(s: string): string {
  // Quote when the value contains a comma, quote, or newline; double inner quotes.
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function recordsToCsv(rows: RecordRow[], columns: string[]): string {
  const header = columns.map(csvField).join(',');
  const lines = rows.map((r) => columns.map((c) => csvField(cellToString(r[c]))).join(','));
  return [header, ...lines].join('\r\n');
}

export function recordsToTsv(rows: RecordRow[], columns: string[]): string {
  // Tabs/newlines inside a cell would break the grid on paste — flatten them.
  const clean = (s: string) => s.replace(/[\t\r\n]+/g, ' ').trim();
  const header = columns.join('\t');
  const lines = rows.map((r) => columns.map((c) => clean(cellToString(r[c]))).join('\t'));
  return [header, ...lines].join('\n');
}

export function recordsToMarkdown(rows: RecordRow[], columns: string[]): string {
  const esc = (s: string) => s.replace(/\|/g, '\\|').replace(/[\r\n]+/g, ' ').trim();
  const header = `| ${columns.join(' | ')} |`;
  const sep = `| ${columns.map(() => '---').join(' | ')} |`;
  const lines = rows.map((r) => `| ${columns.map((c) => esc(cellToString(r[c]))).join(' | ')} |`);
  return [header, sep, ...lines].join('\n');
}

export function recordsToJson(rows: RecordRow[]): string {
  return JSON.stringify(rows, null, 2);
}
