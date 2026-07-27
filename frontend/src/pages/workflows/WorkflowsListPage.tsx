import React, { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useRequireAuth } from '../../hooks/useAuth';
import { useDocumentTitle } from '../../hooks/useDocumentTitle';
import { useNavigate } from 'react-router-dom';
import { WorkflowList } from '../../components/library/WorkflowList';
import { useQuery } from '../../hooks/useQuery';
import { Q } from '../../stores/queryKeys';
import { automationApi } from '../../api/endpoints';
import { SHELF_TOPBAR } from '../../components/library/shelf';
import {
  PlusIcon,
  CursorArrowRaysIcon,
} from '@heroicons/react/24/outline';

export const WorkflowsListPage: React.FC = () => {
  useRequireAuth();
  const { t } = useTranslation();
  useDocumentTitle(t('Workflows'));
  const navigate = useNavigate();
  const [search, setSearch] = useState('');
  const { data: workflows } = useQuery(Q.workflows(), () => automationApi.listWorkflows());

  const workflowList = workflows || [];

  const { activeCount, totalRuns, runningCount } = useMemo(() => ({
    activeCount: workflowList.filter((w: any) => w.is_active).length,
    totalRuns: workflowList.reduce((sum: number, w: any) => sum + (w.usage_count || 0), 0),
    runningCount: workflowList.filter((w: any) => ['running', 'pending', 'assigned', 'queued'].includes(w.last_run_status || '')).length,
  }), [workflowList]);

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Compact inline toolbar — the page's "topbar". Chrome tone so it joins the
          sidebar + shelf list column as one nav frame. */}
      <div className={SHELF_TOPBAR}>
        <CursorArrowRaysIcon className="w-4 h-4 text-tertiary shrink-0" />
        <span className="text-[13px] font-semibold text-ink shrink-0">{t('Workflows')}</span>

        {workflowList.length > 0 && (
          <span className="hidden @pair/stage:inline text-[11px] text-tertiary ml-2">{t('{{active}} active · {{runs}} runs', { active: activeCount, runs: totalRuns })}</span>
        )}
        {runningCount > 0 && (
          <span className="hidden @pair/stage:inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-ink text-surface ml-1">
            <span className="w-1.5 h-1.5 rounded-full bg-surface animate-pulse" />
            {t('{{n}} running', { n: runningCount })}
          </span>
        )}

        <div className="flex-1" />

        {/* Search lives inside the master list column (WorkflowList), not the topbar. */}

        {/* New button */}
        <button
          data-tour="workflows-new"
          onClick={() => navigate('/workflows/new')}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-accent-strong text-accent-on text-[12px] font-medium rounded-lg hover:bg-accent-strong/90 transition-colors shrink-0"
        >
          <PlusIcon className="w-3.5 h-3.5" />
          {t('New')}
        </button>
      </div>

      {/* Body — full-height master-detail list. */}
      <div className="flex flex-1 min-h-0 overflow-hidden">
        <WorkflowList
          search={search}
          onSearchChange={setSearch}
          onItemClick={(id) => navigate(`/workflows/${id}`)}
        />
      </div>
    </div>
  );
};
