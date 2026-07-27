import i18n from '../i18n';

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
  import.meta.env.VITE_DOCS_BASE || 'https://usewrit.app/docs';

/**
 * Absolute URL for a docs page.
 *
 * @param path    Section path, e.g. 'api' or 'workflows/steps'. Omit for the index.
 * @param anchor  Optional #fragment.
 *
 * The reader's UI language rides along as `?lang=`, which the site uses to send
 * them to the matching localized tree instead of always landing on English.
 */
export const docsUrl = (path?: string, anchor?: string): string => {
  const clean = (path || '').replace(/^\/+|\/+$/g, '');
  let url = clean ? `${DOCS_BASE}/${clean}` : DOCS_BASE;

  const lang = (i18n.resolvedLanguage || i18n.language || '').split('-')[0];
  if (lang && lang !== 'en') url += `?lang=${encodeURIComponent(lang)}`;
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
