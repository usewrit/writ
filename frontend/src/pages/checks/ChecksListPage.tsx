import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useRequireAuth } from '../../hooks/useAuth';
import { useDocumentTitle } from '../../hooks/useDocumentTitle';
import { useNavigate } from 'react-router-dom';
import { MonitorList } from '../../components/library/MonitorList';
import { useQuery } from '../../hooks/useQuery';
import { Q } from '../../stores/queryKeys';
import { targetsApi } from '../../api/endpoints';
import { SHELF_TOPBAR } from '../../components/library/shelf';
import { PlusIcon, EyeIcon, QuestionMarkCircleIcon } from '@heroicons/react/24/outline';
import { useTour } from '../../onboarding/TourProvider';

export const ChecksListPage: React.FC = () => {
  useRequireAuth();
  const { t } = useTranslation();
  useDocumentTitle(t('Monitors'));
  const navigate = useNavigate();
  const { startFullTutorial } = useTour();
  const [search, setSearch] = useState('');
  const { data: targets } = useQuery(Q.targets('content'), () => targetsApi.getAll('content'), { pollInterval: 15000 });

  const { targetList, activeCount, totalChanges } = React.useMemo(() => {
    const list = (targets || []).filter((t: any) => (t.checkType ?? t.check_type) === 'content');
    return {
      targetList: list,
      activeCount: list.filter((t: any) => t.enabled !== false).length,
      totalChanges: list.reduce((sum: number, t: any) => sum + (t.changesCount ?? t.changes_count ?? 0), 0),
    };
  }, [targets]);

  return (
    <div className="flex flex-col h-full">
      {/* Compact toolbar — chrome-tone shelf topbar */}
      <div className={SHELF_TOPBAR}>
        <EyeIcon className="w-4 h-4 text-tertiary shrink-0" />
        <span className="text-[13px] font-semibold text-ink shrink-0">{t('Monitors')}</span>
        {targetList.length > 0 && (
          <span className="hidden @pair/stage:inline text-[11px] text-tertiary ml-1 shrink-0">{t('{{active}} active · {{changes}} changes', { active: activeCount, changes: totalChanges })}</span>
        )}
        <div className="flex-1" />

        {/* Search lives inside the master list column (MonitorList), not the topbar. */}

        <button
          type="button"
          title={t('Show me how this works')}
          aria-label={t('Show me how this works')}
          onClick={() => startFullTutorial('content_monitor')}
          className="p-1.5 text-tertiary hover:text-ink rounded-lg hover:bg-surface/60 transition-colors shrink-0"
        >
          <QuestionMarkCircleIcon className="w-4 h-4" />
        </button>
        <button
          data-tour="checks-new"
          onClick={() => navigate('/checks/new')}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-accent-strong text-accent-on text-[12px] font-medium rounded-lg hover:bg-accent-strong/90 transition-colors shrink-0"
        >
          <PlusIcon className="w-3.5 h-3.5" />
          {t('New')}
        </button>
      </div>

      {/* Body — master-detail two-pane (list + live monitor detail) */}
      <div className="flex flex-1 overflow-hidden">
        <MonitorList search={search} onSearchChange={setSearch} />
      </div>
    </div>
  );
};
