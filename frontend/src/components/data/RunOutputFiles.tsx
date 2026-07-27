import React from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowDownTrayIcon, DocumentIcon } from '@heroicons/react/24/outline';
import toast from 'react-hot-toast';
import { useQuery } from '../../hooks/useQuery';
import { automationApi } from '../../api/endpoints';
import { filesApi } from '../../api/files';

interface OutputFile {
  file_id: string;
  filename: string;
  size?: number;
  content_type?: string;
  output_key?: string | null;
}

function formatBytes(n?: number): string {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * Files the workflow's most recent run CAPTURED (a `wait_for_download` step → the vault). Reads
 * `output_files` from the run's results and offers each as a download. Renders nothing when the
 * latest run captured no files (the common case), so it's safe to always mount.
 */
export const RunOutputFiles: React.FC<{ workflowId: number }> = ({ workflowId }) => {
  const { t } = useTranslation();

  // Most recent run (prefer a successful one), then its full result payload. `summary` omits the
  // heavy result_data/screenshots — we only need ids and status to pick the run.
  const { data: tasks } = useQuery(
    ['run-output-files-tasks', String(workflowId)],
    () => automationApi.listTasks({ workflow_id: workflowId, limit: 5, summary: true }),
  );
  const succeeded = (tasks || []).find((task: any) => task.status === 'success' || task.status === 'completed');
  const latest = succeeded ?? (tasks || [])[0];
  const { data: result } = useQuery(
    ['task-results', String(latest?.id ?? 'none')],
    () => automationApi.getTaskResults(latest!.id),
    { enabled: !!latest?.id },
  );

  const files: OutputFile[] = React.useMemo(() => {
    const of = (result as any)?.output_files;
    return Array.isArray(of) ? of : [];
  }, [result]);

  if (files.length === 0) return null;

  const download = async (f: OutputFile) => {
    try {
      await filesApi.download(f.file_id, f.filename);
    } catch {
      toast.error(t('Could not download the file.'));
    }
  };

  return (
    <div className="mb-4 rounded-xl border border-border bg-canvas p-4">
      <div className="text-[13px] font-medium text-ink mb-2">{t('Files captured by the last run')}</div>
      <div className="space-y-1.5">
        {files.map((f) => (
          <div key={f.file_id} className="flex items-center gap-3 text-sm">
            <DocumentIcon className="w-4 h-4 text-ink/40 shrink-0" />
            <span className="flex-1 truncate font-mono text-xs">
              {f.filename}
              {f.output_key ? <span className="text-secondary"> · {f.output_key}</span> : null}
            </span>
            {f.size != null && <span className="text-secondary text-xs">{formatBytes(f.size)}</span>}
            <button
              onClick={() => download(f)}
              className="shrink-0 flex items-center gap-1 px-2 py-1 text-xs font-medium text-ink/70 hover:text-ink hover:bg-ink/5 rounded-md transition-colors"
              title={t('Download')}
            >
              <ArrowDownTrayIcon className="w-3.5 h-3.5" />
              {t('Download')}
            </button>
          </div>
        ))}
      </div>
    </div>
  );
};

export default RunOutputFiles;
