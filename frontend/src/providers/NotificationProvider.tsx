import React, { createContext, useContext, useCallback, useEffect, useMemo, useRef } from 'react';
import toast from 'react-hot-toast';
import { useQuery } from '../hooks/useQuery';
import { useQueryCache } from '../stores/queryCache';
import { inboxApi, NotificationItem } from '../api/inbox';

// In-app notification layer. Mirrors CreditProvider: a thin context
// over a polled query that the bell + inbox dropdown + /notifications page read.
// Polling cadence is deliberately gentle (the bell only needs the unread count);
// the full list is fetched on demand when the dropdown / page opens.

const UNREAD_POLL_MS = 60_000;
export const INBOX_LIST_KEY = 'inbox:list';
export const INBOX_UNREAD_KEY = 'inbox:unread';

interface NotificationContextValue {
  /** Unread badge count (0 while loading / on error). */
  unreadCount: number;
  /** Most-recent notifications (newest first) for the dropdown preview. */
  items: NotificationItem[];
  loading: boolean;
  error: string | null;
  /** Re-fetch both the unread count and the recent list. */
  refresh: () => Promise<void>;
  /** Mark one notification read (optimistic-ish: refreshes after). */
  markRead: (id: string) => Promise<void>;
  /** Mark every notification read. */
  markAllRead: () => Promise<void>;
  /** Dismiss (permanently remove) one notification. */
  dismiss: (id: string) => Promise<void>;
}

const NotificationContext = createContext<NotificationContextValue | null>(null);

export function useNotifications() {
  return useContext(NotificationContext);
}

export const NotificationProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  // Bell badge — light poll, count only.
  const { data: unread, refresh: refreshUnread } = useQuery<number>(
    INBOX_UNREAD_KEY,
    () => inboxApi.unreadCount(),
    { pollInterval: UNREAD_POLL_MS, staleTime: 30_000, silent: true },
  );

  // Recent list for the dropdown preview — shares cache with the dropdown/page.
  const {
    data: list,
    loading,
    error,
    refresh: refreshList,
  } = useQuery<NotificationItem[]>(
    INBOX_LIST_KEY,
    () => inboxApi.list(20, 0).then(r => r.items),
    { pollInterval: UNREAD_POLL_MS, staleTime: 30_000, silent: true },
  );

  const unreadCount = unread ?? 0;
  const items = useMemo(() => list ?? [], [list]);

  // Toast on a NEWLY-arrived unread notification (transition only — mirrors the
  // onPlanLimitHit toast-on-event pattern, deduped by id so a poll that returns
  // the same item doesn't re-toast).
  const lastTopIdRef = useRef<string | null>(null);
  const seededRef = useRef(false);
  useEffect(() => {
    if (items.length === 0) return;
    const top = items[0];
    // First load only seeds the ref — don't toast pre-existing notifications.
    if (!seededRef.current) {
      seededRef.current = true;
      lastTopIdRef.current = top.id;
      return;
    }
    if (top.id !== lastTopIdRef.current && !top.read_at) {
      lastTopIdRef.current = top.id;
      toast(top.title, { icon: '🔔' });
    } else {
      lastTopIdRef.current = top.id;
    }
  }, [items]);

  const refresh = useCallback(async () => {
    await Promise.all([refreshUnread(), refreshList()]);
  }, [refreshUnread, refreshList]);

  const markRead = useCallback(
    async (id: string) => {
      // Optimistic: drop the unread flag locally, then confirm with the server.
      const cache = useQueryCache.getState();
      const cached = cache.get(INBOX_LIST_KEY)?.data as NotificationItem[] | undefined;
      if (cached) {
        cache.set(
          INBOX_LIST_KEY,
          cached.map(n => (n.id === id && !n.read_at ? { ...n, read_at: new Date().toISOString() } : n)),
        );
      }
      try {
        await inboxApi.markRead(id);
      } finally {
        await refresh();
      }
    },
    [refresh],
  );

  const markAllRead = useCallback(async () => {
    const cache = useQueryCache.getState();
    const cached = cache.get(INBOX_LIST_KEY)?.data as NotificationItem[] | undefined;
    if (cached) {
      const now = new Date().toISOString();
      cache.set(INBOX_LIST_KEY, cached.map(n => (n.read_at ? n : { ...n, read_at: now })));
    }
    try {
      await inboxApi.markAllRead();
    } finally {
      await refresh();
    }
  }, [refresh]);

  const dismiss = useCallback(
    async (id: string) => {
      // Optimistic: drop the row locally, then confirm with the server. The
      // badge count is corrected by the refresh() below.
      const cache = useQueryCache.getState();
      const cached = cache.get(INBOX_LIST_KEY)?.data as NotificationItem[] | undefined;
      if (cached) {
        cache.set(INBOX_LIST_KEY, cached.filter(n => n.id !== id));
      }
      try {
        await inboxApi.dismiss(id);
      } finally {
        await refresh();
      }
    },
    [refresh],
  );

  const value = useMemo(
    () => ({ unreadCount, items, loading, error, refresh, markRead, markAllRead, dismiss }),
    [unreadCount, items, loading, error, refresh, markRead, markAllRead, dismiss],
  );

  return <NotificationContext.Provider value={value}>{children}</NotificationContext.Provider>;
};
