import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  CheckBadgeIcon,
  ExclamationTriangleIcon,
  ChevronDownIcon,
  XMarkIcon,
  WrenchScrewdriverIcon,
} from '@heroicons/react/24/outline';
import { formatRelativeTime } from '../../utils/format';
import type { HealthRun } from '../../api/homeHealth';
import { readableRunName, RUN_TYPE_SHORT } from './runLabel';
import { Expand } from '../ui';

interface NeedsAttentionProps {
  failures: HealthRun[];
  loading: boolean;
  /** Dismiss (hide) a failure row by run id — persisted by the caller. */
  onDismiss?: (id: string) => void;
}

/**
 * "Needs attention" — at most three recent failed runs (an overflow row links to the
 * rest, so Home never becomes a full error log). Raw errors are never the headline:
 * each is distilled into a short human title + one-line summary, with the original
 * technical string and support ref tucked behind Details.
 */
export const NeedsAttention: React.FC<NeedsAttentionProps> = ({ failures, loading, onDismiss }) => {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState<string | null>(null);
  const rows = failures.slice(0, 3);
  const overflow = failures.length - rows.length;

  // Distill a raw error into a short title + a COMPACT detail clause (no long helper
  // sentences — the row reads "AI request failed · Provider returned 502"). Raw refs
  // and the full string live in Details only.
  const humanize = (raw?: string | null): { title: string; detail: string; ref?: string } => {
    const text = (raw || '').trim();
    if (!text) return { title: t('Run failed'), detail: '' };
    const ref = text.match(/ref:\s*([0-9a-f-]{6,})/i)?.[1];
    const status = text.match(/\b([45]\d\d)\b/)?.[1];
    const l = text.toLowerCase();
    if (l.includes('ai ') || l.includes('ai request') || l.includes('ai brain') || l.includes('provider') || l.includes('llm')) {
      return { title: t('AI request failed'), detail: status ? t('Provider returned {{status}}', { status }) : t('Provider error'), ref };
    }
    if (l.includes('timeout') || l.includes('timed out')) {
      return { title: t('Timed out'), detail: t('Run exceeded timeout'), ref };
    }
    if (l.includes('not found') && (l.includes('selector') || l.includes('element'))) {
      return { title: t('Element not found'), detail: t('Selector no longer matches'), ref };
    }
    if (l.includes('net::') || l.includes('navigation') || l.includes('unreachable') || l.includes('dns') || l.includes('econnrefused')) {
      return { title: t('Could not reach the page'), detail: t('Site did not respond'), ref };
    }
    if (status) {
      return { title: t('Request failed ({{status}})', { status }), detail: t('Server returned {{status}}', { status }), ref };
    }
    const clean = text.replace(/\(?ref:\s*[0-9a-f-]{6,}\)?/i, '').replace(/^[A-Za-z ]*error:\s*/i, '').trim();
    return { title: clean.split(/[.:]/)[0].slice(0, 80) || t('Run failed'), detail: '', ref };
  };

  return (
    <div>
      <div className="flex items-end justify-between mb-2.5">
        <div>
          <h2 className="text-base font-semibold text-ink tracking-tight">{t('Needs attention')}</h2>
          <p className="text-[12px] text-secondary mt-0.5">{t('Recent runs that failed or may need a fix.')}</p>
        </div>
        {failures.length > 0 && (
          <Link to="/runs?view=failed" className="text-xs text-tertiary hover:text-ink transition-colors flex-shrink-0">
            {t('View all runs')}
          </Link>
        )}
      </div>

      {loading && rows.length === 0 ? (
        <div className="bg-surface border border-border rounded-xl overflow-hidden divide-y divide-border">
          {[0, 1, 2].map((i) => (
            <div key={i} className="px-4 py-2.5 flex items-center gap-2.5">
              <div className="w-5 h-5 rounded-md bg-hover animate-pulse flex-shrink-0" />
              <div className="flex-1 space-y-1.5">
                <div className="h-3 w-1/3 bg-hover rounded animate-pulse" />
                <div className="h-2.5 w-2/3 bg-hover rounded animate-pulse" />
              </div>
            </div>
          ))}
        </div>
      ) : rows.length === 0 ? (
        <div className="flex items-center gap-2.5 rounded-xl border border-border bg-surface px-4 py-3">
          <CheckBadgeIcon className="h-4 w-4 text-emerald-600 shrink-0" />
          <p className="text-[13px] text-secondary">{t('No failed runs recently. Everything is running smoothly.')}</p>
        </div>
      ) : (
        <div className="bg-surface border border-ink/20 rounded-xl overflow-hidden divide-y divide-border shadow">
          {rows.map((run) => {
            const { name, host } = readableRunName(run);
            const human = humanize(run.error);
            const isOpen = expanded === run.id;
            return (
              <div key={run.id} className="group px-4 py-2.5 hover:bg-chrome transition-colors duration-150">
                <div className="flex items-start gap-2.5">
                  <ExclamationTriangleIcon className="w-4 h-4 text-amber-600 flex-shrink-0 mt-0.5" />
                  <div className="flex-1 min-w-0">
                    {/* Line 1 — object name + type, muted host, time on the right */}
                    <div className="flex items-center gap-2">
                      <p className="text-[13px] font-medium text-ink truncate">{name}</p>
                      <span className="text-[10px] text-ink bg-chrome border border-border/60 rounded-full px-1.5 py-0.5 flex-shrink-0">
                        {RUN_TYPE_SHORT[run.run_type] ? t(RUN_TYPE_SHORT[run.run_type]) : run.run_type}
                      </span>
                      {/* AI already tried to fix this one and gave up — say so, so the row
                          doesn't read as an untriaged failure waiting on the AI. Rows the AI
                          DID fix never reach here (Dashboard filters them out). */}
                      {run.repair_failed && (
                        <span
                          className="inline-flex items-center gap-1 rounded-full bg-rose-50 px-1.5 py-0.5 text-[10px] text-rose-700 flex-shrink-0"
                          title={t('AI auto-repair tried to fix this workflow and could not — it needs a look')}
                        >
                          <WrenchScrewdriverIcon className="w-3 h-3" />
                          {t('Repair failed')}
                        </span>
                      )}
                      {host && <span className="text-[11px] text-tertiary truncate hidden @pair/stage:inline">{host}</span>}
                      <span className="ml-auto flex items-center gap-2 flex-shrink-0">
                        <span className="text-[11px] text-tertiary tabular-nums">
                          {run.started_at ? formatRelativeTime(run.started_at) : ''}
                        </span>
                        {onDismiss && (
                          <button
                            onClick={(e) => { e.stopPropagation(); onDismiss(run.id); }}
                            aria-label={t('Dismiss')}
                            title={t('Dismiss')}
                            className="rounded-md p-0.5 text-tertiary hover:text-ink hover:bg-hover transition-all opacity-0 group-hover:opacity-100 focus:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/30"
                          >
                            <XMarkIcon className="w-3.5 h-3.5" />
                          </button>
                        )}
                      </span>
                    </div>
                    {/* Line 2 — humanized result (never the raw error) */}
                    <p className="text-[12px] mt-0.5 truncate">
                      <span className="text-ink font-medium">{human.title}</span>
                      {human.detail && <span className="text-secondary"> · {human.detail}</span>}
                    </p>
                    {/* Secondary actions */}
                    <div className="flex items-center gap-3 mt-1">
                      {run.detail_url_hint && (
                        <Link to={run.detail_url_hint} className="text-[11.5px] font-medium text-secondary hover:text-ink transition-colors">
                          {t('Open')}
                        </Link>
                      )}
                      {(run.error || human.ref) && (
                        <button
                          onClick={() => setExpanded(isOpen ? null : run.id)}
                          className="inline-flex items-center gap-0.5 text-[11.5px] font-medium text-secondary hover:text-ink transition-colors"
                        >
                          {t('Details')}
                          <ChevronDownIcon className={`w-3.5 h-3.5 transition-transform ${isOpen ? 'rotate-180' : ''}`} />
                        </button>
                      )}
                    </div>
                    <Expand open={isOpen} mountOnEnter>
                      <div className="mt-2 rounded-lg bg-chrome border border-border/60 px-2.5 py-2 text-[11px] text-secondary font-mono break-words whitespace-pre-wrap">
                        {run.error || t('No additional detail.')}
                        {human.ref && <div className="mt-1 text-[10px] text-secondary">{t('ref')}: {human.ref}</div>}
                      </div>
                    </Expand>
                  </div>
                </div>
              </div>
            );
          })}
          {overflow > 0 && (
            <Link to="/runs?view=failed" className="flex items-center justify-between px-4 py-2.5 hover:bg-chrome transition-colors">
              <span className="text-[12px] text-secondary">{t('{{n}} more need attention', { n: overflow })}</span>
              <span className="text-[11.5px] font-medium text-secondary hover:text-ink">{t('View all')}</span>
            </Link>
          )}
        </div>
      )}
    </div>
  );
};
