import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import { SectionHead } from '../common/SectionHead';
import { Button } from '../ui/Button';
import { Input } from '../ui/Input';
import { apiErrorMessage } from '../../api/client';
import { getNetworkSettings, updateNetworkSettings, type NetworkSettings } from '../../api/settings';

/**
 * Network & Public URL — the canonical base agents + device-flow dial. Promoted
 * from the WRIT_PUBLIC_URL env to a live settings knob (recorder_proxy reads it
 * without a restart). Trusted hosts harden the reverse-proxy path. The agent-WS
 * / API / MCP URLs are derived read-only from the public URL.
 */
const ReadOnlyUrl: React.FC<{ label: string; value: string | null }> = ({ label, value }) => {
  const { t } = useTranslation();
  return (
    <div>
      <label className="block text-[11px] font-medium uppercase tracking-wider text-tertiary mb-1">{label}</label>
      <div className="px-3 py-2 text-sm font-mono rounded-lg border border-border bg-canvas text-secondary break-all">
        {value || t('— set the Public URL above —')}
      </div>
    </div>
  );
};

export const NetworkSection: React.FC = () => {
  const { t } = useTranslation();
  const [data, setData] = useState<NetworkSettings | null>(null);
  const [publicUrl, setPublicUrl] = useState('');
  const [trustedHosts, setTrustedHosts] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const hydrate = (n: NetworkSettings) => {
    setData(n);
    setPublicUrl(n.public_url || '');
    setTrustedHosts((n.trusted_hosts || []).join(', '));
  };

  useEffect(() => {
    getNetworkSettings()
      .then(hydrate)
      .catch(() => toast.error(t('Failed to load network settings')))
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    setSaving(true);
    try {
      const hosts = trustedHosts
        .split(',')
        .map((h) => h.trim())
        .filter(Boolean);
      const next = await updateNetworkSettings({ public_url: publicUrl.trim(), trusted_hosts: hosts });
      hydrate(next);
      toast.success(t('Network settings saved'));
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to save network settings')));
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
        title={t('Network & Public URL')}
        description={t('The public address agents and device-flow clients dial. Set this before connecting a fleet from other machines.')}
      />

      <div className="border-t border-border pt-4 space-y-5">
        <div>
          <label className="block text-[13px] font-medium text-ink mb-1">{t('Public URL')}</label>
          <Input value={publicUrl} onChange={(e) => setPublicUrl(e.target.value)} placeholder="https://coordinator.example.com" />
          <p className="text-xs text-tertiary mt-1">
            {t('Include the scheme (https://). Agents derive their WebSocket URL from this.')}
          </p>
        </div>

        <div>
          <label className="block text-[13px] font-medium text-ink mb-1">{t('Trusted proxy hosts / allowed origins')}</label>
          <Input value={trustedHosts} onChange={(e) => setTrustedHosts(e.target.value)} placeholder="coordinator.example.com, 10.0.0.5" />
          <p className="text-xs text-tertiary mt-1">{t('Comma-separated. Used behind a reverse proxy to accept forwarded hosts.')}</p>
        </div>

        <div className="flex justify-end">
          <Button onClick={handleSave} loading={saving}>{t('Save')}</Button>
        </div>
      </div>

      <div className="border-t border-border pt-4 space-y-4">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-tertiary">{t('Derived endpoints')}</p>
        <ReadOnlyUrl label={t('Agent WebSocket')} value={data.derived.agent_ws_url} />
        <ReadOnlyUrl label={t('REST / OpenAI-compat base')} value={data.derived.api_base} />
        <ReadOnlyUrl label={t('MCP base')} value={data.derived.mcp_base} />
      </div>
    </div>
  );
};

export default NetworkSection;
