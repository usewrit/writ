import React, { useEffect, useMemo, useState } from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { workflowDataApi, type RecordHistoryResponse, type RecordVersion } from '../../api/workflowData';
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

function isPlainObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** Resolve a changed-fields leaf dot-path (dict-keyed segments only — arrays are leaves). */
function leafValue(fields: Record<string, unknown>, path: string): unknown {
  return path.split('.').reduce<unknown>((acc, k) => (isPlainObj(acc) ? acc[k] : undefined), fields);
}

function leafText(v: unknown): string {
  if (v === null || v === undefined || v === '') return '—';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/** −/+ before/after rows in the monitor-diff convention (− tertiary, + ink, mono). */
const DiffPair: React.FC<{ oldText: string; newText: string }> = ({ oldText, newText }) => (
  <div className="overflow-hidden rounded-md border border-border bg-canvas">
    <div className="flex gap-1.5 whitespace-pre-wrap break-all px-2 py-0.5 font-mono text-[10.5px] leading-relaxed text-tertiary">
      <span className="shrink-0 select-none opacity-70">−</span>
      <span className="min-w-0">{oldText}</span>
    </div>
    <div className="flex gap-1.5 whitespace-pre-wrap break-all px-2 py-0.5 font-mono text-[10.5px] leading-relaxed text-ink">
      <span className="shrink-0 select-none opacity-70">+</span>
      <span className="min-w-0">{newText}</span>
    </div>
  </div>
);

/** One change-point: dated header + its changed leaves as −/+ pairs. */
const VersionEntry: React.FC<{
  version: RecordVersion;
  /** The chronologically previous change-point (null for the first). */
  prev: RecordVersion | null;
  isFirst: boolean;
}> = ({ version, prev, isFirst }) => {
  const { t } = useTranslation();
  // >20 changed leaves: per-field pairs would be noise — fall back to whole-record JSON −/+.
  const blobFallback = (version.changed_leaf_count ?? 0) > 20;
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline gap-2 text-[11px]">
        <span className="tabular-nums font-medium text-ink">{fmtRunDate(version.run_at)}</span>
        {isFirst ? (
          <span className="text-tertiary">{t('First seen')}</span>
        ) : blobFallback ? (
          <span className="text-tertiary">{t('{{count}} values changed', { count: version.changed_leaf_count })}</span>
        ) : null}
      </div>
      {!isFirst &&
        prev &&
        (blobFallback ? (
          <DiffPair oldText={JSON.stringify(prev.fields, null, 1)} newText={JSON.stringify(version.fields, null, 1)} />
        ) : (
          <div className="space-y-1">
            {version.changed_fields.map((path) => (
              <div key={path}>
                <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-tertiary">{path}</div>
                <DiffPair oldText={leafText(leafValue(prev.fields, path))} newText={leafText(leafValue(version.fields, path))} />
              </div>
            ))}
          </div>
        ))}
    </div>
  );
};

/**
 * The expanded record's History section — fetched from
 * GET .../data/records/{uid}/history on expand. Change-points only (the server
 * already collapses identical re-extractions), newest first, the oldest labeled
 * "First seen". `keyFields` MUST echo the identity the uid was computed with;
 * an unknown uid 404s with the current identity, which we re-key on once.
 */
export const RecordHistory: React.FC<{
  workflowId: number;
  recordUid: string;
  keyFields: string[];
  /** Scan-window note: set when the lineage scan was truncated. */
  truncated?: boolean;
  scannedRuns?: number;
}> = ({ workflowId, recordUid, keyFields, truncated, scannedRuns }) => {
  const { t } = useTranslation();
  const keyJoined = keyFields.join(',');
  // The outcome is stored WITH the request it answers, so "still loading" and
  // "this result belongs to the record on screen" are derived during render.
  // Resetting them from the effect instead would cost a render pass per expand
  // AND leave the previous record's history visible until that pass landed.
  const reqKey = `${workflowId}|${recordUid}|${keyJoined}`;
  const [result, setResult] = useState<{ key: string; history: RecordHistoryResponse | null } | null>(null);
  const current = result && result.key === reqKey ? result : null;
  const loading = current == null;
  const history = current?.history ?? null;
  const failed = current != null && current.history == null;

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await workflowDataApi.getHistory(workflowId, recordUid, keyFields);
        if (!cancelled) setResult({ key: reqKey, history: res });
      } catch (err: unknown) {
        // 404 bodies include the CURRENT identity — re-key and retry once.
        const resp = (err as { response?: { status?: number; data?: { identity?: { fields?: string[] } } } }).response;
        const newKey = resp?.status === 404 ? resp.data?.identity?.fields : undefined;
        if (newKey && newKey.length && newKey.join(',') !== keyJoined) {
          try {
            const res = await workflowDataApi.getHistory(workflowId, recordUid, newKey);
            if (!cancelled) setResult({ key: reqKey, history: res });
            return;
          } catch {
            /* fall through */
          }
        }
        if (!cancelled) setResult({ key: reqKey, history: null });
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowId, recordUid, keyJoined]);

  // Newest first for display; entries pair with their chronological predecessor.
  const entries = useMemo(() => {
    const versions = history?.versions ?? [];
    return versions
      .map((v, i) => ({ version: v, prev: i > 0 ? versions[i - 1] : null, isFirst: i === 0 }))
      .reverse();
  }, [history]);

  // "Changed vs Jun 11: comments, title" — the newest change-point vs its predecessor.
  const changedVs = useMemo(() => {
    const versions = history?.versions ?? [];
    if (versions.length < 2) return null;
    const last = versions[versions.length - 1];
    const prevAt = versions[versions.length - 2].run_at;
    if (!last.changed_fields.length) return null;
    const tops = [...new Set(last.changed_fields.map((p) => p.split('.')[0]))];
    return { date: fmtRunDate(prevAt), fields: tops.join(', ') };
  }, [history]);

  return (
    <div className="rounded-lg border border-border bg-surface px-3 py-2">
      <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2">
        <div className="text-[10px] font-semibold uppercase tracking-wide text-tertiary">{t('History')}</div>
        {history && (
          <span
            className="text-[10px] tabular-nums text-tertiary"
            title={truncated && scannedRuns ? t('within the last {{count}} runs', { count: scannedRuns }) : undefined}
          >
            {t('First seen {{date}}', { date: fmtRunDate(history.first_seen_at) })}
            {' · '}
            {t('Last seen {{date}}', { date: fmtRunDate(history.last_seen_at) })}
          </span>
        )}
      </div>
      {changedVs && (
        <p className="mb-2 text-[11px] text-secondary">
          {t('Changed vs {{date}}: {{fields}}', { date: changedVs.date, fields: changedVs.fields })}
        </p>
      )}
      {loading ? (
        <div className="space-y-1.5 py-1" aria-busy="true" aria-label={t('Loading…')}>
          <div className="h-3 w-1/3 animate-pulse rounded bg-hover" />
          <div className="h-3 w-1/2 animate-pulse rounded bg-hover" />
        </div>
      ) : failed ? (
        <p className="text-[11px] text-tertiary">{t('History unavailable.')}</p>
      ) : entries.length === 0 ? (
        <p className="text-[11px] text-tertiary">{t('No history yet.')}</p>
      ) : (
        <div className={clsx('space-y-3', entries.length > 1 && 'pt-0.5')}>
          {entries.map((e) => (
            <VersionEntry key={`${e.version.run_id}-${e.version.record_index}`} version={e.version} prev={e.prev} isFirst={e.isFirst} />
          ))}
        </div>
      )}
    </div>
  );
};

export default RecordHistory;
