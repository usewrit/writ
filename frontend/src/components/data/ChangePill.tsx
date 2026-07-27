import React from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import type { LineageChange } from '../../api/workflowData';

/**
 * The per-row change pill for the lineage lenses. OUTLINE only (WRIT_UI_SYSTEM
 * rations filled pills — this is the row's one meaningful pill); `same` renders
 * nothing so unchanged rows stay quiet. `removed` covers the By-date lens's
 * dimmed removed-records group.
 */
export const ChangePill: React.FC<{
  change: LineageChange | 'removed';
  className?: string;
}> = ({ change, className }) => {
  const { t } = useTranslation();
  if (change === 'same') return null;
  const label =
    change === 'new'
      ? t('New record')
      : change === 'changed'
        ? t('Updated record')
        : change === 'missing'
          ? t('Gone')
          : t('Removed');
  return (
    <span
      className={clsx(
        'inline-flex items-center whitespace-nowrap rounded-full border bg-transparent px-2 py-px text-[10px] font-medium',
        change === 'new' && 'border-emerald-300 text-emerald-700',
        change === 'changed' && 'border-amber-300 text-amber-700',
        (change === 'missing' || change === 'removed') && 'border-border text-tertiary',
        className,
      )}
    >
      {label}
    </span>
  );
};

/**
 * The slim change-signal dot for the table's leading ≈28px column — color
 * carries the change kind, the title tooltip carries the words. `same` renders
 * nothing so unchanged rows stay perfectly quiet; the column itself only
 * exists when the loaded window has signal.
 */
export const ChangeDot: React.FC<{
  change: LineageChange | 'removed';
  /** Changed leaf dot-paths — their top segments feed the tooltip. */
  changedFields?: string[];
  /** Noise-capped row (most fields changed): per-cell tint is suppressed upstream. */
  noisy?: boolean;
  className?: string;
}> = ({ change, changedFields, noisy, className }) => {
  const { t } = useTranslation();
  if (change === 'same') return null;
  const tops = [...new Set((changedFields ?? []).map((p) => p.split('.')[0]))];
  const label =
    change === 'new'
      ? t('New record')
      : change === 'changed'
        ? noisy
          ? t('Updated · most fields')
          : tops.length
            ? t('Updated — {{fields}}', { fields: tops.join(', ') })
            : t('Updated record')
        : change === 'missing'
          ? t('Not in latest run')
          : t('Removed');
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className={clsx(
        'block h-2 w-2 rounded-full',
        change === 'new' && 'bg-emerald-500',
        change === 'changed' && 'bg-amber-500',
        (change === 'missing' || change === 'removed') && 'border border-tertiary/50 bg-transparent',
        className,
      )}
    />
  );
};

export default ChangePill;
