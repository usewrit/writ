import React from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '../../hooks/useQuery';
import { Q } from '../../stores/queryKeys';
import { recipientsApi, triggersApi } from '../../api/endpoints';
import { CrossRefBadge, computeFlowRefs } from './CrossRefBadge';
import { statusStyle } from '../../utils/statusStyle';
import { EmptyHero } from '../ui';
import clsx from 'clsx';
import { channelMeta } from '../notifications/channelMeta';
import { BellIcon } from '@heroicons/react/24/outline';

interface NotificationListProps {
  search: string;
}

export const NotificationList: React.FC<NotificationListProps> = ({ search }) => {
  const { t } = useTranslation();
  const { data: recipients, loading } = useQuery('recipients', () => recipientsApi.getAll().catch(() => []), { pollInterval: 15000 });
  // Used only to derive cross-reference badges (reference metadata, rarely
  // changes) — don't poll; the shared Q.triggers() cache stays warm elsewhere
  // and this refreshes on mount.
  const { data: allFlows } = useQuery(Q.triggers(), () => triggersApi.listAll().catch(() => []));

  const recipientList = (recipients || []).filter((r: any) =>
    !search || r.name?.toLowerCase().includes(search.toLowerCase()) || r.provider?.toLowerCase().includes(search.toLowerCase())
  );

  // Initial load (no cached data) — neutral skeleton instead of empty-state flash.
  if (loading && !recipients) {
    return (
      <div className="space-y-2" aria-busy="true" aria-label={t('Loading notification channels')}>
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="bg-surface border border-border rounded-xl p-4">
            <div className="flex items-center gap-3">
              <div className="w-7 h-7 rounded-lg bg-hover animate-pulse" />
              <div className="h-4 w-48 rounded bg-hover animate-pulse" />
              <div className="flex-1" />
              <div className="w-2.5 h-2.5 rounded-full bg-hover animate-pulse" />
            </div>
          </div>
        ))}
      </div>
    );
  }

  if (recipientList.length === 0) {
    return (
      <EmptyHero
        icon={BellIcon}
        title={search ? t('No notifications match your search') : t('No notification channels configured')}
        description={search ? t('Try a different search term') : t('Set them up in Integrations')}
        className="min-h-[50vh]"
      />
    );
  }

  return (
    <div className="grid gap-3">
      {recipientList.map((r: any) => {
        const meta = channelMeta(r.provider);
        const ProviderIcon = meta.Icon;
        const flowRefs = computeFlowRefs(allFlows || [], (blocks) =>
          blocks.some((b: any) =>
            b.blockType === 'notification' &&
            (b.config?.channels?.includes(r.provider) || b.config?.recipients?.includes(`${r.provider}:${r.id}`))
          )
        );

        return (
          <div
            key={r.id}
            className="bg-surface border border-ink/20 rounded-2xl shadow-sm hover:shadow-md transition-all"
          >
            <div className="flex items-center gap-3 px-4 py-3">
              <div className="w-7 h-7 rounded-lg bg-ink text-white flex items-center justify-center shrink-0">
                <ProviderIcon className="h-3.5 w-3.5" />
              </div>

              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium text-ink">{r.name}</div>
                <div className="flex items-center gap-2 mt-0.5">
                  <span className="text-[10px] px-1.5 py-0.5 rounded-full font-medium border border-border text-secondary">
                    {t(meta.label)}
                  </span>
                  {r.identifier_preview && (
                    <span className="text-xs text-secondary">{r.identifier_preview}</span>
                  )}
                  <CrossRefBadge flows={flowRefs} />
                </div>
              </div>

              <div
                className={clsx(
                  'w-2.5 h-2.5 rounded-full shrink-0',
                  statusStyle(r.enabled ? 'enabled' : 'disabled').dot,
                )}
                title={r.enabled ? t('Enabled') : t('Disabled')}
                aria-label={r.enabled ? t('Enabled') : t('Disabled')}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
};
