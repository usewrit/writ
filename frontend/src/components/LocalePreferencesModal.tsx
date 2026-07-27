import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal } from './ui/Modal';
import { Button } from './ui/Button';
import { LanguageSwitcher } from './LanguageSwitcher';
import { normalizeLanguage } from '../i18n';
import { updatePreferences } from '../api/settings';
import { applyServerPreferences } from '../utils/preferences';

// One-time first-run flag (UI-only, localStorage). Once set we never prompt
// again — language is changed later in Settings → General.
const KEY = 'writ_locale_onboarded';

export function needsLocaleOnboarding(): boolean {
  try { return localStorage.getItem(KEY) !== '1'; } catch { return false; }
}
function markLocaleOnboarded(): void {
  try { localStorage.setItem(KEY, '1'); } catch { /* private mode / quota */ }
}

/**
 * First-run language picker (self-host has no display currency). Shows once so a
 * new coordinator user sets their interface language up front rather than hunting
 * for a sidebar control. Reachable later in Settings → General.
 *
 * The pick is persisted to the coordinator (`PUT /settings/preferences`), not just to
 * this browser's i18next cache, so it follows the owner to their next browser. For the
 * same reason the prompt is suppressed when a stored preference already exists — the
 * flag above is per-browser, but the choice it stands for is not.
 */
export const LocalePreferencesModal: React.FC = () => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!needsLocaleOnboarding()) return;
    let cancelled = false;
    // applyServerPreferences() is single-flight and already in flight from the app's
    // boot effect, so this reuses that response instead of refetching.
    void applyServerPreferences().then((prefs) => {
      if (cancelled) return;
      if (normalizeLanguage(prefs?.language)) {
        markLocaleOnboarded(); // already chosen on another browser — don't ask again
        return;
      }
      setOpen(true);
    });
    return () => { cancelled = true; };
  }, []);

  const persist = (code: string) => {
    void updatePreferences({ language: code }).catch(() => {
      /* the local switch stands; Settings → General can retry */
    });
  };

  const close = () => { markLocaleOnboarded(); setOpen(false); };

  return (
    <Modal
      isOpen={open}
      onClose={close}
      size="sm"
      title={t('Welcome to Writ')}
      subtitle={t('Choose your language. You can change it anytime in Settings.')}
      footer={<Button onClick={close} className="w-full">{t('Continue')}</Button>}
    >
      <div>
        <label className="block text-xs font-medium text-secondary mb-1.5">{t('Language')}</label>
        <LanguageSwitcher className="block w-full" onChange={persist} />
      </div>
    </Modal>
  );
};

export default LocalePreferencesModal;
