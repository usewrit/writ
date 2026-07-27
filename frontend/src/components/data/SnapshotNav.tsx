import React, { useMemo } from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { ChevronLeftIcon, ChevronRightIcon } from '@heroicons/react/24/outline';
import { isToday, isYesterday } from 'date-fns';
import { Select } from '../ui';
import { Popover } from './Popover';
import type { SnapshotDelta, SnapshotRun } from '../../api/workflowData';
import { uiLocale } from '../../utils/format';

/** Compact absolute run date+time, e.g. "Jun 21, 10:03". */
function fmtRunDate(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return (
    d.toLocaleDateString(uiLocale(), { month: 'short', day: 'numeric' }) +
    ' ' +
    d.toLocaleTimeString(uiLocale(), { hour: '2-digit', minute: '2-digit' })
  );
}

/**
 * Plain-text snapshot delta `+2 ~1 −3` — tabular-nums, colored ONLY on nonzero
 * segments, never a fill (delta summaries are text, the row's pill is the one
 * pill). Zero segments are dropped; an all-zero delta reads "no change".
 */
export const DeltaText: React.FC<{ delta: SnapshotDelta; className?: string }> = ({ delta, className }) => {
  const { t } = useTranslation();
  const segs: { text: string; cls: string }[] = [];
  if (delta.new > 0) segs.push({ text: `+${delta.new}`, cls: 'text-emerald-700' });
  if (delta.changed > 0) segs.push({ text: `~${delta.changed}`, cls: 'text-amber-700' });
  if (delta.removed > 0) segs.push({ text: `−${delta.removed}`, cls: 'text-red-600' });
  if (segs.length === 0) return <span className={clsx('text-tertiary', className)}>{t('no change')}</span>;
  return (
    <span className={clsx('inline-flex items-center gap-1 tabular-nums', className)}>
      {segs.map((s) => (
        <span key={s.text} className={s.cls}>
          {s.text}
        </span>
      ))}
    </span>
  );
};

/** Locale-aware day header for the snapshot popover groups. */
function dayLabel(iso: string | null, t: (k: string) => string): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  if (isToday(d)) return t('Today');
  if (isYesterday(d)) return t('Yesterday');
  const opts: Intl.DateTimeFormatOptions =
    d.getFullYear() === new Date().getFullYear()
      ? { weekday: 'short', month: 'short', day: 'numeric' }
      : { month: 'short', day: 'numeric', year: 'numeric' };
  return d.toLocaleDateString(uiLocale(), opts);
}

/**
 * The By-date lens's snapshot stepper: `‹ Jun 21, 10:03 ›` steps run-to-run and
 * the label opens a popover of every snapshot grouped by day. Selection is
 * ephemeral component state owned by the parent — never URL, never localStorage.
 * On <md the stepper renders as a full-width Select instead.
 */
export const SnapshotNav: React.FC<{
  /** Chain members, newest first (the /data/runs order). */
  runs: SnapshotRun[];
  selectedRunId: number | null;
  onSelect: (runId: number) => void;
}> = ({ runs, selectedRunId, onSelect }) => {
  const { t } = useTranslation();
  const idx = runs.findIndex((r) => r.run_id === selectedRunId);
  const selected = idx >= 0 ? runs[idx] : null;
  // runs are newest-first: "‹" walks to the older neighbour, "›" to the newer.
  const older = idx >= 0 && idx < runs.length - 1 ? runs[idx + 1] : null;
  const newer = idx > 0 ? runs[idx - 1] : null;

  const groups = useMemo(() => {
    const out: { label: string; runs: SnapshotRun[] }[] = [];
    for (const r of runs) {
      const label = dayLabel(r.run_at, t);
      const last = out[out.length - 1];
      if (last && last.label === label) last.runs.push(r);
      else out.push({ label, runs: [r] });
    }
    return out;
  }, [runs, t]);

  const timeOf = (iso: string | null) =>
    iso ? new Date(iso).toLocaleTimeString(uiLocale(), { hour: '2-digit', minute: '2-digit' }) : '—';

  const stepBtn = (target: SnapshotRun | null, icon: React.ReactNode, label: string) => (
    <button
      type="button"
      disabled={!target}
      onClick={() => target && onSelect(target.run_id)}
      aria-label={label}
      title={target ? fmtRunDate(target.run_at) : undefined}
      className="inline-flex items-center justify-center px-1.5 py-1.5 text-secondary transition-colors hover:text-ink disabled:opacity-40"
    >
      {icon}
    </button>
  );

  return (
    <>
      {/* Inline stepper — needs a card wide enough to spare ~300px */}
      <div className="hidden items-center overflow-hidden rounded-lg border border-border @pair/stage:inline-flex">
        {stepBtn(older, <ChevronLeftIcon className="h-3.5 w-3.5" />, t('Older snapshot'))}
        <Popover
          width={300}
          trigger={
            <span className="inline-flex items-center border-x border-border px-2.5 py-1.5 text-[12px] font-medium tabular-nums text-ink">
              {selected ? fmtRunDate(selected.run_at) : t('Pick a snapshot')}
            </span>
          }
        >
          {(close) => (
            <div className="max-h-80 overflow-y-auto">
              {groups.map((g) => (
                <div key={g.label}>
                  <div className="px-2 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wide text-tertiary">
                    {g.label}
                  </div>
                  {g.runs.map((r) => {
                    const active = r.run_id === selectedRunId;
                    return (
                      <button
                        key={r.run_id}
                        onClick={() => {
                          onSelect(r.run_id);
                          close();
                        }}
                        className={clsx(
                          'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12px] transition-colors',
                          active ? 'bg-ink/15 text-ink' : 'text-secondary hover:bg-hover hover:text-ink',
                        )}
                      >
                        <span className="tabular-nums">{timeOf(r.run_at)}</span>
                        <span className="tabular-nums text-tertiary">
                          {t('{{count}} records', { count: r.record_count })}
                        </span>
                        <span className="ml-auto text-[11px]">
                          {r.explicit_empty ? (
                            <span className="text-tertiary">{t('empty')}</span>
                          ) : r.delta ? (
                            <DeltaText delta={r.delta} />
                          ) : (
                            <span className="text-tertiary">—</span>
                          )}
                        </span>
                      </button>
                    );
                  })}
                </div>
              ))}
            </div>
          )}
        </Popover>
        {stepBtn(newer, <ChevronRightIcon className="h-3.5 w-3.5" />, t('Newer snapshot'))}
      </div>

      {/* Cramped fallback: full-width picker */}
      <div className="w-full @pair/stage:hidden">
        <Select<number>
          size="sm"
          value={selectedRunId}
          onChange={(v) => onSelect(v)}
          aria-label={t('Snapshot')}
          options={runs.map((r) => ({
            value: r.run_id,
            label: `${fmtRunDate(r.run_at)} · ${r.explicit_empty ? t('empty') : t('{{count}} records', { count: r.record_count })}`,
          }))}
        />
      </div>
    </>
  );
};

export default SnapshotNav;
