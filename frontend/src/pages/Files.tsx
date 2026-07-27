import React, { useRef, useState, useEffect, useMemo, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { useRequireAuth } from '../hooks/useAuth';
import { useQuery } from '../hooks/useQuery';
import { Modal } from '../components/ui/Modal';
import { Select, ViewSwitch, ScrollArea } from '../components/ui';
import { filesApi, StoredFile, FileSource } from '../api/files';
import { fileTypeIcon } from '../utils/fileIcon';
import { apiErrorMessage } from '../api/client';
import { isStorageQuotaError } from '../utils/storageQuota';
import { formatBytes, formatRelativeTime } from '../utils/format';
import { statusStyle } from '../utils/statusStyle';
import { seedTint, tintStyle } from '../utils/tint';
import { SHELF_TOPBAR, RowsSkeleton } from '../components/library/shelf';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import {
  DocumentDuplicateIcon,
  MagnifyingGlassIcon,
  ArrowUpTrayIcon,
  ArrowDownTrayIcon,
  EyeIcon,
  TrashIcon,
  ClipboardDocumentIcon,
  Cog6ToothIcon,
  CheckIcon,
  ListBulletIcon,
  Squares2X2Icon,
} from '@heroicons/react/24/outline';

// Source buckets — client-side filter chips (their mental model: my uploads vs
// files a workflow produced vs API-pushed vs captured by AI/streaming sessions).
// Admin exposes all FIVE FileSource origins; a chip only renders when it has a
// non-zero count (so uncommon origins stay out of the way until they appear).
const SOURCE_CHIPS: { id: '' | FileSource; label: string }[] = [
  { id: '', label: 'All' },
  { id: 'upload', label: 'Uploads' },
  { id: 'workflow_output', label: 'Workflow output' },
  { id: 'api', label: 'API' },
  { id: 'ai_session', label: 'AI sessions' },
  { id: 'streaming', label: 'Streaming' },
];
const SOURCE_LABELS: Record<string, string> = {
  upload: 'Upload',
  api: 'API',
  workflow_output: 'Workflow output',
  ai_session: 'AI session',
  streaming: 'Streaming',
};

type SortMode = 'recent' | 'name' | 'size' | 'type';
type ViewMode = 'list' | 'grid';

function sortFiles(files: StoredFile[], mode: SortMode): StoredFile[] {
  const arr = [...files];
  switch (mode) {
    case 'name': return arr.sort((a, b) => a.filename.localeCompare(b.filename));
    case 'size': return arr.sort((a, b) => b.bytes - a.bytes);
    case 'type': return arr.sort((a, b) => (a.content_type || '').localeCompare(b.content_type || '') || a.filename.localeCompare(b.filename));
    case 'recent':
    default:
      return arr.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  }
}

// `fileTypeIcon` resolves to one of a fixed set of module-level heroicons, but a
// component *type* produced by a call inside a render body reads as "created
// during render" — React would be told the icon is a new component every render
// and remount it (`react-hooks/static-components`). Passing the resolved type in
// as a prop makes it data rather than a locally-declared component.
const FileGlyph: React.FC<{ icon: React.ElementType; className: string }> = ({ icon: Icon, className }) => (
  <Icon className={className} />
);

const isPreviewable = (f: StoredFile) =>
  f.status === 'ready' && ((f.content_type || '').startsWith('image/') || (f.content_type || '') === 'application/pdf');

/**
 * Lazy image thumbnail for grid view — loads the blob only once the tile scrolls
 * into view (IntersectionObserver), renders from an object URL revoked on unmount.
 * Non-images (or not-ready) fall back to a tinted type-icon tile.
 */
const FileThumb: React.FC<{ file: StoredFile; className?: string; iconClass?: string }> = ({ file, className, iconClass }) => {
  const isImage = (file.content_type || '').startsWith('image/') && file.status === 'ready';
  const ref = useRef<HTMLDivElement>(null);
  const [inView, setInView] = useState(false);
  const [url, setUrl] = useState<string | null>(null);
  const Icon = fileTypeIcon(file.content_type, file.filename);

  useEffect(() => {
    if (!isImage || !ref.current || inView) return;
    const el = ref.current;
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { setInView(true); io.disconnect(); }
    }, { rootMargin: '250px' });
    io.observe(el);
    return () => io.disconnect();
  }, [isImage, inView]);

  useEffect(() => {
    if (!inView || !isImage) return;
    let revoked = false;
    let obj: string | null = null;
    filesApi.fetchBlob(file.id)
      .then((b) => { if (revoked) return; obj = URL.createObjectURL(b); setUrl(obj); })
      .catch(() => { /* fall back to the icon tile */ });
    return () => { revoked = true; if (obj) URL.revokeObjectURL(obj); };
  }, [inView, isImage, file.id]);

  return (
    <div
      ref={ref}
      className={clsx('relative flex items-center justify-center overflow-hidden', className)}
      style={!url ? tintStyle(file.content_type ? seedTint(file.content_type.split('/')[0]) : 'neutral') : undefined}
    >
      {url ? (
        <img src={file.status === 'ready' ? url : undefined} alt={file.filename} className="w-full h-full object-cover" />
      ) : (
        <FileGlyph icon={Icon} className={iconClass || 'w-6 h-6 opacity-85'} />
      )}
    </div>
  );
};

export const Files: React.FC = () => {
  const { t } = useTranslation();
  useRequireAuth();
  const [search, setSearch] = useState('');
  const [source, setSource] = useState<'' | FileSource>('');
  const [sortBy, setSortBy] = useState<SortMode>(() => {
    const v = localStorage.getItem('writ.filesSort');
    return (['recent', 'name', 'size', 'type'] as const).includes(v as any) ? (v as SortMode) : 'recent';
  });
  const [viewMode, setViewMode] = useState<ViewMode>(() => (localStorage.getItem('writ.filesView') === 'grid' ? 'grid' : 'list'));
  const [uploading, setUploading] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<StoredFile | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [previewTarget, setPreviewTarget] = useState<StoredFile | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => { try { localStorage.setItem('writ.filesSort', sortBy); } catch { /* noop */ } }, [sortBy]);
  useEffect(() => { try { localStorage.setItem('writ.filesView', viewMode); } catch { /* noop */ } }, [viewMode]);

  // All files (client-side source/search/sort so the chip counts + filters are instant).
  const { data: list, loading, refresh } = useQuery(
    ['files', 'all'],
    () => filesApi.list({ limit: 200 }),
    { pollInterval: 30000 },
  );
  const { data: usage, refresh: refreshUsage } = useQuery('files:usage', () => filesApi.usage(), { pollInterval: 30000 });

  const allFiles = useMemo(() => list?.data || [], [list]);
  const sourceCounts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const f of allFiles) m[f.source] = (m[f.source] || 0) + 1;
    return m;
  }, [allFiles]);

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase();
    const filtered = allFiles
      .filter((f) => !source || f.source === source)
      .filter((f) => !q || f.filename.toLowerCase().includes(q));
    return sortFiles(filtered, sortBy);
  }, [allFiles, source, search, sortBy]);

  // Prune selection to what's visible. This is the render-phase "adjust state
  // when an input changes" pattern rather than an effect: a setState in an effect
  // body only lands after paint (`react-hooks/set-state-in-effect`), so the bulk
  // bar would briefly count rows the filter had already removed.
  const [prunedFor, setPrunedFor] = useState(visible);
  if (prunedFor !== visible) {
    setPrunedFor(visible);
    setSelectedIds((prev) => {
      if (prev.size === 0) return prev;
      const vis = new Set(visible.map((f) => f.id));
      let changed = false;
      const n = new Set<string>();
      for (const id of prev) { if (vis.has(id)) n.add(id); else changed = true; }
      return changed ? n : prev;
    });
  }

  const toggleSelect = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });
  }, []);
  const clearSelection = useCallback(() => setSelectedIds(new Set()), []);
  const selectedFiles = useMemo(() => visible.filter((f) => selectedIds.has(f.id)), [visible, selectedIds]);

  const refreshAll = () => { refresh(); refreshUsage(); };

  const handleUpload = async (file: File | undefined | null) => {
    if (!file) return;
    setUploading(true);
    try {
      const created = await filesApi.upload(file);
      toast.success(t('Uploaded "{{name}}"', { name: created.filename }));
      refreshAll();
    } catch (err) {
      // A storage/file-count quota denial is surfaced by the central storage
      // modal (via the global 402/409 handler) — don't ALSO toast it here, or the
      // user sees the same limit twice. Other upload failures still toast.
      if (isStorageQuotaError(err)) refreshUsage();
      else toast.error(apiErrorMessage(err, t('Upload failed')));
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleDownload = async (f: StoredFile) => {
    try {
      await filesApi.download(f.id, f.filename);
    } catch (err) {
      toast.error(apiErrorMessage(err, t('Download failed')));
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await filesApi.delete(deleteTarget.id);
      toast.success(t('Deleted "{{name}}"', { name: deleteTarget.filename }));
      refreshAll();
    } catch (err) {
      toast.error(apiErrorMessage(err, t('Failed to delete file')));
    } finally {
      setDeleting(false);
      setDeleteTarget(null);
    }
  };

  const handleBulkDelete = async () => {
    setBulkBusy(true);
    let ok = 0; let fail = 0;
    for (const f of selectedFiles) {
      try { await filesApi.delete(f.id); ok += 1; } catch { fail += 1; }
    }
    setBulkBusy(false);
    setBulkDeleteOpen(false);
    clearSelection();
    refreshAll();
    if (fail === 0) toast.success(t('Deleted {{n}}', { n: ok }));
    else toast.error(t('{{ok}} deleted · {{fail}} failed', { ok, fail }));
  };

  const handleBulkDownload = async () => {
    for (const f of selectedFiles) {
      // Sequential so the browser doesn't drop concurrent download prompts.
      // eslint-disable-next-line no-await-in-loop
      await handleDownload(f);
    }
  };

  const copyId = (id: string) => {
    navigator.clipboard.writeText(id);
    toast.success(t('Copied {{id}} to clipboard', { id }), { duration: 2000 });
  };

  // Quota — bytes_limit/file_limit of -1 means UNLIMITED.
  const bytesUsed = usage?.bytes_used ?? 0;
  const bytesLimit = usage?.bytes_limit ?? -1;
  const bytesUnlimited = bytesLimit < 0;
  const bytesPct = bytesUnlimited || bytesLimit === 0 ? 0 : Math.min(100, (bytesUsed / bytesLimit) * 100);
  // At/over the byte ceiling (genuinely full) is distinct from merely "near"
  // (the 90–99% warn band) so the copy reads "reached" vs "close to".
  const bytesOver = !bytesUnlimited && bytesLimit >= 0 && bytesUsed >= bytesLimit;
  const bytesNearCap = !bytesUnlimited && !bytesOver && bytesPct >= 90;
  const fileCount = usage?.file_count ?? allFiles.length;
  const fileLimit = usage?.file_limit ?? -1;
  const filesOver = fileLimit >= 0 && fileCount >= fileLimit;
  // Proactive UX pre-check ONLY — the server stays the authority (fail-closed).
  const overQuota = bytesOver || filesOver;
  const uploadDisabled = uploading || overQuota;

  // The near/over-cap warning copy, as a line under the slim meter rather than
  // inside a card. Self-host quotas are configured locally, so the fix is
  // deleting files (or raising the configured limit) — no plan upsell.
  const quotaWarning = bytesOver
    ? t('Storage limit reached — free up space to upload more.')
    : bytesNearCap
      ? t('You are close to your storage limit. Delete files to free up space.')
      : filesOver
        ? t('File-count limit reached — delete files to upload more.')
        : null;

  const allSelected = visible.length > 0 && selectedIds.size >= visible.length;

  return (
    <>
      <div className="flex flex-col h-full">
        {/* Toolbar — plain inline header (admin idiom, no PageHeader portal) */}
        <div className={SHELF_TOPBAR}>
          <DocumentDuplicateIcon className="w-4 h-4 text-secondary shrink-0" />
          <span className="text-[13px] font-semibold text-ink shrink-0">{t('Files')}</span>
          {allFiles.length > 0 && (
            <span className="hidden @pair/stage:inline text-[11px] text-secondary ml-1">
              {allFiles.length === 1 ? t('{{n}} file', { n: allFiles.length }) : t('{{n}} files', { n: allFiles.length })}
            </span>
          )}
          <span className="hidden @rail/stage:inline text-[11px] text-secondary ml-1 truncate">
            {t('Stored files your workflows upload, capture, and reuse')}
          </span>
          <div className="flex-1" />

          <input ref={fileInputRef} type="file" className="hidden" onChange={(e) => handleUpload(e.target.files?.[0])} />
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={uploadDisabled}
            title={overQuota ? t('Storage full — free up space first') : undefined}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-accent-strong text-accent-on text-[12px] font-semibold rounded-lg shadow-sm hover:bg-accent-strong/90 transition-colors shrink-0 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <ArrowUpTrayIcon className="w-3.5 h-3.5" />
            {uploading ? t('Uploading…') : t('Upload')}
          </button>
        </div>

        {/* Body */}
        <div className="flex flex-col flex-1 min-h-0 bg-surface">
          {/* Slim storage meter — plan quota (over/near-cap warning preserved below it) */}
          <div className="shrink-0 border-b border-border px-4 py-2 text-[11px]">
            <div className="flex items-center gap-3">
              <span className="uppercase tracking-wide font-medium text-tertiary shrink-0">{t('Storage')}</span>
              {!bytesUnlimited && (
                <div className="h-1.5 w-40 max-w-[28vw] rounded-full bg-hover overflow-hidden shrink-0">
                  <div className={clsx('h-full rounded-full transition-all', bytesOver || bytesNearCap ? 'bg-danger' : 'bg-ink')} style={{ width: `${bytesPct}%` }} />
                </div>
              )}
              <span className="tabular-nums text-secondary truncate">
                {formatBytes(bytesUsed)} {bytesUnlimited ? t('used · unlimited') : t('of {{limit}}', { limit: formatBytes(bytesLimit) })}
                {' · '}
                {fileLimit < 0 ? t('{{n}} files', { n: fileCount }) : t('{{n}} / {{limit}} files', { n: fileCount, limit: fileLimit })}
              </span>
              {overQuota && <span className="text-danger font-medium shrink-0">{t('Full')}</span>}
              <div className="flex-1" />
              <Link to="/settings?tab=storage" className="inline-flex items-center gap-1 text-secondary hover:text-ink transition-colors shrink-0">
                <Cog6ToothIcon className="w-3.5 h-3.5" />
                <span className="hidden @pair/stage:inline">{t('Manage')}</span>
              </Link>
            </div>
            {quotaWarning && (
              <p className="text-danger mt-1">{quotaWarning}</p>
            )}
          </div>

          {/* Toolbar: source chips + sort + view — replaced by the bulk bar when a selection is active */}
          {selectedIds.size > 0 ? (
            <div className="shrink-0 flex items-center justify-between gap-2 border-b border-border bg-chrome px-3 py-2 bulkbar-enter">
              <div className="flex items-center gap-2 text-[12px]">
                <button
                  onClick={() => setSelectedIds((prev) => (prev.size >= visible.length ? new Set() : new Set(visible.map((f) => f.id))))}
                  className={clsx('flex h-4 w-4 items-center justify-center rounded border transition-colors', allSelected ? 'border-ink bg-ink text-surface' : 'border-border bg-surface')}
                  aria-label={t('Select all')}
                >
                  {allSelected && <CheckIcon className="h-3 w-3" />}
                </button>
                <span className="font-medium text-ink">{t('{{n}} selected', { n: selectedIds.size })}</span>
                <button onClick={clearSelection} className="text-tertiary hover:text-ink transition-colors">{t('Clear')}</button>
              </div>
              <div className="flex items-center gap-1">
                <button onClick={handleBulkDownload} disabled={bulkBusy} title={t('Download')}
                  className="p-1.5 rounded-lg text-secondary hover:bg-surface hover:text-ink disabled:opacity-50 transition-colors">
                  <ArrowDownTrayIcon className="h-3.5 w-3.5" />
                </button>
                <button onClick={() => setBulkDeleteOpen(true)} disabled={bulkBusy} title={t('Delete')}
                  className="p-1.5 rounded-lg text-danger hover:bg-danger-bg hover:text-danger-fg disabled:opacity-50 transition-colors">
                  <TrashIcon className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          ) : allFiles.length === 0 ? null : (
            <div className="shrink-0 flex items-center gap-2 border-b border-border px-3 py-2">
              <div className="flex items-center gap-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
                {SOURCE_CHIPS.map((c) => {
                  const active = source === c.id;
                  const count = c.id === '' ? allFiles.length : (sourceCounts[c.id] ?? 0);
                  if (c.id !== '' && count === 0 && !active) return null;
                  return (
                    <button
                      key={c.id || 'all'}
                      onClick={() => setSource(c.id)}
                      className={clsx(
                        'inline-flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] font-medium whitespace-nowrap transition-colors',
                        active ? 'bg-ink text-surface' : 'text-secondary hover:bg-chrome hover:text-ink',
                      )}
                    >
                      {t(c.label)}
                      <span className={clsx('tabular-nums', active ? 'text-surface/70' : 'text-tertiary')}>{count}</span>
                    </button>
                  );
                })}
              </div>
              <div className="flex-1" />
              {/* Search — lives with the list it filters, not in the page topbar. */}
              <div className="relative shrink-0">
                <MagnifyingGlassIcon aria-hidden="true" className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-tertiary" />
                <input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder={t('Search...')}
                  aria-label={t('Search files')}
                  className="w-40 pl-8 pr-3 py-1 text-[12px] bg-surface border border-border rounded-lg outline-none focus:ring-2 focus:ring-ink/10 transition-colors placeholder:text-tertiary"
                />
              </div>
              <Select<SortMode>
                size="sm"
                value={sortBy}
                onChange={setSortBy}
                aria-label={t('Sort by')}
                wrapperClassName="w-32"
                options={[
                  { value: 'recent', label: t('Recent') },
                  { value: 'name', label: t('Name A–Z') },
                  { value: 'size', label: t('Largest') },
                  { value: 'type', label: t('Type') },
                ]}
              />
              <ViewSwitch<ViewMode>
                value={viewMode}
                onChange={setViewMode}
                options={[
                  { id: 'list', label: t('List'), icon: ListBulletIcon },
                  { id: 'grid', label: t('Grid'), icon: Squares2X2Icon },
                ]}
              />
            </div>
          )}

          {/* Content */}
          {loading && !list ? (
            <div className="flex-1 min-h-0 overflow-hidden p-4">
              <RowsSkeleton rows={8} label={t('Loading files')} />
            </div>
          ) : visible.length === 0 ? (
            <div className="flex flex-1 flex-col items-center justify-center text-center px-6">
              <div className="w-12 h-12 rounded-2xl bg-hover border border-border flex items-center justify-center mb-4">
                <DocumentDuplicateIcon className="h-6 w-6 text-tertiary" />
              </div>
              <p className="text-sm font-medium text-ink">
                {search || source ? t('No files match your search') : t('No files yet')}
              </p>
              <p className="text-xs text-secondary mt-1 max-w-xs">
                {search || source
                  ? t('Try a different filter')
                  : t('Upload a file to use it in an upload step or pass it to a workflow run.')}
              </p>
              {!search && !source && (
                <button
                  onClick={() => fileInputRef.current?.click()}
                  disabled={uploadDisabled}
                  title={overQuota ? t('Storage full — free up space first') : undefined}
                  className="mt-4 px-4 py-2 bg-accent-strong text-accent-on text-sm font-semibold rounded-lg shadow-sm hover:bg-accent-strong/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {t('Upload your first file')}
                </button>
              )}
            </div>
          ) : (
            <ScrollArea className="flex-1" viewportClassName={viewMode === 'grid' ? 'p-3 sm:p-4' : 'px-2 py-2'}>
              {viewMode === 'grid' ? (
                <div className="grid grid-cols-2 @pair/stage:grid-cols-3 @split/stage:grid-cols-4 @wide/stage:grid-cols-5 gap-3">
                  {visible.map((f) => {
                    const checked = selectedIds.has(f.id);
                    return (
                      <div
                        key={f.id}
                        className={clsx(
                          'group relative rounded-xl border bg-surface overflow-hidden transition-colors',
                          checked ? 'border-ink ring-1 ring-ink/20' : 'border-border hover:border-ink/30',
                        )}
                      >
                        <button
                          onClick={() => (isPreviewable(f) ? setPreviewTarget(f) : handleDownload(f))}
                          className="block w-full text-left"
                        >
                          <FileThumb file={f} className="aspect-[4/3] w-full" iconClass="w-9 h-9 opacity-85" />
                        </button>
                        {/* select checkbox (hover / checked) */}
                        <button
                          onClick={(e) => { e.stopPropagation(); toggleSelect(f.id); }}
                          aria-label={t('Select file')}
                          aria-pressed={checked}
                          className={clsx(
                            'absolute top-2 left-2 flex h-5 w-5 items-center justify-center rounded-md border transition-opacity',
                            checked ? 'bg-ink border-ink text-surface opacity-100' : 'bg-surface/90 border-border opacity-0 group-hover:opacity-100',
                          )}
                        >
                          {checked && <CheckIcon className="h-3.5 w-3.5" />}
                        </button>
                        <div className="px-2.5 py-2 border-t border-border">
                          <div className="flex items-center gap-1.5">
                            <span className="text-[12px] font-medium text-ink truncate flex-1" title={f.filename}>{f.filename}</span>
                            <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
                              <button onClick={(e) => { e.stopPropagation(); copyId(f.id); }} title={t('Copy file id')} className="p-1 text-tertiary hover:text-ink rounded"><ClipboardDocumentIcon className="w-3.5 h-3.5" /></button>
                              <button onClick={(e) => { e.stopPropagation(); handleDownload(f); }} title={t('Download')} className="p-1 text-tertiary hover:text-ink rounded"><ArrowDownTrayIcon className="w-3.5 h-3.5" /></button>
                              <button onClick={(e) => { e.stopPropagation(); setDeleteTarget(f); }} title={t('Delete')} className="p-1 text-tertiary hover:text-danger rounded"><TrashIcon className="w-3.5 h-3.5" /></button>
                            </div>
                          </div>
                          <p className="text-[11px] text-tertiary tabular-nums truncate mt-0.5">
                            {formatBytes(f.bytes)}
                            {f.status !== 'ready' && ` · ${f.status === 'error' ? t('Error') : t('Processing')}`}
                          </p>
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="grid gap-0.5">
                  {visible.map((f) => {
                    const Icon = fileTypeIcon(f.content_type, f.filename);
                    const checked = selectedIds.has(f.id);
                    return (
                      <div
                        key={f.id}
                        className={clsx('group relative flex items-center gap-3 pl-3 pr-2 py-2 rounded-lg transition-colors', checked ? 'bg-hover' : 'hover:bg-canvas')}
                      >
                        {/* leading tile ⇄ checkbox */}
                        <button
                          type="button"
                          onClick={() => toggleSelect(f.id)}
                          aria-label={t('Select file')}
                          aria-pressed={checked}
                          className="relative w-8 h-8 shrink-0 rounded-lg outline-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/20"
                        >
                          <span className={clsx('absolute inset-0 flex items-center justify-center rounded-lg border transition-opacity', checked ? 'bg-ink border-ink text-surface opacity-100' : 'bg-surface border-border opacity-0 group-hover:opacity-100')}>
                            <CheckIcon className={clsx('h-3.5 w-3.5 transition-opacity', checked ? 'opacity-100' : 'opacity-0')} />
                          </span>
                          <span
                            style={tintStyle(f.content_type ? seedTint(f.content_type.split('/')[0]) : 'neutral')}
                            className={clsx('absolute inset-0 flex items-center justify-center rounded-lg transition-opacity', checked ? 'opacity-0' : 'opacity-100 group-hover:opacity-0')}
                          >
                            <Icon className="w-4 h-4" />
                          </span>
                        </button>

                        <button onClick={() => (isPreviewable(f) ? setPreviewTarget(f) : handleDownload(f))} className="flex-1 min-w-0 text-left">
                          <div className="flex items-center gap-2">
                            <span className="text-[13px] font-medium text-ink truncate">{f.filename}</span>
                            <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-hover text-secondary font-medium shrink-0">
                              {SOURCE_LABELS[f.source] || f.source}
                            </span>
                            {f.status !== 'ready' && (
                              <span className={clsx('text-[10px] px-1.5 py-0.5 rounded-full font-medium shrink-0', statusStyle(f.status === 'error' ? 'failed' : 'pending').pill)}>
                                {f.status === 'error' ? t('Error') : t('Processing')}
                              </span>
                            )}
                          </div>
                          <p className="text-[11px] text-tertiary mt-0.5 truncate tabular-nums">
                            {formatBytes(f.bytes)} · {f.content_type}
                            {f.created_at ? ` · ${formatRelativeTime(new Date(f.created_at * 1000))}` : ''}
                          </p>
                        </button>

                        <div className="flex items-center gap-0.5 shrink-0">
                          <button onClick={() => copyId(f.id)} title={t('Copy file id')} className="p-1.5 text-tertiary hover:text-ink hover:bg-chrome rounded-lg transition-colors opacity-0 group-hover:opacity-100">
                            <ClipboardDocumentIcon className="w-3.5 h-3.5" />
                          </button>
                          {isPreviewable(f) && (
                            <button onClick={() => setPreviewTarget(f)} title={t('Preview')} className="p-1.5 text-tertiary hover:text-ink hover:bg-chrome rounded-lg transition-colors opacity-0 group-hover:opacity-100">
                              <EyeIcon className="w-3.5 h-3.5" />
                            </button>
                          )}
                          <button onClick={() => handleDownload(f)} title={t('Download')} className="p-1.5 text-tertiary hover:text-ink hover:bg-chrome rounded-lg transition-colors">
                            <ArrowDownTrayIcon className="w-3.5 h-3.5" />
                          </button>
                          <button onClick={() => setDeleteTarget(f)} title={t('Delete')} className="p-1.5 text-tertiary hover:text-danger hover:bg-chrome rounded-lg transition-colors">
                            <TrashIcon className="w-3.5 h-3.5" />
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </ScrollArea>
          )}
        </div>
      </div>

      {/* Preview modal */}
      <FilePreviewModal target={previewTarget} onClose={() => setPreviewTarget(null)} />

      {/* Delete confirmation */}
      <Modal isOpen={!!deleteTarget} onClose={() => setDeleteTarget(null)} title={t('Delete file')}>
        <p className="text-sm text-secondary mb-1">
          {t('Are you sure you want to delete')}{' '}
          <strong className="text-ink">{deleteTarget?.filename}</strong>?
        </p>
        <p className="text-xs text-tertiary mb-4">
          {t('Any upload step or workflow that links this file by id will fail until you link a different file.')}
        </p>
        <div className="flex items-center justify-end gap-2">
          <button onClick={() => setDeleteTarget(null)} className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg hover:bg-chrome transition-colors">
            {t('Cancel')}
          </button>
          <button onClick={handleDelete} disabled={deleting} className="px-4 py-2 text-sm font-medium bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 transition-colors disabled:opacity-50">
            {deleting ? t('Deleting...') : t('Delete')}
          </button>
        </div>
      </Modal>

      {/* Bulk delete confirmation */}
      <Modal isOpen={bulkDeleteOpen} onClose={() => (bulkBusy ? null : setBulkDeleteOpen(false))} title={t('Delete files')}>
        <p className="text-sm text-secondary mb-1">{t('Delete {{n}} file(s)? This cannot be undone.', { n: selectedFiles.length })}</p>
        <p className="text-xs text-tertiary mb-4">{t('Any upload step or workflow that links these files by id will fail until you link a different file.')}</p>
        <div className="flex items-center justify-end gap-2">
          <button onClick={() => setBulkDeleteOpen(false)} disabled={bulkBusy} className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg hover:bg-chrome transition-colors disabled:opacity-50">
            {t('Cancel')}
          </button>
          <button onClick={handleBulkDelete} disabled={bulkBusy || selectedFiles.length === 0} className="px-4 py-2 text-sm font-medium bg-danger text-danger-fg rounded-lg hover:opacity-90 transition-opacity disabled:opacity-50">
            {bulkBusy ? t('Deleting...') : t('Delete {{n}}', { n: selectedFiles.length })}
          </button>
        </div>
      </Modal>
    </>
  );
};

/**
 * Inline preview of an image or PDF. Bytes are fetched as a Blob through the
 * authenticated client (filesApi.fetchBlob → axios attaches the token + follows
 * the 302 to the presigned GET), then rendered from an object URL that is revoked
 * on unmount/close so we never leak blob URLs or stale auth.
 *
 * SECURITY — why this is an allowlist and not a fallback.
 * A `blob:` URL inherits the ORIGIN OF THE DOCUMENT THAT CREATED IT, so anything
 * rendered from one here is same-origin with the app: it can read `window.parent`,
 * reach `localStorage`, and POST `/api/auth/refresh` with credentials to mint a
 * live access token. The blob's type comes from the stored `content_type`, which
 * is recorded verbatim from whoever uploaded the file (an API-key client, an MCP
 * client, or a workflow saving a downloaded artifact) — so treating it as
 * trustworthy would let a stored `text/html` file execute script in this origin
 * the moment an operator clicked "preview".
 *
 * Two independent guards, either of which alone would close it:
 *   1. Only PREVIEWABLE_TYPES are ever framed; everything else offers a download.
 *   2. The blob is re-typed to the allowlisted value, and the frame is
 *      `sandbox`ed into an opaque origin with scripting off.
 */

/** Content types that may be rendered inline. Nothing else is ever framed. */
const PREVIEWABLE_TYPES = new Set(['application/pdf', 'text/plain']);

const baseContentType = (raw?: string | null): string =>
  (raw || '').split(';')[0].trim().toLowerCase();

/**
 * The MIME type this file may be rendered as, or null if it must not be rendered.
 *
 * Images are safe to pass through: an `<img>` never executes script, not even for
 * SVG, so a mislabelled file simply fails to draw. Everything else must be on the
 * frame allowlist. Anything unrecognised returns null and is offered as a download.
 */
const renderableType = (file?: { content_type?: string | null } | null): string | null => {
  const type = baseContentType(file?.content_type);
  if (type.startsWith('image/')) return type;
  if (PREVIEWABLE_TYPES.has(type)) return type;
  return null;
};
const FilePreviewModal: React.FC<{ target: StoredFile | null; onClose: () => void }> = ({ target, onClose }) => {
  const { t } = useTranslation();
  const [url, setUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);

  // Clear the previous preview the moment a new target arrives. Done in render
  // rather than at the top of the effect below: a setState in an effect body
  // lands only after paint (`react-hooks/set-state-in-effect`), so the modal
  // would show the OLD file's image for a frame under the new filename.
  const [shownTarget, setShownTarget] = useState<StoredFile | null>(null);
  if (shownTarget !== target) {
    setShownTarget(target);
    setUrl(null);
    setError(false);
    setLoading(!!target);
  }

  React.useEffect(() => {
    let revoked = false;
    let objectUrl: string | null = null;
    if (!target) return;
    filesApi
      .fetchBlob(target.id)
      .then((blob) => {
        if (revoked) return;
        // Re-type the blob to the allowlisted value rather than trusting the
        // stored content_type. Without this the object URL would carry whatever
        // MIME the uploader supplied, and `text/html` would render as a
        // same-origin document.
        const safeType = renderableType(target);
        objectUrl = URL.createObjectURL(
          safeType ? new Blob([blob], { type: safeType }) : blob
        );
        setUrl(objectUrl);
      })
      .catch(() => { if (!revoked) setError(true); })
      .finally(() => { if (!revoked) setLoading(false); });
    return () => {
      revoked = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [target]);

  const type = baseContentType(target?.content_type);
  const isImage = type.startsWith('image/');
  const isFramable = PREVIEWABLE_TYPES.has(type);

  return (
    <Modal isOpen={!!target} onClose={onClose} title={target?.filename} size="lg">
      <div className="flex items-center justify-center min-h-[200px]">
        {loading ? (
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-ink" />
        ) : error || !url ? (
          <p className="text-sm text-tertiary">{t('Could not load preview.')}</p>
        ) : isImage ? (
          <img src={url} alt={target?.filename} className="max-w-full max-h-[70vh] object-contain rounded-lg" onError={() => setError(true)} />
        ) : isFramable ? (
          // sandbox="" puts the frame in an opaque origin with scripting off, so
          // even a mis-typed file cannot reach this document. Do not add
          // allow-scripts and allow-same-origin together — that combination lets
          // the frame remove its own sandbox.
          <iframe
            src={url}
            sandbox=""
            referrerPolicy="no-referrer"
            title={target?.filename}
            className="w-full h-[70vh] rounded-lg border border-border"
          />
        ) : (
          <div className="text-center py-8">
            <p className="text-sm text-tertiary">
              {t('No inline preview for this file type.')}
            </p>
            <button
              onClick={() => target && filesApi.download(target.id, target.filename)}
              className="mt-3 px-4 py-2 text-sm font-medium bg-ink text-paper rounded-lg hover:opacity-90 transition-opacity"
            >
              {t('Download')}
            </button>
          </div>
        )}
      </div>
    </Modal>
  );
};

export default Files;
