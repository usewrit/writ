import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import {
  SignalIcon,
  BoltIcon,
  EyeIcon,
  ArrowTopRightOnSquareIcon,
  StopCircleIcon,
} from '@heroicons/react/24/outline';
import i18n from '../../i18n';
import { useActivity } from '../../hooks/useActivity';
import { runsApi, type Run } from '../../api/runs';
import { streamingApi } from '../../api/endpoints';
import { LiveCountdown, IntervalProgress } from '../checks/LiveSince';
import type { StreamingSession, Target } from '../../types/api';

/**
 * Snake/camel-tolerant timing for a monitor row: last check + next check (falling back to
 * lastChecked + interval when `next_run_at` is absent) + whether it's overdue. Mirrors the
 * accessors the monitor list/detail views use so the numbers match.
 */
function monitorTiming(target: any): {
  lastChecked: string | null;
  nextRun: string | null;
  live: boolean;
} {
  const lastChecked = target.lastCheckedAt ?? target.last_checked_at ?? target.lastChecked ?? null;
  const periodMs = target.checkPeriodMs ?? target.check_period_ms ?? 60000;
  const nextRun =
    target.nextRunAt ??
    target.next_run_at ??
    (lastChecked
      ? new Date(new Date(lastChecked).getTime() + Math.max(periodMs, 60000)).toISOString()
      : null);
  const live = nextRun ? new Date(nextRun).getTime() <= Date.now() : false;
  return { lastChecked, nextRun, live };
}

const PANEL_W = 340;
const MARGIN = 8;

/** Compact "Ns / Nm / Nh" elapsed since an ISO timestamp. */
function timeAgo(iso?: string | null): string {
  if (!iso) return '';
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return '';
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  return `${Math.floor(mins / 60)}h`;
}

/** Host portion of a URL for a compact row title, falling back to the raw string. */
function hostOf(url?: string | null): string {
  if (!url) return '';
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

interface RowProps {
  title: string;
  sub?: string;
  onOpen: () => void;
  onStop?: () => void;
  busy?: boolean;
}

/** One live item: a pulsing dot, a click-to-open label, and open / stop actions. */
const Row: React.FC<RowProps> = ({ title, sub, onOpen, onStop, busy }) => (
  <li>
    <div className="group flex items-center gap-2.5 rounded-lg px-3 py-2 hover:bg-hover transition-colors">
      <span aria-hidden="true" className="h-2 w-2 shrink-0 rounded-full bg-amber-500 animate-pulse" />
      <button onClick={onOpen} className="min-w-0 flex-1 text-left">
        <p className="truncate text-[13px] text-ink">{title}</p>
        {sub ? <p className="truncate text-[11px] text-tertiary">{sub}</p> : null}
      </button>
      <div className="flex shrink-0 items-center gap-0.5">
        <button
          onClick={onOpen}
          title={i18n.t('Open')}
          aria-label={i18n.t('Open')}
          className="rounded p-1 text-tertiary hover:bg-hover hover:text-ink transition-colors"
        >
          <ArrowTopRightOnSquareIcon className="h-3.5 w-3.5" />
        </button>
        {onStop ? (
          <button
            onClick={onStop}
            disabled={busy}
            title={i18n.t('Stop')}
            aria-label={i18n.t('Stop')}
            className="rounded p-1 text-tertiary hover:bg-hover hover:text-red-600 transition-colors disabled:opacity-40"
          >
            <StopCircleIcon className="h-3.5 w-3.5" />
          </button>
        ) : null}
      </div>
    </div>
  </li>
);

interface SectionProps {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  count: number;
  children: React.ReactNode;
}

const Section: React.FC<SectionProps> = ({ icon: Icon, label, count, children }) => (
  <div className="px-1 pb-1">
    <div className="flex items-center gap-1.5 px-3 pb-1 pt-2">
      <Icon className="h-3.5 w-3.5 text-tertiary" />
      <span className="text-[11px] font-medium uppercase tracking-wide text-tertiary">{label}</span>
      <span className="text-[11px] tabular-nums text-tertiary">· {count}</span>
    </div>
    <ul className="space-y-0.5">{children}</ul>
  </div>
);

/**
 * One monitor row: host, a live "Next check in …" countdown (or "Checking now…" when overdue), and
 * the same last→next interval progress bar the monitor list/detail show — so the popover carries
 * the live cadence, not just a static label.
 */
const MonitorRow: React.FC<{ target: Target; onOpen: () => void }> = ({ target, onOpen }) => {
  const { t } = useTranslation();
  const { lastChecked, nextRun, live } = monitorTiming(target as any);
  return (
    <li>
      <button
        onClick={onOpen}
        className="group flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left hover:bg-hover transition-colors"
      >
        <span
          aria-hidden="true"
          className={clsx(
            'h-2 w-2 shrink-0 rounded-full',
            live ? 'bg-warning animate-pulse' : 'bg-success',
          )}
        />
        <span className="min-w-0 flex-1">
          <span className="flex items-center justify-between gap-2">
            <span className="truncate text-[13px] text-ink">{hostOf(target.url) || target.url}</span>
            <span className="shrink-0 text-[11px] tabular-nums text-tertiary">
              {nextRun ? <LiveCountdown until={nextRun} live /> : t('Awaiting first check')}
            </span>
          </span>
          <IntervalProgress
            since={lastChecked}
            until={nextRun}
            fillClassName={live ? 'bg-warning/70' : 'bg-success/55'}
            className="mt-1.5"
          />
        </span>
        <ArrowTopRightOnSquareIcon className="h-3.5 w-3.5 shrink-0 text-tertiary group-hover:text-ink transition-colors" />
      </button>
    </li>
  );
};

/**
 * Always-accessible live-activity indicator for the app chrome (sidebar footer +
 * mobile header, next to the notification bell). Auto-hides when nothing is
 * running; when there's live work it shows a pulsing count that opens a portal
 * popover of in-progress runs, live sessions and due checks — each navigable,
 * with an inline Stop for runs and sessions. Portaled + flip/clamp so it never
 * gets clipped by the overflow-hidden sidebar.
 */
export const ActivityIndicator: React.FC<{ floating?: boolean }> = ({ floating = false }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { runs, sessions, monitors, total, refresh } = useActivity();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const btnRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [style, setStyle] = useState<React.CSSProperties | null>(null);

  // Position the portaled panel relative to the trigger, flipping up/down by
  // available space and clamping horizontally so it never leaves the viewport
  // (mirrors NotificationBell — the trigger lives at the bottom of a clipped
  // sidebar, so it usually opens upward).
  const reposition = useCallback(() => {
    const b = btnRef.current?.getBoundingClientRect();
    if (!b) return;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const width = Math.min(PANEL_W, vw - MARGIN * 2);
    const left = Math.max(MARGIN, Math.min(b.left, vw - MARGIN - width));
    const spaceBelow = vh - b.bottom;
    const spaceAbove = b.top;
    const openDown = spaceBelow >= 380 || spaceBelow >= spaceAbove;
    const maxHeight = Math.max(220, (openDown ? spaceBelow : spaceAbove) - MARGIN * 2);
    setStyle({
      position: 'fixed',
      left,
      width,
      maxHeight,
      ...(openDown ? { top: b.bottom + MARGIN } : { bottom: vh - b.top + MARGIN }),
    });
  }, []);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      const node = e.target as Node;
      if (btnRef.current?.contains(node) || panelRef.current?.contains(node)) return;
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

  useLayoutEffect(() => {
    if (open) reposition();
  }, [open, reposition]);
  useEffect(() => {
    if (!open) return;
    refresh(); // freshen the list the moment it opens
    const handler = () => reposition();
    window.addEventListener('resize', handler);
    window.addEventListener('scroll', handler, true);
    return () => {
      window.removeEventListener('resize', handler);
      window.removeEventListener('scroll', handler, true);
    };
  }, [open, reposition, refresh]);

  // Auto-hide when idle: close a stale-open panel if everything finished. Done
  // during render (the indicator unmounts its panel on the very next line, so an
  // effect would only queue a second pass to record what is already true).
  if (total === 0 && open) setOpen(false);

  if (total === 0) return null;

  const go = (to: string | null | undefined, fallback: string) => {
    setOpen(false);
    navigate(to || fallback);
  };

  const withBusy = async (key: string, fn: () => Promise<void>, okMsg: string) => {
    setBusy((b) => ({ ...b, [key]: true }));
    try {
      await fn();
      toast.success(okMsg);
      refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t('Something went wrong'));
    } finally {
      setBusy((b) => ({ ...b, [key]: false }));
    }
  };

  const stopRun = (run: Run) =>
    withBusy(`run-${run.id}`, () => runsApi.cancel(run), t('Run stopped'));
  const stopSession = (s: StreamingSession) =>
    withBusy(`session-${s.session_key}`, async () => {
      await streamingApi.endSession(s.session_key);
    }, t('Session stopped'));

  const runSub = (run: Run) =>
    run.started_at ? t('Running · {{ago}}', { ago: timeAgo(run.started_at) }) : run.trigger_source || '';

  // Active monitors sorted by "live" (overdue) first, then by soonest next check.
  const sortedMonitors = [...monitors].sort((a, b) => {
    const ta = monitorTiming(a as any);
    const tb = monitorTiming(b as any);
    if (ta.live !== tb.live) return ta.live ? -1 : 1;
    const na = ta.nextRun ? new Date(ta.nextRun).getTime() : Infinity;
    const nb = tb.nextRun ? new Date(tb.nextRun).getTime() : Infinity;
    return na - nb;
  });
  const badge = total > 99 ? '99+' : String(total);

  return (
    <div className="relative shrink-0">
      <button
        ref={btnRef}
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="true"
        aria-expanded={open}
        title={t('Live activity')}
        aria-label={t('{{n}} running', { n: total })}
        className={clsx(
          floating
            // Prominent floating pill: a solid dark chip with a pulsing amber
            // "live" dot + count + label + shadow, so a running job is unmissable
            // in the corner.
            ? 'flex items-center gap-2 h-10 rounded-full bg-accent-strong pl-3 pr-4 text-accent-on shadow-lg ring-1 ring-black/10 hover:bg-accent-strong/90 active:scale-[0.98] transition-all'
            : 'relative shrink-0 rounded-md p-1.5 text-zinc-400 hover:bg-white/80 hover:text-ink transition-colors',
        )}
      >
        {floating ? (
          <>
            <span className="relative flex h-2.5 w-2.5" aria-hidden="true">
              <span className="absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-70 animate-ping" />
              <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-amber-400" />
            </span>
            <span className="text-[13px] font-semibold tabular-nums leading-none">{badge}</span>
            <span className="text-[12px] font-medium leading-none text-surface/75">{t('live')}</span>
          </>
        ) : (
          <>
            <SignalIcon className="h-4 w-4" />
            <span className="absolute -right-0.5 -top-0.5 flex h-[15px] min-w-[15px] items-center justify-center rounded-full bg-amber-500 px-1 text-[9px] font-semibold leading-none tabular-nums text-white">
              {badge}
            </span>
          </>
        )}
      </button>

      {open &&
        style &&
        createPortal(
          <div
            ref={panelRef}
            style={style}
            className="z-[60] flex flex-col overflow-hidden rounded-2xl border border-border bg-surface shadow-lg"
          >
            <div className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
              <h3 className="text-sm font-semibold text-ink">{t('Live activity')}</h3>
              <span className="text-[11px] tabular-nums text-tertiary">
                {t('{{count}} active', { count: total })}
              </span>
            </div>

            <div className="flex-1 overflow-y-auto py-1">
              {runs.length > 0 && (
                <Section icon={BoltIcon} label={t('Running workflows')} count={runs.length}>
                  {runs.map((run) => (
                    <Row
                      key={run.id}
                      title={run.entity_name || t('Workflow run')}
                      sub={runSub(run)}
                      onOpen={() => go(run.detail_url_hint, '/runs')}
                      onStop={runsApi.isCancellable(run) ? () => stopRun(run) : undefined}
                      busy={busy[`run-${run.id}`]}
                    />
                  ))}
                </Section>
              )}

              {sessions.length > 0 && (
                <Section icon={SignalIcon} label={t('Live sessions')} count={sessions.length}>
                  {sessions.map((s) => (
                    <Row
                      key={s.session_key}
                      title={hostOf(s.current_url || s.target_url) || t('Live session')}
                      sub={t('Session · {{ago}}', { ago: timeAgo(s.started_at) })}
                      onOpen={() => go(`/streaming/${s.session_key}`, '/streaming')}
                      onStop={() => stopSession(s)}
                      busy={busy[`session-${s.session_key}`]}
                    />
                  ))}
                </Section>
              )}

              {sortedMonitors.length > 0 && (
                <Section icon={EyeIcon} label={t('Monitors')} count={sortedMonitors.length}>
                  {sortedMonitors.map((target) => (
                    <MonitorRow
                      key={target.id}
                      target={target}
                      onOpen={() => go(`/checks/${target.id}`, '/checks')}
                    />
                  ))}
                </Section>
              )}
            </div>

            <button
              onClick={() => go('/runs', '/runs')}
              className="w-full shrink-0 border-t border-border px-4 py-2.5 text-xs font-medium text-ink hover:bg-hover transition-colors"
            >
              {t('View all runs')}
            </button>
          </div>,
          document.body,
        )}
    </div>
  );
};

export default ActivityIndicator;
