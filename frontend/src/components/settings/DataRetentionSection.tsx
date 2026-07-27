import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import { TrashIcon } from '@heroicons/react/24/outline';
import { SectionHead } from '../common/SectionHead';
import { Button } from '../ui/Button';
import { NumberInput } from '../ui';
import { apiErrorMessage } from '../../api/client';
import { getDataSettings, updateDataSettings, purgeOldData, type DataSettings } from '../../api/settings';

/**
 * Data & Retention — how long the coordinator keeps run history, health/change
 * logs, notification logs and uptime checks. 0 = keep everything. A Purge-now
 * button deletes rows older than the window immediately. File storage lives on
 * the /data volume.
 */
export const DataRetentionSection: React.FC = () => {
  const { t } = useTranslation();
  const [data, setData] = useState<DataSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [purging, setPurging] = useState(false);

  useEffect(() => {
    getDataSettings()
      .then(setData)
      .catch(() => toast.error(t('Failed to load data settings')))
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    if (!data) return;
    setSaving(true);
    try {
      const next = await updateDataSettings({ retention_days: data.retention_days });
      setData(next);
      toast.success(t('Retention settings saved'));
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to save retention settings')));
    } finally {
      setSaving(false);
    }
  };

  const handlePurge = async () => {
    if (!data) return;
    if (data.retention_days <= 0) {
      toast.error(t('Set a retention window above 0 to purge.'));
      return;
    }
    if (!window.confirm(t('Delete all records older than {{n}} days? This cannot be undone.', { n: data.retention_days }))) {
      return;
    }
    setPurging(true);
    try {
      const res = await purgeOldData(data.retention_days);
      const total = Object.values(res.purged || {}).reduce((a, b) => a + b, 0);
      toast.success(t('Purged {{n}} records', { n: total }));
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to purge data')));
    } finally {
      setPurging(false);
    }
  };

  if (loading || !data) {
    return <div className="text-sm text-secondary">{t('Loading...')}</div>;
  }

  return (
    <div className="space-y-6">
      <SectionHead
        title={t('Data & Retention')}
        description={t('Control how long run history and logs are kept, and purge old records on demand.')}
      />

      <div className="border-t border-border pt-4 space-y-5">
        <div>
          <label className="block text-[13px] font-medium text-ink mb-1">{t('Keep history (days)')}</label>
          <NumberInput min={0} max={3650} value={data.retention_days} onChange={(v) => setData((d) => (d ? { ...d, retention_days: v ?? 0 } : d))} />
          <p className="text-xs text-tertiary mt-1">{t('0 keeps everything. Otherwise runs, change/health logs and uptime checks older than this are eligible for purge.')}</p>
        </div>

        <div className="flex items-center justify-between gap-4">
          <Button variant="secondary" onClick={handlePurge} loading={purging} disabled={data.retention_days <= 0}>
            <TrashIcon className="w-4 h-4" />
            {t('Purge now')}
          </Button>
          <Button onClick={handleSave} loading={saving}>{t('Save')}</Button>
        </div>
      </div>

      <div className="border-t border-border pt-4">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-tertiary mb-2">{t('File storage')}</p>
        <p className="text-sm text-secondary leading-relaxed">
          {t('Uploaded files and extracted assets are stored on the coordinator’s local /data volume. Back up that volume to preserve your data.')}
        </p>
      </div>
    </div>
  );
};

export default DataRetentionSection;
