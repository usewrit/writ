import React from 'react';
import clsx from 'clsx';

interface BadgeProps {
  children: React.ReactNode;
  /**
   * `live` is the tape-DNA running-state pill ported from the marketing site
   * (the `● RECORDED` / `● API READY` chip): red wash + red type + a pulsing
   * dot. Use it ONLY while something is genuinely in flight — a run executing,
   * a crawl draining, a monitor checking. It is not a "new"/"beta" badge.
   */
  variant?: 'default' | 'success' | 'warning' | 'error' | 'outline' | 'live';
  size?: 'sm' | 'md';
}

export const Badge: React.FC<BadgeProps> = ({
  children,
  variant = 'default',
  size = 'sm',
}) => {
  return (
    <span
      className={clsx(
        'inline-flex items-center font-medium rounded-full transition-colors duration-150',
        size === 'sm' && 'px-2 py-0.5 text-[11px]',
        size === 'md' && 'px-2.5 py-1 text-xs',
        // Scannable status palette: green=success, amber=warning, red=error;
        // default/outline stay neutral gray.
        variant === 'default' && 'border border-border text-secondary',
        variant === 'success' && 'bg-green-50 text-green-700',
        variant === 'warning' && 'bg-amber-50 text-amber-700',
        variant === 'error' && 'bg-red-50 text-red-700',
        variant === 'outline' && 'bg-surface text-secondary border border-border',
        // Running state. `accent-strong` for the type (AA on the wash),
        // `accent` for the dot (a pure graphic — 3:1 applies).
        variant === 'live' && 'gap-1.5 bg-accent/10 text-accent-strong',
      )}
    >
      {variant === 'live' && (
        <span className="relative inline-flex h-1.5 w-1.5 shrink-0" aria-hidden="true">
          {/* Halo is decorative and motion-gated; the dot itself always paints,
              so the state stays visible under prefers-reduced-motion. */}
          <span className="animate-status-pulse absolute inline-flex h-full w-full rounded-full bg-accent opacity-60 motion-reduce:hidden" />
          <span className="relative inline-flex h-full w-full rounded-full bg-accent" />
        </span>
      )}
      {children}
    </span>
  );
};
