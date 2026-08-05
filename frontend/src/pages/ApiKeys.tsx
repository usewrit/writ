import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useRequireAuth } from '../hooks/useAuth';
import { useQuery } from '../hooks/useQuery';
import { Q } from '../stores/queryKeys';
import { apiKeysApi, automationApi, targetsApi } from '../api/endpoints';
import { apiErrorMessage } from '../api/client';
import { formatRelativeTime, formatDate } from '../utils/format';
import { Modal } from '../components/ui/Modal';
import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { Expand, Select, Checkbox, SwapFade, EmptyHero } from '../components/ui';
import { ScrollArea } from '../components/ui/ScrollArea';
import { apiKeysConfig } from '../components/toolrail/configs';
import { tintStyle } from '../utils/tint';
import {
  SHELF_CONTAINER,
  SHELF_LIST_COL,
  SHELF_DETAIL_COL,
  shelfRowClass,
  shelfRowMouseDown,
  ShelfAccentBar,
  shelfFilterChipClass,
  shelfFilterCountClass,
  ShelfSkeleton,
} from '../components/library/shelf';
import toast from 'react-hot-toast';
import {
  PlusIcon,
  TrashIcon,
  ClipboardDocumentIcon,
  KeyIcon,
  PencilIcon,
  ChevronDownIcon,
  ShieldCheckIcon,
  LockClosedIcon,
  PlayIcon,
  AdjustmentsHorizontalIcon,
  ClockIcon,
  CheckIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import type { ApiKey } from '../types/api';
import {
  FALLBACK_CATALOG,
  actionLabel,
  describeGrant,
  groupByResource,
  matchPreset,
  scopeString,
  EXPIRY_OPTIONS,
  expiryToIso,
  type ScopeAction,
  type ScopeCatalog,
} from '../lib/apiKeyScopes';

// Presets are answers to "what is this key for?", which is the question someone
// minting a key is actually asking. The previous screen opened on a grid of
// resource x permission checkboxes — the storage format, rendered as a form.
const PRESET_ICONS: Record<string, React.FC<any>> = {
  read_only: LockClosedIcon,
  run: PlayIcon,
  full: ShieldCheckIcon,
};

type ResourceItems = Record<string, Array<{ id: number; name: string }>>;

// ── plain-language grant summary ───────────────────────────────────────────

const GrantSummary: React.FC<{
  scopes: string[];
  catalog: ScopeCatalog;
  resourceIds?: Record<string, number[]>;
  className?: string;
  /**
   * 'list' reads best where there is room (the detail pane). 'chips' wraps
   * horizontally instead of stacking — a Full-access grant is fifteen lines as a
   * list, which on its own overflowed the create dialog.
   */
  variant?: 'list' | 'chips';
}> = ({ scopes, catalog, resourceIds, className, variant = 'list' }) => {
  const { t } = useTranslation();

  if (variant === 'chips') {
    const groups = groupByResource(scopes, catalog);
    if (!groups.length) {
      return <p className={clsx('text-[11px] text-tertiary', className)}>{t('Nothing yet — pick at least one.')}</p>;
    }
    return (
      <div className={clsx('flex flex-wrap gap-1', className)}>
        {groups.map(({ resource, actions }) => {
          const pins = resourceIds?.[resource.key];
          return (
            <span
              key={resource.key}
              className="inline-flex items-center gap-1 rounded border border-border bg-surface px-1.5 py-0.5 text-[10.5px]"
              title={t(resource.description)}
            >
              <span className="text-ink">{t(resource.label)}</span>
              <span className="text-tertiary">{actions.map((a) => actionLabel(a)).join(' · ').toLowerCase()}</span>
              {pins?.length ? <span className="text-tertiary">({pins.length})</span> : null}
            </span>
          );
        })}
      </div>
    );
  }

  const lines = describeGrant(scopes, catalog, resourceIds);
  if (!lines.length) {
    return <p className={clsx('text-[12px] text-tertiary', className)}>{t('No access — this key cannot call anything.')}</p>;
  }
  return (
    <ul className={clsx('space-y-1', className)}>
      {lines.map((line) => (
        <li key={line} className="flex items-start gap-1.5 text-[12px] text-secondary leading-relaxed">
          <CheckIcon className="h-3.5 w-3.5 text-tertiary shrink-0 mt-[2px]" />
          <span>{line}</span>
        </li>
      ))}
    </ul>
  );
};

// ── scope editor (custom grants) ───────────────────────────────────────────

interface ScopeEditorProps {
  catalog: ScopeCatalog;
  scopes: Set<string>;
  resourceIds: Record<string, number[]>;
  onToggleScope: (scope: string) => void;
  onToggleResource: (resource: string, on: boolean) => void;
  onTogglePin: (resource: string, id: number) => void;
  onClearPins: (resource: string) => void;
  resourceItems: ResourceItems;
}

const ScopeEditor: React.FC<ScopeEditorProps> = ({
  catalog, scopes, resourceIds, onToggleScope, onToggleResource, onTogglePin, onClearPins, resourceItems,
}) => {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState<string | null>(null);

  return (
    <div className="divide-y divide-border rounded-lg border border-border overflow-hidden">
      {catalog.resources.map((resource) => {
        const held = resource.actions.filter((a) => scopes.has(scopeString(resource.key, a)));
        const on = held.length > 0;
        const items = resourceItems[resource.key] || [];
        const pins = resourceIds[resource.key] || [];
        const canPin = resource.pinnable && items.length > 0;
        const isExpanded = expanded === resource.key;

        return (
          <div key={resource.key} className={clsx('transition-colors', on ? 'bg-surface' : 'bg-hover/30')}>
            {/* ONE line per resource. The description moved to the row's title —
                fifteen resources x two lines was most of why this dialog ran off
                the screen, and the label already says what the resource is. */}
            <div className="flex items-center gap-2.5 px-2.5 py-1.5" title={t(resource.description)}>
              <Checkbox
                checked={on}
                onChange={() => onToggleResource(resource.key, !on)}
                size="sm"
                className="shrink-0"
                aria-label={t(resource.label)}
              />
              <span className={clsx('flex-1 min-w-0 truncate text-[12px] font-medium', on ? 'text-ink' : 'text-tertiary')}>
                {t(resource.label)}
              </span>

              {on && canPin && (
                <button
                  type="button"
                  onClick={() => setExpanded(isExpanded ? null : resource.key)}
                  className="flex items-center gap-0.5 shrink-0 text-[10px] text-tertiary hover:text-ink"
                >
                  {pins.length ? t('{{count}} items', { count: pins.length }) : t('All items')}
                  <ChevronDownIcon className={clsx('h-3 w-3 transition-transform', isExpanded && 'rotate-180')} />
                </button>
              )}

              {/* Only the actions this resource actually HAS. A resource with no
                  delete route must not render a Delete toggle that grants a scope
                  no endpoint would ever check — that mismatch is what made the
                  old `files` and `execute` permissions permanently unusable. */}
              <div className="flex items-center gap-0.5 shrink-0">
                {resource.actions.map((action) => {
                  const active = scopes.has(scopeString(resource.key, action));
                  return (
                    <button
                      key={action}
                      type="button"
                      onClick={() => onToggleScope(scopeString(resource.key, action))}
                      className={clsx(
                        'px-1.5 py-0.5 text-[10.5px] rounded border transition-colors',
                        active
                          ? 'border-ink/20 bg-ink text-surface'
                          : 'border-border text-tertiary hover:text-ink hover:border-ink/15',
                      )}
                    >
                      {actionLabel(action as ScopeAction)}
                    </button>
                  );
                })}
              </div>
            </div>

            <Expand open={on && canPin && isExpanded} mountOnEnter className="border-t border-border bg-chrome/40 px-2.5 py-1.5">
              <div className="flex items-center justify-between mb-1">
                <span className="text-[10px] text-tertiary">
                  {pins.length
                    ? t('Limited to {{count}} of {{total}}', { count: pins.length, total: items.length })
                    : t('Pick items to narrow this down')}
                </span>
                {pins.length > 0 && (
                  <button type="button" onClick={() => onClearPins(resource.key)} className="text-[10px] text-tertiary hover:text-ink">
                    {t('Clear')}
                  </button>
                )}
              </div>
              <div className="max-h-28 overflow-y-auto">
                {items.map((item) => (
                  <label
                    key={item.id}
                    className={clsx(
                      'flex items-center gap-2 px-1.5 py-1 rounded cursor-pointer text-[11px] transition-colors',
                      pins.includes(item.id) ? 'bg-hover' : 'hover:bg-surface',
                    )}
                  >
                    <Checkbox size="sm" checked={pins.includes(item.id)} onChange={() => onTogglePin(resource.key, item.id)} />
                    <span className="text-ink truncate">{item.name}</span>
                    <span className="text-tertiary ml-auto shrink-0">#{item.id}</span>
                  </label>
                ))}
              </div>
            </Expand>
          </div>
        );
      })}
    </div>
  );
};

// ── detail pane ────────────────────────────────────────────────────────────

const Fact: React.FC<{ icon: React.FC<any>; label: string; children: React.ReactNode }> = ({ icon: Icon, label, children }) => (
  <div className="flex items-center gap-2 min-w-0">
    <Icon className="h-3.5 w-3.5 text-tertiary shrink-0" />
    <span className="text-[11px] text-tertiary shrink-0">{label}</span>
    <span className="ml-auto text-[12px] text-ink text-right truncate min-w-0">{children}</span>
  </div>
);

/** Preset name when the grant is exactly a preset, else a count. */
function grantLabelFor(k: ApiKey, catalog: ScopeCatalog, t: (key: string, opts?: any) => string): string {
  const preset = k.preset ?? matchPreset(k.scopes || [], catalog);
  if (preset) {
    const def = catalog.presets.find((p) => p.key === preset);
    return t(def?.label ?? preset);
  }
  return t('{{count}} permissions', { count: (k.scopes || []).length });
}

interface DetailProps {
  k: ApiKey | null;
  catalog: ScopeCatalog;
  resourceItems: ResourceItems;
  onDelete: (k: ApiKey) => void;
  editing: boolean;
  editScopes: Set<string>;
  editIds: Record<string, number[]>;
  editSetters: {
    toggleScope: (scope: string) => void;
    toggleResource: (resource: string, on: boolean) => void;
    togglePin: (resource: string, id: number) => void;
    clearPins: (resource: string) => void;
  };
  onStartEdit: (k: ApiKey) => void;
  onCancelEdit: () => void;
  onSaveEdit: (k: ApiKey) => void;
  saving: boolean;
}

const KeyDetailPane: React.FC<DetailProps> = ({
  k, catalog, resourceItems, onDelete, editing, editScopes, editIds, editSetters,
  onStartEdit, onCancelEdit, onSaveEdit, saving,
}) => {
  const { t } = useTranslation();
  // Keys expire in days or months, so one reading taken when the pane mounts is
  // all this badge needs. It also has to be a reading: `Date.now()` called during
  // render is impure (`react-hooks/purity`) — the clock advances without React
  // knowing, so two renders of the same commit could disagree about "expired".
  const [now] = useState(() => Date.now());
  if (!k) {
    return (
      <EmptyHero
        icon={KeyIcon}
        title={t('Select an API key')}
        description={t('Pick one on the left to see exactly what it can reach.')}
        className="flex-1"
      />
    );
  }

  const expired = k.expires_at ? new Date(k.expires_at).getTime() < now : false;

  return (
    <div className="flex flex-1 flex-col min-h-0">
      <div className="px-5 pt-5 pb-4 border-b border-border">
        <div className="flex items-start gap-3">
          <div style={tintStyle('neutral')} className="w-10 h-10 rounded-lg flex items-center justify-center shrink-0">
            <KeyIcon className="h-5 w-5" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-[15px] font-semibold text-ink truncate" title={k.label}>{k.label}</div>
            <div className="mt-1 flex items-center gap-1.5">
              <span className="px-1.5 py-0.5 text-[10px] font-medium rounded-full uppercase text-secondary bg-hover">
                {grantLabelFor(k, catalog, t)}
              </span>
              {expired && (
                <span className="px-1.5 py-0.5 text-[10px] font-medium rounded-full uppercase text-danger bg-danger-bg">
                  {t('Expired')}
                </span>
              )}
            </div>
          </div>
          {!editing && (
            <div className="flex items-center gap-1 shrink-0">
              <button onClick={() => onStartEdit(k)} className="flex items-center gap-1.5 px-2.5 py-1.5 text-[12px] font-medium text-secondary rounded-lg hover:bg-chrome hover:text-ink transition-colors">
                <PencilIcon className="h-3.5 w-3.5" />{t('Edit')}
              </button>
              <button onClick={() => onDelete(k)} className="flex items-center gap-1.5 px-2.5 py-1.5 text-[12px] font-medium text-danger rounded-lg hover:bg-danger-bg hover:text-danger-fg transition-colors">
                <TrashIcon className="h-3.5 w-3.5" />{t('Delete')}
              </button>
            </div>
          )}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden px-5 py-4 space-y-6">
        <div>
          <div className="mb-2">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-tertiary">
              {editing ? t('Access') : t('This key can')}
            </span>
          </div>
          {editing ? (
            <>
              <ScopeEditor
                catalog={catalog}
                scopes={editScopes}
                resourceIds={editIds}
                onToggleScope={editSetters.toggleScope}
                onToggleResource={editSetters.toggleResource}
                onTogglePin={editSetters.togglePin}
                onClearPins={editSetters.clearPins}
                resourceItems={resourceItems}
              />
              <div className="flex justify-end gap-2 mt-3">
                <Button size="sm" variant="secondary" onClick={onCancelEdit}>{t('Cancel')}</Button>
                <Button size="sm" onClick={() => onSaveEdit(k)} disabled={saving || editScopes.size === 0} loading={saving}>
                  {t('Save')}
                </Button>
              </div>
            </>
          ) : (
            <div className="rounded-xl border border-border bg-surface p-3">
              <GrantSummary scopes={k.scopes || []} catalog={catalog} resourceIds={k.resource_ids} />
            </div>
          )}
        </div>

        <div>
          <div className="mb-2"><span className="text-[10px] font-semibold uppercase tracking-wider text-tertiary">{t('Details')}</span></div>
          <div className="space-y-2 rounded-xl border border-border bg-surface p-3">
            <Fact icon={ClockIcon} label={t('Created')}>
              {k.created ? <span title={formatDate(k.created)}>{formatRelativeTime(k.created)}</span> : '—'}
            </Fact>
            <Fact icon={ClockIcon} label={t('Last used')}>{k.lastUsed ? formatRelativeTime(k.lastUsed) : t('Never')}</Fact>
            <Fact icon={ClockIcon} label={t('Expires')}>
              {k.expires_at ? <span title={formatDate(k.expires_at)}>{formatRelativeTime(k.expires_at)}</span> : t('Never')}
            </Fact>
          </div>
        </div>

        <p className="text-[11px] leading-relaxed text-tertiary">
          {t('Anything not listed above is refused — including endpoints added later. The secret is shown once at creation and never again; delete and recreate if it leaks.')}
        </p>
      </div>
    </div>
  );
};

// ── page ───────────────────────────────────────────────────────────────────

export const ApiKeys: React.FC = () => {
  const { t } = useTranslation();
  useRequireAuth();
  // The self-host shell is a fixed two-column shelf (no narrow master->detail
  // swap), so selection is always live and the detail column is always mounted.
  const isWide = true;

  const [catalog, setCatalog] = useState<ScopeCatalog>(FALLBACK_CATALOG);
  const [modalOpen, setModalOpen] = useState(false);
  const [newKeyModalOpen, setNewKeyModalOpen] = useState(false);
  const [newKeyData, setNewKeyData] = useState<any>(null);
  const [savingKeyId, setSavingKeyId] = useState<number | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ApiKey | null>(null);
  const [resourceItems, setResourceItems] = useState<ResourceItems>({});

  const [view, setView] = useState<string>(() => localStorage.getItem('writ.keysView') || 'all');
  const [sortBy, setSortBy] = useState<'created' | 'used' | 'name'>(() => {
    const v = localStorage.getItem('writ.keysSort');
    return (['created', 'used', 'name'] as const).includes(v as any) ? (v as any) : 'created';
  });
  useEffect(() => { try { localStorage.setItem('writ.keysView', view); } catch { /* noop */ } }, [view]);
  useEffect(() => { try { localStorage.setItem('writ.keysSort', sortBy); } catch { /* noop */ } }, [sortBy]);

  const [selectedId, setSelectedId] = useState<number | null>(null);

  // ── create form ──
  const [formLabel, setFormLabel] = useState('');
  const [formPreset, setFormPreset] = useState<string | null>(null);
  const [formScopes, setFormScopes] = useState<Set<string>>(new Set());
  const [formIds, setFormIds] = useState<Record<string, number[]>>({});
  const [formExpiry, setFormExpiry] = useState<string>('90');
  const [creating, setCreating] = useState(false);

  // ── in-place edit ──
  const [editingKeyId, setEditingKeyId] = useState<number | null>(null);
  const [editScopes, setEditScopes] = useState<Set<string>>(new Set());
  const [editIds, setEditIds] = useState<Record<string, number[]>>({});

  const { data: apiKeys, loading, refresh } = useQuery(Q.apiKeys(), () => apiKeysApi.getAll(), { pollInterval: 10000 });

  // The vocabulary comes from the server, so this screen can never offer a
  // permission the backend does not enforce — or miss one it does.
  useEffect(() => {
    apiKeysApi.getCatalog()
      .then((data) => { if (data?.resources?.length) setCatalog(data); })
      .catch(() => { /* FALLBACK_CATALOG stands in */ });
  }, []);

  useEffect(() => {
    Promise.all([
      automationApi.listWorkflows().catch(() => []),
      targetsApi.getAll().catch(() => []),
    ]).then(([workflows, targets]) => {
      setResourceItems({
        workflows: (workflows as any[]).map((w) => ({ id: w.id, name: w.name })),
        monitors: (targets as any[]).map((tgt) => ({ id: Number(tgt.id), name: tgt.url })),
      });
    });
  }, []);

  // Scope-set mutation, shared by the create form and the in-place editor.
  const makeSetters = useCallback(
    (
      setScopes: React.Dispatch<React.SetStateAction<Set<string>>>,
      setIds: React.Dispatch<React.SetStateAction<Record<string, number[]>>>,
      onChange?: () => void,
    ) => ({
      toggleScope: (scope: string) => {
        setScopes((prev) => {
          const next = new Set(prev);
          if (next.has(scope)) next.delete(scope); else next.add(scope);
          return next;
        });
        onChange?.();
      },
      // Turning a resource ON grants read where it exists, else its first action —
      // never everything. Escalation should be a deliberate second click.
      toggleResource: (resourceKey: string, on: boolean) => {
        const resource = catalog.resources.find((r) => r.key === resourceKey);
        if (!resource) return;
        setScopes((prev) => {
          const next = new Set(prev);
          for (const a of resource.actions) next.delete(scopeString(resourceKey, a));
          if (on) {
            const first = resource.actions.includes('read') ? 'read' : resource.actions[0];
            next.add(scopeString(resourceKey, first));
          }
          return next;
        });
        if (!on) setIds((prev) => { const next = { ...prev }; delete next[resourceKey]; return next; });
        onChange?.();
      },
      togglePin: (resourceKey: string, id: number) => {
        setIds((prev) => {
          const current = prev[resourceKey] || [];
          const nextIds = current.includes(id) ? current.filter((i) => i !== id) : [...current, id];
          const out = { ...prev };
          if (nextIds.length) out[resourceKey] = nextIds; else delete out[resourceKey];
          return out;
        });
        onChange?.();
      },
      clearPins: (resourceKey: string) => {
        setIds((prev) => { const next = { ...prev }; delete next[resourceKey]; return next; });
        onChange?.();
      },
    }),
    [catalog],
  );

  const formSetters = useMemo(
    () => makeSetters(setFormScopes, setFormIds, () => setFormPreset(null)),
    [makeSetters],
  );
  const editSetters = useMemo(() => makeSetters(setEditScopes, setEditIds), [makeSetters]);

  const openCreateModal = useCallback(() => {
    setFormLabel('');
    setFormPreset('run');
    setFormScopes(new Set(catalog.presets.find((p) => p.key === 'run')?.scopes || []));
    setFormIds({});
    setFormExpiry('90');
    setModalOpen(true);
  }, [catalog]);

  const applyPreset = useCallback((presetKey: string) => {
    const preset = catalog.presets.find((p) => p.key === presetKey);
    if (!preset) return;
    setFormPreset(presetKey);
    setFormScopes(new Set(preset.scopes));
    setFormIds({});
  }, [catalog]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (creating) return;
    if (!formLabel.trim()) { toast.error(t('Give the key a name so you can recognise it later')); return; }
    if (formScopes.size === 0) { toast.error(t('Pick at least one thing this key may do')); return; }
    setCreating(true);
    try {
      // Send the preset by NAME when one is selected: the server expands it, so a
      // preset means the same thing in every app and cannot drift here.
      const payload: any = { label: formLabel.trim() };
      if (formPreset) payload.preset = formPreset;
      else { payload.scopes = Array.from(formScopes); payload.resource_ids = formIds; }
      const expires = expiryToIso(formExpiry);
      if (expires) payload.expires_at = expires;

      const response: any = await apiKeysApi.create(payload);
      const key = response?.api_key || response?.key;
      if (key) {
        setNewKeyData(response);
        setNewKeyModalOpen(true);
      } else {
        toast.error(t('Key created, but the secret was not returned — delete it and try again'));
      }
      refresh();
      setModalOpen(false);
    } catch (error: any) {
      toast.error(apiErrorMessage(error, t('Failed to create API key')));
    } finally { setCreating(false); }
  };

  const startEdit = useCallback((k: ApiKey) => {
    setEditingKeyId(k.id);
    setEditScopes(new Set(k.scopes || []));
    setEditIds({ ...(k.resource_ids || {}) });
  }, []);
  const cancelEdit = useCallback(() => { setEditingKeyId(null); }, []);

  const handleSaveEdit = async (k: ApiKey) => {
    if (savingKeyId) return;
    setSavingKeyId(k.id);
    try {
      await apiKeysApi.update(k.id, { scopes: Array.from(editScopes), resource_ids: editIds });
      toast.success(t('Permissions updated'));
      setEditingKeyId(null);
      refresh();
    } catch (error: any) {
      toast.error(apiErrorMessage(error, t('Failed to update')));
    } finally { setSavingKeyId(null); }
  };

  const handleDelete = async () => {
    if (!deleteTarget || savingKeyId) return;
    const id = deleteTarget.id;
    setSavingKeyId(id);
    try {
      await apiKeysApi.revoke(id);
      toast.success(t('API key deleted'));
      if (editingKeyId === id) setEditingKeyId(null);
      setDeleteTarget(null);
      refresh();
    } catch { toast.error(t('Failed to delete')); }
    finally { setSavingKeyId(null); }
  };

  const copyText = (text: string) => { navigator.clipboard.writeText(text); toast.success(t('Copied')); };

  const all: ApiKey[] = apiKeys || [];
  const hasKeys = all.length > 0;
  const views = apiKeysConfig.views;
  const viewCounts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const v of views) m[v.id] = all.filter(v.predicate).length;
    return m;
  }, [all, views]);
  const activeView = views.find((v) => v.id === view) || views[0];
  const visibleKeys = useMemo(() => {
    const arr = [...all.filter(activeView.predicate)];
    if (sortBy === 'name') arr.sort((a, b) => (a.label || '').localeCompare(b.label || ''));
    else if (sortBy === 'used') arr.sort((a, b) => (b.lastUsed ? new Date(b.lastUsed).getTime() : 0) - (a.lastUsed ? new Date(a.lastUsed).getTime() : 0));
    else arr.sort((a, b) => (b.created ? new Date(b.created).getTime() : 0) - (a.created ? new Date(a.created).getTime() : 0));
    return arr;
  }, [all, activeView, sortBy]);

  // The detail column is always mounted, so a selection always has to resolve to
  // something: fall back to the first visible key when nothing has been clicked
  // yet or the stored pick was filtered out. Derived during render rather than
  // pushed into state from an effect — an effect's setState only lands after
  // paint (`react-hooks/set-state-in-effect`), so the pane would flash its empty
  // state for a frame on every filter change.
  const selected = visibleKeys.find((k) => k.id === selectedId) || (isWide ? visibleKeys[0] ?? null : null);
  const activeId = selected?.id ?? null;

  // Moving off a key drops any in-place edit, for the same reason: doing it in an
  // effect would let the new key paint in edit mode for a frame first.
  const [editAnchorId, setEditAnchorId] = useState<number | null>(null);
  if (editAnchorId !== activeId) {
    setEditAnchorId(activeId);
    if (editingKeyId !== null) setEditingKeyId(null);
  }

  const previewScopes = useMemo(() => Array.from(formScopes), [formScopes]);

  return (
    <>
      {loading && !apiKeys ? (
        <ShelfSkeleton label={t('Loading API keys')} rows={5} />
      ) : !hasKeys ? (
        <EmptyHero
          icon={KeyIcon}
          title={t('No API keys yet')}
          description={t('A key lets your own scripts and tools act on this account. Start from what the key is for — read your data, run your automations, or full access — then narrow it.')}
          className="h-full bg-surface"
        >
          <Button onClick={openCreateModal} size="sm">{t('Create API Key')}</Button>
        </EmptyHero>
      ) : (
        <div className={clsx('flex h-full', SHELF_CONTAINER)}>
          <div className={SHELF_LIST_COL}>
            <div className="shrink-0 border-b border-border px-3 py-2.5 space-y-2">
              <div className="flex flex-wrap items-center gap-1">
                {views.map((v) => {
                  const Icon = v.icon; const active = v.id === view; const count = viewCounts[v.id] ?? 0;
                  if (count === 0 && v.id !== 'all' && !active) return null;
                  return (
                    <button key={v.id} onClick={() => setView(v.id)} aria-pressed={active} className={shelfFilterChipClass(active)}>
                      <Icon className="w-3 h-3" />{t(v.label)}
                      <span className={shelfFilterCountClass(active)}>{count}</span>
                    </button>
                  );
                })}
              </div>
              <div className="flex items-center justify-between gap-2">
                <Select<'created' | 'used' | 'name'>
                  size="sm" value={sortBy} onChange={setSortBy} aria-label={t('Sort by')} wrapperClassName="w-32"
                  options={[{ value: 'created', label: t('Newest') }, { value: 'used', label: t('Recently used') }, { value: 'name', label: t('Name A–Z') }]}
                />
                <button onClick={openCreateModal} className="flex items-center gap-1.5 px-2.5 py-1.5 bg-accent-strong text-accent-on text-[12px] font-medium rounded-lg hover:bg-accent-strong/90 transition-colors shrink-0">
                  <PlusIcon className="w-3.5 h-3.5" />{t('New')}
                </button>
              </div>
            </div>

            {visibleKeys.length === 0 ? (
              <div className="flex flex-1 flex-col items-center justify-center text-center px-6 py-10">
                <p className="text-[13px] font-medium text-ink">{t('Nothing here')}</p>
                <p className="text-[11px] text-secondary mt-1">{t('No keys match this filter.')}</p>
              </div>
            ) : (
              <ScrollArea className="flex-1" viewportClassName="px-2 py-2" fade="chrome">
                <div className="grid gap-0.5">
                  {visibleKeys.map((k) => {
                    const active = isWide && activeId === k.id;
                    return (
                      <div
                        key={k.id}
                        role="button"
                        tabIndex={0}
                        aria-pressed={active}
                        onClick={() => { setSelectedId(k.id); }}
                        onMouseDown={shelfRowMouseDown}
                        onKeyDown={(e) => { if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) { e.preventDefault(); setSelectedId(k.id); } }}
                        className={shelfRowClass(active)}
                      >
                        {active && <ShelfAccentBar />}
                        <div style={tintStyle('neutral')} className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0">
                          <KeyIcon className="h-4 w-4" />
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="text-[13px] font-medium text-ink truncate" title={k.label}>{k.label}</div>
                          <div className="flex items-center gap-1 text-[11px] truncate mt-0.5">
                            <span className="text-tertiary shrink-0">{grantLabelFor(k, catalog, t)}</span>
                            <span className="text-tertiary/50 shrink-0">·</span>
                            <span className="text-tertiary shrink-0">
                              {k.lastUsed ? t('used {{ago}}', { ago: formatRelativeTime(k.lastUsed) }) : t('never used')}
                            </span>
                          </div>
                        </div>
                        <button
                          onClick={(e) => { e.stopPropagation(); setDeleteTarget(k); }}
                          disabled={savingKeyId === k.id}
                          title={t('Delete')}
                          className="flex items-center justify-center w-7 h-7 rounded-lg text-tertiary hover:bg-chrome hover:text-danger transition-colors disabled:opacity-50 shrink-0 opacity-0 group-hover:opacity-100"
                        >
                          <TrashIcon className="h-4 w-4" />
                        </button>
                      </div>
                    );
                  })}
                </div>
              </ScrollArea>
            )}
          </div>

          <div className={SHELF_DETAIL_COL}>
            <SwapFade swapKey={selected?.id ?? 'none'} className="flex flex-1 flex-col min-w-0 min-h-0">
              <KeyDetailPane
                k={selected}
                catalog={catalog}
                resourceItems={resourceItems}
                onDelete={setDeleteTarget}
                editing={!!selected && editingKeyId === selected.id}
                editScopes={editScopes}
                editIds={editIds}
                editSetters={editSetters}
                onStartEdit={startEdit}
                onCancelEdit={cancelEdit}
                onSaveEdit={handleSaveEdit}
                saving={!!selected && savingKeyId === selected.id}
              />
            </SwapFade>
          </div>
        </div>
      )}

      {/* ── Create ──
          Laid out to fit without scrolling in the common case: name and expiry
          share a row, the four purposes are a single segmented row (they were
          four stacked cards), and the live grant preview wraps as chips rather
          than stacking one line per resource. The scope editor only appears for a
          custom grant and scrolls inside its own box, so the dialog's height is
          bounded no matter how many resources the catalogue grows to. */}
      <Modal
        isOpen={modalOpen}
        onClose={() => setModalOpen(false)}
        title={t('Create API Key')}
        subtitle={t('Give a script or tool its own access to this account.')}
        size="lg"
        footer={
          <div className="flex justify-end gap-2">
            <Button type="button" variant="secondary" size="sm" onClick={() => setModalOpen(false)}>{t('Cancel')}</Button>
            <Button type="submit" form="create-api-key" size="sm" disabled={creating || formScopes.size === 0} loading={creating}>
              {creating ? t('Creating...') : t('Create key')}
            </Button>
          </div>
        }
      >
        <form id="create-api-key" onSubmit={handleSubmit} className="space-y-3">
          <div className="flex items-end gap-2">
            {/* Input renders its own bare wrapper div, so the flex sizing goes here. */}
            <div className="flex-1 min-w-0">
              <Input
                label={t('Name')}
                value={formLabel}
                onChange={(e) => setFormLabel(e.target.value)}
                placeholder={t('e.g. Reporting script')}
                required
              />
            </div>
            <Select<string>
              label={t('Expires')}
              value={formExpiry}
              onChange={setFormExpiry}
              wrapperClassName="w-32 shrink-0"
              options={EXPIRY_OPTIONS.map((o) => ({ value: o.value, label: o.label() }))}
            />
          </div>

          <div>
            <label className="block text-[11px] font-medium text-secondary mb-1.5">{t('What is this key for?')}</label>
            <div className="grid grid-cols-4 gap-1">
              {catalog.presets.map((preset) => {
                const Icon = PRESET_ICONS[preset.key] || ShieldCheckIcon;
                const active = formPreset === preset.key;
                return (
                  <button
                    key={preset.key}
                    type="button"
                    onClick={() => applyPreset(preset.key)}
                    title={t(preset.description)}
                    className={clsx(
                      'flex flex-col items-center gap-1 px-2 py-2 rounded-lg border transition-colors',
                      active ? 'border-ink bg-hover' : 'border-border hover:border-ink/20',
                    )}
                  >
                    <Icon className={clsx('h-4 w-4', active ? 'text-ink' : 'text-tertiary')} />
                    <span className={clsx('text-[11px] font-medium text-center leading-tight', active ? 'text-ink' : 'text-secondary')}>
                      {t(preset.label)}
                    </span>
                  </button>
                );
              })}
              <button
                type="button"
                onClick={() => setFormPreset(null)}
                title={t('Choose exactly which things this key may touch, and which items.')}
                className={clsx(
                  'flex flex-col items-center gap-1 px-2 py-2 rounded-lg border transition-colors',
                  formPreset === null ? 'border-ink bg-hover' : 'border-border hover:border-ink/20',
                )}
              >
                <AdjustmentsHorizontalIcon className={clsx('h-4 w-4', formPreset === null ? 'text-ink' : 'text-tertiary')} />
                <span className={clsx('text-[11px] font-medium text-center leading-tight', formPreset === null ? 'text-ink' : 'text-secondary')}>
                  {t('Custom')}
                </span>
              </button>
            </div>
            {/* One description line for the CHOSEN purpose, rather than four
                descriptions competing for attention at once. */}
            <p className="mt-1.5 text-[11px] text-tertiary leading-snug">
              {formPreset === null
                ? t('Choose exactly which things this key may touch, and which items.')
                : t(catalog.presets.find((p) => p.key === formPreset)?.description ?? '')}
            </p>
          </div>

          {/* The editor only appears for a custom grant — a preset is a decision,
              not a starting point to be silently edited under the same label. */}
          <Expand open={formPreset === null} mountOnEnter>
            <div className="max-h-[34vh] overflow-y-auto">
              <ScopeEditor
                catalog={catalog}
                scopes={formScopes}
                resourceIds={formIds}
                onToggleScope={formSetters.toggleScope}
                onToggleResource={formSetters.toggleResource}
                onTogglePin={formSetters.togglePin}
                onClearPins={formSetters.clearPins}
                resourceItems={resourceItems}
              />
            </div>
          </Expand>

          <div className="rounded-lg border border-border bg-hover/40 px-2.5 py-2">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-tertiary mb-1.5">{t('This key will be able to')}</div>
            <GrantSummary scopes={previewScopes} catalog={catalog} resourceIds={formIds} variant="chips" />
          </div>
        </form>
      </Modal>

      {/* ── Created ── */}
      <Modal
        isOpen={newKeyModalOpen}
        onClose={() => {}}
        title={t('API Key Created')}
        subtitle={t("Copy it now — it is never shown again.")}
        size="lg"
      >
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <code className="flex-1 min-w-0 bg-ink text-surface px-3 py-2 rounded-lg text-[13px] font-mono break-all">{newKeyData?.api_key}</code>
            <Button size="sm" onClick={() => { if (newKeyData?.api_key) copyText(newKeyData.api_key); }}>
              <ClipboardDocumentIcon className="h-4 w-4" />
            </Button>
          </div>
          {newKeyData?.scopes?.length > 0 && (
            <div className="rounded-lg border border-border bg-hover/40 px-2.5 py-2">
              <div className="text-[10px] font-semibold uppercase tracking-wider text-tertiary mb-1.5">{t('It can')}</div>
              <GrantSummary scopes={newKeyData.scopes} catalog={catalog} resourceIds={newKeyData.resource_ids} variant="chips" />
            </div>
          )}
          <Button variant="secondary" size="sm" onClick={() => { setNewKeyModalOpen(false); setNewKeyData(null); }} className="w-full">
            {t('Done')}
          </Button>
        </div>
      </Modal>

      {/* ── Delete ── */}
      <Modal isOpen={!!deleteTarget} onClose={() => setDeleteTarget(null)} title={t('Delete API key')}>
        <p className="text-sm text-secondary mb-1">{t('Delete')} <strong className="text-ink">{deleteTarget?.label}</strong>?</p>
        <p className="text-xs text-tertiary mb-4">{t('This cannot be undone. Any integration using this key will immediately stop working.')}</p>
        <div className="flex items-center justify-end gap-2">
          <Button variant="secondary" size="sm" onClick={() => setDeleteTarget(null)}>{t('Cancel')}</Button>
          <Button size="sm" onClick={handleDelete} loading={deleteTarget ? savingKeyId === deleteTarget.id : false}>{t('Delete key')}</Button>
        </div>
      </Modal>
    </>
  );
};

export default ApiKeys;
