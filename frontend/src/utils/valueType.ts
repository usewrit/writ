import { stripFrontmatter } from './frontmatter';

/**
 * Classify an extracted field value so the UI can pick the right viewer:
 * JSON objects/arrays as table-or-raw, markdown/text rendered, URLs as links,
 * image links as thumbnails, numbers/booleans formatted. Pure, no rendering.
 */

export type ValueKind =
  | 'empty'
  | 'boolean'
  | 'number'
  | 'url'
  | 'image'
  | 'json'
  | 'markdown'
  | 'text';

const IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)(\?.*)?$/i;

export function isHttpUrl(s: string): boolean {
  const t = s.trim();
  return /^https?:\/\/\S+$/i.test(t) && !/\s/.test(t);
}

export function isImageUrl(s: string): boolean {
  return isHttpUrl(s) && IMAGE_EXT.test(s.trim());
}

/** Parse a string that holds JSON (object/array). Returns undefined otherwise. */
export function tryParseJson(s: string): unknown | undefined {
  const t = s.trim();
  if (t.length < 2) return undefined;
  const first = t[0];
  if (first !== '{' && first !== '[') return undefined;
  try {
    const parsed = JSON.parse(t);
    return typeof parsed === 'object' && parsed !== null ? parsed : undefined;
  } catch {
    return undefined;
  }
}

// Heuristic markdown detection — block markers (#, lists, quotes, fences, GFM
// tables) or inline emphasis/links. Deliberately ignores `_underscore_` so
// snake_case identifiers in plain data aren't mistaken for markdown.
const MD_BLOCK = /(^|\n)\s{0,3}(#{1,6}\s|[-*+]\s|\d+\.\s|>\s|```|\|.*\|)/;
const MD_INLINE = /\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|!\[[^\]]*\]\([^)]+\)/;
// Our crawler prefixes every page's markdown with a `---` YAML frontmatter block.
const MD_FRONTMATTER = /^ ?---[ \t]*\r?\n[\s\S]*?\r?\n---[ \t]*(\r?\n|$)/;

export function looksLikeMarkdown(s: string): boolean {
  return MD_FRONTMATTER.test(s) || MD_BLOCK.test(s) || MD_INLINE.test(s);
}

export interface Classified {
  kind: ValueKind;
  /** Present when kind === 'json' — the object/array to render (parsed if it came from a string). */
  json?: unknown;
}

export function classifyValue(v: unknown): Classified {
  if (v === null || v === undefined || v === '') return { kind: 'empty' };
  if (typeof v === 'boolean') return { kind: 'boolean' };
  if (typeof v === 'number') return { kind: 'number' };
  if (typeof v === 'object') return { kind: 'json', json: v };
  if (typeof v === 'string') {
    const s = v.trim();
    const parsed = tryParseJson(s);
    if (parsed !== undefined) return { kind: 'json', json: parsed };
    if (isImageUrl(s)) return { kind: 'image' };
    if (isHttpUrl(s)) return { kind: 'url' };
    if (looksLikeMarkdown(v)) return { kind: 'markdown' };
    return { kind: 'text' };
  }
  return { kind: 'text' };
}

/** One-line preview text for a value in a dense table cell. Frontmatter is
 *  stripped first so a crawled page previews its content, not its `---` header. */
export function previewText(v: unknown): string {
  if (v === null || v === undefined) return '';
  if (typeof v === 'string') return stripFrontmatter(v).replace(/\s+/g, ' ').trim();
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}
