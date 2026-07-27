import React from 'react';
import { useTranslation } from 'react-i18next';
import { SUPPORTED_LANGUAGES, setLanguage } from '../i18n';

interface LanguageSwitcherProps {
  className?: string;
  /**
   * Called with the newly picked code AFTER the UI has switched. `setLanguage`
   * only updates i18next + its localStorage cache, so callers that want the
   * choice to follow the owner across browsers persist it here
   * (`PUT /settings/preferences`).
   */
  onChange?: (code: string) => void;
}

export const LanguageSwitcher: React.FC<LanguageSwitcherProps> = ({ className, onChange }) => {
  const { t, i18n } = useTranslation();

  return (
    <label className={className}>
      <span className="sr-only">{t('Language')}</span>
      <select
        value={i18n.resolvedLanguage || 'en'}
        onChange={(e) => {
          const code = e.target.value;
          void setLanguage(code);
          onChange?.(code);
        }}
        className="w-full px-2.5 py-2 border border-border rounded-lg bg-surface text-[13px] text-ink hover:border-ink/20 focus:outline-none focus:ring-2 focus:ring-ink/15 focus:border-ink/40 transition-colors"
        aria-label={t('Language')}
      >
        {SUPPORTED_LANGUAGES.map((lang) => (
          <option key={lang.code} value={lang.code}>
            {lang.label}
          </option>
        ))}
      </select>
    </label>
  );
};
