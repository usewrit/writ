import React from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';
import { KeyIcon, CircleStackIcon } from '@heroicons/react/24/outline';
import { useRequireAuth } from '../../hooks/useAuth';
import { useDocumentTitle } from '../../hooks/useDocumentTitle';
import { ApiKeys } from '../ApiKeys';
import { EndpointsPage } from './EndpointsPage';
import { ViewSwitch } from '../../components/ui/ViewSwitch';

// ─────────────────────────────────────────────────────────────────────────────
// Developers — one home for the two programmatic-access surfaces that used to be
// separate pages (API Keys at /keys, Endpoints at /developers/endpoints). They
// were easy to confuse ("which keys page?"), so they're unified under a single
// tabbed shell. Each tab still renders its existing, self-contained page
// component (with its own contextual toolbar + actions) below the tab strip.
// ─────────────────────────────────────────────────────────────────────────────

type DevTab = 'keys' | 'endpoints';

const TABS: { id: DevTab; label: string; icon: typeof KeyIcon }[] = [
  { id: 'keys', label: 'API Keys', icon: KeyIcon },
  { id: 'endpoints', label: 'Endpoints', icon: CircleStackIcon },
];

export const DevelopersPage: React.FC = () => {
  const { t } = useTranslation();
  useRequireAuth();
  useDocumentTitle(t('Developers'));
  const [searchParams, setSearchParams] = useSearchParams();

  const tabParam = searchParams.get('tab');
  const activeTab: DevTab = tabParam === 'endpoints' ? 'endpoints' : 'keys';
  const setActiveTab = (tab: DevTab) => {
    // Default tab (keys) leaves the URL clean; deep tabs are shareable.
    setSearchParams(tab === 'keys' ? {} : { tab }, { replace: true });
  };

  return (
    <div className="flex flex-col h-full">
      {/* Top-level Developers tab strip — selects which programmatic surface.
          The selected page renders its own contextual toolbar (actions,
          sub-sections) directly below this strip. */}
      <div className="flex items-center gap-1 h-11 px-4 sm:px-6 bg-chrome chrome-topbar border-b border-border shrink-0">
        <span className="text-[13px] font-semibold text-ink shrink-0 mr-2">{t('Developers')}</span>
        {/* ViewSwitch = the shared segmented control: the active pill GLIDES
            between tabs instead of bg+shadow snapping. */}
        <ViewSwitch
          value={activeTab}
          onChange={setActiveTab}
          options={TABS.map((tab) => ({ id: tab.id, label: t(tab.label), icon: tab.icon }))}
        />
      </div>

      {/* Active surface fills the remaining height (each page is `h-full`).
          Keyed on the tab: switching replays a 160ms opacity settle. */}
      <div key={activeTab} className="flex-1 min-h-0 animate-content-in">
        {activeTab === 'keys' ? <ApiKeys /> : <EndpointsPage />}
      </div>
    </div>
  );
};

export default DevelopersPage;
