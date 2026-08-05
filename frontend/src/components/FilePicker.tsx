import React, { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal } from './ui/Modal';
import { filesApi, StoredFile, FileUsage } from '../api/files';
import { apiErrorMessage } from '../api/client';
import { isStorageQuotaError } from '../utils/storageQuota';
import { formatBytes, formatRelativeTime } from '../utils/format';
import { fileTypeIcon } from '../utils/fileIcon';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import {
  ArrowUpTrayIcon,
  MagnifyingGlassIcon,
  CheckCircleIcon,
} from '@heroicons/react/24/outline';

/** The selection a picker returns to its parent. */
export interface PickedFile {
  file_id: string;
  filename: string;
}

interface FilePickerProps {
  /** Controls the modal. */
  isOpen: boolean;
  onClose: () => void;
  /** Called with the chosen file when the user confirms a selection. */
  onSelect: (file: PickedFile) => void;
  /** Currently-linked file id (highlighted in the list). */
  selectedId?: string | null;
  /** Restrict the browsable list to one origin (e.g. 'upload' to hide artifacts). */
  source?: StoredFile['source'];
  /** Modal title override. */
  title?: string;
}

/**
 * FilePicker — a modal that lists the stored files, supports inline upload, and
 * returns a `{file_id, filename}` selection. Reused by the upload step editor and
 * the run-time file-input UI so binding a stored file to a step (or a run) is one
 * consistent surface.
 *
 * Browsing + upload both go through filesApi (the backend enforces ownership /
 * quota / size / content-type), so this component never
 * decides eligibility — it just lists what the server returns.
 */
export const FilePicker: React.FC<FilePickerProps> = ({
  isOpen,
  onClose,
  onSelect,
  selectedId,
  source,
  title,
}) => {
  const { t } = useTranslation();
  const [files, setFiles] = useState<StoredFile[]>([]);
  // Seeded from `isOpen`: a picker mounted already-open is loading from frame one.
  const [loading, setLoading] = useState(isOpen);
  const [search, setSearch] = useState('');
  const [uploading, setUploading] = useState(false);
  const [picked, setPicked] = useState<string | null>(selectedId ?? null);
  // Quota snapshot — fetched on open so the picker can hint remaining space and
  // proactively disable upload when already over the ceiling (server stays the
  // authority; this is a UX pre-check only).
  const [usage, setUsage] = useState<FileUsage | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Both loaders write only from their promise continuations — the "now loading"
  // flip belongs to whatever OPENS the picker (see the reset below), so the open
  // effect never has to set state synchronously.
  const load = React.useCallback(
    () =>
      filesApi
        .list({ source, limit: 200 })
        .then((res) => setFiles(res.data))
        .catch((err) => toast.error(apiErrorMessage(err, t('Failed to load files'))))
        .finally(() => setLoading(false)),
    [source, t],
  );

  const loadUsage = React.useCallback(
    () =>
      filesApi
        .usage()
        .then(setUsage)
        // Non-fatal: without usage we just don't show the hint / pre-disable.
        .catch(() => setUsage(null)),
    [],
  );

  // Reset the local pick + search each time the picker opens (and re-sync the
  // pick if the linked file changes underneath it). Adjusted DURING RENDER —
  // React's derive-state-from-props escape hatch — so the first painted frame
  // already shows the current selection and the loading list.
  const seedKey = isOpen ? `open:${selectedId ?? ''}` : null;
  const [seededFor, setSeededFor] = React.useState<string | null>(seedKey);
  if (seededFor !== seedKey) {
    setSeededFor(seedKey);
    if (isOpen) {
      setPicked(selectedId ?? null);
      setSearch('');
      setLoading(true);
    }
  }

  // Refresh on open. `selectedId` stays in the deps because the reset above flips
  // `loading` back on when it changes — this is what turns it off again.
  React.useEffect(() => {
    if (!isOpen) return;
    void load();
    void loadUsage();
  }, [isOpen, selectedId, load, loadUsage]);

  // Remaining-space affordances (mirror Files.tsx; -1 limit = unlimited).
  const bytesLimit = usage?.bytes_limit ?? -1;
  const bytesUsed = usage?.bytes_used ?? 0;
  const fileLimit = usage?.file_limit ?? -1;
  const fileCount = usage?.file_count ?? 0;
  const bytesOver = bytesLimit >= 0 && bytesUsed >= bytesLimit;
  const filesOver = fileLimit >= 0 && fileCount >= fileLimit;
  const overQuota = bytesOver || filesOver;
  const usageHint = usage
    ? (bytesLimit < 0
        ? t('{{used}} used · unlimited', { used: formatBytes(bytesUsed) })
        : t('{{used}} of {{limit}} used', { used: formatBytes(bytesUsed), limit: formatBytes(bytesLimit) }))
    : null;

  const handleUpload = async (file: File | undefined | null) => {
    if (!file) return;
    setUploading(true);
    try {
      const created = await filesApi.upload(file);
      toast.success(t('Uploaded "{{name}}"', { name: created.filename }));
      // Surface the new file at the top + auto-select it.
      setFiles((prev) => [created, ...prev.filter((f) => f.id !== created.id)]);
      setPicked(created.id);
      void loadUsage();
    } catch (err) {
      // The central storage modal owns a quota denial (global 402/409 handler) —
      // don't double-surface it as a local toast. Other failures still toast.
      if (isStorageQuotaError(err)) {
        void loadUsage();
      } else {
        toast.error(apiErrorMessage(err, t('Upload failed')));
      }
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const confirm = () => {
    const sel = files.find((f) => f.id === picked);
    if (!sel) {
      toast.error(t('Pick a file first'));
      return;
    }
    onSelect({ file_id: sel.id, filename: sel.filename });
    onClose();
  };

  const q = search.trim().toLowerCase();
  const visible = q
    ? files.filter((f) => f.filename.toLowerCase().includes(q))
    : files;

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={title || t('Choose a file')}
      subtitle={t('Pick a stored file or upload a new one.')}
      size="lg"
      footer={
        <div className="flex items-center justify-between gap-2">
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            onChange={(e) => handleUpload(e.target.files?.[0])}
          />
          <div className="flex items-center gap-2 min-w-0">
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading || overQuota}
              title={overQuota ? t('Storage full — free up space first') : undefined}
              className="inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-ink bg-hover rounded-lg hover:bg-active transition-colors disabled:opacity-50 disabled:cursor-not-allowed shrink-0"
            >
              <ArrowUpTrayIcon className="h-4 w-4" />
              {uploading ? t('Uploading…') : t('Upload new')}
            </button>
            {usageHint && (
              <span
                className={clsx('text-[11px] tabular-nums truncate', overQuota ? 'text-red-600 font-medium' : 'text-tertiary')}
                title={usageHint}
              >
                {overQuota ? t('Storage full') : usageHint}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg hover:bg-hover transition-colors"
            >
              {t('Cancel')}
            </button>
            <button
              type="button"
              onClick={confirm}
              disabled={!picked}
              className="px-4 py-2 text-sm font-medium bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 transition-colors disabled:opacity-50"
            >
              {t('Select')}
            </button>
          </div>
        </div>
      }
    >
      <div className="space-y-3">
        {/* Search */}
        <div className="relative">
          <MagnifyingGlassIcon className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-tertiary" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('Search files…')}
            className="w-full pl-9 pr-3 py-2 text-sm bg-canvas border border-border rounded-lg text-ink placeholder:text-tertiary focus:outline-none focus:border-ink/40 transition-colors"
          />
        </div>

        {/* List */}
        {loading ? (
          <div className="flex items-center justify-center py-12">
            <div className="h-5 w-5 animate-spin rounded-full border-2 border-border border-t-zinc-800" />
          </div>
        ) : visible.length === 0 ? (
          <div className="rounded-xl border border-border bg-canvas px-4 py-8 text-center">
            <p className="text-[13px] font-medium text-ink">
              {q ? t('No files match your search') : t('No files yet')}
            </p>
            <p className="text-xs text-tertiary mt-0.5">
              {q ? t('Try a different name') : t('Upload a file to get started.')}
            </p>
          </div>
        ) : (
          <div className="border border-border rounded-xl overflow-hidden divide-y divide-border max-h-[48vh] overflow-y-auto">
            {visible.map((f) => {
              const Icon = fileTypeIcon(f.content_type, f.filename);
              const isPicked = picked === f.id;
              return (
                <button
                  key={f.id}
                  type="button"
                  onClick={() => setPicked(f.id)}
                  className={clsx(
                    'w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors',
                    isPicked ? 'bg-ink/5' : 'hover:bg-hover',
                  )}
                >
                  <div className="w-8 h-8 rounded-lg bg-hover flex items-center justify-center shrink-0">
                    <Icon className="w-4 h-4 text-tertiary" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="text-[13px] font-medium text-ink truncate">{f.filename}</div>
                    <div className="text-xs text-tertiary mt-0.5 tabular-nums">
                      {formatBytes(f.bytes)}
                      {f.created_at ? ` · ${formatRelativeTime(new Date(f.created_at * 1000))}` : ''}
                    </div>
                  </div>
                  {isPicked && <CheckCircleIcon className="w-5 h-5 text-ink shrink-0" />}
                </button>
              );
            })}
          </div>
        )}
      </div>
    </Modal>
  );
};

export default FilePicker;
