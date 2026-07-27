import React, { useEffect, useRef, useState } from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { CheckIcon, FingerPrintIcon } from '@heroicons/react/24/outline';
import { Popover } from './Popover';
import { shelfFilterChipClass } from '../library/shelf';
import { workflowDataApi, type DataIdentity } from '../../api/workflowData';

/**
 * "Grouped by url" — the identity chip for the lineage lenses. Opens a popover
 * explaining how records are matched across runs, with a column checklist to
 * override the key, a live merge preview (a real view=latest refetch with the
 * candidate key), and a reset back to automatic. The chosen key is persisted by
 * the PARENT (localStorage `writ.dataKey.{workflowId}`) and applies per device.
 */
export const IdentityChip: React.FC<{
  workflowId: number;
  identity: DataIdentity;
  /** Current data columns — the checklist source. */
  columns: string[];
  /** The applied custom key, null when automatic. */
  overrideKey: string[] | null;
  onApply: (fields: string[] | null) => void;
  /** All-rows total across snapshots (Σ record_count from /data/runs), for the preview line. */
  rowsTotal: number | null;
}> = ({ workflowId, identity, columns, overrideKey, onApply, rowsTotal }) => {
  const { t } = useTranslation();
  // The checklist is seeded from the applied key and re-seeded when it changes.
  // Adjusted DURING RENDER (React's derive-state-from-props escape hatch) rather
  // than from an effect, so the popover never paints the previous key first.
  const appliedKey = overrideKey ?? identity.fields;
  const [seededFrom, setSeededFrom] = useState<string[]>(appliedKey);
  const [picked, setPicked] = useState<string[]>(appliedKey);
  if (seededFrom !== appliedKey) {
    setSeededFrom(appliedKey);
    setPicked(appliedKey);
  }

  // Live preview: refetch view=latest with the candidate key (limit 1 — only the
  // counts matter). Debounced; a stale response is dropped via the seq guard.
  // The result is stored WITH the key it was computed for, which makes both
  // "still checking" and "this preview is for the current selection" derivable
  // during render — no effect has to write a loading flag.
  const [preview, setPreview] = useState<{
    key: string[];
    data: { records: number; rejected: boolean } | null;
  } | null>(null);
  const previewData = preview && preview.key === picked ? preview.data : null;
  const previewLoading = picked.length > 0 && preview?.key !== picked;
  const seqRef = useRef(0);
  useEffect(() => {
    if (picked.length === 0) return;
    const seq = ++seqRef.current;
    const timer = setTimeout(async () => {
      try {
        const res = await workflowDataApi.getTable(workflowId, { view: 'latest', key: picked, limit: 1 });
        if (seq !== seqRef.current) return;
        const c = res.counts;
        setPreview({
          key: picked,
          data: {
            records: c ? c.new + c.changed + c.same + c.missing : res.total,
            rejected: Boolean(res.identity?.requested_key_rejected?.length),
          },
        });
      } catch {
        // Record the failure against this key too, so the line stops saying
        // "Checking…" forever instead of falling back to no preview.
        if (seq === seqRef.current) setPreview({ key: picked, data: null });
      }
    }, 400);
    return () => clearTimeout(timer);
  }, [picked, workflowId]);

  const chipLabel =
    identity.mode === 'hash' && !overrideKey
      ? t('Matching exact duplicates — choose fields to track changes')
      : identity.mode === 'singleton' && identity.fields.length === 0
        ? t('One record per run')
        : t('Grouped by {{fields}}', { fields: (overrideKey ?? identity.fields).join(', ') });

  const toggleField = (c: string) =>
    setPicked((prev) => (prev.includes(c) ? prev.filter((f) => f !== c) : [...prev, c]));

  const dirty =
    picked.length > 0 &&
    JSON.stringify([...picked].sort()) !== JSON.stringify([...(overrideKey ?? identity.fields)].sort());

  return (
    <Popover
      width={280}
      trigger={
        <span
          className={clsx(shelfFilterChipClass(false), 'max-w-[280px]')}
          title={overrideKey ? t('Custom key — applies on this device only') : undefined}
        >
          <FingerPrintIcon className="h-3 w-3 shrink-0 text-tertiary" />
          <span className="truncate">{chipLabel}</span>
          {overrideKey && <span className="shrink-0 text-tertiary">· {t('custom')}</span>}
        </span>
      }
    >
      {(close) => (
        <div className="space-y-2 p-1">
          <div className="text-[12px] font-medium text-ink">{t('How records are matched across runs')}</div>
          <p className="text-[11px] leading-relaxed text-tertiary">
            {identity.mode === 'hash' && !overrideKey
              ? t('No stable field was found, so only exact duplicates merge. Pick the fields that identify a record.')
              : t('Records sharing the same value in these fields are treated as versions of one record.')}
          </p>
          <div className="max-h-48 space-y-0.5 overflow-y-auto">
            {columns.map((c) => {
              const on = picked.includes(c);
              return (
                <button
                  key={c}
                  onClick={() => toggleField(c)}
                  className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-[12px] text-secondary transition-colors hover:bg-hover hover:text-ink"
                >
                  <span
                    className={clsx(
                      'flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border',
                      on ? 'border-ink bg-ink text-white' : 'border-zinc-300 bg-surface',
                    )}
                  >
                    {on && <CheckIcon className="h-2.5 w-2.5" />}
                  </span>
                  <span className="truncate">{c}</span>
                </button>
              );
            })}
            {columns.length === 0 && <p className="px-2 py-1 text-[11px] text-tertiary">{t('No fields.')}</p>}
          </div>
          <div className="min-h-[16px] text-[11px] text-tertiary" aria-live="polite">
            {previewLoading
              ? t('Checking…')
              : previewData?.rejected
                ? t('These fields are empty in most records — automatic matching would be used.')
                : previewData && rowsTotal != null
                  ? t('would merge {{rows}} rows into {{records}} records', { rows: rowsTotal, records: previewData.records })
                  : previewData
                    ? t('{{count}} unique records', { count: previewData.records })
                    : null}
          </div>
          <div className="flex items-center justify-between gap-2 border-t border-border pt-2">
            <button
              onClick={() => {
                onApply(null);
                close();
              }}
              disabled={!overrideKey}
              className="text-[11px] font-medium text-tertiary transition-colors hover:text-ink disabled:opacity-40"
            >
              {t('Reset to automatic')}
            </button>
            <button
              onClick={() => {
                onApply(picked);
                close();
              }}
              disabled={!dirty || picked.length === 0}
              className="rounded-lg bg-accent-strong px-2.5 py-1 text-[11px] font-medium text-accent-on hover:bg-accent-strong/90 disabled:opacity-40"
            >
              {t('Apply')}
            </button>
          </div>
        </div>
      )}
    </Popover>
  );
};

export default IdentityChip;
