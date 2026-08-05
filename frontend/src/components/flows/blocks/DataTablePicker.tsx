import React from 'react';
import { TableCellsIcon } from '@heroicons/react/24/outline';
import { useTranslation } from 'react-i18next';
import { useQuery } from '../../../hooks/useQuery';
import { workflowDataApi } from '../../../api/workflowData';
import { Select } from '../../ui';

interface DataTablePickerProps {
  /** Selected source workflow id, or null. */
  value: number | null;
  onChange: (workflowId: number | null) => void;
  label?: string;
  allowClear?: boolean;
}

/**
 * Interactive data-table picker for block editors — lists the workflows that have
 * produced extracted data (workflowDataApi.listWorkflows) and, once one is chosen,
 * shows a small REDACTED column/row preview via the preview endpoint so the author
 * can confirm the shape before wiring an export/append action.
 */
export const DataTablePicker: React.FC<DataTablePickerProps> = ({
  value,
  onChange,
  label,
  allowClear = false,
}) => {
  const { t } = useTranslation();

  const { data: workflows } = useQuery(
    ['flow-data-workflows'],
    () => workflowDataApi.listWorkflows(),
  );

  const { data: preview, loading: previewLoading } = useQuery(
    ['flow-data-preview', String(value ?? 'none')],
    () => (value ? workflowDataApi.preview(value, { limit: 3 }) : Promise.resolve(null)),
    { enabled: value != null },
  );

  const columns = preview?.columns || [];
  const rows = preview?.rows || [];

  return (
    <div className="space-y-2">
      <label className="flex items-center gap-1.5 text-xs font-medium text-secondary">
        <TableCellsIcon className="w-4 h-4" />
        {label || t('Data source')}
      </label>
      <Select<number>
        value={value ?? undefined}
        onChange={(v) => onChange(v === -1 ? null : v)}
        aria-label={label || t('Data source')}
        placeholder={allowClear ? t('No data source') : t('Select a workflow with data…')}
        options={[
          ...(allowClear ? [{ value: -1, label: t('No data source') }] : []),
          ...((workflows || []).map((w) => ({
            value: w.workflow_id,
            label: `${w.workflow_name} · ${t('{{n}} runs', { n: w.run_count })}`,
          }))),
        ]}
        className="w-full"
      />

      {value != null && (
        <div className="rounded-lg border border-border bg-canvas overflow-hidden">
          {previewLoading ? (
            <div className="px-3 py-3 text-[11px] text-tertiary">{t('Loading preview…')}</div>
          ) : columns.length === 0 ? (
            <div className="px-3 py-3 text-[11px] text-tertiary">{t('No extracted data yet.')}</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="border-b border-border">
                    {columns.map((c) => (
                      <th key={c} className="px-2.5 py-1.5 text-left font-medium text-secondary whitespace-nowrap">
                        {c}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.slice(0, 3).map((row, i) => (
                    <tr key={i} className="border-b border-border last:border-0">
                      {columns.map((c) => (
                        <td key={c} className="px-2.5 py-1.5 text-ink whitespace-nowrap max-w-[160px] truncate">
                          {formatCell(row[c])}
                        </td>
                      ))}
                    </tr>
                  ))}
                  {rows.length === 0 && (
                    <tr>
                      <td colSpan={columns.length} className="px-2.5 py-1.5 text-tertiary">
                        {t('Columns detected — no sample rows.')}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

function formatCell(v: unknown): string {
  if (v == null) return '';
  if (typeof v === 'object') {
    try {
      return JSON.stringify(v);
    } catch {
      return String(v);
    }
  }
  return String(v);
}

export default DataTablePicker;
