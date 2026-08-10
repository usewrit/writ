import React from 'react';

/**
 * A compact, dependency-free Markdown renderer that builds React elements (never
 * dangerouslySetInnerHTML), so it is XSS-safe by construction: all text is
 * rendered as React text nodes (auto-escaped) and link/image hrefs are
 * scheme-checked (and resolved against an optional base URL).
 *
 * Tuned for the real markdown our crawler emits (trafilatura/readability output),
 * so it handles the cases that trip a naive renderer:
 *   - ATX (`#`) AND setext (text underlined with `===` / `---`) headings — the
 *     converter emits setext headings under links, which a naive renderer would
 *     mistake for horizontal rules.
 *   - Linked images `[![alt](src)](href)` — article cards in listing pages.
 *   - HTML entities (`&amp;`, `&#39;`, …) and backslash escapes (`\*`, `\_`).
 * Plus the usual: bold (`**`), italic (`*`), inline/fenced code, links, images,
 * autolinks, blockquotes, unordered/ordered lists, horizontal rules, GFM tables,
 * and paragraphs. Underscore emphasis is intentionally NOT supported so
 * snake_case data is safe.
 */

const SAFE_SCHEME = /^(https?:|mailto:)/i;

/** Resolve a possibly-relative URL against the document's base, then scheme-check
 *  it. Returns undefined for anything not safe to link/embed. */
export function safeHref(href: string, base?: string): string | undefined {
  let h = href.trim();
  if (!h) return undefined;
  if (h.startsWith('#')) return h;
  // Protocol-relative → https.
  if (h.startsWith('//')) h = `https:${h}`;
  // Resolve relative refs (e.g. "/o/sf", "img/foo.webp") against the page URL.
  if (base && !/^[a-z][a-z0-9+.-]*:/i.test(h)) {
    try {
      h = new URL(h, base).href;
    } catch {
      return undefined;
    }
  }
  if (SAFE_SCHEME.test(h)) return h;
  return undefined;
}

// ── Text decoding: HTML entities + backslash escapes. Safe because output is a
// React text node (never innerHTML). ─────────────────────────────────────────
const NAMED_ENTITIES: Record<string, string> = {
  amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ',
  hellip: '…', mdash: '—', ndash: '–', laquo: '«', raquo: '»',
  copy: '©', reg: '®', trade: '™', deg: '°', middot: '·', bull: '•',
  lsquo: '‘', rsquo: '’', ldquo: '“', rdquo: '”',
};

function decodeEntities(s: string): string {
  if (s.indexOf('&') === -1) return s;
  return s.replace(/&(#x?[0-9a-f]+|[a-z][a-z0-9]*);/gi, (m, code: string) => {
    if (code[0] === '#') {
      const n = code[1] === 'x' || code[1] === 'X' ? parseInt(code.slice(2), 16) : parseInt(code.slice(1), 10);
      return Number.isFinite(n) && n > 0 ? String.fromCodePoint(n) : m;
    }
    const key = code.toLowerCase();
    return key in NAMED_ENTITIES ? NAMED_ENTITIES[key] : m;
  });
}

// Backslash escapes must be neutralised BEFORE inline scanning — otherwise `\*`
// would either stay literal-with-slash or (if unescaped first) wrongly turn on
// emphasis. We swap `\X` for a private-use sentinel + X so the escaped punctuation
// is invisible to the emphasis/link regexes, then restore the bare X in decode().
// Escaped punctuation is mapped into a Private-Use codepoint (0xE000 + charcode)
// so the actual `*`/`[`/\u2026 disappears from the scanning text entirely \u2014 a sentinel
// merely placed *beside* the char wouldn't stop the emphasis regex from matching
// it. restoreEscapes maps the PUA codepoints back to their ASCII punctuation.
const ESC_BASE = 0xe000;
const ESC_RANGE = /[\uE000-\uE07F]/g;
function protectEscapes(s: string): string {
  if (s.indexOf('\\') === -1) return s;
  return s.replace(/\\([\\`*_{}[\]()#+\-.!>~|"'])/g, (_m, c: string) =>
    String.fromCharCode(ESC_BASE + c.charCodeAt(0)),
  );
}
function restoreEscapes(s: string): string {
  return s.replace(ESC_RANGE, (c) => String.fromCharCode(c.charCodeAt(0) - ESC_BASE));
}

/** Decode entities + restore escaped punctuation on a literal text run. */
function decode(s: string): string {
  return decodeEntities(restoreEscapes(s));
}

// ── Images ───────────────────────────────────────────────────────────────────────────
// The app's CSP is `img-src 'self' data: blob: …` — deliberately NOT widened to
// arbitrary remote hosts, so a crawled page's <img> pointing at its origin CANNOT
// load here. Rendering one anyway produced the browser's broken-image chrome for
// every picture in a crawled article.
//
// So an image is only EMBEDDED when its URL is one the CSP actually permits
// (data: / blob: inline bytes, or our own origin). Anything else degrades to its
// alt text — the caption the page's author wrote — which is real information
// rather than a broken tile. Both markdown `![](…)` and raw-HTML <img> take this
// same path, so the two never diverge.
const INLINE_DATA_SCHEME = /^(data:image\/|blob:)/i;

/** The URL to actually put in an <img src>, or undefined when the CSP would block
 *  it (→ caller falls back to alt text). Relative refs resolve against `base`. */
function embeddableImageSrc(src: string, base?: string): string | undefined {
  const raw = src.trim();
  if (!raw) return undefined;
  // Inline bytes are allowed by the CSP verbatim — and carry no third-party fetch.
  if (INLINE_DATA_SCHEME.test(raw)) return raw;
  const abs = safeHref(raw, base);
  if (!abs) return undefined;
  // Same-origin only: 'self' in the CSP.
  try {
    if (typeof window !== 'undefined' && new URL(abs).origin === window.location.origin) return abs;
  } catch {
    return undefined;
  }
  return undefined;
}

/** `src`/`alt` out of a raw HTML <img …> tag. Attribute VALUES only — never markup —
 *  so the result still flows through React text/props and can't inject anything.
 *  `data-src` is honoured because lazy-loading crawled pages leave `src` empty. */
function parseImgTag(tag: string): { src: string; alt: string } {
  const attr = (name: string): string => {
    const m = new RegExp(`\\b${name}\\s*=\\s*("([^"]*)"|'([^']*)'|([^\\s>]+))`, 'i').exec(tag);
    return m ? (m[2] ?? m[3] ?? m[4] ?? '') : '';
  };
  return { src: attr('src') || attr('data-src') || attr('data-original'), alt: attr('alt') };
}

// linkedImage(1 alt,2 src,3 href) | image(4 alt,5 src) | link(6 text,7 href)
// | bold(8) | italic(9) | code(10) | autolink(11)
// | htmlLinkedImage(12 href,13 tag) | htmlImage(14 tag)
//
// The two HTML forms are LAST so they never pre-empt markdown syntax, but they
// still win over the autolink rule for a tag like `<img src="https://…">`: the
// tag starts at `<`, which is left of the bare URL, and JS regex alternation
// takes the leftmost match. Without them that URL matched the autolink branch and
// the rest of the tag fell through as literal text — which is exactly how an
// HTML-embedded image rendered as raw `<img src="` gibberish.
const INLINE_SRC = [
  /\[!\[([^\]]*)\]\(([^)\s]+)[^)]*\)\]\(([^)\s]+)[^)]*\)/,
  /!\[([^\]]*)\]\(([^)\s]+)[^)]*\)/,
  /\[([^\]]+)\]\(([^)\s]+)[^)]*\)/,
  /\*\*([^*]+)\*\*/,
  /\*([^*\n]+)\*/,
  /`([^`]+)`/,
  /(https?:\/\/[^\s)]+)/,
  /<a\b[^>]*\bhref\s*=\s*["']([^"']*)["'][^>]*>\s*(<img\b[^>]*>)\s*<\/a>/,
  /(<img\b[^>]*\/?>)/,
]
  .map((r) => r.source)
  .join('|');

const IMG_CLASS = 'my-1 max-h-64 max-w-full rounded-lg border border-border object-contain';

/** An image, or — when the CSP would block its URL — the alt text standing in for it.
 *  `href` (when present) keeps the surrounding link clickable either way, so a
 *  non-embeddable image is still a way to reach the thing it depicted. */
function LinkedImage({ alt, src, href, k }: { alt: string; src?: string; href?: string; k: string }): React.ReactNode {
  const label = decode(alt);
  const img = src ? (
    <img src={src} alt={label} loading="lazy" referrerPolicy="no-referrer" className={IMG_CLASS} />
  ) : label ? (
    <span>{label}</span>
  ) : null;
  if (!href) return <React.Fragment key={k}>{img}</React.Fragment>;
  // A link with no image AND no alt would render as an empty, unclickable anchor —
  // fall back to the destination so the reference is never silently dropped.
  return (
    <a key={k} href={href} target="_blank" rel="noopener noreferrer" className="inline-block">
      {img ?? <span className="underline decoration-border underline-offset-2">{href}</span>}
    </a>
  );
}

function renderInline(input: string, keyBase: string, base?: string): React.ReactNode[] {
  // Neutralise backslash escapes up front so `\*`/`\[` can't trigger emphasis or
  // links; decode() restores the bare punctuation on every emitted text run.
  const text = protectEscapes(input);
  const nodes: React.ReactNode[] = [];
  let last = 0;
  let i = 0;
  let m: RegExpExecArray | null;
  // A fresh regex per call: renderInline recurses (link text), so a shared
  // global regex's lastIndex would be clobbered by the inner call.
  const re = new RegExp(INLINE_SRC, 'g');
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) nodes.push(decode(text.slice(last, m.index)));
    const tok = m[0];
    const k = `${keyBase}-${i++}`;
    if (m[2] !== undefined || m[1] !== undefined) {
      // Linked image [![alt](src)](href)
      nodes.push(<LinkedImage key={k} alt={m[1] || ''} src={embeddableImageSrc(m[2], base)} href={safeHref(m[3], base)} k={k} />);
    } else if (m[5] !== undefined) {
      const src = embeddableImageSrc(m[5], base);
      nodes.push(
        src ? (
          <img key={k} src={src} alt={decode(m[4] || '')} loading="lazy" referrerPolicy="no-referrer" className={IMG_CLASS} />
        ) : (
          decode(m[4] || '')
        ),
      );
    } else if (m[13] !== undefined) {
      // Raw HTML linked image: <a href="…"><img src="…" alt="…"></a>
      const { src, alt } = parseImgTag(m[13]);
      nodes.push(<LinkedImage key={k} alt={alt} src={embeddableImageSrc(src, base)} href={safeHref(m[12] || '', base)} k={k} />);
    } else if (m[14] !== undefined) {
      // Raw HTML image: <img src="…" alt="…">
      const { src, alt } = parseImgTag(m[14]);
      const embed = embeddableImageSrc(src, base);
      nodes.push(
        embed ? (
          <img key={k} src={embed} alt={decode(alt)} loading="lazy" referrerPolicy="no-referrer" className={IMG_CLASS} />
        ) : (
          decode(alt)
        ),
      );
    } else if (m[7] !== undefined) {
      const href = safeHref(m[7], base);
      nodes.push(
        href ? (
          <a key={k} href={href} target="_blank" rel="noopener noreferrer" className="font-medium text-ink underline decoration-border underline-offset-2 transition-colors hover:decoration-ink">
            {renderInline(m[6], `${k}i`, base)}
          </a>
        ) : (
          decode(m[6])
        ),
      );
    } else if (m[8] !== undefined) {
      nodes.push(<strong key={k} className="font-semibold text-ink">{decode(m[8])}</strong>);
    } else if (m[9] !== undefined) {
      nodes.push(<em key={k}>{decode(m[9])}</em>);
    } else if (m[10] !== undefined) {
      nodes.push(
        <code key={k} className="rounded bg-canvas px-1 py-0.5 font-mono text-[0.85em] text-ink">
          {restoreEscapes(m[10])}
        </code>,
      );
    } else if (m[11] !== undefined) {
      const href = safeHref(m[11], base);
      nodes.push(
        href ? (
          <a key={k} href={href} target="_blank" rel="noopener noreferrer" className="text-ink underline decoration-border underline-offset-2 break-all hover:decoration-ink">
            {m[11]}
          </a>
        ) : (
          m[11]
        ),
      );
    } else {
      nodes.push(decode(tok));
    }
    last = m.index + tok.length;
  }
  if (last < text.length) nodes.push(decode(text.slice(last)));
  return nodes;
}

const HEADING_CLASS: Record<number, string> = {
  1: 'mt-5 mb-2 text-[17px] font-semibold leading-snug tracking-tight text-ink first:mt-0',
  2: 'mt-5 mb-2 text-[15px] font-semibold leading-snug text-ink first:mt-0',
  3: 'mt-4 mb-1.5 text-[13.5px] font-semibold text-ink first:mt-0',
  4: 'mt-3 mb-1 text-[12.5px] font-semibold text-ink first:mt-0',
  5: 'mt-3 mb-1 text-[12px] font-semibold text-secondary first:mt-0',
  6: 'mt-3 mb-1 text-[11px] font-semibold uppercase tracking-wide text-tertiary first:mt-0',
};

function Heading({ level, text, k, base }: { level: number; text: string; k: string; base?: string }): React.ReactNode {
  const Tag = (`h${Math.min(level, 6)}` as unknown) as keyof JSX.IntrinsicElements;
  return (
    <Tag key={k} className={HEADING_CLASS[Math.min(level, 6)]}>
      {renderInline(text, `${k}t`, base)}
    </Tag>
  );
}

function splitRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((c) => c.trim());
}

function renderTable(header: string[], rows: string[][], key: string, base?: string): React.ReactNode {
  return (
    <div key={key} className="my-3 overflow-x-auto rounded-lg border border-border">
      <table className="w-full border-collapse text-[12px]">
        <thead>
          <tr className="bg-canvas">
            {header.map((h, idx) => (
              <th key={idx} className="border-b border-border px-3 py-2 text-left font-semibold text-secondary">
                {renderInline(h, `${key}-h${idx}`, base)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, ri) => (
            <tr key={ri} className="border-t border-border first:border-t-0 odd:bg-canvas/40">
              {header.map((_, ci) => (
                <td key={ci} className="px-3 py-2 align-top text-ink">
                  {renderInline(r[ci] ?? '', `${key}-r${ri}c${ci}`, base)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// A line of only `=` (setext H1) or `-` (setext H2), CommonMark-style.
const SETEXT = /^\s{0,3}(=+|-+)\s*$/;
const BLOCK_START = /^\s*(#{1,6}\s|[-*+]\s|\d+\.\s|>\s|```)/;

function parseBlocks(src: string, base?: string): React.ReactNode[] {
  const lines = src.replace(/\r\n/g, '\n').split('\n');
  const out: React.ReactNode[] = [];
  let i = 0;
  let key = 0;
  const nextKey = () => `b${key++}`;

  while (i < lines.length) {
    const line = lines[i];

    if (/^\s*$/.test(line)) {
      i++;
      continue;
    }

    // Fenced code block
    const fence = line.match(/^\s*```(\w*)\s*$/);
    if (fence) {
      const buf: string[] = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
        buf.push(lines[i]);
        i++;
      }
      i++; // closing fence
      out.push(
        <pre key={nextKey()} className="my-3 overflow-x-auto rounded-lg border border-border bg-canvas p-3 text-[12px] leading-relaxed">
          <code className="font-mono text-ink">{buf.join('\n')}</code>
        </pre>,
      );
      continue;
    }

    // ATX heading
    const h = line.match(/^\s*(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (h) {
      out.push(<Heading key={nextKey()} level={h[1].length} text={h[2]} k={nextKey()} base={base} />);
      i++;
      continue;
    }

    // Horizontal rule (thematic break: 3+ of - * _ on their own line)
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      out.push(<hr key={nextKey()} className="my-4 border-border" />);
      i++;
      continue;
    }

    // Blockquote
    if (/^\s*>\s?/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ''));
        i++;
      }
      out.push(
        <blockquote key={nextKey()} className="my-3 border-l-2 border-signal/40 pl-3.5 text-secondary [&>*]:my-1">
          {parseBlocks(buf.join('\n'), base)}
        </blockquote>,
      );
      continue;
    }

    // GFM table (header row + |---|---| separator)
    if (
      /\|/.test(line) &&
      i + 1 < lines.length &&
      /^\s*\|?[\s:|-]*-[\s:|-]*$/.test(lines[i + 1])
    ) {
      const header = splitRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && /\|/.test(lines[i]) && !/^\s*$/.test(lines[i])) {
        rows.push(splitRow(lines[i]));
        i++;
      }
      out.push(renderTable(header, rows, nextKey(), base));
      continue;
    }

    // Unordered list
    if (/^\s*[-*+]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*+]\s+/, ''));
        i++;
      }
      out.push(
        <ul key={nextKey()} className="my-2 list-disc space-y-1 pl-5 marker:text-tertiary">
          {items.map((it, idx) => (
            <li key={idx}>{renderInline(it, `li${idx}`, base)}</li>
          ))}
        </ul>,
      );
      continue;
    }

    // Ordered list
    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ''));
        i++;
      }
      out.push(
        <ol key={nextKey()} className="my-2 list-decimal space-y-1 pl-5 marker:text-tertiary">
          {items.map((it, idx) => (
            <li key={idx}>{renderInline(it, `oli${idx}`, base)}</li>
          ))}
        </ol>,
      );
      continue;
    }

    // Paragraph — gather until a blank line or a new block starts. A trailing
    // setext underline (`===` / `---`) turns the gathered text into a heading.
    const buf: string[] = [];
    while (i < lines.length && !/^\s*$/.test(lines[i]) && !BLOCK_START.test(lines[i])) {
      if (buf.length > 0 && SETEXT.test(lines[i])) break;
      buf.push(lines[i]);
      i++;
    }
    if (i < lines.length && buf.length > 0 && SETEXT.test(lines[i])) {
      const level = lines[i].trim()[0] === '=' ? 1 : 2;
      i++; // consume the underline
      out.push(<Heading key={nextKey()} level={level} text={buf.join(' ')} k={nextKey()} base={base} />);
      continue;
    }
    out.push(
      <p key={nextKey()} className="my-2 leading-[1.7] first:mt-0 last:mb-0">
        {renderInline(buf.join('\n'), nextKey(), base)}
      </p>,
    );
  }

  return out;
}

export const Markdown: React.FC<{ source: string; baseUrl?: string }> = ({ source, baseUrl }) => (
  <div className="text-[13px] text-ink [&_a]:break-words">{parseBlocks(source, baseUrl)}</div>
);

export default Markdown;
