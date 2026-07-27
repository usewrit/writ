import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  IdentificationIcon, PlusIcon, QrCodeIcon,
  QuestionMarkCircleIcon,
} from '@heroicons/react/24/outline';
import { useRequireAuth } from '../hooks/useAuth';
import { useQuery } from '../hooks/useQuery';
import { personasApi } from '../api/endpoints';
import { Q } from '../stores/queryKeys';
import type { Persona } from '../types/api';
import { PersonaWizard } from '../components/workflows/PersonaWizard';
import { PersonaHelpModal } from '../components/workflows/PersonaHelpModal';
import { AuthenticatorImportModal } from '../components/workflows/AuthenticatorImportModal';
import { PersonaList } from '../components/library/PersonaList';
import { SHELF_TOPBAR, SHELF_TOPBAR_BTN } from '../components/library/shelf';

export const PersonasPage: React.FC = () => {
  const { t } = useTranslation();
  useRequireAuth();
  // Shared cache with PersonaList — powers the header count without a second request.
  const { data: personas, refresh } = useQuery<Persona[]>(Q.personas(), () => personasApi.list(), { pollInterval: 30000 });
  const [search, setSearch] = useState('');
  const [showCreate, setShowCreate] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [showHelp, setShowHelp] = useState(false);

  const count = (personas || []).length;

  return (
    <>
      <div className="flex flex-col h-full">
        {/* Toolbar */}
        <div className={SHELF_TOPBAR}>
          <IdentificationIcon className="w-4 h-4 text-secondary shrink-0" />
          <span className="text-[13px] font-semibold text-ink shrink-0">{t('Personas')}</span>
          {count > 0 && (
            <span className="hidden @pair/stage:inline text-[11px] text-secondary ml-1 shrink-0">{count === 1 ? t('{{n}} identity', { n: count }) : t('{{n}} identities', { n: count })}</span>
          )}
          <span className="hidden @rail/stage:inline text-[11px] text-secondary ml-1 truncate">{t('Reusable cloud logins that pass 2FA and reuse a warm session.')}</span>

          <div className="flex-1" />

          <button
            type="button"
            onClick={() => setShowHelp(true)}
            title={t('How personas work')}
            aria-label={t('How personas work')}
            className="p-1.5 text-secondary hover:text-ink rounded-lg hover:bg-surface/60 transition-colors shrink-0"
          >
            <QuestionMarkCircleIcon className="w-4 h-4" />
          </button>

          <button
            onClick={() => setShowImport(true)}
            title={t('Import existing 2FA secrets from your authenticator app')}
            className={SHELF_TOPBAR_BTN}
          >
            <QrCodeIcon className="w-3.5 h-3.5" /> {t('Import')}
          </button>

          <button
            data-tour="personas-new"
            onClick={() => setShowCreate(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-accent-strong text-accent-on text-[12px] font-semibold rounded-lg shadow-sm hover:bg-accent-strong/90 transition-colors shrink-0"
          >
            <PlusIcon className="w-3.5 h-3.5" /> {t('New')}
          </button>
        </div>

        {/* Body — master–detail two-pane (list + identity detail) */}
        <div className="flex flex-1 overflow-hidden">
          <PersonaList search={search} onSearchChange={setSearch} onCreate={() => setShowCreate(true)} />
        </div>
      </div>

      {/* Quick-start help */}
      <PersonaHelpModal isOpen={showHelp} onClose={() => setShowHelp(false)} />

      {/* Create wizard */}
      <PersonaWizard
        isOpen={showCreate}
        onClose={() => setShowCreate(false)}
        onSaved={() => refresh()}
      />

      {/* Bulk import existing authenticator seeds → personas */}
      <AuthenticatorImportModal
        isOpen={showImport}
        onClose={() => setShowImport(false)}
        onImported={() => refresh()}
      />
    </>
  );
};

export default PersonasPage;
