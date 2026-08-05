import React, { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Dialog, Transition } from '@headlessui/react';
import { useRecorderActivity } from './RecorderActivityContext';
import {
  MagnifyingGlassIcon,
  HomeIcon,
  EyeIcon,
  CursorArrowRaysIcon,
  PlayIcon,
  BoltIcon,
  KeyIcon,
  ServerStackIcon,
  LockClosedIcon,
  IdentificationIcon,
  CpuChipIcon,
  Cog6ToothIcon,
  DocumentDuplicateIcon,
  SignalIcon,
} from '@heroicons/react/24/outline';
import { useTranslation } from 'react-i18next';
import { useQuery } from '../hooks/useQuery';
import { Q } from '../stores/queryKeys';
import { commandPaletteApi } from '../api/commandPalette';
import i18n from '../i18n';

// ── Static page navigation ──────────────────────────────────────────────────

interface PageEntry {
  name: string;
  href: string;
  icon: React.ElementType;
  keywords?: string[];
}

const PAGES: PageEntry[] = [
  { name: 'Home', href: '/', icon: HomeIcon, keywords: ['dashboard', 'overview'] },
  { name: 'Monitors', href: '/checks', icon: EyeIcon, keywords: ['checks', 'check', 'monitor', 'targets', 'surveil'] },
  { name: 'Workflows', href: '/workflows', icon: CursorArrowRaysIcon, keywords: ['recordings', 'flows'] },
  { name: 'Streaming', href: '/streaming', icon: SignalIcon, keywords: ['live', 'session', 'browser'] },
  { name: 'Runs', href: '/runs', icon: PlayIcon, keywords: ['executions', 'tasks', 'history'] },
  { name: 'Automations', href: '/automations', icon: BoltIcon, keywords: ['triggers', 'flows'] },
  { name: 'API Keys', href: '/keys', icon: KeyIcon, keywords: ['tokens', 'api'] },
  { name: 'Endpoints', href: '/developers/endpoints', icon: ServerStackIcon, keywords: ['webhooks', 'hooks', 'callbacks', 'mcp', 'model context protocol', 'rest', 'servers', 'developers'] },
  { name: 'Secrets', href: '/secrets', icon: LockClosedIcon, keywords: ['credentials', 'vault', 'passwords'] },
  { name: 'Personas', href: '/personas', icon: IdentificationIcon, keywords: ['identities', 'profiles'] },
  { name: 'Files', href: '/files', icon: DocumentDuplicateIcon, keywords: ['assets', 'uploads', 'documents'] },
  { name: 'Fleet', href: '/fleet', icon: CpuChipIcon, keywords: ['nodes', 'runners', 'agents', 'workers'] },
  { name: 'Settings', href: '/settings', icon: Cog6ToothIcon, keywords: ['preferences', 'account'] },
];

// ── Result model ────────────────────────────────────────────────────────────

interface PaletteResult {
  key: string;
  group: string;
  title: string;
  subtitle?: string;
  icon: React.ElementType;
  href: string;
}

const ENTITY_LIMIT = 5;

const matches = (q: string, ...fields: (string | undefined)[]) =>
  fields.some((f) => f && f.toLowerCase().includes(q));

// Small debounce helper for the search input.
function useDebouncedValue<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

// ── Component ───────────────────────────────────────────────────────────────

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  /** Called when the global Cmd+K / Ctrl+K hotkey fires while closed. */
  onOpen?: () => void;
}

export const CommandPalette: React.FC<CommandPaletteProps> = ({ open, onClose, onOpen }) => {
  const { t } = useTranslation();
  // Route palette jumps through the recording guard so leaving a live recorder
  // via ⌘K confirms first, exactly like a sidebar click.
  const { guardedNavigate } = useRecorderActivity();
  const [query, setQuery] = useState('');
  // Raw highlight index. The index actually rendered is clamped to the result
  // list below, so it can never point past the last row.
  const [rawActiveIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const debouncedQuery = useDebouncedValue(query, 150);
  const q = debouncedQuery.trim().toLowerCase();

  // Global hotkey — Cmd+K / Ctrl+K toggles the palette.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        if (open) onClose();
        else onOpen?.();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [open, onClose, onOpen]);

  // Reset on open — adjusted during render (the documented reset pattern) rather
  // than in an effect, so the palette's very first painted frame is already
  // empty instead of briefly showing the previous session's query.
  const [prevOpen, setPrevOpen] = useState(open);
  if (prevOpen !== open) {
    setPrevOpen(open);
    if (open) {
      setQuery('');
      setActiveIndex(0);
    }
  }

  // Entity data — same query keys + endpoints as the list pages, so the
  // shared zustand cache is reused (instant results after visiting a list).
  const { data: checks, loading: checksLoading, error: checksError } = useQuery(
    Q.targets('content'),
    () => commandPaletteApi.listChecks(),
    { enabled: open }
  );
  const { data: workflows, loading: workflowsLoading, error: workflowsError } = useQuery(
    Q.workflows(),
    () => commandPaletteApi.listWorkflows(),
    { enabled: open }
  );
  const { data: automations, loading: automationsLoading, error: automationsError } = useQuery(
    Q.triggers(),
    () => commandPaletteApi.listAutomations(),
    { enabled: open }
  );

  const entityLoading = checksLoading || workflowsLoading || automationsLoading;
  const entityError =
    !!checksError && !!workflowsError && !!automationsError
      ? t('Could not load search results')
      : null;

  // Build flat, grouped result list
  const results = useMemo<PaletteResult[]>(() => {
    const out: PaletteResult[] = [];

    const pages = q
      ? PAGES.filter((p) => matches(q, p.name, i18n.t(p.name), ...(p.keywords || [])))
      : PAGES;
    pages.forEach((p) =>
      out.push({ key: `page:${p.href}`, group: i18n.t('Pages'), title: i18n.t(p.name), icon: p.icon, href: p.href })
    );

    // Entity results only when searching
    if (q) {
      (checks || [])
        .filter((c: any) => matches(q, c.name, c.url))
        .slice(0, ENTITY_LIMIT)
        .forEach((c: any) =>
          out.push({
            key: `check:${c.id}`,
            group: i18n.t('Monitors'),
            title: c.name || c.url,
            subtitle: c.name ? c.url : undefined,
            icon: EyeIcon,
            href: `/checks/${c.id}`,
          })
        );

      (workflows || [])
        .filter((w: any) => matches(q, w.name, w.entry_url))
        .slice(0, ENTITY_LIMIT)
        .forEach((w: any) =>
          out.push({
            key: `workflow:${w.id}`,
            group: i18n.t('Workflows'),
            title: w.name,
            subtitle: w.workflow_type,
            icon: CursorArrowRaysIcon,
            href: `/workflows/${w.id}`,
          })
        );

      (automations || [])
        .filter((a: any) => matches(q, a.name))
        .slice(0, ENTITY_LIMIT)
        .forEach((a: any) =>
          out.push({
            key: `automation:${a.id}`,
            group: i18n.t('Automations'),
            title: a.name || i18n.t('Automation #{{id}}', { id: a.id }),
            subtitle: a.enabled ? i18n.t('Active') : i18n.t('Paused'),
            icon: BoltIcon,
            href: `/automations/${a.id}`,
          })
        );
    }

    return out;
  }, [q, checks, workflows, automations]);

  // Reset the highlight to the first row whenever the (debounced) query changes.
  const [prevQ, setPrevQ] = useState(q);
  if (prevQ !== q) {
    setPrevQ(q);
    setActiveIndex(0);
  }

  // Clamp during render instead of in an effect: the highlight is a pure
  // function of the raw index and the current result count, so a shrinking list
  // never paints a frame with the highlight past the end.
  const activeIndex = results.length ? Math.min(rawActiveIndex, results.length - 1) : 0;

  // Keep the active row visible
  useEffect(() => {
    const el = listRef.current?.querySelector(`[data-index="${activeIndex}"]`);
    el?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex]);

  const select = useCallback(
    (result: PaletteResult) => {
      onClose();
      guardedNavigate(result.href);
    },
    [guardedNavigate, onClose]
  );

  const handleKeyDown = (e: React.KeyboardEvent) => {
    // Step from the CLAMPED index — the raw one may sit past the last row after
    // the result list shrank.
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIndex(results.length ? (activeIndex + 1) % results.length : 0);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIndex(results.length ? (activeIndex - 1 + results.length) % results.length : 0);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const result = results[activeIndex];
      if (result) select(result);
    }
    // Escape is handled by the headlessui Dialog (onClose)
  };

  // Render rows with group headers, keeping a flat index for keyboard nav
  let lastGroup: string | null = null;

  return (
    <Transition appear show={open} as={Fragment}>
      <Dialog as="div" className="relative z-50" onClose={onClose} initialFocus={inputRef}>
        <Transition.Child
          as={Fragment}
          enter="ease-out duration-200"
          enterFrom="opacity-0"
          enterTo="opacity-100"
          leave="ease-in duration-150"
          leaveFrom="opacity-100"
          leaveTo="opacity-0"
        >
          <div className="fixed inset-0 bg-black/20 backdrop-blur-sm" />
        </Transition.Child>

        <div className="fixed inset-0 overflow-y-auto">
          <div className="flex min-h-full items-start justify-center p-4 pt-[12vh]">
            <Transition.Child
              as={Fragment}
              enter="ease-out duration-200"
              enterFrom="opacity-0 scale-[0.98]"
              enterTo="opacity-100 scale-100"
              leave="ease-in duration-150"
              leaveFrom="opacity-100 scale-100"
              leaveTo="opacity-0 scale-[0.98]"
            >
              <Dialog.Panel
                className="w-full max-w-lg bg-surface rounded-xl shadow-lg overflow-hidden"
                onKeyDown={handleKeyDown}
              >
                {/* Search input */}
                <div className="flex items-center gap-3 px-4 border-b border-border">
                  <MagnifyingGlassIcon className="w-4 h-4 text-tertiary shrink-0" />
                  <input
                    ref={inputRef}
                    type="text"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder={t('Search pages, monitors, workflows, automations...')}
                    className="flex-1 py-3.5 text-sm bg-transparent text-ink focus:outline-none placeholder:text-tertiary"
                    autoComplete="off"
                    spellCheck={false}
                  />
                  <kbd className="text-[10px] text-tertiary font-mono shrink-0">esc</kbd>
                </div>

                {/* Results */}
                <div ref={listRef} className="max-h-[320px] overflow-y-auto py-1.5">
                  {results.map((result, index) => {
                    const header =
                      result.group !== lastGroup ? (
                        <div className="px-4 pt-2.5 pb-1 text-[10px] font-semibold uppercase tracking-wider text-tertiary">
                          {result.group}
                        </div>
                      ) : null;
                    lastGroup = result.group;
                    const active = index === activeIndex;
                    const Icon = result.icon;
                    return (
                      <Fragment key={result.key}>
                        {header}
                        <button
                          type="button"
                          data-index={index}
                          onClick={() => select(result)}
                          onMouseEnter={() => setActiveIndex(index)}
                          className={`flex items-center gap-2.5 w-full px-4 py-2 text-left text-[13px] transition-colors ${
                            active ? 'bg-hover text-ink' : 'text-secondary'
                          }`}
                        >
                          <Icon className={`w-4 h-4 shrink-0 ${active ? 'text-ink' : 'text-tertiary'}`} />
                          <span className="truncate">{result.title}</span>
                          {result.subtitle && (
                            <span className="text-[11px] text-tertiary truncate shrink min-w-0">
                              {result.subtitle}
                            </span>
                          )}
                        </button>
                      </Fragment>
                    );
                  })}

                  {/* Loading state — entity lists still fetching */}
                  {q && entityLoading && (
                    <div className="px-4 py-2 text-[12px] text-tertiary">{t('Searching…')}</div>
                  )}

                  {/* Error state */}
                  {q && !entityLoading && entityError && results.length === 0 && (
                    <div className="px-4 py-2 text-[12px] text-tertiary">{entityError}</div>
                  )}

                  {/* Empty state */}
                  {q && !entityLoading && !entityError && results.length === 0 && (
                    <div className="flex flex-col items-center justify-center py-8 text-center">
                      <div className="w-9 h-9 rounded-xl bg-hover border border-border flex items-center justify-center mb-2">
                        <MagnifyingGlassIcon className="h-4 w-4 text-tertiary" />
                      </div>
                      <p className="text-[12px] font-medium text-ink">{t('No results')}</p>
                      <p className="text-[11px] text-tertiary mt-0.5">
                        {t('Nothing matches "{{query}}"', { query: debouncedQuery.trim() })}
                      </p>
                    </div>
                  )}
                </div>

                {/* Footer hints */}
                <div className="flex items-center gap-3 px-4 py-2 border-t border-border">
                  <span className="text-[10px] text-tertiary">
                    <kbd className="font-mono text-tertiary">↑↓</kbd> {t('navigate')}
                  </span>
                  <span className="text-[10px] text-tertiary">
                    <kbd className="font-mono text-tertiary">↵</kbd> {t('open')}
                  </span>
                  <span className="text-[10px] text-tertiary">
                    <kbd className="font-mono text-tertiary">esc</kbd> {t('close')}
                  </span>
                </div>
              </Dialog.Panel>
            </Transition.Child>
          </div>
        </div>
      </Dialog>
    </Transition>
  );
};
