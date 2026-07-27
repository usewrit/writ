import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';

// Natural-language keys: the English source string IS the key, so `en`
// needs no dictionary (missing keys fall back to the key itself).
export const SUPPORTED_LANGUAGES = [
  { code: 'en', label: 'English' },
  { code: 'fr', label: 'Français' },
  { code: 'es', label: 'Español' },
] as const;

// The fr/es dictionaries are ~200 KB each. Importing them statically forced
// every user (including English-only ones) to download all of them in the
// initial bundle. Load only the active language on demand instead.
const localeLoaders: Record<string, () => Promise<{ default: Record<string, string> }>> = {
  fr: () => import('./locales/fr.json'),
  es: () => import('./locales/es.json'),
};

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {}, // populated on demand by ensureLanguage()
    fallbackLng: 'en',
    supportedLngs: SUPPORTED_LANGUAGES.map((l) => l.code),
    nonExplicitSupportedLngs: true,
    // English-string keys contain '.' and ':' freely.
    keySeparator: false,
    nsSeparator: false,
    interpolation: {
      // React already escapes rendered values.
      escapeValue: false,
    },
    detection: {
      order: ['localStorage', 'navigator'],
      caches: ['localStorage'],
      lookupLocalStorage: 'wt_language',
    },
    returnEmptyString: false,
  });

/**
 * Lazily fetch and register the dictionary for `lng` if we haven't already.
 * English needs no dictionary (keys are the English strings), so it's a no-op.
 */
export async function ensureLanguage(lng?: string): Promise<void> {
  const base = (lng || '').split('-')[0];
  const loader = localeLoaders[base];
  if (!loader || i18n.hasResourceBundle(base, 'translation')) return;
  try {
    const mod = await loader();
    i18n.addResourceBundle(base, 'translation', mod.default, true, true);
    // Re-emit so already-mounted components re-render with the loaded strings.
    if (i18n.language === base || i18n.resolvedLanguage === base) {
      await i18n.changeLanguage(base);
    }
  } catch {
    /* network/parse failure → stay on English key fallback */
  }
}

/** Normalize any tag to a supported base code (`fr-CA` → `fr`), or null if unsupported. */
export function normalizeLanguage(lng: string | null | undefined): string | null {
  const base = (lng || '').split('-')[0].trim().toLowerCase();
  return SUPPORTED_LANGUAGES.some((l) => l.code === base) ? base : null;
}

/**
 * Switch languages, loading the target dictionary first. This updates i18n + the
 * localStorage detector cache only; persisting the choice to the coordinator
 * (`PUT /settings/preferences`) is the caller's job, so this stays usable before
 * the owner has signed in.
 */
export async function setLanguage(lng: string): Promise<void> {
  await ensureLanguage(lng);
  await i18n.changeLanguage(lng);
}

/**
 * Apply the owner's saved coordinator language on app load. The stored preference is
 * the source of truth across browsers and devices, so it wins over the
 * localStorage/navigator detection — but only when it's a real supported value
 * (null/unsupported → keep whatever the detector resolved). No-op when it already
 * matches. Does NOT write back (this is a read/apply, not a user action).
 */
export async function applyUserLanguage(userLanguage: string | null | undefined): Promise<void> {
  const target = normalizeLanguage(userLanguage);
  if (!target || target === i18n.resolvedLanguage) return;
  await setLanguage(target);
}

// Keep <html lang> in sync for a11y / screen readers.
i18n.on('languageChanged', (lng) => {
  document.documentElement.lang = lng;
});
document.documentElement.lang = i18n.resolvedLanguage || 'en';

// Load whatever language was detected on boot (English is a no-op, resolves
// instantly). Exposed so the app can hold first paint until the active
// dictionary is present — avoids a flash of English keys for fr/es users.
export const i18nReady: Promise<void> = ensureLanguage(i18n.resolvedLanguage);

export default i18n;
