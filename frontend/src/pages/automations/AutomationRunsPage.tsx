import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams, useNavigate, Link } from 'react-router-dom';
import { useRequireAuth } from '../../hooks/useAuth';
import { triggersApi } from '../../api/endpoints';
import { formatRelativeTime } from '../../utils/format';
import { statusStyle } from '../../utils/statusStyle';
import { useQuery } from '../../hooks/useQuery';
import { RowsSkeleton } from '../../components/library/shelf';
import { apiErrorMessage } from '../../api/client';
import toast from 'react-hot-toast';
import { Q } from '../../stores/queryKeys';
import { Expand } from '../../components/ui/Expand';
import { Button, EmptyHero } from '../../components/ui';
import clsx from 'clsx';
import {
  CheckCircleIcon,
  XCircleIcon,
  ArrowPathIcon,
  ArrowLeftIcon,
  ClockIcon,
  ExclamationTriangleIcon,
} from '@heroicons/react/24/outline';

// Icon per status; color comes from statusStyle() so failure reads as the loud
// mark (red) and success stays calm (green) — the one place color is allowed.
const STATUS_ICONS: Record<string, any> = {
  success: CheckCircleIcon,
  completed: CheckCircleIcon,
  failed: XCircleIcon,
  error: XCircleIcon,
  running: ArrowPathIcon,
  pending: ClockIcon,
};

export const AutomationRunsPage: React.FC = () => {
  useRequireAuth();
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [flowName, setFlowName] = useState('');
  const [expandedExecId, setExpandedExecId] = useState<number | null>(null);
  const [page, setPage] = useState(0);
  const PER_PAGE = 20;

  useEffect(() => {
    if (id) {
      triggersApi.get(parseInt(id)).then((data: any) => {
        setFlowName(data.name || t('Untitled'));
      }).catch((err: any) => {
        // Only bounce back to the list when the automation truly doesn't
        // exist — a transient error shouldn't silently navigate away.
        if (err?.response?.status === 404) {
          toast.error(t('Automation not found'));
          navigate('/automations');
        } else {
          toast.error(apiErrorMessage(err, t('Failed to load automation')));
        }
      });
    }
  }, [id, navigate, t]);

  const [pollInterval, setPollInterval] = useState<number | undefined>(undefined);
  const { data: executions, loading, error, refresh } = useQuery(
    Q.executions(id),
    () => id ? triggersApi.getExecutions(parseInt(id)) : Promise.resolve([]),
    { pollInterval },
  );

  const executionList = executions || [];
  const initialLoading = loading && !executions;
  const loadFailed = !executions && !initialLoading && Boolean(error);

  // Poll fast while a run is in-flight; otherwise keep a slow idle baseline.
  // Executions are triggered elsewhere (schedule/webhook/manual run), so an
  // undefined idle interval would never surface an externally-started run until
  // manual refresh. 60s idle still cuts the bulk of the polling.
  // Adjusted during render (guarded) rather than in an effect — `executionList`
  // is a fresh array every render, so the effect form re-ran on every pass.
  const wantPoll = executionList.some((e: any) => ['running', 'pending'].includes(e.status || '')) ? 5000 : 60000;
  if (pollInterval !== wantPoll) setPollInterval(wantPoll);

  const pageCount = Math.max(1, Math.ceil(executionList.length / PER_PAGE));
  const safePage = Math.min(page, pageCount - 1);
  const pageStart = safePage * PER_PAGE;
  const pageRows = executionList.slice(pageStart, pageStart + PER_PAGE);

  return (
    <>
      <div className="flex flex-col h-full">
        {/* Toolbar */}
        <div className="flex items-center gap-3 h-12 px-4 sm:px-6 bg-chrome chrome-topbar border-b border-border shrink-0">
          <Link to={`/automations/${id}`} className="p-1 text-secondary hover:text-ink transition-colors">
            <ArrowLeftIcon className="w-3.5 h-3.5" />
          </Link>
          <span className="text-[13px] font-semibold text-ink truncate">{flowName}</span>
          <span className="text-[11px] text-secondary">{t('Runs')}</span>
          {executionList.length > 0 && <span className="hidden @pair/stage:inline text-[11px] text-secondary">· {t('{{n}} total', { n: executionList.length })}</span>}
          <div className="flex-1" />
        </div>

        <div className="flex-1 overflow-auto">
          <div className="px-4 sm:px-6 py-4">
            {!initialLoading && !loadFailed && executionList.length > 0 && (
              <div className="mb-3">
                <h1 className="text-base font-semibold text-ink">{t('Run history')}</h1>
                <p className="text-xs text-tertiary mt-0.5 max-w-2xl">
                  {t('Every time this automation fired, newest first. Select a failed run to read the full error.')}
                </p>
              </div>
            )}
            {initialLoading && <RowsSkeleton rows={7} label={t('Loading runs…')} />}

            {loadFailed && (
              <EmptyHero
                icon={ExclamationTriangleIcon}
                title={t("Couldn't load runs")}
                description={error}
                className="min-h-[50vh]"
              >
                <Button onClick={() => refresh()} size="sm">{t('Retry')}</Button>
              </EmptyHero>
            )}

            {!initialLoading && !loadFailed && executionList.length === 0 && (
          <EmptyHero
            icon={ClockIcon}
            title={t('No runs yet')}
            description={t("This automation hasn't been triggered yet. Once its trigger fires, runs will appear here.")}
            className="min-h-[50vh]"
          />
        )}

        {!initialLoading && !loadFailed && executionList.length > 0 && (
          <>
            <div className="bg-surface border border-ink/20 rounded-xl shadow-sm overflow-hidden">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left">
                    <th className="px-4 py-2.5 text-[11px] font-medium uppercase tracking-wide text-tertiary">{t('Status')}</th>
                    <th className="px-4 py-2.5 text-[11px] font-medium uppercase tracking-wide text-tertiary">{t('Detail')}</th>
                    <th className="px-4 py-2.5 text-[11px] font-medium uppercase tracking-wide text-tertiary text-right whitespace-nowrap">{t('Duration')}</th>
                    <th className="px-4 py-2.5 text-[11px] font-medium uppercase tracking-wide text-tertiary text-right whitespace-nowrap">{t('When')}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {pageRows.map((exec: any) => {
                    const StatusIcon = STATUS_ICONS[exec.status] || STATUS_ICONS.pending;
                    const style = statusStyle(exec.status);
                    const expanded = expandedExecId === exec.id;
                    const duration = exec.completed_at && exec.triggered_at
                      ? `${((new Date(exec.completed_at).getTime() - new Date(exec.triggered_at).getTime()) / 1000).toFixed(1)}s`
                      : '—';

                    return (
                      <React.Fragment key={exec.id}>
                        <tr
                          className={clsx('align-top', exec.error_message && 'cursor-pointer hover:bg-chrome transition-colors')}
                          onClick={() => exec.error_message && setExpandedExecId(expanded ? null : exec.id)}
                        >
                          <td className="px-4 py-3 whitespace-nowrap">
                            <span className="flex items-center gap-2">
                              <StatusIcon className={clsx('h-4 w-4 shrink-0', style.text, exec.status === 'running' && 'animate-spin')} />
                              <span className="text-sm font-medium text-ink capitalize">{exec.status}</span>
                            </span>
                          </td>
                          <td className="px-4 py-3 min-w-0">
                            {exec.error_message ? (
                              <span className="text-xs text-secondary truncate block max-w-md">{exec.error_message}</span>
                            ) : (
                              <span className="text-xs text-tertiary">—</span>
                            )}
                          </td>
                          <td className="px-4 py-3 text-right text-xs text-tertiary tabular-nums whitespace-nowrap">{duration}</td>
                          <td className="px-4 py-3 text-right text-xs text-tertiary whitespace-nowrap">
                            {exec.triggered_at ? formatRelativeTime(exec.triggered_at) : '—'}
                          </td>
                        </tr>
                        {expanded && exec.error_message && (
                          <tr>
                            {/* Padding inside the Expand so the row GROWS from 0 (appear)
                                instead of popping to full height. */}
                            <td colSpan={4} className="p-0">
                              <Expand open appear className="px-4 pb-3">
                                <pre className="bg-canvas border border-border rounded-lg p-3 text-[12px] font-mono text-ink whitespace-pre-wrap break-all max-h-64 overflow-y-auto">{exec.error_message}</pre>
                              </Expand>
                            </td>
                          </tr>
                        )}
                      </React.Fragment>
                    );
                  })}
                </tbody>
              </table>

              {executionList.length > PER_PAGE && (
                <div className="flex items-center justify-between px-4 py-2.5 border-t border-border">
                  <span className="text-[11px] text-tertiary tabular-nums">
                    {t('{{from}}–{{to}} of {{total}}', {
                      from: pageStart + 1,
                      to: Math.min(pageStart + PER_PAGE, executionList.length),
                      total: executionList.length,
                    })}
                  </span>
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => setPage(p => Math.max(0, p - 1))}
                      disabled={safePage === 0}
                      className="px-2.5 py-1 text-[12px] font-medium text-secondary rounded-md hover:bg-chrome hover:text-ink disabled:opacity-40 disabled:hover:bg-transparent transition-colors"
                    >
                      {t('Previous')}
                    </button>
                    <button
                      onClick={() => setPage(p => Math.min(pageCount - 1, p + 1))}
                      disabled={safePage >= pageCount - 1}
                      className="px-2.5 py-1 text-[12px] font-medium text-secondary rounded-md hover:bg-chrome hover:text-ink disabled:opacity-40 disabled:hover:bg-transparent transition-colors"
                    >
                      {t('Next')}
                    </button>
                  </div>
                </div>
              )}
            </div>
          </>
        )}
          </div>
        </div>
      </div>
    </>
  );
};
