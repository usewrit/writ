import React from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { uiLocale } from '../../utils/format';

interface UsageMeterProps {
  label: string;
  used: number;
  /** -1 (or any negative) = unlimited. */
  limit: number;
  /** Optional unit suffix, e.g. "GB". */
  unit?: string;
  /** Neutral note under the bar (e.g. "Resets monthly"); replaced by the
   *  approaching/over-limit warning when relevant. */
  note?: string;
  /** Format used + limit numbers (default: locale integer). */
  format?: (n: number) => string;
}

/**
 * A single usage/limit meter — the canonical primitive for "X / Y used".
 * Monochrome by default; the bar adopts the one allowed status color as it
 * approaches (amber ≥75%) or hits (red ≥90%) the limit, so over-consumption is
 * impossible to miss. Unlimited plans show "∞" with no (meaningless) bar fill.
 */
export const UsageMeter: React.FC<UsageMeterProps> = ({ label, used, limit, unit, note, format }) => {
  const { t } = useTranslation();
  const unlimited = limit < 0;
  const pct = unlimited || limit === 0 ? 0 : Math.min(100, (used / limit) * 100);
  const near = pct >= 75 && pct < 90;
  const over = pct >= 90;
  const fmt = format || ((n) => n.toLocaleString(uiLocale()));
  const u = unit ? ` ${unit}` : '';

  return (
    <div>
      <div className="flex items-center justify-between gap-2 mb-1.5">
        <span className="text-xs text-secondary truncate">{label}</span>
        <span className="text-xs font-medium text-ink tabular-nums shrink-0">
          {fmt(used)}{u}
          <span className="text-tertiary"> / {unlimited ? '∞' : `${fmt(limit)}${u}`}</span>
        </span>
      </div>
      <div className="h-1.5 bg-hover rounded-full overflow-hidden">
        <div
          className={clsx(
            'h-full rounded-full transition-all duration-500',
            over ? 'bg-red-500' : near ? 'bg-amber-500' : 'bg-ink',
          )}
          style={{ width: unlimited ? '0%' : `${pct}%` }}
        />
      </div>
      {(over || near || note) && (
        <p
          className={clsx(
            'text-[11px] mt-1',
            over ? 'text-red-600' : near ? 'text-amber-600' : 'text-tertiary',
          )}
        >
          {over ? t('Limit reached') : near ? t('Approaching limit') : note}
        </p>
      )}
    </div>
  );
};
