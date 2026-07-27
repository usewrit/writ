import React from 'react';
import { DocumentIcon, ArrowUpTrayIcon, CheckCircleIcon } from '@heroicons/react/24/outline';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import { useQuery } from '../../../hooks/useQuery';
import { filesApi, StoredFile } from '../../../api/files';
import { Select } from '../../ui';

interface FilePickerProps {
  /** Selected file handle ("file_<id>") or null. */
  value: string | null;
  onChange: (fileId: string | null) => void;
  label?: string;
  /** Optional source filter (e.g. 'workflow_output' to only show captured artifacts). */
  source?: 'upload' | 'api' | 'workflow_output';
  allowClear?: boolean;
}

function formatBytes(bytes: number): string {
  if (!bytes || bytes < 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let n = bytes;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n % 1 === 0 ? n : n.toFixed(1)} ${units[i]}`;
}

/**
 * Interactive file picker for block editors — lists the file library (via
 * filesApi.list) with an upload-in-place affordance. Selecting a file writes its
 * stable `file_<id>` handle into the block config.
 */
export const FilePicker: React.FC<FilePickerProps> = ({
  value,
  onChange,
  label,
  source,
  allowClear = true,
}) => {
  const { t } = useTranslation();
  const inputRef = React.useRef<HTMLInputElement | null>(null);
  const [uploading, setUploading] = React.useState(false);

  const { data, refresh } = useQuery(
    ['flow-files', source || 'all'],
    () => filesApi.list({ source, limit: 200 }),
  );
  const files: StoredFile[] = data?.data || [];
  const selected = files.find((f) => f.id === value) || null;

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (inputRef.current) inputRef.current.value = '';
    if (!file) return;
    setUploading(true);
    try {
      const created = await filesApi.upload(file);
      await refresh();
      onChange(created.id);
      toast.success(t('File uploaded'));
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || err?.message || t('Failed to upload file'));
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="space-y-1.5">
      <label className="flex items-center gap-1.5 text-xs font-medium text-zinc-500">
        <DocumentIcon className="w-4 h-4" />
        {label || t('File')}
      </label>
      <div className="flex items-center gap-2">
        <Select
          value={value ?? ''}
          onChange={(v) => onChange(v === '' ? null : String(v))}
          aria-label={label || t('File')}
          placeholder={allowClear ? t('No file selected') : undefined}
          options={[
            ...(allowClear ? [{ value: '', label: t('No file selected') }] : []),
            ...files.map((f) => ({
              value: f.id,
              label: `${f.filename} · ${formatBytes(f.bytes)}`,
            })),
          ]}
          className="flex-1"
        />
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          disabled={uploading}
          title={t('Upload a file')}
          className="flex items-center gap-1 px-2.5 py-2 text-xs font-medium text-zinc-500 border border-zinc-200 rounded-lg hover:bg-hover hover:text-zinc-900 transition-colors disabled:opacity-50 shrink-0"
        >
          <ArrowUpTrayIcon className="w-4 h-4" />
          {uploading ? t('Uploading…') : t('Upload')}
        </button>
        <input ref={inputRef} type="file" className="hidden" onChange={handleUpload} />
      </div>
      {selected && (
        <p className="flex items-center gap-1.5 text-[11px] text-zinc-400">
          <CheckCircleIcon className="w-3.5 h-3.5" />
          <span className="text-zinc-500">{selected.filename}</span>
          {selected.source && <span>· {selected.source}</span>}
        </p>
      )}
    </div>
  );
};

export default FilePicker;
