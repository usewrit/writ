import React from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import {
  CheckIcon,
  XMarkIcon,
  ArrowPathIcon,
  ClockIcon,
} from '@heroicons/react/24/outline';
import { useQuery } from '../../hooks/useQuery';
import { Q } from '../../stores/queryKeys';
import { homeHealthApi, type HealthRun } from '../../api/homeHealth';
import { formatRelativeTime } from '../../utils/format';
import { statusStyle } from '../../utils/statusStyle';
import { readableRunName, RUN_TYPE_LABEL } from './runLabel';

const fmtDuration = (ms: number): string =>
  ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;

/**
 * Recent activity — the operational feed across every run type (workflow runs,
 * monitor checks, AI sessions, automations). When there are no failures this becomes
 * the primary section of the Home page. Rows stay scannable: a status glyph, the
 * event type + object, a short useful outcome, and a relative timestamp.
 */
export const RecentActivity: React.FC = () => {
  const { t } = useTranslation();
  const { data, loading } = useQuery(
    Q.key('home:recent-runs'),
    () => homeHealthApi.listRuns({ limit: 12 }),
    { pollInterval: 30000 },
  );
  const runs = data || [];

  const outcome = (run: HealthRun): string => {
    if (run.status === 'success') {
      return run.duration_ms != null
        ? t('Completed in {{d}}', { d: fmtDuration(run.duration_ms) })
        : t('Completed');
    }
    if (run.status === 'failed') return t('Failed');
    if (run.status === 'running') return t('Running…');
    return run.status ? run.status.charAt(0).toUpperCase() + run.status.slice(1) : '';
  };

  return (
    <div>
      <div className="flex items-end justify-between mb-3">
        <div>
          <h2 className="text-base font-semibold text-ink tracking-tight">{t('Recent API executions')}</h2>
          <p className="text-[12px] text-secondary mt-0.5">{t('Latest runs, data extractions, API calls, and monitor changes.')}</p>
        </div>
        {runs.length > 0 && (
          <Link to="/runs" className="text-xs text-tertiary hover:text-ink transition-colors flex-shrink-0">
            {t('View all')}
          </Link>
        )}
      </div>

      {loading && runs.length === 0 ? (
        <div className="bg-surface border border-border rounded-xl overflow-hidden divide-y divide-border">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="px-4 sm:px-5 py-3 flex items-center gap-3">
              <div className="w-6 h-6 rounded-lg bg-hover animate-pulse flex-shrink-0" />
              <div className="flex-1 space-y-1.5">
                <div className="h-3 w-1/3 bg-hover rounded animate-pulse" />
                <div className="h-2.5 w-1/4 bg-hover rounded animate-pulse" />
              </div>
            </div>
          ))}
        </div>
      ) : runs.length === 0 ? (
        <div className="border border-dashed border-border rounded-xl px-4 py-8 text-center">
          <ClockIcon className="w-5 h-5 text-tertiary mx-auto" />
          <p className="text-[13px] text-secondary mt-2">{t('Nothing has run yet.')}</p>
          <p className="text-[12px] text-tertiary mt-0.5">{t('Convert a website, then call it through REST, MCP, a schedule, or an automation.')}</p>
        </div>
      ) : (
        <div className="bg-surface border border-ink/20 rounded-xl overflow-hidden divide-y divide-border shadow">
          {runs.map((run) => {
            const StatusIcon = run.status === 'success' ? CheckIcon
              : run.status === 'failed' ? XMarkIcon
              : run.status === 'running' ? ArrowPathIcon
              : ClockIcon;
            const sStyle = statusStyle(run.status);
            const typeLabel = RUN_TYPE_LABEL[run.run_type] ? t(RUN_TYPE_LABEL[run.run_type]) : t('Run');

            // Readable name ("Amazon monitor" / "Amazon page"); the full host drops
            // to muted metadata on the result line — never a raw URL as the title. A
            // descriptive lead ("Concierge browse") replaces the generic type label.
            const { name, host, lead } = readableRunName(run);

            // Muted result line: lead/type · outcome · host — name-first hierarchy.
            const meta = [lead || typeLabel, outcome(run), host].filter(Boolean).join(' · ');

            const row = (
              <div className="px-4 sm:px-5 py-2.5 flex items-center gap-3 hover:bg-chrome transition-colors duration-150">
                <div className={clsx('w-6 h-6 rounded-lg flex items-center justify-center flex-shrink-0', sStyle.pill)}>
                  <StatusIcon className={clsx('w-3.5 h-3.5', run.status === 'running' && 'animate-spin')} />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-[13px] font-medium text-ink truncate">{name}</p>
                  <p className="text-[11.5px] text-tertiary truncate">{meta}</p>
                </div>
                <span className="text-[11px] text-tertiary flex-shrink-0 tabular-nums">
                  {run.started_at ? formatRelativeTime(run.started_at) : ''}
                </span>
              </div>
            );
            return run.detail_url_hint ? (
              <Link key={run.id} to={run.detail_url_hint} className="block">{row}</Link>
            ) : (
              <div key={run.id}>{row}</div>
            );
          })}
        </div>
      )}
    </div>
  );
};
