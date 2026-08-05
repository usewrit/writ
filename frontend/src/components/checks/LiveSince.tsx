import React from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { formatElapsed, formatElapsedShort } from '../../utils/format';

/**
 * Re-render once a second while `active`, so live elapsed displays keep moving,
 * and hand back the timestamp of the current tick.
 *
 * Callers must measure against the returned value rather than calling
 * `Date.now()` themselves: the clock advances without React knowing, so reading
 * it during render is impure and two renders of the same commit can disagree
 * (`react-hooks/purity`). While `active` is false the value is frozen — which is
 * exactly what the paused displays below document.
 */
export function useSecondTick(active: boolean): number {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [active]);
  return now;
}

/** Magnitude of a FUTURE delta ("45s", "4m 48s", "2h 03m", "1d 04h"). Language-neutral. */
function formatRemaining(seconds: number): string {
  let s = Math.max(0, Math.floor(seconds));
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  if (d > 0) return `${d}d ${String(h).padStart(2, '0')}h`;
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}

interface LiveElapsedProps {
  /** ISO timestamp of the last check. */
  since?: string | null;
  /** While true the value ticks every second; while false it's frozen (paused). */
  live?: boolean;
  className?: string;
  /** Shown when there's no timestamp yet (never checked). */
  fallback?: React.ReactNode;
}

/**
 * Bare, second-ticking elapsed magnitude ("3m 05s") — the principal metric for
 * the monitor hero. Resets when a fresh `since` lands from polling. The tick
 * lives here so the per-second re-render stays scoped to this node, not the
 * whole page.
 */
export const LiveElapsed: React.FC<LiveElapsedProps> = ({ since, live = true, className, fallback = '—' }) => {
  useSecondTick(Boolean(live && since));
  if (!since) return <span className={className}>{fallback}</span>;
  return <span className={clsx('tabular-nums', className)}>{formatElapsed(since)}</span>;
};

interface LiveSinceProps {
  since?: string | null;
  /**
   * When true the timer keeps ticking and shows a live pulse; when false
   * (monitor paused / never checked) it renders the static elapsed text with
   * no ticking — nothing new will land, so there's nothing to count up to.
   */
  live?: boolean;
  className?: string;
}

/**
 * Inline "Checked 3m 05s ago" with a live pulse — for list rows / compact spots.
 */
export const LiveSince: React.FC<LiveSinceProps> = ({ since, live = true, className }) => {
  const { t } = useTranslation();
  useSecondTick(Boolean(live && since));

  if (!since) return <span className={className}>{t('Not checked yet')}</span>;

  return (
    <span className={clsx('inline-flex items-center gap-1.5 tabular-nums', className)}>
      {live && (
        <span className="relative flex h-1.5 w-1.5" aria-hidden="true">
          <span className="absolute inline-flex h-full w-full rounded-full bg-green-500 opacity-60 animate-ping" />
          <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-green-500" />
        </span>
      )}
      {t('Checked {{when}} ago', { when: formatElapsedShort(since) })}
    </span>
  );
};

interface LiveCountdownProps {
  /** ISO timestamp of the next scheduled check (`next_run_at`, or derived). */
  until?: string | null;
  /** While true the value ticks down every second; while false it's frozen. */
  live?: boolean;
  className?: string;
}

/**
 * Live "Next check in 4m 48s" countdown to `until`, ticking every second so the
 * list shows the real cadence in motion. Once the target time passes it reads
 * "Checking now…" until the next poll lands a fresh schedule. The tick is scoped
 * to this node so only the countdown re-renders, not the whole row.
 */
export const LiveCountdown: React.FC<LiveCountdownProps> = ({ until, live = true, className }) => {
  const { t } = useTranslation();
  const now = useSecondTick(Boolean(live && until));
  if (!until) return null;
  const ms = new Date(until).getTime();
  if (isNaN(ms)) return null;
  const remaining = (ms - now) / 1000;
  if (remaining <= 1) {
    return <span className={clsx('tabular-nums', className)}>{t('Checking now…')}</span>;
  }
  return (
    <span className={clsx('tabular-nums', className)}>
      {t('Next check in {{when}}', { when: formatRemaining(remaining) })}
    </span>
  );
};

interface IntervalProgressProps {
  /** ISO of the last check (bar origin). */
  since?: string | null;
  /** ISO of the next scheduled check (bar target). */
  until?: string | null;
  /** While true the fill advances every second; while false it's frozen. */
  live?: boolean;
  /** Tailwind class for the FILL — pass a tone (e.g. `bg-green-500/60`, `bg-amber-500/70`). */
  fillClassName?: string;
  className?: string;
}

/**
 * Thin progress bar that fills from `since`→`until` (last check → next check),
 * advancing once a second so the cadence is visible at a glance. The fill colour
 * is caller-supplied so the bar can carry the row's health tone (the one place
 * we spend colour). Renders nothing without both endpoints.
 */
export const IntervalProgress: React.FC<IntervalProgressProps> = ({
  since,
  until,
  live = true,
  fillClassName = 'bg-green-500/60',
  className,
}) => {
  const now = useSecondTick(Boolean(live && since && until));
  if (!since || !until) return null;
  const start = new Date(since).getTime();
  const end = new Date(until).getTime();
  if (isNaN(start) || isNaN(end) || end <= start) return null;
  const pct = Math.max(0, Math.min(1, (now - start) / (end - start)));
  return (
    <div className={clsx('h-1 w-full rounded-full bg-hover overflow-hidden', className)} aria-hidden="true">
      <div
        className={clsx('h-full rounded-full', !live && 'opacity-40', fillClassName)}
        style={{ width: `${(pct * 100).toFixed(1)}%` }}
      />
    </div>
  );
};
