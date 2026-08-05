import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { BellIcon } from '@heroicons/react/24/outline';
import { useNotifications } from '../../providers/NotificationProvider';
import { NotificationRow } from './NotificationRow';
import { EmptyHero } from '../ui';

// Sidebar/header bell with an unread badge + an inbox dropdown preview.
// Reads everything from NotificationProvider; safe to mount even
// when the provider is absent (renders nothing).
//
// The panel is rendered in a PORTAL with fixed positioning anchored to the bell.
// The bell lives in the bottom of a 220px `overflow-hidden` sidebar (desktop)
// and in the top mobile header — an in-flow `absolute` panel got clipped by the
// sidebar and overflowed the viewport. Portaling + flip/clamp keeps it fully on
// screen in both placements.

const PANEL_W = 320;
const MARGIN = 8;

export const NotificationBell: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const ctx = useNotifications();
  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [style, setStyle] = useState<React.CSSProperties | null>(null);

  // Position the portaled panel relative to the bell, flipping up/down by
  // available space and clamping horizontally so it never leaves the viewport.
  const reposition = useCallback(() => {
    const b = btnRef.current?.getBoundingClientRect();
    if (!b) return;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const width = Math.min(PANEL_W, vw - MARGIN * 2);
    // Horizontal: align to the bell's left edge, then clamp into the viewport.
    const left = Math.max(MARGIN, Math.min(b.left, vw - MARGIN - width));
    // Vertical: open downward when there's room below; otherwise open upward.
    const spaceBelow = vh - b.bottom;
    const spaceAbove = b.top;
    const openDown = spaceBelow >= 380 || spaceBelow >= spaceAbove;
    const maxHeight = Math.max(220, (openDown ? spaceBelow : spaceAbove) - MARGIN * 2);
    setStyle({
      position: 'fixed',
      left,
      width,
      maxHeight,
      ...(openDown
        ? { top: b.bottom + MARGIN }
        : { bottom: vh - b.top + MARGIN }),
    });
  }, []);

  // Close on outside click / Escape. The panel is portaled, so the outside
  // check must allow clicks on EITHER the bell or the panel.
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      const target = e.target as Node;
      if (btnRef.current?.contains(target)) return;
      if (panelRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  // Keep the panel anchored while open (scroll inside the sidebar, resize).
  useLayoutEffect(() => {
    if (open) reposition();
  }, [open, reposition]);
  useEffect(() => {
    if (!open) return;
    const handler = () => reposition();
    window.addEventListener('resize', handler);
    window.addEventListener('scroll', handler, true);
    return () => {
      window.removeEventListener('resize', handler);
      window.removeEventListener('scroll', handler, true);
    };
  }, [open, reposition]);

  // Refresh the list when the dropdown opens so the preview is current.
  useEffect(() => {
    if (open) ctx?.refresh();
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!ctx) return null;
  const { unreadCount, items, loading, error, markRead, markAllRead, dismiss, refresh } = ctx;
  const badge = unreadCount > 99 ? '99+' : String(unreadCount);

  return (
    <div className="relative shrink-0" data-tour="notification-bell">
      <button
        ref={btnRef}
        onClick={() => setOpen(o => !o)}
        aria-label={t('Notifications')}
        aria-haspopup="true"
        aria-expanded={open}
        title={t('Notifications')}
        className="relative p-1.5 text-tertiary hover:text-ink rounded-md hover:bg-surface/80 transition-colors shrink-0"
      >
        <BellIcon className="w-4 h-4" />
        {unreadCount > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[15px] h-[15px] px-1 flex items-center justify-center rounded-full bg-ink text-white text-[9px] font-semibold tabular-nums leading-none">
            {badge}
          </span>
        )}
      </button>

      {open && style && createPortal(
        <div
          ref={panelRef}
          style={style}
          className="z-[60] flex flex-col bg-surface border border-border rounded-2xl shadow-lg overflow-hidden"
        >
          <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
            <h3 className="text-sm font-semibold text-ink">{t('Notifications')}</h3>
            {unreadCount > 0 && (
              <button
                onClick={() => markAllRead()}
                className="text-[11px] text-tertiary hover:text-ink transition-colors"
              >
                {t('Mark all read')}
              </button>
            )}
          </div>

          <div className="flex-1 overflow-y-auto">
            {loading && items.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-10 text-center">
                <div className="w-2 h-2 rounded-full bg-active animate-pulse mb-3" />
                <p className="text-xs text-tertiary">{t('Loading…')}</p>
              </div>
            ) : error && items.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-10 text-center px-4">
                <p className="text-xs text-secondary">{t("Couldn't load notifications")}</p>
                <button
                  onClick={() => refresh()}
                  className="mt-3 px-3 py-1.5 bg-accent-strong text-accent-on text-xs font-medium rounded-lg hover:bg-accent-strong/90 transition-colors"
                >
                  {t('Retry')}
                </button>
              </div>
            ) : items.length === 0 ? (
              <EmptyHero
                size="sm"
                icon={BellIcon}
                title={t('No notifications yet')}
                description={t('Updates about your runs, payouts and listings will appear here')}
                className="py-10"
              />
            ) : (
              <ul className="divide-y divide-border">
                {items.slice(0, 8).map(n => (
                  <NotificationRow
                    key={n.id}
                    notification={n}
                    compact
                    onActivate={async () => {
                      if (!n.read_at) await markRead(n.id);
                      if (n.link) {
                        setOpen(false);
                        navigate(n.link);
                      }
                    }}
                    onDismiss={() => dismiss(n.id)}
                  />
                ))}
              </ul>
            )}
          </div>

          <button
            onClick={() => {
              setOpen(false);
              navigate('/notifications');
            }}
            className={clsx(
              'w-full px-4 py-2.5 text-xs font-medium text-ink border-t border-border shrink-0',
              'hover:bg-hover transition-colors',
            )}
          >
            {t('View all notifications')}
          </button>
        </div>,
        document.body,
      )}
    </div>
  );
};
