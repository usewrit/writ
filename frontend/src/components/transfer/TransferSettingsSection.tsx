import React, { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  ArrowDownTrayIcon,
  ArrowUpTrayIcon,
  ClockIcon,
  ShieldExclamationIcon,
} from '@heroicons/react/24/outline';
import { Button } from '../ui/Button';
import { Badge } from '../ui/Badge';
import { apiErrorMessage } from '../../api/client';
import { formatDate } from '../../utils/format';
import { transferApi, type TransferImportRow } from '../../api/transfer';
import { ExportWizard } from './ExportWizard';
import { ImportWizard } from './ImportWizard';

/**
 * Settings → Import & export. The account-level home for portable `.writ`
 * packages: the two wizards, plus the history of packages imported here (which is
 * also where a recent import's Undo lives).
 */
export const TransferSettingsSection: React.FC = () => {
  const { t } = useTranslation();
  const [exportOpen, setExportOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [history, setHistory] = useState<TransferImportRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    transferApi
      .list()
      .then((res) => { setHistory(res.imports || []); setError(null); })
      .catch((err) => setError(apiErrorMessage(err, 'Could not load import history.')));
  }, []);

  useEffect(load, [load]);

  return (
    <div className="space-y-6">
      <div className="grid gap-3 sm:grid-cols-2">
        <button
          type="button"
          onClick={() => setExportOpen(true)}
          className="text-left rounded-xl border border-border p-4 hover:border-accent/60 transition-colors"
        >
          <ArrowDownTrayIcon className="w-5 h-5 text-accent-strong" />
          <p className="mt-2 text-sm font-medium text-ink">
            {t('Export a package')}
          </p>
          <p className="mt-0.5 text-xs text-secondary">
            {t('Package workflows, monitors and their wiring into one encrypted file you can import into Writ Cloud or another self-hosted install.')}
          </p>
        </button>

        <button
          type="button"
          onClick={() => setImportOpen(true)}
          className="text-left rounded-xl border border-border p-4 hover:border-accent/60 transition-colors"
        >
          <ArrowUpTrayIcon className="w-5 h-5 text-accent-strong" />
          <p className="mt-2 text-sm font-medium text-ink">
            {t('Import a package')}
          </p>
          <p className="mt-0.5 text-xs text-secondary">
            {t('Open a .writ file, choose what to bring in, and attach your own logins and keys. Nothing is created until you confirm.')}
          </p>
        </button>
      </div>

      <p className="text-xs text-secondary">
        {t('A package carries the names of the logins and keys it needs, not their values — so sharing one never shares your credentials. Moving your own account is the exception, and it asks for your password first.')}
      </p>

      {error && <p className="text-xs text-red-600">{error}</p>}

      {history.length > 0 && (
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-secondary mb-1.5">
            {t('Imported here')}
          </p>
          <div className="rounded-lg border border-border divide-y divide-border">
            {history.map((row) => (
              <div key={row.id} className="p-3 flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm text-ink truncate">
                    {row.label || `Package from ${row.producer.app || 'Writ'}`}
                  </p>
                  <p className="text-xs text-secondary">
                    {row.created_at ? formatDate(row.created_at) : ''}
                    {' · '}
                    {Object.entries(row.counts || {})
                      .filter(([k, n]) => n > 0 && !k.startsWith('data_'))
                      .map(([k, n]) => `${n} ${k.replace('_', ' ')}`)
                      .join(', ') || 'nothing'}
                  </p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  {row.has_sealed_credentials && (
                    <Badge variant="error">
                      <ShieldExclamationIcon className="w-3.5 h-3.5 mr-1" />
                      credentials
                    </Badge>
                  )}
                  <Badge variant={STATUS_TONE[row.status] || 'default'}>{row.status}</Badge>
                  {row.status === 'committed' && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={async () => {
                        try {
                          await transferApi.undo(row.id);
                          load();
                        } catch (err) {
                          setError(apiErrorMessage(err, 'Could not undo that import.'));
                        }
                      }}
                    >
                      <ClockIcon className="w-3.5 h-3.5 mr-1" />
                      {t('Undo')}
                    </Button>
                  )}
                </div>
              </div>
            ))}
          </div>
          <p className="text-xs text-secondary mt-1.5">
            {t('An import can be undone for 24 hours. Anything that has run or collected data since is kept.')}
          </p>
        </div>
      )}

      <ExportWizard isOpen={exportOpen} onClose={() => setExportOpen(false)} />
      <ImportWizard
        isOpen={importOpen}
        onClose={() => { setImportOpen(false); load(); }}
        onImported={load}
      />
    </div>
  );
};

const STATUS_TONE: Record<string, 'default' | 'success' | 'warning' | 'error'> = {
  staged: 'warning',
  planned: 'warning',
  committing: 'warning',
  committed: 'success',
  failed: 'error',
  undone: 'default',
  expired: 'default',
};

export default TransferSettingsSection;
