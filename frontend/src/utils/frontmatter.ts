/**
 * Parse the leading YAML frontmatter our crawler prefixes onto every page's
 * markdown (see the agent's `_frontmatter`): a `---` fenced block of
 * `key: "value"` lines carrying title/url/description/author/site/dates. The UI
 * renders these as a document header (labels) instead of leaking them into the
 * body. Deliberately tiny — only the flat `key: scalar` shape we emit, not a
 * general YAML parser.
 */

export interface DocMeta {
  title?: string;
  url?: string;
  description?: string;
  author?: string;
  published?: string;
  site?: string;
  crawled_at?: string;
  [key: string]: string | undefined;
}

// A leading `---\n … \n---` block (optionally after a BOM), ending at the first
// closing fence. `[\s\S]*?` is lazy so the FIRST `---` line closes it.
const FM_RE = /^ ?---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/;

/** Strip the surrounding quotes from a YAML scalar and unescape `\"` / `\\`. */
function unquote(raw: string): string {
  const s = raw.trim();
  if (s.length >= 2 && s[0] === '"' && s[s.length - 1] === '"') {
    return s.slice(1, -1).replace(/\\(["\\])/g, '$1');
  }
  if (s.length >= 2 && s[0] === "'" && s[s.length - 1] === "'") {
    return s.slice(1, -1).replace(/''/g, "'");
  }
  return s;
}

export interface ParsedDoc {
  /** The parsed frontmatter, or null when the source has none. */
  meta: DocMeta | null;
  /** The markdown body with any frontmatter removed. */
  body: string;
}

export function parseFrontmatter(src: string): ParsedDoc {
  if (typeof src !== 'string') return { meta: null, body: src };
  const m = FM_RE.exec(src);
  if (!m) return { meta: null, body: src };
  const meta: DocMeta = {};
  for (const line of m[1].split(/\r?\n/)) {
    const mm = /^([A-Za-z0-9_-]+)\s*:\s*(.*)$/.exec(line);
    if (mm && mm[2].trim()) meta[mm[1]] = unquote(mm[2]);
  }
  const body = src.slice(m[0].length).replace(/^\s*\n/, '');
  return { meta: Object.keys(meta).length ? meta : null, body };
}

/** Just the body — frontmatter removed. Used for one-line previews. */
export function stripFrontmatter(src: string): string {
  return parseFrontmatter(src).body;
}
