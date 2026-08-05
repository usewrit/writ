import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { GlobeAltIcon, PlusIcon, TrashIcon, CheckIcon, MinusIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useRequireAuth } from '../../hooks/useAuth';
import { useDocumentTitle } from '../../hooks/useDocumentTitle';
import { useQuery } from '../../hooks/useQuery';
import { useQueryCache } from '../../stores/queryCache';
import { Q } from '../../stores/queryKeys';
import { crawlApi, crawlAgentsWorking, isCrawlTerminal, type CrawlView } from '../../api/crawl';
import { formatElapsedShort } from '../../utils/format';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { Button } from '../../components/ui/Button';
import { ScrollArea } from '../../components/ui/ScrollArea';
import { Stagger, SwapFade } from '../../components/ui/Animated';
import {
  SHELF_CONTAINER,
  SHELF_LIST_COL,
  SHELF_DETAIL_COL,
  SHELF_TOPBAR,
  ShelfListSearch,
  ShelfSkeleton,
  shelfRowClass,
  shelfRowMouseDown,
  ShelfAccentBar,
  shelfFilterChipClass,
  shelfFilterCountClass,
} from '../../components/library/shelf';
import { CrawlStatusChip, CrawlProgressBar, crawlStatusMeta } from '../../components/dragnet/CrawlStatus';
import { CrawlDetailView } from '../../components/dragnet/CrawlDetailView';
import { AuthImage } from '../../components/common/AuthImage';
import { EmptyHero } from '../../components/ui';

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return url;
  }
}

/**
 * True once THIS page's own root is at least as wide as `@split/stage` (860px)
 * — mirrors SHELF_DETAIL_COL's own breakpoint (shelf.tsx: `hidden @split/stage:flex`),
 * so the row-click select-inline-vs-navigate decision matches what the CSS
 * actually shows. Measures the root node's own box via ResizeObserver, NOT
 * `window.matchMedia` — the window can stay wide while the card this page
 * renders inside (a sidebar sits beside it) is narrow, which used to leave
 * rows selecting into a detail pane that CSS had already hidden.
 */
function useIsWide(ref: React.RefObject<HTMLElement>): boolean {
  const [wide, setWide] = useState(true);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? el.clientWidth;
      setWide(w >= 860);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return wide;
}

type CrawlFilter = 'all' | 'running' | 'completed' | 'failed';

// A crawl is one site, so its glyph is that site's favicon — served same-origin
// by the coordinator's crawl route (404→globe). Loaded via <AuthImage> (blob-
// fetched with the app token), so it never breaks the way a cross-site <img src>
// would. Falls back to the globe when the backend doesn't serve one.
const SiteGlyph: React.FC<{ crawl: CrawlView }> = ({ crawl }) => (
  <AuthImage
    src={crawl.favicon || undefined}
    alt=""
    className="h-full w-full rounded-lg object-cover"
    fallback={<GlobeAltIcon className="h-4 w-4 text-secondary" />}
  />
);

// Bulk-select checkbox (matches the WorkflowList DNA: ink-filled when on).
const SelectBox: React.FC<{
  checked: boolean;
  indeterminate?: boolean;
  onChange: () => void;
  label: string;
}> = ({ checked, indeterminate, onChange, label }) => (
  <button
    type="button"
    role="checkbox"
    aria-checked={indeterminate ? 'mixed' : checked}
    aria-label={label}
    onClick={(e) => { e.stopPropagation(); onChange(); }}
    className={clsx(
      'flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors',
      checked || indeterminate ? 'border-ink bg-ink text-surface' : 'border-border bg-surface hover:border-ink/40',
    )}
  >
    {indeterminate ? <MinusIcon className="h-3 w-3" /> : checked ? <CheckIcon className="h-3 w-3" /> : null}
  </button>
);

// ── Master-list row ─────────────────────────────────────────────────────────
// Globe tile + title/host, status pill, a live progress bar, and page counts.
// The self-hosted coordinator runs DETERMINISTIC crawls only, so there is no
// AI-executor badge.
const CrawlRow: React.FC<{
  crawl: CrawlView;
  selected: boolean;
  checked: boolean;
  onSelect: (c: CrawlView) => void;
  onToggleCheck: (c: CrawlView) => void;
}> = React.memo(({ crawl, selected, checked, onSelect, onToggleCheck }) => {
  const { t } = useTranslation();
  const meta = crawlStatusMeta(crawl.status);
  const total = crawl.pages_discovered || 0;
  const done = crawl.pages_done || 0;
  const working = crawlAgentsWorking(crawl);
  const title = crawl.name?.trim() || hostOf(crawl.seed_url);
  // Only terminal crawls can be removed, so only they flip to a checkbox.
  const checkable = isCrawlTerminal(crawl.status);

  return (
    <div
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      aria-label={title}
      onClick={() => onSelect(crawl)}
      onMouseDown={shelfRowMouseDown}
      onKeyDown={(e) => {
        if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) {
          e.preventDefault();
          onSelect(crawl);
        }
      }}
      className={shelfRowClass(selected)}
    >
      {selected && <ShelfAccentBar />}
      {meta.live && !selected && (
        <span aria-hidden className="absolute left-0 top-0 bottom-0 w-[3px] rounded-r-full bg-info" />
      )}
      {/* Leading tile — globe at rest; flips to a multi-select checkbox on hover /
          when checked (terminal crawls only, since a live crawl can't be removed). */}
      {checkable ? (
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); onToggleCheck(crawl); }}
          aria-label={t('Select crawl')}
          aria-pressed={checked}
          className="relative flex h-8 w-8 shrink-0 items-center justify-center rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-ink/20"
        >
          <span className={clsx(
            'absolute inset-0 flex items-center justify-center rounded-lg border transition-opacity',
            checked ? 'border-ink bg-ink text-surface opacity-100' : 'border-border bg-surface opacity-0 group-hover:opacity-100',
          )}>
            <CheckIcon className={clsx('h-3.5 w-3.5 transition-opacity', checked ? 'opacity-100' : 'opacity-0')} />
          </span>
          <span className={clsx(
            'absolute inset-0 flex items-center justify-center rounded-lg bg-hover transition-opacity',
            checked ? 'opacity-0' : 'opacity-100 group-hover:opacity-0',
          )}>
            <SiteGlyph crawl={crawl} />
          </span>
        </button>
      ) : (
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-hover">
          <SiteGlyph crawl={crawl} />
        </span>
      )}
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          {/* Name: truncates at rest; marquees on row hover to reveal overflow
              (containerType lets the keyframe no-op when the name already fits). */}
          <span
            className="block min-w-0 flex-1 overflow-hidden text-[13px] font-medium text-ink"
            style={{ containerType: 'inline-size' }}
            title={title}
          >
            <span className="inline-block min-w-full whitespace-nowrap group-hover:[animation:marquee-hover_8s_ease-in-out_infinite]">
              {title}
            </span>
          </span>
          <CrawlStatusChip status={crawl.status} size="sm" className="shrink-0" />
        </span>
        <span className="mt-1 flex items-center gap-2">
          <span className="w-16 shrink-0 @pair/stage:w-24"><CrawlProgressBar crawl={crawl} size="sm" /></span>
          <span className="flex min-w-0 flex-1 items-center gap-1 text-[11px]">
            <span className="shrink-0 tabular-nums text-secondary">
              {total > 0
                ? t('{{done}} / {{total}}', { done, total })
                : meta.live ? t('Mapping…') : t('{{done}} pages', { done })}
            </span>
            {meta.live && working > 0 ? (
              <>
                <span className="shrink-0 text-tertiary/50">·</span>
                <span className="shrink-0 font-medium text-info-fg">{t('{{n}} working', { n: working })}</span>
              </>
            ) : !meta.live ? (
              <>
                <span className="shrink-0 text-tertiary/50">·</span>
                <span className="truncate text-tertiary">
                  {t('{{ago}} ago', { ago: formatElapsedShort(crawl.completed_at || crawl.created_at) })}
                </span>
              </>
            ) : null}
          </span>
        </span>
      </span>
    </div>
  );
});
CrawlRow.displayName = 'CrawlRow';

export const CrawlsPage: React.FC = () => {
  useRequireAuth();
  const { t } = useTranslation();
  const navigate = useNavigate();
  const rootRef = useRef<HTMLDivElement>(null);
  const isWide = useIsWide(rootRef);
  useDocumentTitle(t('Harvest'));

  const [filter, setFilter] = useState<CrawlFilter>('all');
  const [search, setSearch] = useState('');
  const [pickedId, setSelectedId] = useState<number | null>(null);

  const [poll, setPoll] = useState<number | undefined>(undefined);
  const { data: crawlsRaw, loading } = useQuery(Q.crawls(), () => crawlApi.list(), { pollInterval: poll });
  // Poll fast only while a crawl is still live. Adjusted during render (guarded,
  // so it converges in one extra pass) rather than in an effect, which would
  // commit the stale cadence before tearing it down.
  const wantPoll = (crawlsRaw || []).some((c) => !isCrawlTerminal(c.status)) ? 2500 : undefined;
  if (poll !== wantPoll) setPoll(wantPoll);

  const all = crawlsRaw || [];

  const counts = useMemo(() => {
    let running = 0, completed = 0, failed = 0;
    for (const c of all) {
      if (!isCrawlTerminal(c.status)) running++;
      else if (c.status === 'completed') completed++;
      else if (c.status === 'failed') failed++;
    }
    return { all: all.length, running, completed, failed };
  }, [all]);

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase();
    return all.filter((c) => {
      if (filter === 'running' && isCrawlTerminal(c.status)) return false;
      if (filter === 'completed' && c.status !== 'completed') return false;
      if (filter === 'failed' && c.status !== 'failed') return false;
      if (q) {
        const hay = `${c.name || ''} ${c.seed_url || ''}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [all, filter, search]);

  // Auto-select the first visible crawl on wide screens when nothing is chosen
  // (or the selection scrolled out of the current filter). DERIVED during render
  // rather than pushed through an effect, which would paint one frame with an
  // empty detail pane before correcting itself.
  const selectedId = useMemo(() => {
    if (!isWide) return pickedId;
    if (pickedId != null && visible.some((c) => c.id === pickedId)) return pickedId;
    return visible[0]?.id ?? null;
  }, [isWide, visible, pickedId]);

  const selected = useMemo(() => all.find((c) => c.id === selectedId) || null, [all, selectedId]);

  const goNew = () => navigate('/crawls/new');
  const onRowSelect = (c: CrawlView) => {
    // Wide: show the full detail in the pane. Narrow: no pane — go to the page.
    if (isWide) setSelectedId(c.id);
    else navigate(`/crawls/${c.id}`);
  };

  // Bulk select + remove — only terminal crawls are selectable (a live crawl
  // can't be removed until it's stopped), like any other list page.
  const [checkedIds, setCheckedIds] = useState<Set<number>>(new Set());
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const toggleCheck = useCallback((c: CrawlView) => {
    setCheckedIds((prev) => {
      const n = new Set(prev);
      if (n.has(c.id)) n.delete(c.id); else n.add(c.id);
      return n;
    });
  }, []);
  const clearChecks = useCallback(() => setCheckedIds(new Set()), []);
  const handleBulkDelete = async () => {
    const ids = Array.from(checkedIds);
    if (!ids.length || deleting) return;
    setDeleting(true);
    let ok = 0, fail = 0;
    for (const cid of ids) {
      try { await crawlApi.remove(cid); if (selectedId === cid) setSelectedId(null); ok++; }
      catch { fail++; }
    }
    setDeleting(false);
    setBulkDeleteOpen(false);
    clearChecks();
    useQueryCache.getState().invalidate(Q.crawls());
    if (fail === 0) toast.success(t('Removed {{n}}', { n: ok }));
    else toast.error(t('{{ok}} removed · {{fail}} couldn’t be removed', { ok, fail }));
  };

  const FILTERS: Array<{ id: CrawlFilter; label: string; count: number }> = [
    { id: 'all', label: t('All'), count: counts.all },
    { id: 'running', label: t('Running'), count: counts.running },
    { id: 'completed', label: t('Completed'), count: counts.completed },
    { id: 'failed', label: t('Failed'), count: counts.failed },
  ];

  // Select-all spans the removable (terminal) rows currently in view.
  const checkable = useMemo(() => visible.filter((c) => isCrawlTerminal(c.status)), [visible]);
  const allChecked = checkable.length > 0 && checkable.every((c) => checkedIds.has(c.id));
  const someChecked = checkedIds.size > 0 && !allChecked;
  const toggleAll = () => setCheckedIds(allChecked ? new Set() : new Set(checkable.map((c) => c.id)));

  if (loading && all.length === 0) {
    return <ShelfSkeleton withSearch label={t('Loading crawls')} />;
  }

  return (
    <div ref={rootRef} className="flex h-full flex-col">
      {/* Topbar */}
      <div className={SHELF_TOPBAR}>
        <GlobeAltIcon className="h-5 w-5 text-ink" />
        <span className="text-[13px] font-semibold text-ink">{t('Harvest')}</span>
        <div className="flex-1" />
        <Button size="sm" onClick={goNew} data-tour="new-crawl">
          <PlusIcon className="h-4 w-4" />
          {t('New crawl')}
        </Button>
      </div>

      {/* Two-pane shelf */}
      <div className={SHELF_CONTAINER}>
        {/* Master list */}
        <div className={SHELF_LIST_COL}>
          <div className="shrink-0 space-y-2 border-b border-border px-3 py-2.5">
            <ShelfListSearch value={search} onChange={setSearch} ariaLabel={t('Search crawls')} placeholder={t('Search crawls…')} />
            <div className="flex flex-wrap items-center gap-1">
              {FILTERS.map((f) => {
                const active = filter === f.id;
                if (f.count === 0 && f.id !== 'all' && !active) return null;
                return (
                  <button key={f.id} onClick={() => setFilter(f.id)} aria-pressed={active} className={shelfFilterChipClass(active)}>
                    {f.label}
                    <span className={shelfFilterCountClass(active)}>{f.count}</span>
                  </button>
                );
              })}
            </div>
            <span className="block text-[11px] text-tertiary">{t('{{n}} shown', { n: visible.length })}</span>
          </div>

          {/* Bulk action bar — appears once any crawl is selected. */}
          {checkedIds.size > 0 && (
            <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border bg-chrome px-3 py-2 bulkbar-enter">
              <div className="flex items-center gap-2 text-[12px]">
                <SelectBox checked={allChecked} indeterminate={someChecked} onChange={toggleAll} label={t('Select all')} />
                <span className="font-medium text-ink">{t('{{n}} selected', { n: checkedIds.size })}</span>
                <button onClick={clearChecks} className="text-tertiary transition-colors hover:text-ink">{t('Clear')}</button>
              </div>
              <button
                onClick={() => setBulkDeleteOpen(true)}
                disabled={deleting}
                title={t('Remove')}
                className="rounded-lg p-1.5 text-danger transition-colors hover:bg-danger-bg hover:text-danger-fg disabled:opacity-50"
              >
                <TrashIcon className="h-3.5 w-3.5" />
              </button>
            </div>
          )}

          {all.length === 0 ? (
            <EmptyHero
              icon={GlobeAltIcon}
              title={t('No crawls yet')}
              description={t('Turn any website into a dataset. Point Harvest at a URL and it collects every page for you.')}
              className="flex-1 py-10"
            >
              <Button size="sm" onClick={goNew}>
                <PlusIcon className="h-4 w-4" />
                {t('New crawl')}
              </Button>
            </EmptyHero>
          ) : visible.length === 0 ? (
            <EmptyHero
              icon={GlobeAltIcon}
              title={t('Nothing here')}
              description={t('No crawls match this filter or search.')}
              className="flex-1 py-10"
            />
          ) : (
            <ScrollArea className="flex-1" viewportClassName="px-2 py-2" fade="chrome">
              <Stagger className="grid gap-0.5" staggerMs={16}>
                {visible.map((c) => (
                  <CrawlRow
                    key={c.id}
                    crawl={c}
                    selected={isWide && selectedId === c.id}
                    checked={checkedIds.has(c.id)}
                    onSelect={onRowSelect}
                    onToggleCheck={toggleCheck}
                  />
                ))}
              </Stagger>
            </ScrollArea>
          )}
        </div>

        {/* Detail pane — the SAME full CrawlDetailView the /crawls/:id page renders */}
        <div className={SHELF_DETAIL_COL}>
          <SwapFade swapKey={selected?.id ?? 'none'} className="flex flex-1 flex-col min-w-0 min-h-0">
            {selected ? (
              <CrawlDetailView crawlId={selected.id} onDeleted={() => setSelectedId(null)} />
            ) : (
              <EmptyHero
                icon={GlobeAltIcon}
                title={t('Select a crawl')}
                description={t('Pick a crawl to see its live progress and data, or start a new one.')}
                className="flex-1"
              >
                <Button size="sm" variant="secondary" onClick={goNew}>
                  <PlusIcon className="h-4 w-4" />
                  {t('New crawl')}
                </Button>
              </EmptyHero>
            )}
          </SwapFade>
        </div>
      </div>

      <ConfirmDialog
        isOpen={bulkDeleteOpen}
        onClose={() => (deleting ? undefined : setBulkDeleteOpen(false))}
        onConfirm={handleBulkDelete}
        title={t('Remove {{n}} crawls?', { n: checkedIds.size })}
        message={t('This permanently removes the selected crawls and the datasets they collected. This cannot be undone.')}
        confirmText={deleting ? t('Removing…') : t('Remove {{n}}', { n: checkedIds.size })}
        variant="danger"
      />
    </div>
  );
};

export default CrawlsPage;
