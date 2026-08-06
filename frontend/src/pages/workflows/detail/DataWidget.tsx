import React from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowRightIcon, TableCellsIcon } from '@heroicons/react/24/outline';
import { formatRelativeTime } from '../../../utils/format';
import { useQuery } from '../../../hooks/useQuery';
import { Q } from '../../../stores/queryKeys';
import { workflowDataApi } from '../../../api/workflowData';
import { Section } from './Section';

/**
 * Detail-page data widget: a fast, scannable peek at the most recent run's
 * extracted output, with a one-click jump to the full Data tab (sortable /
 * filterable / exportable table across every run). Surfaces the result without
 * making the user leave the Detail page first.
 *
 * The newest row comes from the same extracted-data endpoint the Data tab uses
 * (limit 1) rather than the workflow row's `last_run_extracted_data`. That field
 * only ever describes the SINGLE most recent run, so a failed (or still-running)
 * newest run made this widget claim "No output yet" while the Data tab happily
 * listed every earlier run's records. The workflow field stays as a fallback for
 * a payload that carries it but has no data rows yet. Matches the desktop app.
 */
export const DataWidget: React.FC<{
  workflow: any;
  /** Jump to the full Data tab. */
  onOpenData: () => void;
}> = ({ workflow, onOpenData }) => {
  const { t } = useTranslation();
  const { data: table, loading } = useQuery(
    Q.workflowData(workflow.id, 'widget'),
    () => workflowDataApi.getTable(workflow.id, { limit: 1 }),
    { enabled: !!workflow?.id, silent: true, staleTime: 15000 },
  );
  const latestRow = table?.rows?.[0];
  // A run's extracted_data is EITHER a single record OR a list of them (a listing-page
  // scrape returns the list form). Peek at the first record either way — Object.entries
  // on a raw list would render array indices as field names.
  const lastRaw = latestRow?.fields ?? workflow.last_run_extracted_data;
  const last = (Array.isArray(lastRaw) ? lastRaw[0] : lastRaw) as Record<string, unknown> | undefined;
  const entries = last && typeof last === 'object' ? Object.entries(last) : [];
  const lastAt = latestRow?.run_at ?? workflow.last_run_at;

  return (
    <Section
      title={t('Data')}
      description={t('What this workflow produces — the latest output, with everything it has ever extracted one click away.')}
      right={
        <button onClick={onOpenData} className="inline-flex items-center gap-1 text-[11px] text-tertiary hover:text-ink transition-colors">
          {t('Open Data')}
          <ArrowRightIcon className="w-3 h-3" />
        </button>
      }
    >
      {entries.length === 0 ? (
        <button
          onClick={onOpenData}
          className="w-full flex items-center gap-3 px-4 py-3 bg-surface border border-ink/20 shadow-sm rounded-xl hover:bg-chrome transition-colors text-left"
        >
          <TableCellsIcon className="w-4 h-4 text-tertiary shrink-0" />
          <span className="text-[13px] text-tertiary flex-1">
            {loading && !table ? t('Loading latest output…') : t('No output yet — run this workflow to extract data.')}
          </span>
          <ArrowRightIcon className="w-3.5 h-3.5 text-tertiary shrink-0" />
        </button>
      ) : (
        <div className="bg-surface border border-ink/20 shadow-sm rounded-xl overflow-hidden">
          <div className="flex items-center justify-between px-4 py-2 border-b border-border">
            <span className="text-[11px] font-medium text-secondary">{t('Latest output')}</span>
            {lastAt && (
              <span className="text-[10px] text-tertiary">{formatRelativeTime(lastAt)}</span>
            )}
          </div>
          <div className="p-3 space-y-2 max-h-56 overflow-y-auto">
            {entries.slice(0, 6).map(([key, value]) => (
              <div key={key}>
                <span className="text-[10px] text-tertiary uppercase tracking-wider">{key}</span>
                <div className="text-xs text-ink font-mono whitespace-pre-wrap break-all max-h-24 overflow-y-auto mt-0.5">
                  {typeof value === 'string' ? value : JSON.stringify(value, null, 2)}
                </div>
              </div>
            ))}
            {entries.length > 6 && (
              <p className="text-[11px] text-tertiary pt-1">{t('+ {{n}} more fields', { n: entries.length - 6 })}</p>
            )}
          </div>
          <button
            onClick={onOpenData}
            className="w-full flex items-center justify-center gap-1.5 px-4 py-2 border-t border-border text-[12px] font-medium text-secondary hover:text-ink hover:bg-chrome transition-colors"
          >
            <TableCellsIcon className="w-3.5 h-3.5" />
            {t('View all extracted data')}
          </button>
        </div>
      )}
    </Section>
  );
};

export default DataWidget;
