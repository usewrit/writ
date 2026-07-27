import React from 'react';
import clsx from 'clsx';

export interface StatBarItem {
  /** Small uppercase label. */
  label: string;
  /** Primary value (string or node). */
  value: React.ReactNode;
  /** Optional secondary line under the value. */
  sub?: React.ReactNode;
  /** Optional inline action shown top-right of the segment (e.g. "Change"). */
  action?: { label: string; onClick: () => void };
}

/**
 * KPI stat-bar — a single bordered bar split into segments by thin separators,
 * each segment a labelled metric. The app's canonical way to lead a page with
 * its key numbers WITHOUT a pile of separate cards (see Billing for the
 * reference usage). Adapt the items per page; the chrome stays consistent.
 */
export const StatBar: React.FC<{ items: StatBarItem[]; className?: string }> = ({ items, className }) => {
  // The horizontal rule only exists to separate stacked ROWS, so it has to
  // disappear at whatever width that item count reaches a single row.
  const cols =
    items.length >= 4 ? 'grid-cols-2 @split/stage:grid-cols-4 @split/stage:divide-y-0'
    : items.length === 3 ? 'grid-cols-1 @rail/stage:grid-cols-3 @rail/stage:divide-y-0'
    : items.length === 2 ? 'grid-cols-1 @pair/stage:grid-cols-2 @pair/stage:divide-y-0'
    : 'grid-cols-1 divide-y-0';
  return (
    <div className={clsx('bg-surface rounded-xl border border-ink/20 shadow-sm grid divide-x divide-y divide-border', cols, className)}>
      {items.map((it, i) => (
        <div key={i} className="p-5 min-w-0">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11px] uppercase tracking-wide text-tertiary font-medium truncate">{it.label}</span>
            {it.action && (
              <button onClick={it.action.onClick} className="text-[11px] font-medium text-secondary hover:text-ink transition-colors shrink-0">
                {it.action.label}
              </button>
            )}
          </div>
          <div className="text-xl font-semibold text-ink mt-1.5 tabular-nums truncate">{it.value}</div>
          {it.sub != null && <div className="text-xs text-tertiary mt-0.5 truncate">{it.sub}</div>}
        </div>
      ))}
    </div>
  );
};
