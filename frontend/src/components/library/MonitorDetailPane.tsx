import React from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import {
  PlayIcon,
  PauseIcon,
  PencilSquareIcon,
  TrashIcon,
  ArrowTopRightOnSquareIcon,
  GlobeAltIcon,
  ClockIcon,
  SignalIcon,
  EyeIcon,
  BellAlertIcon,
  BoltIcon,
} from '@heroicons/react/24/outline';
import { useQuery } from '../../hooks/useQuery';
import { Q } from '../../stores/queryKeys';
import { targetsApi, selectorsApi } from '../../api/endpoints';
import { formatRelativeTime, formatDate } from '../../utils/format';
import { tintStyle } from '../../utils/tint';
import { LiveElapsed, LiveCountdown, IntervalProgress } from '../checks/LiveSince';

const PERIOD_LABELS: Record<number, string> = {
  10000: '10s', 30000: '30s', 60000: '1m', 300000: '5m',
  900000: '15m', 3600000: '1h', 86400000: '24h',
};

/** Normalize a stored screenshot field to a renderable src (daemon returns raw base64). */
function imgSrc(raw?: string | null): string | null {
  if (!raw) return null;
  return /^(data:|https?:)/.test(raw) ? raw : `data:image/png;base64,${raw}`;
}

function hostOf(url?: string): string {
  if (!url) return '';
  try { return new URL(/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(url) ? url : 'https://' + url).hostname.replace(/^www\./, ''); } catch { return url; }
}
function pathOf(url?: string): string {
  if (!url) return '';
  try { const u = new URL(/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(url) ? url : 'https://' + url); return (u.pathname + u.search) || '/'; } catch { return ''; }
}

interface Health {
  dot: string; fill: string; label: string; attention: boolean;
}
function monitorHealth(state: string | undefined, enabled: boolean, t: (k: string) => string): Health {
  if (!enabled) return { dot: 'bg-active', fill: 'bg-active', label: t('Paused'), attention: false };
  switch ((state || '').toLowerCase()) {
    case 'down':
    case 'error': return { dot: 'bg-danger', fill: 'bg-danger/60', label: t('Down'), attention: true };
    case 'stale': return { dot: 'bg-warning', fill: 'bg-warning/70', label: t('Stale'), attention: true };
    case 'changed': return { dot: 'bg-warning', fill: 'bg-warning/70', label: t('Changed'), attention: true };
    case 'up':
    case 'ok':
    case 'unchanged': return { dot: 'bg-success', fill: 'bg-success/55', label: t('Healthy'), attention: false };
    default: return { dot: 'bg-active', fill: 'bg-active', label: t('Pending'), attention: false };
  }
}

const SectionLabel: React.FC<{ children: React.ReactNode; right?: React.ReactNode }> = ({ children, right }) => (
  <div className="flex items-center justify-between mb-2">
    <span className="text-[10px] font-semibold uppercase tracking-wider text-secondary">{children}</span>
    {right}
  </div>
);

const Fact: React.FC<{ icon: React.FC<any>; label: string; children: React.ReactNode; valueClass?: string }> = ({
  icon: Icon, label, children, valueClass,
}) => (
  <div className="flex items-center gap-2 min-w-0">
    <Icon className="h-3.5 w-3.5 text-tertiary shrink-0" />
    <span className="text-[11px] text-tertiary shrink-0">{label}</span>
    <span className={clsx('ml-auto text-[12px] text-right truncate min-w-0 tabular-nums', valueClass || 'text-ink')}>{children}</span>
  </div>
);

interface MonitorDetailPaneProps {
  /** Lightweight list row for the selected monitor (instant render). Null → empty state. */
  target: any | null;
  onToggle: (t: any) => void;
  onDelete: (target: { id: string; url: string }) => void;
  togglingRow: boolean;
  /** Refresh the list query after an in-pane mutation. */
  onChanged: () => void;
}

/**
 * Right pane of the monitors master–detail. Shows the selected monitor live —
 * time since last check + health, the next-check countdown, key facts, what it
 * watches, and the recent-change feed — plus one-tap Check now / Pause. "Open
 * page" jumps to the full monitor page for deep settings editing.
 */
export const MonitorDetailPane: React.FC<MonitorDetailPaneProps> = ({
  target: row,
  onToggle,
  onDelete,
  togglingRow,
}) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const id = row?.id != null ? String(row.id) : null;

  const rowEnabled = row ? (row.enabled !== false && row.enabled !== 0) : false;
  const poll = rowEnabled ? 15000 : undefined;

  const { data: full } = useQuery(
    id != null ? Q.target(id) : 'target:none',
    () => targetsApi.getById(id as string),
    { enabled: id != null, staleTime: 10000, pollInterval: poll },
  );
  const { data: changes } = useQuery(
    id != null ? Q.targetChanges(id) : 'target:none:changes',
    () => targetsApi.getChanges(id as string).catch(() => []),
    { enabled: id != null, staleTime: 15000, pollInterval: rowEnabled ? 30000 : undefined },
  );
  const { data: selectors } = useQuery(
    id != null ? Q.targetSelectors(id) : 'target:none:selectors',
    () => selectorsApi.listForTarget(Number(id)).catch(() => []),
    { enabled: id != null, staleTime: 30000 },
  );


  const tg: any = full || row;

  // ── Empty state ──
  if (!row) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center text-center px-6">
        <div className="w-12 h-12 rounded-2xl bg-chrome border border-border flex items-center justify-center mb-4">
          <EyeIcon className="h-6 w-6 text-tertiary" />
        </div>
        <p className="text-sm font-medium text-ink">{t('Select a monitor')}</p>
        <p className="text-xs text-secondary mt-1 max-w-xs">
          {t('Pick one on the left to see its status, what it watches, and recent changes.')}
        </p>
      </div>
    );
  }

  // ── Field accessors (snake/camel tolerant) ──
  const enabled = tg.enabled !== false && tg.enabled !== 0;
  const state = tg.state ?? tg.health_state;
  const health = monitorHealth(state, enabled, t);
  const lastChecked = tg.lastCheckedAt ?? tg.last_checked_at;
  const lastChange = tg.lastChangeAt ?? tg.last_change_at;
  const periodMs = tg.checkPeriodMs ?? tg.check_period_ms ?? 60000;
  const intervalLabel = PERIOD_LABELS[periodMs] || `${Math.round(periodMs / 1000)}s`;
  const changesCount = tg.changesCount ?? tg.changes_count ?? (changes || []).length;
  const statusCode = tg.statusCode ?? tg.status_code;
  const isBrowser = Boolean(tg.requiresPlaywright ?? tg.requires_playwright);
  const nextRun = tg.nextRunAt ?? tg.next_run_at
    ?? (lastChecked && periodMs ? new Date(new Date(lastChecked).getTime() + Math.max(periodMs, 60000)).toISOString() : null);
  const selectorList = selectors || [];
  const changeList = changes || [];

  const respOk = statusCode != null && statusCode < 400;
  const respErr = statusCode != null && statusCode >= 400;
  const respClass = respOk ? 'text-success-fg' : respErr ? 'text-danger-fg' : 'text-ink';

  return (
    <div className="flex flex-1 flex-col min-h-0">
      {/* ── Header ── */}
      <div className="px-5 pt-5 pb-4 border-b border-border">
        <div className="flex items-start gap-3">
          <div
            style={tintStyle('neutral')}
            className="relative w-10 h-10 rounded-lg flex items-center justify-center shrink-0"
          >
            <GlobeAltIcon className="h-5 w-5" />
            <span className={clsx('absolute -bottom-1 -right-1 w-3.5 h-3.5 rounded-full border-2 border-surface', health.dot)} />
          </div>

          <div className="flex-1 min-w-0">
            <button
              onClick={() => navigate(`/checks/${tg.id}`)}
              className="text-[15px] font-semibold text-ink truncate hover:underline text-left block max-w-full min-w-0"
              title={tg.url}
            >
              {hostOf(tg.url)}
            </button>
            <div className="text-[11px] text-tertiary truncate font-mono">{pathOf(tg.url)}</div>
          </div>

          {/* Pause / resume */}
          <button
            onClick={() => onToggle(tg)}
            disabled={togglingRow}
            className={clsx(
              'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-[11px] font-medium shrink-0 transition-colors disabled:opacity-50',
              enabled ? 'border-border text-ink hover:bg-chrome' : 'border-transparent bg-accent-strong text-accent-on hover:bg-accent-strong/90',
            )}
          >
            {enabled ? <PauseIcon className="h-3.5 w-3.5" /> : <PlayIcon className="h-3.5 w-3.5" />}
            {enabled ? t('Pause') : t('Resume')}
          </button>
        </div>

        {/* Live status line */}
        <div className="mt-4">
          <div className="text-[10px] uppercase tracking-wide text-tertiary font-medium">{t('Time since last check')}</div>
          <div className="flex items-baseline gap-2 mt-1">
            <LiveElapsed since={lastChecked} live={enabled} className="text-[28px] leading-none font-semibold text-ink" />
            <span className="inline-flex items-center gap-1.5 text-[12px] text-secondary">
              {enabled && health.attention && (
                <span className="relative flex h-1.5 w-1.5" aria-hidden="true">
                  <span className={clsx('absolute inline-flex h-full w-full rounded-full opacity-60 animate-ping', health.dot)} />
                  <span className={clsx('relative inline-flex h-1.5 w-1.5 rounded-full', health.dot)} />
                </span>
              )}
              {health.label}
            </span>
          </div>
        </div>

        {/* Next-check cadence bar */}
        {enabled && lastChecked && nextRun && (
          <div className="mt-3 space-y-1">
            <div className="flex items-center justify-between text-[11px] text-tertiary">
              <span>{t('Every {{period}}', { period: intervalLabel })}</span>
              <LiveCountdown until={nextRun} live={enabled} />
            </div>
            <IntervalProgress since={lastChecked} until={nextRun} live={enabled} fillClassName={health.fill} />
          </div>
        )}

        {/* Secondary actions */}
        <div className="flex items-center gap-1.5 mt-3">
          <button
            onClick={() => navigate(`/checks/${tg.id}?tab=settings`)}
            className="flex items-center gap-1.5 px-2.5 py-1.5 text-[12px] font-medium text-secondary border border-border rounded-lg hover:bg-chrome hover:text-ink transition-colors"
          >
            <PencilSquareIcon className="h-3.5 w-3.5" />
            {t('Edit')}
          </button>
          <button
            onClick={() => navigate(`/checks/${tg.id}`)}
            className="flex items-center gap-1.5 px-2.5 py-1.5 text-[12px] font-medium text-secondary border border-border rounded-lg hover:bg-chrome hover:text-ink transition-colors"
          >
            <ArrowTopRightOnSquareIcon className="h-3.5 w-3.5" />
            {t('Open page')}
          </button>
          <div className="flex-1" />
          <button
            onClick={() => onDelete({ id: String(tg.id), url: tg.url })}
            className="flex items-center gap-1.5 px-2.5 py-1.5 text-[12px] font-medium text-danger rounded-lg hover:bg-danger-bg hover:text-danger-fg transition-colors"
          >
            <TrashIcon className="h-3.5 w-3.5" />
            {t('Delete')}
          </button>
        </div>
      </div>

      {/* ── Body ── */}
      <div className="flex-1 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden px-5 py-4 space-y-6">
        {/* Facts */}
        <div>
          <SectionLabel>{t('Details')}</SectionLabel>
          <div className="space-y-2 rounded-xl border border-ink/20 bg-surface p-3">
            <Fact icon={ClockIcon} label={t('Interval')}>{t('Every {{period}}', { period: intervalLabel })}</Fact>
            <Fact icon={BoltIcon} label={t('Changes')}>{changesCount}</Fact>
            <Fact icon={SignalIcon} label={t('Last response')} valueClass={respClass}>{statusCode != null ? statusCode : '—'}</Fact>
            <Fact icon={EyeIcon} label={t('Engine')}>{isBrowser ? t('Browser render') : t('HTTP fetch')}</Fact>
            <Fact icon={EyeIcon} label={t('Watching')}>
              {selectorList.length === 0 ? t('Whole page') : t('{{n}} selectors', { n: selectorList.length })}
            </Fact>
            {lastChange && (
              <Fact icon={BellAlertIcon} label={t('Last change')}>
                <span title={formatDate(lastChange)}>{formatRelativeTime(lastChange)}</span>
              </Fact>
            )}
          </div>
        </div>

        {/* Watched elements */}
        {selectorList.length > 0 && (
          <div>
            <SectionLabel>{t('What it watches')}</SectionLabel>
            <div className="rounded-xl border border-ink/20 bg-surface divide-y divide-border overflow-hidden">
              {selectorList.slice(0, 8).map((s: any) => {
                const isVisual = s.content_type === 'visual' || Boolean(s.visual_region);
                return (
                  <div key={s.id} className="flex items-baseline justify-between gap-3 px-3 py-2">
                    <span className="text-[12px] text-ink truncate shrink-0">{s.name || s.label || t('Tracked element')}</span>
                    {isVisual ? (
                      <span className="text-[11px] text-tertiary shrink-0">{t('Visual zone')}</span>
                    ) : (
                      <code className="text-[11px] text-tertiary font-mono break-all text-right truncate">{s.selector || s.cssSelector || s.css_selector || '—'}</code>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Recent changes */}
        <div>
          <SectionLabel
            right={changeList.length > 0 ? (
              <button onClick={() => navigate(`/checks/${tg.id}`)} className="text-[10px] text-tertiary hover:text-ink transition-colors">
                {t('All changes')}
              </button>
            ) : undefined}
          >
            {t('Recent changes')}
          </SectionLabel>
          {changeList.length === 0 ? (
            <p className="text-[12px] text-tertiary">{t('No changes detected yet — you’ll see them here.')}</p>
          ) : (
            <div className="rounded-xl border border-ink/20 bg-surface divide-y divide-border overflow-hidden">
              {changeList.slice(0, 6).map((change: any, i: number) => {
                const when = change.timestamp || change.detected_at || change.last_detected_at || change.first_detected_at;
                const before = imgSrc(change.screenshotBefore || change.screenshot_before);
                const after = imgSrc(change.screenshotAfter || change.screenshot_after);
                const diff = imgSrc(change.screenshotDiff || change.screenshot_diff);
                const isVisual = Boolean(before || after || diff);
                const snippet = (change.diff || change.diff_snippet || '').toString();
                return (
                  <div key={change.id || i} className="px-3 py-2.5">
                    <div className="flex items-center justify-between gap-2">
                      <span className="inline-flex items-center gap-1.5 text-[12px] text-ink truncate">
                        <BoltIcon className="h-3 w-3 text-warning shrink-0" />
                        {change.selectorName || change.selector_name || t('Change detected')}
                      </span>
                      <span className="text-[11px] text-tertiary shrink-0">{when ? formatRelativeTime(when) : ''}</span>
                    </div>
                    {isVisual ? (
                      <div className="flex gap-2 mt-2">
                        {[{ label: t('Before'), src: before }, { label: t('After'), src: after }].map((img) => img.src && (
                          <img key={img.label} src={img.src} alt={img.label} className="h-16 rounded-lg border border-border bg-canvas object-cover" />
                        ))}
                      </div>
                    ) : (() => {
                      // Prefer the REAL before/after snapshot over the stored diff
                      // snippet, which the capture pipeline truncates to a preview
                      // (often just a "... (truncated)" marker). Parenthetical
                      // backend placeholders count as "no content". Contained +
                      // scrolls; never overflows the pane.
                      const clean = (s?: string | null) => {
                        const v = (s ?? '').toString().trim();
                        return v && !/^\(.*\)$/.test(v) ? v : '';
                      };
                      const oldC = clean(change.oldContent ?? change.content_before);
                      const newC = clean(change.newContent ?? change.content_after);
                      const rows: { sign: string; text: string; cls: string }[] =
                        oldC || newC
                          ? [
                              ...(oldC ? [{ sign: '−', text: oldC, cls: 'text-tertiary' }] : []),
                              ...(newC ? [{ sign: '+', text: newC, cls: 'text-ink' }] : []),
                            ]
                          : snippet
                            ? [{ sign: '', text: snippet, cls: 'text-secondary' }]
                            : [];
                      if (rows.length === 0) return null;
                      return (
                        <div className="mt-1.5 rounded-lg border border-border bg-canvas overflow-hidden">
                          <div className="max-h-24 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
                            {rows.map((r, ri) => (
                              <div key={ri} className={clsx('flex gap-1.5 px-2 py-0.5 font-mono text-[10.5px] leading-relaxed whitespace-pre-wrap break-all', r.cls)}>
                                {r.sign && <span className="select-none shrink-0 opacity-70">{r.sign}</span>}
                                <span className="min-w-0">{r.text}</span>
                              </div>
                            ))}
                          </div>
                        </div>
                      );
                    })()}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
