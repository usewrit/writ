import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import { InformationCircleIcon } from '@heroicons/react/24/outline';
import { SectionHead } from '../common/SectionHead';
import { Button } from '../ui/Button';
import { NumberInput, Switch } from '../ui';
import { apiErrorMessage } from '../../api/client';
import { getRuntimeSettings, updateRuntimeSettings, type RuntimeSettings } from '../../api/settings';

/**
 * Runtime — the coordinator-local execution governor. Only relevant because the
 * coordinator can also run browsers directly; remote fleet agents carry their
 * own runtime. Governor knobs (concurrency / RAM / headless) are boot-fixed →
 * a restart-to-apply banner shows when they change; monitor floors apply live.
 */
const GOVERNOR_KEYS: (keyof RuntimeSettings)[] = [
  'max_concurrent_runs',
  'max_background_runs',
  'rss_soft_watermark_mb',
  'browser_headless',
];

export const RuntimeSection: React.FC = () => {
  const { t } = useTranslation();
  const [data, setData] = useState<RuntimeSettings | null>(null);
  const [saved, setSaved] = useState<RuntimeSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getRuntimeSettings()
      .then((r) => {
        setData(r);
        setSaved(r);
      })
      .catch(() => toast.error(t('Failed to load runtime settings')))
      .finally(() => setLoading(false));
  }, []);

  const set = <K extends keyof RuntimeSettings>(key: K, value: RuntimeSettings[K]) => {
    setData((d) => (d ? { ...d, [key]: value } : d));
  };

  const governorChanged =
    !!data && !!saved && GOVERNOR_KEYS.some((k) => data[k] !== saved[k]);

  const handleSave = async () => {
    if (!data) return;
    setSaving(true);
    try {
      const next = await updateRuntimeSettings(data);
      setData(next);
      setSaved(next);
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
        description={t('Execution limits for browsers the coordinator runs directly. Remote fleet agents use their own runtime.')}
      />

      {governorChanged && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-300/60 bg-amber-50 px-4 py-3 text-[13px] text-amber-800">
          <InformationCircleIcon className="w-4 h-4 mt-0.5 shrink-0" />
          <span>{t('Concurrency, RAM watermark and headless mode apply after a coordinator restart.')}</span>
        </div>
      )}

      <div className="border-t border-border pt-4 space-y-5">
        {/* Concurrency governor. Floors only — this IS the operator's own governor,
            so an upper bound here would just be the app second-guessing the person
            who owns the hardware. The RAM watermark below is the real safety net. */}
        <div className="grid @pair/stage:grid-cols-2 gap-4">
          <div>
            <label className="block text-[13px] font-medium text-ink mb-1">{t('Max concurrent runs')}</label>
            <NumberInput min={1} value={data.max_concurrent_runs} onChange={(v) => set('max_concurrent_runs', v ?? 1)} />
          </div>
          <div>
            <label className="block text-[13px] font-medium text-ink mb-1">{t('Max background runs')}</label>
            <NumberInput min={0} value={data.max_background_runs} onChange={(v) => set('max_background_runs', v ?? 0)} />
          </div>
        </div>

        <div>
          <label className="block text-[13px] font-medium text-ink mb-1">{t('Soft RAM watermark (MB)')}</label>
          <NumberInput min={0} step={128} value={data.rss_soft_watermark_mb} onChange={(v) => set('rss_soft_watermark_mb', v ?? 0)} />
          <p className="text-xs text-tertiary mt-1">{t('0 disables the watermark. When exceeded, new runs wait for headroom.')}</p>
        </div>

        <Switch
          checked={data.browser_headless}
          onChange={(next) => set('browser_headless', next)}
          label={t('Run browsers headless')}
          description={t('Off shows a visible browser window (only useful for local debugging).')}
          reverse
        />

        <div className="border-t border-border pt-5">
          <p className="text-[11px] font-semibold uppercase tracking-wider text-tertiary mb-3">{t('Check interval floors (apply live)')}</p>
          <div className="grid @pair/stage:grid-cols-2 gap-4">
            <div>
              <label className="block text-[13px] font-medium text-ink mb-1">{t('Min content-check interval (s)')}</label>
              <NumberInput min={30} step={10} value={data.min_content_check_interval_s} onChange={(v) => set('min_content_check_interval_s', v ?? 30)} />
              <p className="text-xs text-tertiary mt-1">{t('Hard floor: 30s.')}</p>
            </div>
            <div>
              <label className="block text-[13px] font-medium text-ink mb-1">{t('Min browser-check interval (s)')}</label>
              <NumberInput min={60} step={30} value={data.min_browser_check_interval_s} onChange={(v) => set('min_browser_check_interval_s', v ?? 60)} />
              <p className="text-xs text-tertiary mt-1">{t('Hard floor: 60s.')}</p>
            </div>
          </div>
        </div>

        <div className="flex justify-end">
          <Button onClick={handleSave} loading={saving}>{t('Save')}</Button>
        </div>
      </div>
    </div>
  );
};

export default RuntimeSection;
