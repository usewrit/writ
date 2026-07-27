/**
 * Owner UI preferences (language / theme) — the boot-time apply.
 *
 * These live in the coordinator DB (`GET|PUT /settings/preferences`) so they follow
 * the owner across browsers and devices. i18next's localStorage detector (`wt_language`)
 * and the `dark` class are only a per-browser CACHE of that value.
 *
 * Without this module the stored preference was write-only: Settings → General saved
 * `language`/`theme` to the coordinator but nothing ever read them back, so a new
 * browser (or one whose localStorage was cleared) silently fell back to the navigator
 * language and the light theme — and Settings then showed the *saved* value in its
 * dropdowns while the UI rendered the *detected* one.
 *
 * Precedence matches the desktop + cloud clients:
 *   explicit local choice (this session) > stored owner preference > navigator > en
 */
import { applyUserLanguage } from '../i18n';
import { getPreferences, type PreferencesSettings } from '../api/settings';

/**
 * Apply the theme preference to the document root. Also sets `color-scheme` so
 * native form controls / scrollbars match.
 */
export function applyTheme(theme: string | null | undefined): void {
  const root = document.documentElement;
  const prefersDark = window.matchMedia?.('(prefers-color-scheme: dark)').matches;
  const dark = theme === 'dark' || ((!theme || theme === 'system') && prefersDark);
  root.classList.toggle('dark', dark);
  root.style.colorScheme = dark ? 'dark' : 'light';
}

// Single-flight: several authenticated surfaces mount their own boot effect
// (the app layout and the standalone device-flow route), and React StrictMode
// double-invokes effects in development. One fetch serves them all.
let inFlight: Promise<PreferencesSettings | null> | null = null;

/**
 * Fetch the stored owner preferences once and apply language + theme to this session.
 * Requires an authenticated session, so call it from a protected surface. Never
 * throws — offline / not-yet-signed-in keeps whatever the detector resolved.
 */
export function applyServerPreferences(): Promise<PreferencesSettings | null> {
  if (inFlight) return inFlight;
  inFlight = (async () => {
    try {
      const prefs = await getPreferences();
      await applyUserLanguage(prefs.language);
      applyTheme(prefs.theme);
      return prefs;
    } catch {
      return null; // unauthenticated / offline — detector result stands
    }
  })();
  return inFlight;
}

/** Drop the cached fetch so the next authenticated surface re-reads (used on logout). */
export function resetPreferencesBoot(): void {
  inFlight = null;
}
