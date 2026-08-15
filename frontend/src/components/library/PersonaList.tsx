import React, { useState, useMemo, useCallback, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '../../hooks/useQuery';
import { Q } from '../../stores/queryKeys';
import { personasApi } from '../../api/endpoints';
import type { Persona } from '../../types/api';
import { formatElapsedShort } from '../../utils/format';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { PersonaWizard } from '../workflows/PersonaWizard';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import {
  IdentificationIcon,
  ShieldCheckIcon,
  TrashIcon,
  CheckIcon,
  PlusIcon,
} from '@heroicons/react/24/outline';
import { Stagger, Select, EmptyHero } from '../ui';
import { ScrollArea } from '../ui/ScrollArea';
import { tintStyle } from '../../utils/tint';
import { PersonaDetailPane } from './PersonaDetailPane';
import { SendToAgentModal } from '../fleet/SendToAgentModal';
import { personasConfig } from '../toolrail/configs';
import {
  SHELF_CONTAINER,
  SHELF_LIST_COL,
  SHELF_DETAIL_COL,
  shelfRowClass,
  shelfRowMouseDown,
  ShelfAccentBar,
  shelfFilterChipClass,
  shelfFilterCountClass,
  ShelfListSearch,
  ShelfSkeleton,
} from './shelf';

// ── session state ──
interface RowSession { dot: string; label: string; className: string; attention: boolean; }
function rowSession(p: Persona, t: (k: string) => string): RowSession {
  if (!p.has_warm_session) return { dot: 'bg-active', label: t('No session'), className: 'text-tertiary', attention: false };
  if (p.validation_status === 'expired') return { dot: 'bg-warning', label: t('Expired'), className: 'text-warning font-medium', attention: true };
  return { dot: 'bg-success', label: t('Valid'), className: 'text-tertiary', attention: false };
}
const TWOFA_SHORT: Record<string, string> = {
  none: '', totp: 'TOTP', email_otp: 'Email', sms: 'SMS',
};

type SortMode = 'used' | 'attention' | 'name' | 'domain' | 'workflows';
const ts = (v: any): number => (v ? new Date(v).getTime() : 0);
function attentionRank(p: Persona): number {
  if (p.has_warm_session && p.validation_status === 'expired') return 0;
  if (!p.has_warm_session) return 1;
  return 2;
}
function sortPersonas(list: Persona[], mode: SortMode): Persona[] {
  const arr = [...list];
  switch (mode) {
    case 'name': return arr.sort((a, b) => a.name.localeCompare(b.name));
    case 'domain': return arr.sort((a, b) => (a.target_domain || '').localeCompare(b.target_domain || ''));
    case 'workflows': return arr.sort((a, b) => (b.linked_workflows?.length || 0) - (a.linked_workflows?.length || 0));
    case 'attention': return arr.sort((a, b) => attentionRank(a) - attentionRank(b) || ts(b.last_used_at) - ts(a.last_used_at));
    case 'used':
    default:
      return arr.sort((a, b) => ts(b.last_used_at) - ts(a.last_used_at));
  }
}

/** lg breakpoint (1024px) — master-detail two-pane only renders this wide. */
function useIsWide(): boolean {
  const [wide, setWide] = useState(() =>
    typeof window !== 'undefined' ? window.matchMedia('(min-width: 1024px)').matches : true,
  );
  useEffect(() => {
    const mq = window.matchMedia('(min-width: 1024px)');
    const onChange = (e: MediaQueryListEvent) => setWide(e.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);
  return wide;
}

interface PersonaRowProps {
  p: Persona;
  selected: boolean;
  checked: boolean;
  testing: boolean;
  onSelect: (p: Persona) => void;
  onToggleCheck: () => void;
  onTest: (p: Persona) => void;
}

const PersonaRow: React.FC<PersonaRowProps> = React.memo(({
  p, selected, checked, testing, onSelect, onToggleCheck, onTest,
}) => {
  const { t } = useTranslation();
  const session = rowSession(p, t);
  const has2fa = p.twofa_method !== 'none';
  const twofaShort = TWOFA_SHORT[p.twofa_method] || '';

  return (
    <div
      onMouseDown={shelfRowMouseDown}
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      aria-label={p.name}
      onClick={() => onSelect(p)}
      onKeyDown={(e) => {
        if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) {
          e.preventDefault();
          onSelect(p);
        }
      }}
      className={shelfRowClass(selected)}
    >
      {selected && <ShelfAccentBar />}

      {/* Leading tile — identity icon with a session badge; flips to a checkbox on hover / when checked. */}
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); onToggleCheck(); }}
        aria-label={t('Select persona')}
        aria-pressed={checked}
        className="relative w-8 h-8 shrink-0 rounded-lg outline-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/20"
      >
        <span
          className={clsx(
            'absolute inset-0 flex items-center justify-center rounded-lg border transition-opacity',
            checked ? 'bg-ink border-ink text-surface opacity-100' : 'bg-surface border-border opacity-0 group-hover:opacity-100',
          )}
        >
          <CheckIcon className={clsx('h-3.5 w-3.5 transition-opacity', checked ? 'opacity-100' : 'opacity-0')} />
        </span>
        <span
          style={tintStyle('neutral')}
          className={clsx('absolute inset-0 flex items-center justify-center rounded-lg transition-opacity', checked ? 'opacity-0' : 'opacity-100 group-hover:opacity-0')}
        >
          <IdentificationIcon className="h-4 w-4" />
        </span>
        {!checked && (
          <span className={clsx('absolute -bottom-1 -right-1 w-3.5 h-3.5 rounded-full border-2 border-surface transition-opacity group-hover:opacity-0', session.dot)} />
        )}
      </button>

      <div className="flex-1 min-w-0">
        <div className="text-[13px] font-medium text-ink overflow-hidden" style={{ containerType: 'inline-size' }} title={p.name}>
          <span className="inline-block whitespace-nowrap min-w-full group-hover:[animation:marquee-hover_8s_ease-in-out_infinite]">
            {p.name}
          </span>
        </div>
        <div className="flex items-center gap-1 text-[11px] truncate mt-0.5">
          {p.target_domain && <><span className="text-tertiary truncate">{p.target_domain}</span><span className="text-tertiary/50 shrink-0">·</span></>}
          {twofaShort && <><span className="text-tertiary shrink-0">{twofaShort}</span><span className="text-tertiary/50 shrink-0">·</span></>}
          <span className={clsx('shrink-0', session.className)}>
            {session.attention || !p.has_warm_session
              ? session.label
              : p.last_used_at
                ? t('{{ago}} ago', { ago: formatElapsedShort(p.last_used_at) })
                : session.label}
          </span>
        </div>
      </div>

      {/* Test 2FA quick action (only when a 2FA method is set) */}
      {has2fa && (
        <button
          onClick={(e) => { e.stopPropagation(); onTest(p); }}
          disabled={testing}
          title={t('Test 2FA')}
          aria-label={t('Test 2FA')}
          className={clsx(
            'flex items-center justify-center w-7 h-7 rounded-lg transition-all active:scale-95 disabled:opacity-50 shrink-0',
            'text-tertiary hover:bg-chrome hover:text-ink',
          )}
        >
          <ShieldCheckIcon className={clsx('h-4 w-4', testing && 'animate-pulse')} />
        </button>
      )}
    </div>
  );
});
PersonaRow.displayName = 'PersonaRow';

interface PersonaListProps {
  search: string;
  /** When set, the list renders its own search input at the top of the master column. */
  onSearchChange?: (v: string) => void;
  onItemClick?: (id: number) => void;
  filter?: (p: any) => boolean;
  /** Open the create wizard from the list's empty state. */
  onCreate?: () => void;
}

export const PersonaList: React.FC<PersonaListProps> = ({ search, onSearchChange, onItemClick, filter, onCreate }) => {
  const { t } = useTranslation();
  const isWide = useIsWide();

  const { data: personas, loading, error, refresh } = useQuery<Persona[]>(Q.personas(), () => personasApi.list(), { pollInterval: 30000 });

  const [view, setView] = useState<string>(() => localStorage.getItem('writ.personaView') || 'all');
  const [sortBy, setSortBy] = useState<SortMode>(() => {
    const v = localStorage.getItem('writ.personaSortBy');
    return (['used', 'attention', 'name', 'domain', 'workflows'] as const).includes(v as any) ? (v as SortMode) : 'used';
  });
  useEffect(() => { try { localStorage.setItem('writ.personaView', view); } catch { /* noop */ } }, [view]);
  useEffect(() => { try { localStorage.setItem('writ.personaSortBy', sortBy); } catch { /* noop */ } }, [sortBy]);

  const searched = useMemo(() => {
    const q = search.trim().toLowerCase();
    return (personas || [])
      .filter((p) => !q || p.name.toLowerCase().includes(q)
        || (p.target_domain || '').toLowerCase().includes(q)
        || (p.login_username || '').toLowerCase().includes(q))
      .filter((p) => !filter || filter(p));
  }, [personas, search, filter]);

  const views = personasConfig.views;
  const viewCounts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const v of views) m[v.id] = searched.filter(v.predicate).length;
    return m;
  }, [searched, views]);

  const activeView = views.find((v) => v.id === view) || views[0];
  const personaList = useMemo(
    () => sortPersonas(searched.filter(activeView.predicate), sortBy),
    [searched, activeView, sortBy],
  );

  const [pickedId, setSelectedId] = useState<number | null>(null);
  const [rawCheckedIds, setCheckedIds] = useState<Set<number>>(new Set());

  const visibleIds = useMemo(() => new Set(personaList.map((p) => p.id)), [personaList]);

  // The detail selection is DERIVED, not stored: the user's pick while that
  // persona is still listed, otherwise the first row. An effect would paint one
  // frame against a persona the current filter no longer shows.
  const selectedId = useMemo(() => {
    if (!isWide) return pickedId;
    if (personaList.length === 0) return null;
    return pickedId != null && visibleIds.has(pickedId) ? pickedId : personaList[0].id;
  }, [pickedId, personaList, visibleIds, isWide]);

  // Bulk checks pruned ON READ — a persona leaving the list (filter, search,
  // delete) drops out of the effective selection with no state write.
  const checkedIds = useMemo(
    () => new Set([...rawCheckedIds].filter((idv) => visibleIds.has(idv))),
    [rawCheckedIds, visibleIds],
  );

  const toggleCheck = useCallback((id: number) => {
    setCheckedIds((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });
  }, []);
  const clearChecks = useCallback(() => setCheckedIds(new Set()), []);

  const checkedPersonas = useMemo(
    () => personaList.filter((p) => checkedIds.has(p.id)),
    [personaList, checkedIds],
  );

  const handleSelect = useCallback((p: Persona) => {
    if (isWide) setSelectedId(p.id);
    else if (onItemClick) onItemClick(p.id);
  }, [isWide, onItemClick]);

  const [testingId, setTestingId] = useState<number | null>(null);
  const [editPersona, setEditPersona] = useState<Persona | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Persona | null>(null);
  const [sendTarget, setSendTarget] = useState<Persona | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);

  const handleTest = useCallback(async (p: Persona) => {
    setTestingId(p.id);
    const toastId = toast.loading(t('Testing 2FA…'));
    try {
      const r = await personasApi.test2fa(p.id);
      toast.success(r.message || t('2FA OK ({{detail}})', { detail: `${r.method}${r.kind ? `, ${r.kind}` : ''}` }), { id: toastId });
    } catch (err: any) {
      const d = err?.response?.data?.detail;
      toast.error(typeof d === 'string' ? d : t('2FA test failed'), { id: toastId });
    } finally {
      setTestingId(null);
    }
  }, [t]);

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await personasApi.delete(deleteTarget.id);
      toast.success(t('Persona deleted'));
      refresh();
      setDeleteTarget(null);
    } catch {
      toast.error(t('Failed to delete persona'));
    } finally {
      setDeleting(false);
    }
  };

  const handleBulkDelete = useCallback(async () => {
    setBulkBusy(true);
    let ok = 0; let fail = 0;
    for (const p of checkedPersonas) {
      try { await personasApi.delete(p.id); ok += 1; } catch { fail += 1; }
    }
    setBulkBusy(false);
    setBulkDeleteOpen(false);
    clearChecks();
    refresh();
    if (fail === 0) toast.success(t('Deleted {{n}}', { n: ok }));
    else toast.error(t('{{ok}} deleted · {{fail}} failed', { ok, fail }));
  }, [checkedPersonas, clearChecks, refresh, t]);

  if (loading && !personas) return <ShelfSkeleton withSearch={!!onSearchChange} label={t('Loading personas')} />;
  if (!personas && error) return <ErrorState message={error} onRetry={refresh} />;
  if ((personas || []).length === 0) return <EmptyState onCreate={onCreate} />;

  const selectedPersona = personaList.find((p) => p.id === selectedId) || null;
  const allChecked = personaList.length > 0 && checkedIds.size >= personaList.length;

  return (
    <div className={SHELF_CONTAINER}>
      {/* ── Master list ── */}
      <div className={SHELF_LIST_COL}>
        {/* Filter + sort header */}
        <div className="shrink-0 border-b border-border px-3 py-2.5 space-y-2">
          {/* Search — lives with the list it filters, above the chips. */}
          {onSearchChange && <ShelfListSearch value={search} onChange={onSearchChange} ariaLabel={t('Search personas')} />}
          <div className="flex flex-wrap items-center gap-1">
            {views.map((v) => {
              const Icon = v.icon;
              const active = v.id === view;
              const count = viewCounts[v.id] ?? 0;
              if (count === 0 && v.id !== 'all' && !active) return null;
              return (
                <button
                  key={v.id}
                  onClick={() => setView(v.id)}
                  aria-pressed={active}
                  className={shelfFilterChipClass(active)}
                >
                  <Icon className="w-3 h-3" />
                  {t(v.label)}
                  <span className={shelfFilterCountClass(active)}>{count}</span>
                </button>
              );
            })}
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11px] text-tertiary">{t('{{n}} shown', { n: personaList.length })}</span>
            <Select<SortMode>
              size="sm"
              value={sortBy}
              onChange={setSortBy}
              aria-label={t('Sort by')}
              wrapperClassName="w-40"
              options={[
                { value: 'used', label: t('Last used') },
                { value: 'attention', label: t('Needs attention') },
                { value: 'workflows', label: t('Most used by') },
                { value: 'name', label: t('Name A–Z') },
                { value: 'domain', label: t('Domain') },
              ]}
            />
          </div>
        </div>

        {/* Bulk action bar */}
        {checkedIds.size > 0 && (
          <div className="shrink-0 flex items-center justify-between gap-2 border-b border-border bg-chrome px-3 py-2 bulkbar-enter">
            <div className="flex items-center gap-2 text-[12px]">
              <button
                onClick={() => setCheckedIds(allChecked ? new Set() : new Set(personaList.map((p) => p.id)))}
                className={clsx('flex h-4 w-4 items-center justify-center rounded border transition-colors', allChecked ? 'border-ink bg-ink text-surface' : 'border-border bg-surface')}
                aria-label={t('Select all')}
              >
                {allChecked && <CheckIcon className="h-3 w-3" />}
              </button>
              <span className="font-medium text-ink">{t('{{n}} selected', { n: checkedIds.size })}</span>
              <button onClick={clearChecks} className="text-tertiary hover:text-ink transition-colors">{t('Clear')}</button>
            </div>
            <button onClick={() => setBulkDeleteOpen(true)} disabled={bulkBusy} title={t('Delete')}
              className="p-1.5 rounded-lg text-danger hover:bg-danger-bg hover:text-danger-fg disabled:opacity-50 transition-colors">
              <TrashIcon className="h-3.5 w-3.5" />
            </button>
          </div>
        )}

        {/* Rows */}
        {personaList.length === 0 ? (
          <div className="flex flex-1 flex-col items-center justify-center text-center px-6 py-10">
            <p className="text-[13px] font-medium text-ink">{t('Nothing here')}</p>
            <p className="text-[11px] text-secondary mt-1">{t('No personas match this filter.')}</p>
          </div>
        ) : (
          <ScrollArea className="flex-1" viewportClassName="px-2 py-2" fade="chrome">
            <Stagger className="grid gap-0.5" staggerMs={24}>
              {personaList.map((p) => (
                <PersonaRow
                  key={p.id}
                  p={p}
                  selected={isWide && selectedId === p.id}
                  checked={checkedIds.has(p.id)}
                  testing={testingId === p.id}
                  onSelect={handleSelect}
                  onToggleCheck={() => toggleCheck(p.id)}
                  onTest={handleTest}
                />
              ))}
            </Stagger>
          </ScrollArea>
        )}
      </div>

      {/* ── Detail pane (lg only) ── */}
      <div className={SHELF_DETAIL_COL}>
        <PersonaDetailPane
          onChanged={refresh}
          persona={selectedPersona}
          onEdit={setEditPersona}
          onDelete={setDeleteTarget}
          onSend={setSendTarget}
        />
      </div>

      {/* ── Modals ── */}
      <PersonaWizard
        isOpen={!!editPersona}
        persona={editPersona}
        onClose={() => setEditPersona(null)}
        onSaved={() => refresh()}
      />

      <SendToAgentModal
        open={!!sendTarget}
        onClose={() => setSendTarget(null)}
        kind="persona"
        entityId={sendTarget?.id ?? 0}
        entityName={sendTarget?.name ?? ''}
      />

      <Modal isOpen={!!deleteTarget} onClose={() => setDeleteTarget(null)} title={t('Delete persona')}>
        <p className="text-sm text-secondary mb-1">
          {t('Delete')} <strong className="text-ink">{deleteTarget?.name}</strong>?
        </p>
        <p className="text-xs text-tertiary mb-4">
          {t('Any linked workflows will fall back to manual login, and the saved warm session and credentials are removed. This cannot be undone.')}
        </p>
        <div className="flex items-center justify-end gap-2">
          <Button variant="secondary" size="sm" onClick={() => setDeleteTarget(null)}>{t('Cancel')}</Button>
          <Button size="sm" onClick={handleDelete} loading={deleting}>{t('Delete persona')}</Button>
        </div>
      </Modal>

      <Modal isOpen={bulkDeleteOpen} onClose={() => (bulkBusy ? null : setBulkDeleteOpen(false))} title={t('Delete personas')}>
        <p className="text-sm text-secondary mb-4">
          {t('Delete {{n}} persona(s)? Linked workflows fall back to manual login. This cannot be undone.', { n: checkedPersonas.length })}
        </p>
        <div className="flex items-center justify-end gap-2">
          <Button variant="secondary" size="sm" onClick={() => setBulkDeleteOpen(false)} disabled={bulkBusy}>{t('Cancel')}</Button>
          <Button size="sm" onClick={handleBulkDelete} loading={bulkBusy} disabled={checkedPersonas.length === 0}>
            {t('Delete {{n}}', { n: checkedPersonas.length })}
          </Button>
        </div>
      </Modal>
    </div>
  );
};

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  const { t } = useTranslation();
  return (
    <EmptyHero
      icon={IdentificationIcon}
      title={t("Couldn't load personas")}
      description={message}
      className="flex-1 min-h-0 bg-surface"
    >
      <Button onClick={onRetry} size="sm">{t('Retry')}</Button>
    </EmptyHero>
  );
}

function EmptyState({ onCreate }: { onCreate?: () => void }) {
  const { t } = useTranslation();
  return (
    <EmptyHero
      icon={IdentificationIcon}
      title={t('No personas yet')}
      description={t('A persona is a reusable identity — a login plus 2FA, a consistent fingerprint, and a warm session. Attach one to a workflow and it logs in automatically on every run.')}
      className="flex-1 min-h-0 bg-surface"
    >
      {onCreate && (
        <Button onClick={onCreate} size="sm">
          <PlusIcon className="w-3.5 h-3.5" /> {t('Create persona')}
        </Button>
      )}
    </EmptyHero>
  );
}
