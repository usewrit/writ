import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '../../hooks/useQuery';
import { Q } from '../../stores/queryKeys';
import { webhookTriggersApi, triggersApi } from '../../api/endpoints';
import { formatRelativeTime } from '../../utils/format';
import { CrossRefBadge } from './CrossRefBadge';
import { PortalMenu } from './PortalMenu';
import { Modal } from '../ui/Modal';
import toast from 'react-hot-toast';
import {
  LinkIcon,
  ClipboardIcon,
  ClipboardDocumentCheckIcon,
  TrashIcon,
  EllipsisHorizontalIcon,
  PlayIcon,
} from '@heroicons/react/24/outline';

interface WebhookListProps {
  search: string;
}

export const WebhookList: React.FC<WebhookListProps> = ({ search }) => {
  const { t } = useTranslation();
  const { data: webhooks, loading, refresh } = useQuery(Q.webhookTriggers(), () => webhookTriggersApi.list().catch(() => []), { pollInterval: 15000 });
  // Reference metadata rarely changes — don't poll it; it refreshes on mount.
  const { data: flowReferences } = useQuery(Q.flowReferences(), () => triggersApi.references().catch(() => null));
  const [copiedToken, setCopiedToken] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<{ id: number; name: string } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [togglingId, setTogglingId] = useState<number | null>(null);

  const webhookList = (webhooks || []).filter((w: any) =>
    !search || w.name?.toLowerCase().includes(search.toLowerCase())
  );

  const copyUrl = (token: string) => {
    navigator.clipboard.writeText(webhookTriggersApi.getWebhookUrl(token));
    setCopiedToken(token);
    toast.success(t('Copied'));
    setTimeout(() => setCopiedToken(null), 2000);
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await webhookTriggersApi.delete(deleteTarget.id);
      toast.success(t('Webhook deleted'));
      refresh();
    } catch {
      toast.error(t('Failed to delete'));
    } finally {
      setDeleting(false);
      setDeleteTarget(null);
    }
  };

  const handleToggle = async (w: any) => {
    if (togglingId) return;
    setTogglingId(w.id);
    try {
      await webhookTriggersApi.update(w.id, { enabled: !w.enabled });
      toast.success(w.enabled ? t('Disabled') : t('Enabled'));
      refresh();
    } catch {
      toast.error(t('Failed to toggle'));
    } finally {
      setTogglingId(null);
    }
  };

  // Initial load (no cached data) — neutral skeleton instead of empty-state flash.
  if (loading && !webhooks) {
    return (
      <div className="space-y-2" aria-busy="true" aria-label={t('Loading webhooks')}>
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="bg-surface border border-border rounded-xl p-4">
            <div className="flex items-center gap-3">
              <div className="w-7 h-7 rounded-lg bg-hover animate-pulse" />
              <div className="h-4 w-48 rounded bg-hover animate-pulse" />
              <div className="flex-1" />
              <div className="h-4 w-16 rounded bg-hover animate-pulse" />
            </div>
          </div>
        ))}
      </div>
    );
  }

  if (webhookList.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[50vh] text-center">
        <div className="w-12 h-12 rounded-2xl bg-chrome border border-border flex items-center justify-center mb-4">
          <LinkIcon className="h-6 w-6 text-tertiary" />
        </div>
        <p className="text-sm font-medium text-ink">
          {search ? t('No webhooks match your search') : t('No webhooks yet')}
        </p>
        <p className="text-xs text-secondary mt-1">
          {search ? t('Try a different search term') : t("Create an automation with a 'Webhook' source to generate one")}
        </p>
      </div>
    );
  }

  return (
    <>
      <div className="grid gap-3">
        {webhookList.map((w: any) => {
          const flowRefs = flowReferences?.webhook?.[String(w.id)] || [];

          return (
            <div
              key={w.id}
              className="bg-surface border border-ink/20 rounded-2xl shadow-sm hover:shadow-md transition-all"
            >
              <div className="flex items-center gap-3 px-4 py-3">
                <div className="w-7 h-7 rounded-lg bg-ink text-white flex items-center justify-center shrink-0">
                  <LinkIcon className="h-3.5 w-3.5" />
                </div>

                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium text-ink">{w.name || t('Unnamed Webhook')}</div>
                  {w.token && (
                    <div className="flex items-center gap-1.5 mt-1">
                      <code className="text-xs text-secondary bg-canvas px-2 py-0.5 rounded-lg border border-border truncate max-w-[280px] block font-mono">
                        {webhookTriggersApi.getWebhookUrl(w.token)}
                      </code>
                      <button
                        onClick={() => copyUrl(w.token)}
                        className="shrink-0 p-1 hover:bg-chrome rounded-lg transition-colors"
                      >
                        {copiedToken === w.token ? (
                          <ClipboardDocumentCheckIcon className="h-3.5 w-3.5 text-green-600" />
                        ) : (
                          <ClipboardIcon className="h-3.5 w-3.5 text-secondary" />
                        )}
                      </button>
                    </div>
                  )}
                  <div className="flex items-center gap-2 mt-1">
                    <span className="text-[10px] px-1.5 py-0.5 rounded-full font-medium border border-border text-secondary">
                      {w.action}
                    </span>
                    <CrossRefBadge flows={flowRefs} />
                  </div>
                </div>

                <div className="flex items-center gap-4 shrink-0">
                  {(w.trigger_count > 0 || w.last_triggered_at) ? (
                    <div className="text-right hidden @pair/stage:block">
                      {w.trigger_count > 0 && (
                        <div className="text-xs text-secondary">{t('{{n}} calls', { n: w.trigger_count })}</div>
                      )}
                      {w.last_triggered_at && (
                        <div className="text-xs text-tertiary">{formatRelativeTime(w.last_triggered_at)}</div>
                      )}
                    </div>
                  ) : (
                    <span className="text-xs text-tertiary hidden @pair/stage:block">{t('Never called')}</span>
                  )}

                  <PortalMenu trigger={<EllipsisHorizontalIcon className="h-4 w-4" />}>
                    {(close) => (
                      <>
                        <button
                          onClick={() => { close(); handleToggle(w); }}
                          className="flex items-center gap-2 w-full px-3 py-2 text-sm rounded-lg text-secondary hover:bg-chrome hover:text-ink"
                        >
                          <PlayIcon className="h-4 w-4" /> {w.enabled ? t('Disable') : t('Enable')}
                        </button>
                        <div className="border-t border-border my-1" />
                        <button
                          onClick={() => { close(); setDeleteTarget({ id: w.id, name: w.name || t('Unnamed Webhook') }); }}
                          className="flex items-center gap-2 w-full px-3 py-2 text-sm rounded-lg text-red-500 hover:bg-red-50 hover:text-red-600"
                        >
                          <TrashIcon className="h-4 w-4" /> {t('Delete')}
                        </button>
                      </>
                    )}
                  </PortalMenu>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <Modal isOpen={!!deleteTarget} onClose={() => setDeleteTarget(null)} title={t('Delete Webhook')}>
        <p className="text-sm text-secondary mb-4">
          {t('Are you sure you want to delete')} <strong className="text-ink">{deleteTarget?.name}</strong>?
        </p>
        <div className="flex items-center justify-end gap-2">
          <button onClick={() => setDeleteTarget(null)} className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg hover:bg-chrome transition-colors">
            {t('Cancel')}
          </button>
          <button onClick={handleDelete} disabled={deleting} className="px-4 py-2 text-sm font-medium bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors disabled:opacity-50">
            {deleting ? t('Deleting...') : t('Delete')}
          </button>
        </div>
      </Modal>
    </>
  );
};
