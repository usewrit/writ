import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { BellIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { useQuery } from '../../hooks/useQuery';
import { inboxApi, NotificationItem } from '../../api/inbox';
import { useNotifications } from '../../providers/NotificationProvider';
import { NotificationRow } from './NotificationRow';
import { Button, EmptyHero } from '../ui';

// Full /notifications inbox page. Loads a deeper, paginated list than
// the bell dropdown; reuses NotificationRow + the standard loading/error/empty
// three-state pattern from the list pages.

const PAGE_SIZE = 30;

export const NotificationsPage: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const ctx = useNotifications();
  const [limit, setLimit] = useState(PAGE_SIZE);

  const { data, loading, error, refresh } = useQuery<NotificationItem[]>(
    ['inbox', 'page', String(limit)],
    () => inboxApi.list(limit, 0).then(r => r.items),
    { staleTime: 15_000 },
  );

  const items = data ?? [];
  const initialLoading = loading && items.length === 0;
  const hasUnread = items.some(n => !n.read_at);

  const handleActivate = async (n: NotificationItem) => {
    if (!n.read_at) {
      // Keep the provider (badge + dropdown) in sync, then refresh this page.
      await (ctx ? ctx.markRead(n.id) : inboxApi.markRead(n.id));
      await refresh();
    }
    if (n.link) navigate(n.link);
  };

  const handleMarkAll = async () => {
    await (ctx ? ctx.markAllRead() : inboxApi.markAllRead());
    await refresh();
  };

  const handleDismiss = async (n: NotificationItem) => {
    // Keep the provider (badge + bell dropdown) in sync, then refresh this page.
    await (ctx ? ctx.dismiss(n.id) : inboxApi.dismiss(n.id));
    await refresh();
  };

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 sm:px-6 py-4 border-b border-border shrink-0">
        <h1 className="text-base font-semibold text-ink">{t('Notifications')}</h1>
        <div className="flex-1" />
        {hasUnread && (
          <button
            onClick={handleMarkAll}
            className="text-xs font-medium text-tertiary hover:text-ink transition-colors"
          >
            {t('Mark all read')}
          </button>
        )}
      </div>

      {/* Content */}
      <div className="flex-1 relative overflow-auto">
        <div
          className="absolute inset-0 pointer-events-none"
          style={{
            backgroundImage: 'radial-gradient(circle, #d4d4d8 1px, transparent 1px)',
            backgroundSize: '24px 24px',
            opacity: 0.12,
          }}
        />
        <div className="relative z-10 py-4 px-4 sm:px-6 max-w-2xl mx-auto">
          {/* Loading */}
          {initialLoading && (
            <div className="flex flex-col items-center justify-center min-h-[50vh] text-center">
              <div className="w-2 h-2 rounded-full bg-active animate-pulse mb-3" />
              <p className="text-xs text-tertiary">{t('Loading…')}</p>
            </div>
          )}

          {/* Error */}
          {error && items.length === 0 && (
            <EmptyHero
              icon={ExclamationTriangleIcon}
              title={t("Couldn't load notifications")}
              description={error}
              className="min-h-[50vh]"
            >
              <Button onClick={() => refresh()} size="sm">{t('Retry')}</Button>
            </EmptyHero>
          )}

          {/* Empty */}
          {!initialLoading && !error && items.length === 0 && (
            <EmptyHero
              icon={BellIcon}
              title={t('No notifications yet')}
              description={t('Updates about your runs, payouts and listings will appear here')}
              className="min-h-[50vh]"
            />
          )}

          {/* List */}
          {items.length > 0 && (
            <ul className="bg-surface border border-border rounded-2xl overflow-hidden divide-y divide-border">
              {items.map(n => (
                <NotificationRow
                  key={n.id}
                  notification={n}
                  onActivate={() => handleActivate(n)}
                  onDismiss={() => handleDismiss(n)}
                />
              ))}
            </ul>
          )}

          {/* Load more — show when the page came back full (likely more rows). */}
          {items.length > 0 && items.length >= limit && (
            <div className="flex justify-center mt-4">
              <button
                onClick={() => setLimit(l => l + PAGE_SIZE)}
                disabled={loading}
                className="px-4 py-2 text-xs font-medium text-ink bg-hover rounded-xl hover:bg-active transition-colors disabled:opacity-50"
              >
                {loading ? t('Loading…') : t('Load more')}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default NotificationsPage;
