import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import { SparklesIcon } from '@heroicons/react/24/outline';
import { SectionHead } from '../common/SectionHead';
import { SourceOffer } from '../common/SourceOffer';
import { Button } from '../ui/Button';
import { Select } from '../ui';
import { SUPPORTED_LANGUAGES, setLanguage } from '../../i18n';
import { useTour } from '../../onboarding/TourProvider';
import { resetOnboarding, setGlobalStatus } from '../../onboarding/storage';
import { apiErrorMessage } from '../../api/client';
import { getPreferences, updatePreferences, type PreferencesSettings } from '../../api/settings';

/**
 * General — per-owner UI preferences: language (en/fr/es) and a re-run
 * onboarding action. Persisted to /settings/preferences; the language is also
 * applied to this session immediately.
 *
 * There is deliberately NO theme selector. The stored `theme` preference is
 * still read and applied at boot, so an existing choice is honoured — this
 * removes the control, not the capability.
 *
 * There is deliberately NO analytics switch here. One used to exist, promising
 * "sends anonymized usage metrics to help improve the app" — but nothing in the
 * coordinator ever read the flag, so the toggle claimed an upload that never
 * happened. A self-hosted build sends nothing (README: "no telemetry phoning
 * home"), so the honest UI is the absence of the control, not a control wired to
 * nothing. If that ever changes, the README claim has to change with it.
 */
export const GeneralSection: React.FC = () => {
  const { t, i18n } = useTranslation();
  const { startTour } = useTour();
  const [data, setData] = useState<PreferencesSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getPreferences()
      .then(setData)
      .catch(() => toast.error(t('Failed to load preferences')))
      .finally(() => setLoading(false));
  }, []);

  const persist = async (patch: Partial<PreferencesSettings>) => {
    setSaving(true);
    try {
      const next = await updatePreferences(patch);
      setData(next);
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to save preferences')));
    } finally {
      setSaving(false);
    }
  };

  const handleLanguage = (lng: string) => {
    setData((d) => (d ? { ...d, language: lng } : d));
    setLanguage(lng);
    persist({ language: lng });
  };

  const handleReRunOnboarding = () => {
    resetOnboarding();
    setGlobalStatus('pending');
    startTour('global');
    toast.success(t('Onboarding restarted'));
  };

  if (loading || !data) {
    return <div className="text-sm text-secondary">{t('Loading...')}</div>;
  }

  return (
    <div className="space-y-6">
      <SectionHead title={t('General')} description={t('Display language and privacy for this coordinator.')} />

      <div className="border-t border-border pt-4 space-y-5">
        <div className="grid @pair/stage:grid-cols-2 gap-4">
          <div>
            <label className="block text-[13px] font-medium text-ink mb-1">{t('Language')}</label>
            <Select
              value={data.language || i18n.resolvedLanguage || 'en'}
              onChange={(v) => handleLanguage(String(v))}
              options={SUPPORTED_LANGUAGES.map((l) => ({ value: l.code, label: l.label }))}
            />
          </div>
        </div>

        {saving && <p className="text-xs text-tertiary">{t('Saving…')}</p>}
      </div>

      <div className="border-t border-border pt-4 flex items-center justify-between gap-4">
        <div>
          <p className="text-[13px] font-medium text-ink">{t('Onboarding tour')}</p>
          <p className="text-xs text-tertiary mt-0.5">{t('Replay the guided tour of the coordinator.')}</p>
        </div>
        <Button variant="secondary" onClick={handleReRunOnboarding}>
          <SparklesIcon className="w-4 h-4" />
          {t('Re-run onboarding')}
        </Button>
      </div>

      <SourceOffer />
    </div>
  );
};

export default GeneralSection;
