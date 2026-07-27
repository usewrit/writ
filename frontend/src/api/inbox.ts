import client from './client';

// In-app notification inbox. Backs the sidebar bell + inbox dropdown
// + the /notifications page. Distinct from api/notifications.ts, which configures
// notification PROVIDERS (Signal/SMS/email) for target-change alerts — this is
// the user-facing message inbox served by the coordinator notifications-inbox router
// (prefix "/inbox").

// Mirrors backend NotificationType string values.
export type NotificationType =
  | 'review_approved'
  | 'review_rejected'
  | 'payout_paid'
  | 'payout_failed'
  | 'refund_issued'
  | 'clawback_applied'
  | 'run_failed'
  | 'low_balance'
  | 'update_available'
  | 'account_security'
  | 'workflow_removed';

export interface NotificationItem {
  id: string;
  type: string;
  title: string;
  body?: string | null;
  link?: string | null;
  payload?: Record<string, unknown> | null;
  read_at?: string | null;
  created_at?: string | null;
}

export interface NotificationListResponse {
  items: NotificationItem[];
  limit: number;
  offset: number;
}

export interface UnreadCountResponse {
  unread: number;
}

export const inboxApi = {
  list: (limit = 20, offset = 0, unreadOnly = false) =>
    client
      .get<NotificationListResponse>('/inbox', {
        params: { limit, offset, unread_only: unreadOnly },
      })
      .then(r => r.data),

  unreadCount: () =>
    client.get<UnreadCountResponse>('/inbox/unread-count').then(r => r.data.unread),

  markRead: (id: string) =>
    client.post<{ success: boolean }>(`/inbox/${id}/read`).then(r => r.data),

  markAllRead: () =>
    client.post<{ success: boolean; updated: number }>('/inbox/read-all').then(r => r.data),

  /** Dismiss (permanently delete) one notification. */
  dismiss: (id: string) =>
    client.delete<{ success: boolean }>(`/inbox/${id}`).then(r => r.data),
};
