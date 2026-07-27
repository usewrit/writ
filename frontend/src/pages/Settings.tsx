import React from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';
import clsx from 'clsx';
import {
  UserCircleIcon,
  KeyIcon,
  SparklesIcon,
  CpuChipIcon,
  BellIcon,
  CircleStackIcon,
  GlobeAltIcon,
  ShieldCheckIcon,
  Cog6ToothIcon,
  AdjustmentsHorizontalIcon,
} from '@heroicons/react/24/outline';
import { useRequireAuth } from '../hooks/useAuth';
import { useDocumentTitle } from '../hooks/useDocumentTitle';
import { ScrollArea } from '../components/ui/ScrollArea';
import { AccountSection } from '../components/settings/AccountSection';
import { AIProvidersSection } from '../components/settings/AIProvidersSection';
import { RuntimeSection } from '../components/settings/RuntimeSection';
import { NotificationsSection } from '../components/settings/NotificationsSection';
import { DataRetentionSection } from '../components/settings/DataRetentionSection';
import { NetworkSection } from '../components/settings/NetworkSection';
import { SecuritySection } from '../components/settings/SecuritySection';
import { GeneralSection } from '../components/settings/GeneralSection';
import { PointerSection } from '../components/settings/PointerSection';

// ─────────────────────────────────────────────────────────────────────────────
// Settings (self-host coordinator) — the desktop-modeled 10-section surface,
// adapted to the single-owner build. API Keys and Agents/Fleet are shallow
// pointers (their full UI lives on /developers and /fleet respectively); the
// rest are edited in-place against the coordinator's /api/settings/* + auth +
// notifications endpoints.
// ─────────────────────────────────────────────────────────────────────────────

type TabId =
  | 'account'
  | 'api-keys'
  | 'ai'
  | 'runtime'
  | 'notifications'
  | 'data'
  | 'network'
  | 'security'
  | 'general'
  | 'agents';

const TABS: { id: TabId; label: string; icon: typeof UserCircleIcon }[] = [
  { id: 'account', label: 'Account', icon: UserCircleIcon },
  { id: 'api-keys', label: 'API Keys', icon: KeyIcon },
  { id: 'ai', label: 'AI Providers', icon: SparklesIcon },
  { id: 'runtime', label: 'Runtime', icon: AdjustmentsHorizontalIcon },
  { id: 'notifications', label: 'Notifications', icon: BellIcon },
  { id: 'data', label: 'Data & Retention', icon: CircleStackIcon },
  { id: 'network', label: 'Network', icon: GlobeAltIcon },
  { id: 'security', label: 'Security', icon: ShieldCheckIcon },
  { id: 'general', label: 'General', icon: Cog6ToothIcon },
  { id: 'agents', label: 'Agents & Fleet', icon: CpuChipIcon },
];

const ALL_IDS = TABS.map((t) => t.id);

export const Settings: React.FC = () => {
  useRequireAuth();
  const { t } = useTranslation();
  useDocumentTitle(t('Settings'));
  const [searchParams, setSearchParams] = useSearchParams();

  const tabParam = searchParams.get('tab') as TabId | null;
  const activeTab: TabId = tabParam && ALL_IDS.includes(tabParam) ? tabParam : 'account';
  const setActiveTab = (tab: TabId) => {
    setSearchParams(tab === 'account' ? {} : { tab }, { replace: true });
  };

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar + tab strip */}
      <div className="flex items-center gap-3 h-12 px-4 sm:px-6 bg-chrome chrome-topbar border-b border-border shrink-0 overflow-x-auto">
        <Cog6ToothIcon className="w-4 h-4 text-secondary shrink-0" />
        <span className="text-[13px] font-semibold text-ink shrink-0">{t('Settings')}</span>
        <div className="flex items-center gap-0.5 ml-2" role="tablist" aria-label={t('Settings')}>
          {TABS.map((tab) => {
            const active = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                role="tab"
                aria-selected={active}
                onClick={() => setActiveTab(tab.id)}
                className={clsx(
                  // No `transition-colors` override: the interaction baseline
                  // (index.css) also eases box-shadow, so the active pill
                  // settles instead of snapping.
                  'flex items-center gap-1.5 px-2.5 py-1 text-[12px] font-medium rounded-md whitespace-nowrap',
                  active ? 'bg-zinc-100 text-ink' : 'text-secondary hover:text-ink',
                )}
              >
                <tab.icon className="w-3.5 h-3.5" />
                {t(tab.label)}
              </button>
            );
          })}
        </div>
      </div>

      {/* Active section — keyed on the tab: switching replays a 160ms opacity
          settle (content-in) so panels swap smoothly, never snap. */}
      <ScrollArea className="flex-1 min-h-0">
        <div key={activeTab} className="px-5 py-7 sm:px-8 max-w-3xl mx-auto animate-content-in">
          {activeTab === 'account' && <AccountSection />}
          {activeTab === 'api-keys' && (
            <PointerSection
              title={t('API Keys')}
              description={t('Scoped keys you mint to call your own automations over REST, MCP and the OpenAI-compatible API.')}
              linkTo="/developers?tab=keys"
              linkLabel={t('Manage API keys')}
              bullets={[
                t('Per-resource scope matrix (read / write / delete) with an optional id allowlist.'),
                t('The raw key is shown once at creation and stored only as a hash.'),
                t('Track created / last-used and revoke any key.'),
              ]}
            />
          )}
          {activeTab === 'ai' && <AIProvidersSection />}
          {activeTab === 'runtime' && <RuntimeSection />}
          {activeTab === 'notifications' && <NotificationsSection />}
          {activeTab === 'data' && <DataRetentionSection />}
          {activeTab === 'network' && <NetworkSection />}
          {activeTab === 'security' && <SecuritySection />}
          {activeTab === 'general' && <GeneralSection />}
          {activeTab === 'agents' && (
            <PointerSection
              title={t('Agents & Fleet')}
              description={t('Connect and manage the writ-agent-fleet workers that run your workflows and monitor checks.')}
              linkTo="/fleet"
              linkLabel={t('Open Fleet')}
              bullets={[
                t('Mint long-lived connect tokens and see the ready-to-run agent command.'),
                t('View connected agents with platform, status, capacity and last-seen.'),
                t('Revoke a token to disconnect its bound agent.'),
              ]}
            />
          )}
        </div>
      </ScrollArea>
    </div>
  );
};

export default Settings;
