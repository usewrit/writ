import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import { CheckCircleIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { SectionHead } from '../common/SectionHead';
import { Button } from '../ui/Button';
import { NumberInput, Switch } from '../ui';
import { apiErrorMessage } from '../../api/client';
import { getSecuritySettings, updateSecuritySettings, type SecuritySettings } from '../../api/settings';

/**
 * Security — session / JWT policy (access + refresh TTL), idle timeout, and a
 * require-MFA-to-sign-in switch (opt-in, off by default). Also surfaces a
 * read-only encryption-key status indicator (never the key itself).
 */
export const SecuritySection: React.FC = () => {
  const { t } = useTranslation();
  const [data, setData] = useState<SecuritySettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getSecuritySettings()
      .then(setData)
      .catch(() => toast.error(t('Failed to load security settings')))
      .finally(() => setLoading(false));
  }, []);

  const set = <K extends keyof SecuritySettings>(key: K, value: SecuritySettings[K]) => {
    setData((d) => (d ? { ...d, [key]: value } : d));
  };

  const handleSave = async () => {
    if (!data) return;
    setSaving(true);
    try {
      const next = await updateSecuritySettings({
        session_ttl_min: data.session_ttl_min,
        refresh_ttl_days: data.refresh_ttl_days,
        idle_timeout_min: data.idle_timeout_min,
        require_mfa: data.require_mfa,
      });
      setData(next);
      toast.success(t('Security settings saved'));
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to save security settings')));
    } finally {
      setSaving(false);
    }
  };

  if (loading || !data) {
    return <div className="text-sm text-secondary">{t('Loading...')}</div>;
  }

  return (
    <div className="space-y-6">
      <SectionHead title={t('Security')} description={t('Session lifetime and sign-in policy for the owner account.')} />

      <div className="border-t border-border pt-4 space-y-5">
        <div className="grid @pair/stage:grid-cols-2 gap-4">
          <div>
            <label className="block text-[13px] font-medium text-ink mb-1">{t('Access token TTL (minutes)')}</label>
            <NumberInput min={1} max={1440} value={data.session_ttl_min} onChange={(v) => set('session_ttl_min', v ?? 15)} />
          </div>
          <div>
            <label className="block text-[13px] font-medium text-ink mb-1">{t('Refresh token TTL (days)')}</label>
            <NumberInput min={1} max={3650} value={data.refresh_ttl_days} onChange={(v) => set('refresh_ttl_days', v ?? 30)} />
          </div>
        </div>

        <div>
          <label className="block text-[13px] font-medium text-ink mb-1">{t('Idle timeout (minutes)')}</label>
          <NumberInput min={0} value={data.idle_timeout_min} onChange={(v) => set('idle_timeout_min', v ?? 0)} />
          <p className="text-xs text-tertiary mt-1">{t('0 disables the idle timeout.')}</p>
        </div>

        <Switch
          checked={data.require_mfa}
          onChange={(next) => set('require_mfa', next)}
          label={t('Require MFA to sign in')}
          description={t('Off by default. Enable only after enrolling a TOTP authenticator on the Account tab.')}
          reverse
        />

        <div className="flex justify-end">
          <Button onClick={handleSave} loading={saving}>{t('Save')}</Button>
        </div>
      </div>

      <div className="border-t border-border pt-4">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-tertiary mb-3">{t('Encryption key')}</p>
        {data.encryption_key_configured ? (
          <div className="flex items-center gap-2 text-sm text-emerald-600">
            <CheckCircleIcon className="w-5 h-5" />
            {t('A secret encryption key is configured. Vault secrets and provider keys are sealed at rest.')}
          </div>
        ) : (
          <div className="flex items-start gap-2 text-sm text-amber-700">
            <ExclamationTriangleIcon className="w-5 h-5 shrink-0" />
            <span>{t('No encryption key configured. Set SECRET_ENCRYPTION_KEY on the coordinator to seal secrets at rest.')}</span>
          </div>
        )}
      </div>
    </div>
  );
};

export default SecuritySection;
