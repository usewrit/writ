import React, { useId, useState, useMemo, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useRequireAuth } from '../hooks/useAuth';
import { useQuery } from '../hooks/useQuery';
import { Modal } from '../components/ui/Modal';
import { Select, ScrollArea } from '../components/ui';
import { vaultApi } from '../api/endpoints';
import { seedTint, tintStyle } from '../utils/tint';
import {
  SHELF_CONTAINER,
  SHELF_LIST_COL,
  SHELF_DETAIL_COL,
  shelfRowClass,
  shelfRowMouseDown,
  ShelfAccentBar,
  shelfFilterChipClass,
  shelfFilterCountClass,
  SHELF_TOPBAR,
  SHELF_SEARCH_INPUT,
  ShelfSkeleton,
} from '../components/library/shelf';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import {
  PlusIcon,
  MagnifyingGlassIcon,
  LockClosedIcon,
  KeyIcon,
  TrashIcon,
  PencilSquareIcon,
  ClipboardDocumentIcon,
  CheckIcon,
  UserCircleIcon,
  TagIcon,
  CpuChipIcon,
} from '@heroicons/react/24/outline';
import { SendToAgentModal } from '../components/fleet/SendToAgentModal';

const CAT_CHIPS: { id: string; label: string }[] = [
  { id: '', label: 'All' },
  { id: 'credentials', label: 'Credentials' },
  { id: 'api_keys', label: 'API Keys' },
  { id: 'tokens', label: 'Tokens' },
  { id: 'other', label: 'Other' },
];
const catOf = (s: any): string => (s.is_credential ? 'credentials' : (s.category || 'other'));
const catLabel = (id: string) => CAT_CHIPS.find((c) => c.id === id)?.label || id;

type SortMode = 'name' | 'uses' | 'category';
function sortSecrets(list: any[], mode: SortMode): any[] {
  const arr = [...list];
  switch (mode) {
    case 'uses': return arr.sort((a, b) => (b.use_count || 0) - (a.use_count || 0));
    case 'category': return arr.sort((a, b) => catOf(a).localeCompare(catOf(b)) || a.name.localeCompare(b.name));
    case 'name':
    default:
      return arr.sort((a, b) => a.name.localeCompare(b.name));
  }
}

/** lg breakpoint (1024px) — the two-pane only renders this wide. */
function useIsWide(): boolean {
  const [wide, setWide] = useState(() => typeof window !== 'undefined' ? window.matchMedia('(min-width: 1024px)').matches : true);
  useEffect(() => {
    const mq = window.matchMedia('(min-width: 1024px)');
    const onChange = (e: MediaQueryListEvent) => setWide(e.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);
  return wide;
}

// ── Row ──
interface RowProps {
  s: any;
  selected: boolean;
  checked: boolean;
  onSelect: (s: any) => void;
  onToggleCheck: () => void;
  onCopy: (name: string) => void;
}
const SecretRow: React.FC<RowProps> = React.memo(({ s, selected, checked, onSelect, onToggleCheck, onCopy }) => {
  const { t } = useTranslation();
  const Icon = s.is_credential ? UserCircleIcon : LockClosedIcon;
  return (
    <div
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      aria-label={s.name}
      onClick={() => onSelect(s)}
      onMouseDown={shelfRowMouseDown}
      onKeyDown={(e) => { if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) { e.preventDefault(); onSelect(s); } }}
      className={shelfRowClass(selected)}
    >
      {selected && <ShelfAccentBar />}
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); onToggleCheck(); }}
        aria-label={t('Select secret')}
        aria-pressed={checked}
        className="relative w-8 h-8 shrink-0 rounded-lg outline-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/20"
      >
        <span className={clsx('absolute inset-0 flex items-center justify-center rounded-lg border transition-opacity', checked ? 'bg-ink border-ink text-surface opacity-100' : 'bg-surface border-border opacity-0 group-hover:opacity-100')}>
          <CheckIcon className={clsx('h-3.5 w-3.5 transition-opacity', checked ? 'opacity-100' : 'opacity-0')} />
        </span>
        <span style={tintStyle(seedTint(catOf(s)))} className={clsx('absolute inset-0 flex items-center justify-center rounded-lg transition-opacity', checked ? 'opacity-0' : 'opacity-100 group-hover:opacity-0')}>
          <Icon className="h-4 w-4" />
        </span>
      </button>
      <div className="flex-1 min-w-0">
        <div className="text-[13px] font-medium text-ink font-mono overflow-hidden" style={{ containerType: 'inline-size' }} title={s.name}>
          <span className="inline-block whitespace-nowrap min-w-full group-hover:[animation:marquee-hover_8s_ease-in-out_infinite]">
            {s.name}
          </span>
        </div>
        <div className="flex items-center gap-1 text-[11px] truncate mt-0.5">
          <span className="text-tertiary shrink-0">{s.is_credential ? t('Login') : catLabel(catOf(s))}</span>
          {s.is_credential && s.username && <><span className="text-tertiary/50 shrink-0">·</span><span className="text-tertiary truncate">{s.username}</span></>}
          {!s.is_credential && s.description && <><span className="text-tertiary/50 shrink-0">·</span><span className="text-tertiary/80 truncate">{s.description}</span></>}
        </div>
      </div>
      <button
        onClick={(e) => { e.stopPropagation(); onCopy(s.name); }}
        title={t('Copy reference')}
        aria-label={t('Copy reference')}
        className="flex items-center justify-center w-7 h-7 rounded-lg text-tertiary hover:bg-chrome hover:text-ink transition-colors shrink-0"
      >
        <ClipboardDocumentIcon className="h-4 w-4" />
      </button>
    </div>
  );
});
SecretRow.displayName = 'SecretRow';

// ── Detail pane ──
const RefBlock: React.FC<{ label: string; value: string }> = ({ label, value }) => {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const copy = () => { navigator.clipboard.writeText(value); setCopied(true); toast.success(t('Copied {{ref}} to clipboard', { ref: value }), { duration: 2000 }); setTimeout(() => setCopied(false), 1500); };
  return (
    <button onClick={copy} className="w-full flex items-center gap-2 rounded-lg border border-border bg-canvas px-3 py-2 text-left hover:border-ink/30 transition-colors group/ref">
      <span className="text-[10px] text-tertiary shrink-0 w-16">{label}</span>
      <code className="flex-1 text-[12px] font-mono text-ink truncate">{value}</code>
      {copied ? <CheckIcon className="h-3.5 w-3.5 text-success shrink-0" /> : <ClipboardDocumentIcon className="h-3.5 w-3.5 text-tertiary group-hover/ref:text-ink shrink-0" />}
    </button>
  );
};
const Fact: React.FC<{ icon: React.FC<any>; label: string; children: React.ReactNode }> = ({ icon: Icon, label, children }) => (
  <div className="flex items-center gap-2 min-w-0">
    <Icon className="h-3.5 w-3.5 text-tertiary shrink-0" />
    <span className="text-[11px] text-tertiary shrink-0">{label}</span>
    <span className="ml-auto text-[12px] text-ink text-right truncate min-w-0">{children}</span>
  </div>
);

const SecretDetailPane: React.FC<{ s: any | null; onEdit: (s: any) => void; onDelete: (s: any) => void; onSend: (s: any) => void }> = ({ s, onEdit, onDelete, onSend }) => {
  const { t } = useTranslation();
  if (!s) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center text-center px-6">
        <div className="w-12 h-12 rounded-2xl bg-hover border border-border flex items-center justify-center mb-4"><LockClosedIcon className="h-6 w-6 text-tertiary" /></div>
        <p className="text-sm font-medium text-ink">{t('Select a secret')}</p>
        <p className="text-xs text-secondary mt-1 max-w-xs">{t('Pick one on the left to copy its reference for use in a workflow.')}</p>
      </div>
    );
  }
  const Icon = s.is_credential ? UserCircleIcon : LockClosedIcon;
  return (
    <div className="flex flex-1 flex-col min-h-0">
      <div className="px-5 pt-5 pb-4 border-b border-border">
        <div className="flex items-start gap-3">
          <div style={tintStyle(seedTint(catOf(s)))} className="w-10 h-10 rounded-lg flex items-center justify-center shrink-0"><Icon className="h-5 w-5" /></div>
          <div className="flex-1 min-w-0">
            <div className="text-[15px] font-semibold text-ink font-mono truncate" title={s.name}>{s.name}</div>
            <div className="flex items-center gap-2 mt-1 text-[11px] text-tertiary">
              <span>{s.is_credential ? t('Login credential') : catLabel(catOf(s))}</span>
              {(s.use_count ?? 0) > 0 && <><span className="text-tertiary/50">·</span><span>{t('{{n}} uses', { n: s.use_count })}</span></>}
            </div>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            <button onClick={() => onSend(s)} className="flex items-center gap-1.5 px-2.5 py-1.5 text-[12px] font-medium text-secondary rounded-lg hover:bg-chrome hover:text-ink transition-colors">
              <CpuChipIcon className="h-3.5 w-3.5" />{t('Send to agent')}
            </button>
            <button onClick={() => onEdit(s)} className="flex items-center gap-1.5 px-2.5 py-1.5 text-[12px] font-medium text-secondary rounded-lg hover:bg-chrome hover:text-ink transition-colors">
              <PencilSquareIcon className="h-3.5 w-3.5" />{t('Edit')}
            </button>
            <button onClick={() => onDelete(s)} className="flex items-center gap-1.5 px-2.5 py-1.5 text-[12px] font-medium text-danger rounded-lg hover:bg-danger-bg hover:text-danger-fg transition-colors">
              <TrashIcon className="h-3.5 w-3.5" />{t('Delete')}
            </button>
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden px-5 py-4 space-y-6">
        <div>
          <div className="mb-2"><span className="text-[10px] font-semibold uppercase tracking-wider text-tertiary">{t('Reference in workflows')}</span></div>
          <div className="space-y-1.5">
            {s.is_credential ? (
              <>
                <RefBlock label={t('Username')} value={`{{vault:${s.name}.username}}`} />
                <RefBlock label={t('Password')} value={`{{vault:${s.name}.password}}`} />
              </>
            ) : (
              <RefBlock label={t('Value')} value={`{{vault:${s.name}}}`} />
            )}
          </div>
        </div>

        <div>
          <div className="mb-2"><span className="text-[10px] font-semibold uppercase tracking-wider text-tertiary">{t('Details')}</span></div>
          <div className="space-y-2 rounded-xl border border-ink/20 shadow-sm bg-surface p-3">
            <Fact icon={TagIcon} label={t('Category')}>{s.is_credential ? t('Credentials') : catLabel(catOf(s))}</Fact>
            {s.is_credential && s.username && <Fact icon={UserCircleIcon} label={t('Username')}>{s.username}</Fact>}
            {s.is_credential && <Fact icon={KeyIcon} label={t('Password')}>••••••••</Fact>}
            {s.description && <Fact icon={TagIcon} label={t('Description')}>{s.description}</Fact>}
            <Fact icon={LockClosedIcon} label={t('Uses')}>{s.use_count ?? 0}</Fact>
          </div>
        </div>

        <p className="text-[11px] leading-relaxed text-tertiary">
          {t('Stored encrypted and never returned in plaintext. Reference it in a workflow with the token above; the daemon substitutes the value at run time.')}
        </p>
      </div>
    </div>
  );
};

// Secrets live in the built-in vault; there are no external secret-store
// providers (AWS/Vault/Azure/GCP) in this build.

export const SecretsPage: React.FC = () => {
  const { t } = useTranslation();
  useRequireAuth();
  const isWide = useIsWide();
  const [search, setSearch] = useState('');
  const [category, setCategory] = useState('');
  const [sortBy, setSortBy] = useState<SortMode>('name');
  const [showCreate, setShowCreate] = useState(false);
  const [editTarget, setEditTarget] = useState<any>(null);
  const [deleteTarget, setDeleteTarget] = useState<any>(null);
  const [sendTarget, setSendTarget] = useState<any>(null);
  const [deleting, setDeleting] = useState(false);
  const [pickedId, setSelectedId] = useState<number | null>(null);
  const [rawCheckedIds, setCheckedIds] = useState<Set<number>>(new Set());
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);

  const { data: secrets, loading, refresh } = useQuery('vault:secrets', () => vaultApi.listSecrets({}), { pollInterval: 30000 });
  const all = useMemo(() => secrets || [], [secrets]);

  const catCounts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const s of all) m[catOf(s)] = (m[catOf(s)] || 0) + 1;
    return m;
  }, [all]);

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase();
    const filtered = all
      .filter((s: any) => !category || catOf(s) === category)
      .filter((s: any) => !q || s.name.toLowerCase().includes(q) || (s.description || '').toLowerCase().includes(q) || (s.username || '').toLowerCase().includes(q));
    return sortSecrets(filtered, sortBy);
  }, [all, category, search, sortBy]);

  const visibleIds = useMemo(() => new Set(visible.map((s: any) => s.id)), [visible]);

  // Detail selection is DERIVED, not stored: the user's pick while that secret
  // is still listed, otherwise the first row. An effect would paint one frame
  // against a secret the current filter no longer shows.
  const selectedId = useMemo(() => {
    if (!isWide) return pickedId;
    if (visible.length === 0) return null;
    return pickedId != null && visibleIds.has(pickedId) ? pickedId : visible[0].id;
  }, [pickedId, visible, visibleIds, isWide]);

  // Bulk checks pruned ON READ — a secret leaving the list (filter, search,
  // delete) drops out of the effective selection with no state write.
  const checkedIds = useMemo(
    () => new Set([...rawCheckedIds].filter((id) => visibleIds.has(id))),
    [rawCheckedIds, visibleIds],
  );

  const toggleCheck = useCallback((id: number) => setCheckedIds((prev) => { const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n; }), []);
  const clearChecks = useCallback(() => setCheckedIds(new Set()), []);
  const checkedSecrets = useMemo(() => visible.filter((s: any) => checkedIds.has(s.id)), [visible, checkedIds]);

  const copyRef = (name: string) => {
    navigator.clipboard.writeText(`{{vault:${name}}}`);
    toast.success(t('Copied {{ref}} to clipboard', { ref: `{{vault:${name}}}` }), { duration: 2000 });
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await vaultApi.deleteSecret(deleteTarget.id);
      toast.success(t('Secret "{{name}}" deleted', { name: deleteTarget.name }));
      refresh();
    } catch { toast.error(t('Failed to delete secret')); }
    finally { setDeleting(false); setDeleteTarget(null); }
  };

  const handleBulkDelete = async () => {
    setBulkBusy(true);
    let ok = 0; let fail = 0;
    for (const s of checkedSecrets) { try { await vaultApi.deleteSecret(s.id); ok += 1; } catch { fail += 1; } }
    setBulkBusy(false);
    setBulkDeleteOpen(false);
    clearChecks();
    refresh();
    if (fail === 0) toast.success(t('Deleted {{n}}', { n: ok }));
    else toast.error(t('{{ok}} deleted · {{fail}} failed', { ok, fail }));
  };

  const selected = visible.find((s: any) => s.id === selectedId) || null;
  const allChecked = visible.length > 0 && checkedIds.size >= visible.length;

  return (
    <>
      <div className="flex flex-col h-full">
        {/* Inline toolbar (admin idiom) — chrome topbar tone, carded controls. */}
        <div className={SHELF_TOPBAR}>
          <LockClosedIcon className="w-4 h-4 text-secondary shrink-0" />
          <span className="text-[13px] font-semibold text-ink shrink-0">{t('Secrets')}</span>
          {all.length > 0 && <span className="hidden @pair/stage:inline text-[11px] text-secondary ml-1">{all.length === 1 ? t('{{n}} secret', { n: all.length }) : t('{{n}} secrets', { n: all.length })}</span>}
          <span className="hidden @rail/stage:inline text-[11px] text-secondary ml-1 truncate">{t('Encrypted credentials your workflows reference at run time')}</span>
          <div className="flex-1" />
          <div className="relative hidden @pair/stage:block">
            <MagnifyingGlassIcon aria-hidden="true" className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-tertiary" />
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder={t('Search...')} aria-label={t('Search secrets')}
              className={clsx('w-44', SHELF_SEARCH_INPUT)} />
          </div>
          <button onClick={() => setShowCreate(true)} className="flex items-center gap-1.5 px-3 py-1.5 bg-accent-strong text-accent-on text-[12px] font-semibold rounded-lg shadow-sm hover:bg-accent-strong/90 transition-colors shrink-0">
            <PlusIcon className="w-3.5 h-3.5" />{t('New')}
          </button>
        </div>

        <div className="flex flex-1 overflow-hidden">
          {loading && !secrets ? (
            <ShelfSkeleton label={t('Loading secrets')} />
          ) : all.length === 0 ? (
            <div className="flex flex-1 flex-col bg-surface">
              <div className="flex flex-1 flex-col items-center justify-center text-center px-6">
                <div className="w-12 h-12 rounded-2xl bg-hover border border-border flex items-center justify-center mb-4"><LockClosedIcon className="h-6 w-6 text-tertiary" /></div>
                <p className="text-sm font-medium text-ink">{t('No secrets yet')}</p>
                <p className="text-xs text-secondary mt-1 max-w-sm">{t('Create a secret to securely store credentials your workflows reference with {{vault:name}}.')}</p>
                <button onClick={() => setShowCreate(true)} className="mt-4 px-4 py-2 bg-accent-strong text-accent-on text-sm font-semibold rounded-lg shadow-sm hover:bg-accent-strong/90 transition-colors">{t('Create your first secret')}</button>
              </div>
            </div>
          ) : (
            <div className={SHELF_CONTAINER}>
              {/* ── Master list ── */}
              <div className={SHELF_LIST_COL}>
                <div className="shrink-0 border-b border-border px-3 py-2.5 space-y-2">
                  <div className="flex flex-wrap items-center gap-1">
                    {CAT_CHIPS.map((c) => {
                      const active = category === c.id;
                      const count = c.id === '' ? all.length : (catCounts[c.id] ?? 0);
                      if (c.id !== '' && count === 0 && !active) return null;
                      return (
                        <button key={c.id || 'all'} onClick={() => setCategory(c.id)} aria-pressed={active}
                          className={shelfFilterChipClass(active)}>
                          {t(c.label)}
                          <span className={shelfFilterCountClass(active)}>{count}</span>
                        </button>
                      );
                    })}
                  </div>
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] text-tertiary">{t('{{n}} shown', { n: visible.length })}</span>
                    <Select<SortMode> size="sm" value={sortBy} onChange={setSortBy} aria-label={t('Sort by')} wrapperClassName="w-32"
                      options={[{ value: 'name', label: t('Name A–Z') }, { value: 'uses', label: t('Most used') }, { value: 'category', label: t('Category') }]} />
                  </div>
                </div>

                {checkedIds.size > 0 && (
                  <div className="shrink-0 flex items-center justify-between gap-2 border-b border-border bg-chrome px-3 py-2 bulkbar-enter">
                    <div className="flex items-center gap-2 text-[12px]">
                      <button onClick={() => setCheckedIds(allChecked ? new Set() : new Set(visible.map((s: any) => s.id)))}
                        className={clsx('flex h-4 w-4 items-center justify-center rounded border transition-colors', allChecked ? 'border-ink bg-ink text-surface' : 'border-border bg-surface')} aria-label={t('Select all')}>
                        {allChecked && <CheckIcon className="h-3 w-3" />}
                      </button>
                      <span className="font-medium text-ink">{t('{{n}} selected', { n: checkedIds.size })}</span>
                      <button onClick={clearChecks} className="text-tertiary hover:text-ink transition-colors">{t('Clear')}</button>
                    </div>
                    <button onClick={() => setBulkDeleteOpen(true)} disabled={bulkBusy} title={t('Delete')} className="p-1.5 rounded-lg text-danger hover:bg-danger-bg hover:text-danger-fg disabled:opacity-50 transition-colors"><TrashIcon className="h-3.5 w-3.5" /></button>
                  </div>
                )}

                {visible.length === 0 ? (
                  <div className="flex flex-1 flex-col items-center justify-center text-center px-6 py-10">
                    <p className="text-[13px] font-medium text-ink">{t('Nothing here')}</p>
                    <p className="text-[11px] text-secondary mt-1">{t('No secrets match this filter.')}</p>
                  </div>
                ) : (
                  <ScrollArea className="flex-1" viewportClassName="px-2 py-2" fade="chrome">
                    <div className="grid gap-0.5">
                      {visible.map((s: any) => (
                        <SecretRow
                          key={s.id}
                          s={s}
                          selected={isWide && s.id === selectedId}
                          checked={checkedIds.has(s.id)}
                          onSelect={(x) => setSelectedId(x.id)}
                          onToggleCheck={() => toggleCheck(s.id)}
                          onCopy={copyRef}
                        />
                      ))}
                    </div>
                  </ScrollArea>
                )}
              </div>

              {/* ── Detail pane ── */}
              <div className={SHELF_DETAIL_COL}>
                <SecretDetailPane s={selected} onEdit={setEditTarget} onDelete={setDeleteTarget} onSend={setSendTarget} />
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Create / Edit Modal */}
      <SecretFormModal
        isOpen={showCreate || !!editTarget}
        onClose={() => { setShowCreate(false); setEditTarget(null); }}
        onSave={refresh}
        editSecret={editTarget}
      />

      {/* Send to a fleet agent */}
      <SendToAgentModal
        open={!!sendTarget}
        onClose={() => setSendTarget(null)}
        kind="secret"
        entityId={sendTarget?.id}
        entityName={sendTarget?.name ?? ''}
      />

      {/* Delete Confirmation */}
      <Modal isOpen={!!deleteTarget} onClose={() => setDeleteTarget(null)} title={t('Delete Secret')}>
        <p className="text-sm text-secondary mb-1">
          {t('Are you sure you want to delete')} <strong className="text-ink font-mono">{deleteTarget?.name}</strong>?
        </p>
        <p className="text-xs text-tertiary mb-4">
          {t('Any workflows referencing {{ref}} will fail until a new secret with this name is created.', { ref: `{{vault:${deleteTarget?.name}}}` })}
        </p>
        <div className="flex items-center justify-end gap-2">
          <button onClick={() => setDeleteTarget(null)} className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg hover:bg-chrome transition-colors">{t('Cancel')}</button>
          <button onClick={handleDelete} disabled={deleting} className="px-4 py-2 text-sm font-medium bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 transition-colors disabled:opacity-50">{deleting ? t('Deleting...') : t('Delete')}</button>
        </div>
      </Modal>

      {/* Bulk delete */}
      <Modal isOpen={bulkDeleteOpen} onClose={() => (bulkBusy ? null : setBulkDeleteOpen(false))} title={t('Delete secrets')}>
        <p className="text-sm text-secondary mb-1">{t('Delete {{n}} secret(s)? This cannot be undone.', { n: checkedSecrets.length })}</p>
        <p className="text-xs text-tertiary mb-4">{t('Any workflows referencing them will fail until recreated.')}</p>
        <div className="flex items-center justify-end gap-2">
          <button onClick={() => setBulkDeleteOpen(false)} disabled={bulkBusy} className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg hover:bg-chrome transition-colors disabled:opacity-50">{t('Cancel')}</button>
          <button onClick={handleBulkDelete} disabled={bulkBusy || checkedSecrets.length === 0} className="px-4 py-2 text-sm font-medium bg-danger text-danger-fg rounded-lg hover:opacity-90 transition-colors disabled:opacity-50">{bulkBusy ? t('Deleting...') : t('Delete {{n}}', { n: checkedSecrets.length })}</button>
        </div>
      </Modal>
    </>
  );
};

// Secret create / edit form modal — admin supports in-place update (updateSecret)
// keyed by numeric id; the name is immutable so it's disabled on edit.
const SecretFormModal: React.FC<{ isOpen: boolean; onClose: () => void; onSave: () => void; editSecret?: any }> = ({ isOpen, onClose, onSave, editSecret }) => {
  const { t } = useTranslation();
  const fieldId = useId();
  type Kind = 'credential' | 'value';
  const [kind, setKind] = useState<Kind>('credential');
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [description, setDescription] = useState('');
  const [category, setCategory] = useState('api_keys');
  const [saving, setSaving] = useState(false);

  // Load the form from the secret being edited (or clear it for a new one) when
  // the modal opens or the target changes. Adjusted during render — the
  // documented reset pattern — so the fields are already right on the first
  // painted frame instead of flashing the previous secret's values.
  const [prevSecret, setPrevSecret] = useState<any>(editSecret);
  const [prevOpen, setPrevOpen] = useState(isOpen);
  if (prevSecret !== editSecret || prevOpen !== isOpen) {
    setPrevSecret(editSecret);
    setPrevOpen(isOpen);
    if (editSecret) {
      setKind(editSecret.is_credential ? 'credential' : 'value');
      setName(editSecret.name);
      setValue('');
      setUsername(editSecret.username || '');
      setPassword('');
      setDescription(editSecret.description || '');
      setCategory(editSecret.is_credential ? 'credentials' : (editSecret.category || 'api_keys'));
    } else {
      setKind('credential'); setName(''); setValue(''); setUsername(''); setPassword(''); setDescription(''); setCategory('api_keys');
    }
  }

  const handleSubmit = async () => {
    if (!editSecret && !name) { toast.error(t('A name is required')); return; }
    if (kind === 'credential') {
      if (!editSecret && (!username || !password)) { toast.error(t('An email/username and a password are required')); return; }
      if (editSecret && !username) { toast.error(t('An email/username is required')); return; }
    } else if (!editSecret && !value) { toast.error(t('A value is required')); return; }
    setSaving(true);
    try {
      if (editSecret) {
        await vaultApi.updateSecret(
          editSecret.id,
          kind === 'credential'
            ? { username, password: password || undefined, description }
            : { value: value || undefined, description, category: category || undefined },
        );
        toast.success(t('Secret "{{name}}" updated', { name: editSecret.name }));
      } else {
        await vaultApi.createSecret(
          kind === 'credential'
            ? { name, username, password, category: 'credentials', description }
            : { name, value, description, category: category || undefined },
        );
        toast.success(t('Secret "{{name}}" created', { name }));
      }
      onSave();
      onClose();
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      toast.error(typeof detail === 'string' ? detail : t('Failed to save secret'));
    } finally { setSaving(false); }
  };

  const fieldCls = 'w-full px-3 py-2 text-sm border border-border rounded-lg bg-surface focus:outline-none focus:ring-1 focus:ring-ink/20';

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={editSecret ? t('Edit Secret') : t('New Secret')}>
      <div className="space-y-4">
        <div>
          <span className="text-xs font-medium text-secondary block mb-1" id={`${fieldId}-type`}>{t('Type')}</span>
          <div className="flex items-center gap-1 bg-active rounded-lg p-0.5 text-xs" role="group" aria-labelledby={`${fieldId}-type`}>
            {([['credential', t('Login credential')], ['value', t('API key / token')]] as [Kind, string][]).map(([k, label]) => (
              <button key={k} type="button" onClick={() => !editSecret && setKind(k)}
                disabled={!!editSecret && editSecret.is_credential !== (k === 'credential')}
                className={clsx('flex-1 px-2.5 py-1.5 rounded-md font-medium transition-colors',
                  kind === k ? 'bg-surface text-ink shadow-sm' : 'text-tertiary hover:text-secondary',
                  !!editSecret && 'disabled:opacity-40 disabled:cursor-not-allowed')}>
                {label}
              </button>
            ))}
          </div>
          {kind === 'credential' && (
            <p className="text-[10px] text-tertiary mt-1">{t('Stores an email/username + password pair you can link to a persona.')}</p>
          )}
        </div>

        <div>
          <label htmlFor={`${fieldId}-name`} className="text-xs font-medium text-secondary block mb-1">{t('Name')}</label>
          <input id={`${fieldId}-name`} value={name} onChange={(e) => setName(e.target.value)} disabled={!!editSecret}
            placeholder={kind === 'credential' ? t('e.g. shopify_login') : t('e.g. stripe_api_key')} className={clsx(fieldCls, 'font-mono disabled:opacity-50')} />
          {!editSecret && (
            <p className="text-[10px] text-tertiary mt-1">{t('Letters, numbers, underscores, dots, dashes. Cannot be changed later.')}</p>
          )}
        </div>

        {kind === 'credential' ? (
          <>
            <div>
              <label htmlFor={`${fieldId}-username`} className="text-xs font-medium text-secondary block mb-1">{t('Email or username')}</label>
              <input id={`${fieldId}-username`} value={username} onChange={(e) => setUsername(e.target.value)} placeholder="you@example.com" autoComplete="off" className={fieldCls} />
            </div>
            <div>
              <label htmlFor={`${fieldId}-password`} className="text-xs font-medium text-secondary block mb-1">
                {t('Password')} {editSecret && <span className="text-tertiary">{t('(leave blank to keep current)')}</span>}
              </label>
              <input id={`${fieldId}-password`} type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                placeholder={editSecret ? t('Enter new value to change') : '••••••••'} autoComplete="new-password" className={clsx(fieldCls, 'font-mono')} />
            </div>
          </>
        ) : (
          <div>
            <label htmlFor={`${fieldId}-value`} className="text-xs font-medium text-secondary block mb-1">
              {t('Value')} {editSecret && <span className="text-tertiary">{t('(leave blank to keep current)')}</span>}
            </label>
            <input id={`${fieldId}-value`} type="password" value={value} onChange={(e) => setValue(e.target.value)}
              placeholder={editSecret ? t('Enter new value to change') : t('Secret value')} className={clsx(fieldCls, 'font-mono')} />
          </div>
        )}

        <div>
          <label htmlFor={`${fieldId}-description`} className="text-xs font-medium text-secondary block mb-1">{t('Description')}</label>
          <input id={`${fieldId}-description`} value={description} onChange={(e) => setDescription(e.target.value)} placeholder={t('Optional description')} className={fieldCls} />
        </div>

        {kind === 'value' && (
          <div>
            <label htmlFor={`${fieldId}-category`} className="text-xs font-medium text-secondary block mb-1">{t('Category')}</label>
            <Select id={`${fieldId}-category`} value={category} onChange={setCategory} aria-label={t('Category')}
              options={[{ value: 'api_keys', label: t('API Keys') }, { value: 'tokens', label: t('Tokens') }, { value: 'other', label: t('Other') }]} />
          </div>
        )}

        {!editSecret && name && (
          <div className="bg-hover border border-border rounded-lg px-3 py-2">
            <p className="text-[10px] font-medium text-tertiary mb-0.5">{t('Reference in workflows:')}</p>
            {kind === 'credential' ? (
              <code className="text-xs font-mono text-ink block">{`{{vault:${name}.username}}`} · {`{{vault:${name}.password}}`}</code>
            ) : (
              <code className="text-xs font-mono text-ink">{'{{vault:'}{name}{'}}'}</code>
            )}
          </div>
        )}

        <div className="flex items-center justify-end gap-2 pt-2">
          <button onClick={onClose} className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg hover:bg-chrome transition-colors">{t('Cancel')}</button>
          <button onClick={handleSubmit} disabled={saving} className="px-4 py-2 text-sm font-medium bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 transition-colors disabled:opacity-50">{saving ? t('Saving...') : editSecret ? t('Update') : t('Create')}</button>
        </div>
      </div>
    </Modal>
  );
};

export default SecretsPage;
