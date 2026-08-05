import React, { useState, useEffect } from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import { ClockIcon, WrenchScrewdriverIcon } from '@heroicons/react/24/outline';
import { automationApi } from '../../../api/endpoints';
import { formatRelativeTime } from '../../../utils/format';
import { Section } from './Section';
import { RepairHistory } from './RepairHistory';
import { requestSurface } from '../../../onboarding/surfaceTrigger';
import { Expand } from '../../../components/ui';
import { STATUS_DOT } from './meta';
import { TierBadge } from '../../../components/TierBadge';
import type { ExecutionTier } from '../../../components/TierBadge';

/** Pull the run's execution tier from whichever field the backend stamped. */
const taskTier = (task: any): ExecutionTier | null => {
  const v = task?.execution_tier ?? task?.isolation_tier ?? null;
  return v === 'isolated' || v === 'shared' ? v : null;
};

interface RunsTabProps {
  workflow: any;
  tasks: any[];
  /** Skip the outer "Runs" Section header — the host already provides one
   *  (e.g. the Detail page's "Run history" section). */
  bare?: boolean;
}

/** Execution history with expandable errors, plus AI repair history. */
export const RunsTab: React.FC<RunsTabProps> = ({ workflow, tasks, bare = false }) => {
  const { t } = useTranslation();
  const [expandedRunId, setExpandedRunId] = useState<number | null>(null);
  const taskList = tasks || [];

  // A workflow that healed itself is the "aha" for AI auto-repair. First time a
  // new user sees repair history, surface the one-time hint (no-op once seen /
  // when onboarding is off).
  const hasRepairs = !!(workflow.ai_repair_history && workflow.ai_repair_history.length > 0);
  useEffect(() => { if (hasRepairs) requestSurface('autorepair'); }, [hasRepairs]);

  // When `bare`, render the runs list/empty-state without the wrapping Section
  // so it slots cleanly under a host-provided header.
  const wrapRuns = (children: React.ReactNode, emptyState: boolean) =>
    bare ? <>{children}</> : (
      <Section
        title={t('Runs')}
        description={emptyState ? t('Execution history shows up here once this workflow runs.') : t('Click a failed run to see its full error.')}
      >
        {children}
      </Section>
    );

  return (
    <div className="space-y-6">
      {taskList.length === 0 ? (
        wrapRuns(
          <div className="flex items-start gap-3 rounded-xl border border-ink/20 shadow-sm bg-surface px-4 py-3.5">
            <ClockIcon className="h-4 w-4 text-tertiary mt-0.5 shrink-0" />
            <div className="min-w-0">
              <p className="text-[13px] font-medium text-ink">{t('No runs yet')}</p>
              <p className="text-[11px] text-tertiary mt-0.5">{t('Run this workflow to see execution history')}</p>
            </div>
          </div>,
          true,
        )
      ) : (
        wrapRuns(
          <div className="bg-surface border border-ink/20 shadow-sm rounded-xl overflow-hidden divide-y divide-border">
            {taskList.map((task: any) => (
              <React.Fragment key={task.id}>
                <div
                  className={clsx(
                    'flex items-center justify-between px-4 py-2.5 hover:bg-chrome transition-colors',
                    task.error_message && 'cursor-pointer',
                  )}
                  onClick={() => task.error_message && setExpandedRunId(expandedRunId === task.id ? null : task.id)}
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <div className={clsx('w-2 h-2 rounded-full flex-shrink-0', STATUS_DOT[task.status] || 'bg-active')} />
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-medium text-ink capitalize">{task.status}</span>
                        {task.engine === 'http' && (
                          <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-hover text-secondary" title={t('Ran without a browser')}>{t('HTTP')}</span>
                        )}
                        {task.engine === 'hybrid' && (
                          <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-hover text-secondary" title={t('Browser sign-in, then HTTP')}>{t('Hybrid')}</span>
                        )}
                        {task.ai_repair_attempted && (
                          <span className="text-[10px] text-tertiary">
                            {task.ai_repair_result?.success ? t('repaired') : t('repair failed')}
                          </span>
                        )}
                      </div>
                      {task.error_message && (
                        <p className="text-[11px] text-tertiary truncate max-w-xs sm:max-w-md">{task.error_message}</p>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-3 shrink-0">
                    {taskTier(task) && <TierBadge tier={taskTier(task)} compact />}
                    {task.status === 'failed' && !task.ai_repair_attempted && (
                      <button onClick={async (e) => {
                        e.stopPropagation();
                        try { await automationApi.repairWorkflow(workflow.id, task.id); toast.success(t('Repair started')); }
                        catch (err: any) { toast.error(err?.response?.data?.detail || t('Repair failed')); }
                      }} className="flex items-center gap-1 px-2 py-0.5 text-[11px] font-medium text-ink border border-border rounded-md hover:bg-chrome transition-colors">
                        <WrenchScrewdriverIcon className="w-3 h-3" />
                        {t('Repair')}
                      </button>
                    )}
                    <div className="text-[11px] text-tertiary text-right whitespace-nowrap">
                      {task.duration_ms && (
                        <span>{task.duration_ms > 1000 ? `${(task.duration_ms / 1000).toFixed(1)}s` : `${task.duration_ms}ms`}</span>
                      )}
                      {task.created_at && <span className="ml-2">{formatRelativeTime(task.created_at)}</span>}
                    </div>
                  </div>
                </div>
                {task.error_message && (
                  <Expand open={expandedRunId === task.id} mountOnEnter className="px-4 py-3 bg-hover">
                    <pre className="bg-surface border border-border rounded-lg p-3 text-[12px] font-mono text-ink whitespace-pre-wrap break-all max-h-64 overflow-y-auto">{task.error_message}</pre>
                  </Expand>
                )}
              </React.Fragment>
            ))}
          </div>,
          false,
        )
      )}

      {hasRepairs && (
        <div>
          {/* Zero-height marker at the Repairs header — the auto-repair hint points here. */}
          <span data-surface="autorepair" aria-hidden="true" className="block h-0" />
          <Section
            title={t('Repairs ({{n}})', { n: workflow.ai_repair_history.length })}
            description={t('Times AI auto-repair rewrote broken steps — expand to compare before and after.')}
          >
            <RepairHistory history={workflow.ai_repair_history} />
          </Section>
        </div>
      )}
    </div>
  );
};
