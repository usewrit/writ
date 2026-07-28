
// ─────────────────────────────────────────────────────────────────────────────
// Documentation links.
//
// The coordinator does NOT embed documentation — there is no in-app /docs route
// and there should not be one. Docs live on the public site and are versioned,
// searched, and translated there; a copy shipped inside the container would go
// stale the moment it built. Every "read the docs" affordance in this UI is an
// ordinary external link that opens in the user's browser.
//
// Always route through `docsUrl()` so the base and the language hand-off stay
// in exactly one place.
// ─────────────────────────────────────────────────────────────────────────────

// Overridable at build time (`VITE_DOCS_BASE=… npm run build`). A fork that
// keeps its own documentation — or an operator building the SPA before the
// upstream docs site is reachable — points this at their own base and every
// "read the docs" link in the UI follows, rather than sending readers somewhere
// that does not answer.
export const DOCS_BASE =
  import.meta.env.VITE_DOCS_BASE || 'https://github.com/usewrit/writ/wiki';

/**
 * Absolute URL for a docs page.
 *
 * @param path    Section path, e.g. 'api' or 'workflows/steps'. Omit for the index.
 * @param anchor  Optional #fragment.
 *
 * NOTE: no `?lang=` hand-off. The wiki has no localized tree, so appending one
 * only produced `…/wiki?lang=fr` on every link. If VITE_DOCS_BASE is pointed at
 * a site that does have localized docs, reinstate it there.
 */
/**
 * Call sites ask for a topic, not a filename. Mapping here means a page can be
 * renamed or a docs host swapped without touching every component — and a topic
 * with no page yet lands on the index rather than a 404.
 */
const PAGES: Record<string, string> = {
  api: 'REST-API',
  authentication: 'Personas-and-Secrets',
  agents: 'Connecting-Agents',
  workflows: 'Workflows',
  monitors: 'Monitors',
  crawl: 'Crawling',
  documents: 'Documents-and-OCR',
  mcp: 'MCP',
  configuration: 'Configuration',
  deployment: 'Production-Deployment',
  security: 'Security-Model',
  troubleshooting: 'Troubleshooting',
};

export const docsUrl = (path?: string, anchor?: string): string => {
  const topic = (path || '').replace(/^\/+|\/+$/g, '');
  const page = topic ? PAGES[topic] : undefined;
  let url = page ? `${DOCS_BASE}/${page}` : DOCS_BASE;
  if (anchor) url += `#${anchor}`;
  return url;
};

/** Spread onto an <a> so docs always open in a new tab, never in the app shell. */
export const DOCS_LINK_PROPS = {
  target: '_blank',
  rel: 'noopener noreferrer',
} as const;

/** Imperative equivalent, for click handlers that can't be a plain <a>. */
export const openDocs = (path?: string, anchor?: string): void => {
  window.open(docsUrl(path, anchor), '_blank', 'noopener,noreferrer');
};
