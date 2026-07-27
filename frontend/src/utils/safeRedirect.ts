/**
 * Validation for navigation targets that came from somewhere untrusted —
 * today that means `?redirect=` on the login page, which anyone can craft.
 *
 * WHY THIS IS NOT JUST `raw.startsWith('/')`:
 *
 * The obvious check — "starts with `/`, but not `//`" — is bypassable, and was
 * bypassed here. `/\evil.com` satisfies both conditions, but browsers (and
 * React Router's own path normalisation) treat a backslash as equivalent to a
 * forward slash, so it resolves as the protocol-relative URL `//evil.com` and
 * navigates off-site. That is CVE-2025-68470 and its follow-up bypass,
 * GHSA-wrjc-x8rr-h8h6, which affect every React Router below 7.18.
 *
 * So this does not enumerate bad prefixes at all. It resolves the candidate
 * against the current origin and refuses anything that does not land back on
 * it — the same decision the browser will make, made before we navigate rather
 * than after. That is why it keeps working no matter which router version is
 * installed, and why it does not need revisiting the next time someone finds a
 * new way to spell "//".
 */

/** Paths a post-login redirect must never target, to avoid a bounce loop. */
const BLOCKED_PREFIXES = ['/login', '/setup', '/logout'];

/**
 * Return `raw` if it is a safe same-origin path, otherwise `fallback`.
 *
 * @param raw       Candidate path, typically from a query parameter. Anything
 *                  at all may be passed: null, an absolute URL, a scheme.
 * @param fallback  Returned whenever `raw` is not usable. Must itself be a
 *                  trusted literal — it is not validated.
 * @param origin    The origin to resolve against. Defaults to the live one;
 *                  parameterised so it can be exercised without a browser.
 */
export function safeInternalPath(
  raw: string | null | undefined,
  fallback = '/',
  origin: string = typeof window !== 'undefined' ? window.location.origin : 'http://localhost',
): string {
  if (!raw) return fallback;

  // A control character (or a raw newline) can smuggle a second value past a
  // downstream consumer, and no legitimate path contains one.
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(raw)) return fallback;

  let resolved: URL;
  try {
    resolved = new URL(raw, origin);
  } catch {
    return fallback; // not a resolvable reference at all
  }

  // The decisive check: after the browser's own parsing rules have been
  // applied, does it still point at us? `//evil.com`, `/\evil.com`,
  // `https://evil.com` and `javascript:…` all fail here.
  if (resolved.origin !== new URL(origin).origin) return fallback;

  const path = resolved.pathname + resolved.search + resolved.hash;
  if (!path.startsWith('/')) return fallback;

  // Compare on the normalised path, so `/LOGIN` or `/login/../login` cannot
  // slip through a raw-string prefix test and restart the redirect loop.
  const lower = resolved.pathname.toLowerCase();
  if (BLOCKED_PREFIXES.some((p) => lower === p || lower.startsWith(`${p}/`))) return fallback;

  return path;
}
