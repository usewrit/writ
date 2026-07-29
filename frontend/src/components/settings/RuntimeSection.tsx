import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import { SectionHead } from '../common/SectionHead';
import { Button } from '../ui/Button';
import { NumberInput } from '../ui';
import { apiErrorMessage } from '../../api/client';
import { getRuntimeSettings, updateRuntimeSettings, type RuntimeSettings } from '../../api/settings';

/**
 * Runtime — how many scheduled runs the coordinator dispatches at once.
 *
 * This section used to carry five more knobs (max background runs, a soft RAM
 * watermark, a headless toggle, and two monitor interval floors) under the
 * premise that "the coordinator can also run browsers directly". It cannot — the
 * coordinator launches no browser, ever; agents do. All five were persisted,
 * rendered, and read by nothing, so they are gone rather than reworded.
 *
 * The one that survived was ALSO dead, for a subtler reason: the scheduler looked
 * for a top-level Config row literally keyed `max_concurrent_runs`, while this
 * form writes the whole section as one JSON row under `coordinator_runtime`. It
 * now reads the section, so the number here finally governs dispatch — and it
 * does so live, per scheduler tick, which is why the old "applies after a
 * restart" banner is gone too.
 */
export const RuntimeSection: React.FC = () => {
  const { t } = useTranslation();
  const [data, setData] = useState<RuntimeSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getRuntimeSettings()
      .then(setData)
      .catch(() => toast.error(t('Failed to load runtime settings')))
      .finally(() => setLoading(false));
  }, [t]);

  const handleSave = async () => {
    if (!data) return;
    setSaving(true);
    try {
      setData(await updateRuntimeSettings(data));
      toast.success(t('Runtime settings saved'));
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to save runtime settings')));
    } finally {
      setSaving(false);
    }
  };

  if (loading || !data) {
    return <div className="text-sm text-secondary">{t('Loading...')}</div>;
  }

  return (
    <div className="space-y-6">
      <SectionHead
        title={t('Runtime')}
        description={t('How much scheduled work this coordinator dispatches at once. The browsers themselves run on your agents — set each agent’s capacity in Fleet.')}
      />

      <div className="border-t border-border pt-4 space-y-5">
        <div>
          <label className="block text-[13px] font-medium text-ink mb-1">{t('Max concurrent runs')}</label>
          {/* Floor only. This IS the operator's own governor, so an upper bound
              would just be the app second-guessing the person who owns the fleet;
              real backpressure comes from live agent capacity. */}
          <NumberInput min={1} value={data.max_concurrent_runs} onChange={(v) => setData({ ...data, max_concurrent_runs: v ?? 1 })} />
          <p className="text-xs text-tertiary mt-1">
            {t('Scheduled workflows dispatched at the same time. Applies immediately — runs already in flight are unaffected.')}
          </p>
        </div>

        <div className="flex justify-end">
          <Button onClick={handleSave} loading={saving}>{t('Save')}</Button>
        </div>
      </div>
    </div>
  );
};

export default RuntimeSection;
