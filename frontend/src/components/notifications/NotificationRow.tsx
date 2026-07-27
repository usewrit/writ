import React from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { XMarkIcon } from '@heroicons/react/24/outline';
import { formatRelativeTime } from '../../utils/format';
import { NotificationItem } from '../../api/inbox';
import { iconForNotification } from './notificationMeta';

// One inbox row, shared by the bell dropdown (compact) and the /notifications
// page. Monochrome: unread = subtle ink tint + a dot; read = muted.

// `iconForNotification` resolves to one of a fixed set of module-level heroicons,
// but a component *type* produced by a call inside a render body reads as
// "created during render" — React would be told the icon is a new component on
// every render and remount it (`react-hooks/static-components`). Passing the
// resolved type in as a prop makes it data rather than a locally-declared
// component, so the identity stays stable.
const NotificationGlyph: React.FC<{
  icon: ReturnType<typeof iconForNotification>;
  className: string;
}> = ({ icon: Icon, className }) => <Icon className={className} />;

interface Props {
  notification: NotificationItem;
  /** Tighter padding for the dropdown preview. */
  compact?: boolean;
  /** Click/Enter handler (mark-read + optional navigate). */
  onActivate?: () => void;
  /** Dismiss (permanently remove) handler — renders an X on hover/focus. */
  onDismiss?: () => void;
}

export const NotificationRow: React.FC<Props> = ({ notification: n, compact, onActivate, onDismiss }) => {
  const { t } = useTranslation();
  const Icon = iconForNotification(n.type);
  const unread = !n.read_at;
  const interactive = !!onActivate;

  return (
    <li
      onClick={onActivate}
      onKeyDown={e => {
        if (interactive && (e.key === 'Enter' || e.key === ' ')) {
          e.preventDefault();
          onActivate?.();
        }
      }}
      role={interactive ? 'button' : undefined}
      tabIndex={interactive ? 0 : undefined}
      className={clsx(
        'group flex items-start gap-3 transition-colors',
        compact ? 'px-4 py-3' : 'px-4 py-3.5',
        interactive && 'cursor-pointer hover:bg-hover focus:bg-hover focus:outline-none',
        unread && 'bg-ink/[0.03]',
      )}
    >
      <div
        className={clsx(
          'shrink-0 rounded-xl flex items-center justify-center',
          compact ? 'w-7 h-7' : 'w-9 h-9',
          unread ? 'bg-ink/10 text-ink' : 'bg-zinc-100 text-zinc-400',
        )}
      >
        <NotificationGlyph icon={Icon} className={compact ? 'h-4 w-4' : 'h-5 w-5'} />
      </div>

      <div className="min-w-0 flex-1">
        <p className={clsx('text-sm leading-snug', unread ? 'font-medium text-ink' : 'text-secondary')}>
          {n.title}
        </p>
        {n.body && (
          <p className={clsx('text-xs text-tertiary mt-0.5', compact && 'line-clamp-2')}>{n.body}</p>
        )}
        {n.created_at && (
          <p className="text-[11px] text-tertiary mt-1">{formatRelativeTime(n.created_at)}</p>
        )}
      </div>

      <div className="shrink-0 flex items-start justify-end w-6">
        {unread && (
          <span
            className={clsx(
              'mt-1.5 w-1.5 h-1.5 rounded-full bg-ink',
              onDismiss && 'group-hover:hidden group-focus-within:hidden',
            )}
          />
        )}
        {onDismiss && (
          <button
            type="button"
            onClick={e => {
              e.stopPropagation();
              onDismiss();
            }}
            onKeyDown={e => e.stopPropagation()}
            aria-label={t('Dismiss notification')}
            className={clsx(
              '-mt-1 -mr-1 shrink-0 rounded-md p-1 text-tertiary transition-colors',
              'hover:bg-hover hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/40',
              'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100',
            )}
          >
            <XMarkIcon className="h-4 w-4" aria-hidden="true" />
          </button>
        )}
      </div>
    </li>
  );
};
