import React, { useState } from 'react';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import { automationApi } from '../../api/endpoints';
import { Select, Switch } from '../ui';

/**
 * One labelled setting row. It lives at MODULE scope rather than inside
 * RunSettingsCard: a component declared in a render body is a brand-new
 * component *type* on every render, and React compares types by identity — so
 * it unmounts and remounts the whole row instead of updating it, dropping any
 * focus or open dropdown inside. (`react-hooks/static-components`.) It closes
 * over nothing, so hoisting is all that's needed.
 */
const Row: React.FC<{ label: string; desc: string; children: React.ReactNode }> = ({ label, desc, children }) => (
  <div className="flex items-center justify-between px-4 py-2.5 gap-4">
    <div className="min-w-0">
      <div className="text-[13px] text-ink">{label}</div>
      <div className="text-[11px] text-tertiary mt-0.5">{desc}</div>
    </div>
    <div className="shrink-0">{children}</div>
  </div>
);

/**
 * Browser run settings (timeout / retries / headless) as explained setting rows.
 */
export const RunSettingsCard: React.FC<{
  workflow: { id: number; timeout_ms: number; retry_count: number; headless: boolean };
  onUpdate?: () => void;
}> = ({ workflow, onUpdate }) => {
  const { t } = useTranslation();
  const [settings, setSettings] = useState({
    timeout_ms: workflow.timeout_ms,
    retry_count: workflow.retry_count,
    headless: workflow.headless,
  });
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  const update = (patch: Partial<typeof settings>) => {
    setSettings(s => ({ ...s, ...patch }));
    setDirty(true);
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      await automationApi.updateWorkflow(workflow.id, settings);
      toast.success(t('Settings saved'));
      setDirty(false);
      onUpdate?.();
    } catch {
      toast.error(t('Failed to save'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="bg-surface border border-border/80 rounded-xl overflow-hidden divide-y divide-zinc-100">
      <Row label={t('Timeout')} desc={t('Give up if a run takes longer than this')}>
        <Select<number>
          size="sm"
          value={settings.timeout_ms}
          onChange={v => update({ timeout_ms: v })}
          options={[15000, 30000, 60000, 120000, 300000].map(v => ({ value: v, label: `${v / 1000}s` }))}
        />
      </Row>
      <Row label={t('Retries')} desc={t('Re-attempt the run this many times on failure')}>
        <Select<number>
          size="sm"
          value={settings.retry_count}
          onChange={v => update({ retry_count: v })}
          options={[0, 1, 2, 3, 5].map(v => ({ value: v, label: String(v) }))}
        />
      </Row>
      <Row label={t('Headless browser')} desc={t('Run without a visible browser window')}>
        <Switch
          size="sm"
          checked={settings.headless}
          onChange={() => update({ headless: !settings.headless })}
        />
      </Row>
      {dirty && (
        <div className="flex items-center justify-end gap-2 px-4 py-2.5 bg-canvas/60">
          <button onClick={() => { setSettings({ timeout_ms: workflow.timeout_ms, retry_count: workflow.retry_count, headless: workflow.headless }); setDirty(false); }}
            className="px-3 py-1 text-xs text-secondary hover:text-ink rounded-lg hover:bg-hover transition-colors">
            {t('Discard')}
          </button>
          <button onClick={handleSave} disabled={saving}
            className="px-3 py-1 text-xs font-medium bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 disabled:opacity-50">
            {saving ? t('Saving...') : t('Save')}
          </button>
        </div>
      )}
    </div>
  );
};
