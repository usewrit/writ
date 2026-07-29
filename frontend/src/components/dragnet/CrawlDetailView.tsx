import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { DatasetApiModal } from '../data/DatasetApiModal';
import { CrawlApiModal } from './CrawlApiModal';
import { safeHref } from '../data/Markdown';
import toast from 'react-hot-toast';
import {
  ArrowLeftIcon,
  ArrowTopRightOnSquareIcon,
  StopIcon,
  TableCellsIcon,
  CodeBracketIcon,
  ExclamationTriangleIcon,
  GlobeAltIcon,
  DocumentTextIcon,
  BoltIcon,
  TrashIcon,
  ArrowPathIcon,
  CircleStackIcon,
} from '@heroicons/react/24/outline';
import { useQuery } from '../../hooks/useQuery';
import { useQueryCache } from '../../stores/queryCache';
import { Q } from '../../stores/queryKeys';
import { ConfirmDialog } from '../ConfirmDialog';
import { AuthImage } from '../common/AuthImage';
import {
  crawlApi,
  crawlAgentsWorking,
  isCrawlCancellable,
  isCrawlTerminal,
  type CrawlView,
  type CrawlDefinition,
  type MapUrlItem,
} from '../../api/crawl';
import { workflowDataApi, type DataRow } from '../../api/workflowData';
import { apiErrorMessage } from '../../api/client';
import { formatElapsed, formatNumber, formatRelativeTime } from '../../utils/format';
import { Button } from '../ui/Button';
import {
  CrawlStatusChip,
  CrawlProgressBar,
  AgentsWorking,
  crawlStatusMeta,
} from './CrawlStatus';
import clsx from 'clsx';

// ── Count-up: smoothly tween a displayed integer toward its real value so the
// live page counter *moves* between polls instead of jumping. ────────────────
function useCountUp(target: number, live: boolean): number {
  const [display, setDisplay] = useState(target);
  // The tween's start value. It is only ever written and read from the rAF loop
  // / effect body — never during render — so the effect no longer has to read
  // `display` out of the render scope (which is what forced the stale-deps
  // suppression this hook used to carry).
  const shownRef = useRef(target);
  const rafRef = useRef<number>();
  useEffect(() => {
    // Paused: the hook returns `target` directly, so `display` only has to be
    // primed for the next live stretch. Priming happens on the next frame
    // rather than synchronously in the effect body — a synchronous setState
    // here cascades a render, and the value is invisible until `live` flips.
    if (!live) {
      rafRef.current = requestAnimationFrame(() => {
        shownRef.current = target;
        setDisplay(target);
      });
      return () => { if (rafRef.current) cancelAnimationFrame(rafRef.current); };
    }
    const from = shownRef.current;
    const delta = target - from;
    if (delta === 0) return;
    const dur = 600;
    const startedAt = performance.now();
    const tick = (now: number) => {
      const p = Math.min(1, (now - startedAt) / dur);
      const eased = 1 - Math.pow(1 - p, 3); // easeOutCubic
      const value = Math.round(from + delta * eased);
      shownRef.current = value;
      setDisplay(value);
      if (p < 1) rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => { if (rafRef.current) cancelAnimationFrame(rafRef.current); };
  }, [target, live]);
  return live ? display : target;
}

// A collected page distilled from a raw extracted-data row for the live feed.
interface CollectedPage {
  key: string;
  url: string;
  title: string;
  fieldCount: number;
  words?: number;
  contentKind?: string;
  /** Remote favicon URL (public — loaded directly). */
  faviconUrl?: string;
  /** Authenticated same-origin thumbnail path (loaded via <AuthImage>). */
  screenshotUrl?: string;
}

function pickString(fields: Record<string, unknown>, keys: string[]): string | undefined {
  for (const k of Object.keys(fields)) {
    if (keys.includes(k.toLowerCase())) {
      const v = fields[k];
      if (typeof v === 'string' && v.trim()) return v.trim();
    }
  }
  return undefined;
}

function toCollectedPage(row: DataRow): CollectedPage {
  const f = row.fields || {};
  const url = pickString(f, ['url', 'link', 'href', 'page_url', 'source_url', 'loc']) || '';
  const title = pickString(f, ['title', 'name', 'heading', 'h1', 'page_title']) || url || '—';
  const words = (() => {
    const w = (f['word_count'] ?? f['words']) as unknown;
    return typeof w === 'number' ? w : undefined;
  })();
  const contentKind = pickString(f, ['content_kind']);
  // The agent stamps a public favicon URL on browser-rendered pages, and the
  // coordinator rewrites each inline thumbnail to a served, authenticated path.
  const faviconUrl = pickString(f, ['favicon', 'favicon_url', 'icon']);
  const screenshotUrl = pickString(f, ['screenshot', 'thumbnail', 'screenshot_url']);
  const key = url || `${row.run_id}:${row.record_index}`;
  return { key, url, title, fieldCount: Object.keys(f).length, words, contentKind, faviconUrl, screenshotUrl };
}

function pagePath(url: string): string {
  try {
    const u = new URL(url);
    return (u.pathname + u.search) || '/';
  } catch {
    return url;
  }
}

const CONTENT_KIND_LABELS: Record<string, string> = {
  html: 'HTML', pdf: 'PDF', ocr: 'OCR', docx: 'DOCX', xlsx: 'XLSX', pptx: 'PPTX',
  json: 'JSON', csv: 'CSV', image: 'IMG', text: 'TXT',
};
function contentKindLabel(kind?: string): string | null {
  return (kind && CONTENT_KIND_LABELS[kind]) || null;
}
// Feed row badge: HTML is the common case, so suppress it (noise on every row).
function contentKindRowBadge(kind?: string): string | null {
  if (!kind || kind === 'html') return null;
  return contentKindLabel(kind);
}

// Cap the inline feed so a big crawl's page list never pushes the header actions
// and dataset shortcuts off-screen; the rest lives one click away in the dataset.
const FEED_CAP = 12;

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return url;
  }
}

// ── Lightweight section primitives (mirror WorkflowDetailPane's writ language:
// a quiet uppercase label + un-carded content, NOT a stack of headered cards). ─

/** Small uppercase section header; optional right-aligned affordance. */
const SectionLabel: React.FC<{ children: React.ReactNode; right?: React.ReactNode }> = ({ children, right }) => (
  <div className="mb-2 flex items-center justify-between gap-2">
    <span className="text-[10px] font-semibold uppercase tracking-wider text-tertiary">{children}</span>
    {right}
  </div>
);

// ── Site map (on-demand): the ranked frontier the crawl would/did discover. ──
// Loaded lazily on first open — mapping is a read-only sitemap + seed-page harvest
// that creates no crawl and dispatches no agent, so it is free to run here.
const SiteMapSection: React.FC<{ crawl: CrawlView }> = ({ crawl }) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [urls, setUrls] = useState<MapUrlItem[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setErr(null);
    try {
      const res = await crawlApi.map({ url: crawl.seed_url, search: crawl.intent || undefined, limit: 80 });
      setUrls(res.urls || []);
    } catch (e) {
      setErr(apiErrorMessage(e, t('Could not map the site')));
    } finally {
      setLoading(false);
    }
  };

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && urls == null && !loading) void load();
  };

  return (
    <div>
      <SectionLabel
        right={
          <button onClick={toggle} className="text-[10px] font-medium text-tertiary transition-colors hover:text-ink">
            {open ? t('Hide') : urls == null ? t('Map the site') : t('Show')}
          </button>
        }
      >
        {t('Site map')}
      </SectionLabel>
      {open ? (
        <div className="overflow-hidden rounded-xl border border-border bg-surface">
          {loading ? (
            <div className="flex items-center gap-2.5 px-3 py-3 text-[12px] text-tertiary">
              <span className="relative h-1.5 w-24 overflow-hidden rounded-full bg-canvas">
                <span className="dragnet-indeterminate absolute inset-y-0 left-0 w-1/3 rounded-full bg-info/80" />
              </span>
              {t('Mapping {{host}}…', { host: hostOf(crawl.seed_url) })}
            </div>
          ) : err ? (
            <p className="px-3 py-3 text-[12px] text-danger-fg">{err}</p>
          ) : urls && urls.length > 0 ? (
            <>
              <p className="border-b border-border px-3 py-2 text-[11px] text-tertiary">
                {t('{{n}} URLs ranked by relevance. The frontier is crawled best-first.', { n: urls.length })}
              </p>
              <ul className="divide-y divide-border">
                {urls.map((u) => {
                  const pct = Math.max(4, Math.round((u.score || 0) * 100));
                  return (
                    <li key={u.url} className="flex items-center gap-3 px-3 py-2">
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-[12px] text-ink">{u.title || pagePath(u.url)}</div>
                        <div className="truncate text-[11px] text-tertiary">{pagePath(u.url)}</div>
                      </div>
                      <span className="h-1.5 w-16 overflow-hidden rounded-full bg-canvas" title={`${pct}%`}>
                        <span className="block h-full rounded-full bg-ink/40" style={{ width: `${pct}%` }} />
                      </span>
                      <span className="w-9 shrink-0 text-right text-[11px] tabular-nums text-tertiary">{pct}%</span>
                    </li>
                  );
                })}
              </ul>
            </>
          ) : (
            <p className="px-3 py-3 text-[12px] text-tertiary">{t('No URLs discovered from the seed page.')}</p>
          )}
        </div>
      ) : (
        <p className="text-[12px] text-tertiary">
          {t('Preview the ranked frontier this crawl discovers from {{host}}.', { host: hostOf(crawl.seed_url) })}
        </p>
      )}
    </div>
  );
};

/** One compact stat cell in the "Overview" grid. */
const Stat: React.FC<{ label: string; value: React.ReactNode; tone?: 'default' | 'danger' | 'muted' }> = ({
  label, value, tone = 'default',
}) => (
  // Ruled column, not a box. Six bordered stat cells side by side was the bulk
  // of this page's visual weight; the top hairline is enough of a frame (the
  // marketing site's de-card rule — cards are for card-shaped things).
  <div className="border-t border-border pt-2">
    <div className="text-[10px] font-semibold uppercase tracking-wider text-tertiary">{label}</div>
    <div className={clsx('mt-0.5 text-[17px] font-semibold leading-tight tabular-nums',
      tone === 'danger' ? 'text-danger-fg' : tone === 'muted' ? 'text-tertiary' : 'text-ink')}>
      {value}
    </div>
  </div>
);

// How long a just-collected page keeps its highlight. Matches the live poll
// cadence, which is how long the old "new since the previous poll" set held it.
const FRESH_MS = 2500;

/**
 * One row of the collected-pages feed.
 *
 * A row is "fresh" because it just MOUNTED: the feed keys each `<li>` by page
 * key, so React only mounts a row for a page it has never rendered — exactly
 * the test the old seen-set performed, except the memory now lives in the DOM
 * instead of a ref read during render (which is not allowed: refs are mutable
 * and render must be pure). The highlight decays on a timer rather than being
 * cleared by the next poll, so a finished crawl doesn't leave rows lit forever.
 */
const FeedRow: React.FC<{ page: CollectedPage; isSchema: boolean }> = ({ page: p, isSchema }) => {
  const { t } = useTranslation();
  const [fresh, setFresh] = useState(true);
  useEffect(() => {
    const h = window.setTimeout(() => setFresh(false), FRESH_MS);
    return () => window.clearTimeout(h);
  }, []);
  const kindLabel = contentKindRowBadge(p.contentKind);

  return (
    <li className={clsx('flex items-center gap-3 px-3 py-2.5', fresh && 'animate-wizard-field-in')}>
      {/* A rendered page has a thumbnail; an HTTP-lane page falls back to the
          site favicon, and that to the generic document glyph. */}
      {p.screenshotUrl ? (
        <AuthImage
          src={p.screenshotUrl}
          alt={p.title}
          className="h-6 w-6 shrink-0 rounded-md object-cover"
          fallback={
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-hover">
              <DocumentTextIcon className="h-3.5 w-3.5 text-tertiary" />
            </span>
          }
        />
      ) : p.faviconUrl ? (
        <span className={clsx('flex h-6 w-6 shrink-0 items-center justify-center rounded-md', fresh ? 'bg-info/10' : 'bg-hover')}>
          <img
            src={p.faviconUrl}
            alt=""
            aria-hidden="true"
            loading="lazy"
            className="h-3.5 w-3.5 object-contain"
            onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none'; }}
          />
        </span>
      ) : (
        <span className={clsx('flex h-6 w-6 shrink-0 items-center justify-center rounded-md', fresh ? 'bg-info/10' : 'bg-hover')}>
          <DocumentTextIcon className={clsx('h-3.5 w-3.5', fresh ? 'text-info-fg' : 'text-tertiary')} />
        </span>
      )}
      <div className="min-w-0 flex-1">
        <div className="truncate text-[13px] text-ink">{p.title}</div>
        {p.url && <div className="truncate text-[11px] text-tertiary">{pagePath(p.url)}</div>}
      </div>
      {kindLabel && (
        <span className="shrink-0 rounded bg-hover px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-[0.04em] text-secondary">
          {kindLabel}
        </span>
      )}
      <span className="shrink-0 text-[11px] tabular-nums text-tertiary">
        {!isSchema && p.words != null
          ? t('{{n}} words', { n: formatNumber(p.words) })
          : t('{{n}} fields', { n: p.fieldCount })}
      </span>
    </li>
  );
};

/** A small mode/config pill for the header (Regular · Structured · render · ocr). */
const ModePill: React.FC<{ icon?: React.ElementType; children: React.ReactNode }> = ({ icon: Icon, children }) => (
  <span className="inline-flex items-center gap-1 rounded-full bg-hover px-1.5 py-0.5 text-[10px] font-medium text-secondary">
    {Icon && <Icon className="h-3 w-3" />}
    {children}
  </span>
);

const DetailSkeleton: React.FC = () => (
  <div className="mx-auto w-full max-w-3xl px-5 py-6">
    <div className="flex items-center gap-3">
      <div className="h-10 w-10 rounded-lg bg-hover" />
      <div className="flex-1 space-y-2">
        <div className="h-3 w-48 rounded bg-hover" />
        <div className="h-2 w-64 rounded bg-hover" />
      </div>
    </div>
    <div className="mt-5 h-2 w-full rounded-full bg-hover" />
    <div className="crawl-stats mt-6">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="border-t border-border pt-2">
          <div className="h-2.5 w-14 rounded bg-hover" />
          <div className="mt-2 h-4 w-10 rounded bg-hover" />
        </div>
      ))}
    </div>
  </div>
);

/**
 * The full crawl detail — a compact identity header (no chrome topbar) over a
 * scannable body of quiet sections: overview stats, the collected-pages feed,
 * composition and terminal actions. Shared by the `/crawls/:id` page AND the
 * `/crawls` shelf detail pane, so both read identically. Self-contained: it
 * fetches its own crawl + dataset. `onBack` renders a back affordance (the page
 * passes it; the shelf pane doesn't). `isPage` updates the document title.
 *
 * The self-hosted coordinator runs DETERMINISTIC crawls only, so there is no AI
 * executor pill, no intent/targeting, and no site-map surface — only the output
 * shape (markdown | schema) and render/OCR lane badges. The live accent is the
 * `info` palette (no Signal brand accent on the OSS build).
 */
export const CrawlDetailView: React.FC<{
  crawlId: string | number;
  onBack?: () => void;
  isPage?: boolean;
  /** Called after the crawl is removed — the shelf clears its selection, the page
   *  navigates back. Falls back to navigating to /crawls. */
  onDeleted?: () => void;
}> = ({ crawlId, onBack, isPage, onDeleted }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const id = String(crawlId);

  // The poll cadence depends on the crawl that this very query returns, so it
  // cannot be read back off `useQuery`'s result — it used to round-trip through
  // state written from an effect. Read the SAME cache entry `useQuery`
  // subscribes to instead: identical value, available a render earlier, and the
  // cadence stays a pure derivation with no state to cascade.
  //
  // A crawl that never loaded (deleted / bad id → 404) must not poll forever
  // behind the not-found screen; a transient error while data is on screen
  // keeps the live cadence.
  const cachedEntry = useQueryCache((s) => s.cache[Q.crawl(id)]);
  const cachedCrawl = (cachedEntry?.data ?? null) as CrawlView | null;
  const pollInterval =
    (!cachedCrawl && !!cachedEntry?.error) || (cachedCrawl && isCrawlTerminal(cachedCrawl.status))
      ? undefined
      : 2500;

  const { data: crawl, loading, error, refresh } = useQuery<CrawlView>(
    Q.crawl(id),
    () => crawlApi.get(id),
    { pollInterval, staleTime: 0 },
  );

  useEffect(() => {
    if (isPage && crawl?.name) document.title = `${crawl.name} · Harvest`;
  }, [isPage, crawl?.name]);

  const live = crawl ? crawlStatusMeta(crawl.status).live : false;

  // Smooth 1s elapsed ticker while live.
  const [, setNow] = useState(0);
  useEffect(() => {
    if (!live) return;
    const h = window.setInterval(() => setNow((n) => n + 1), 1000);
    return () => window.clearInterval(h);
  }, [live]);

  const dataWfId = crawl?.data_workflow_id ?? null;
  const { data: table } = useQuery(
    Q.key(`crawl-feed:${id}`),
    () => workflowDataApi.getTable(dataWfId as number, { limit: 120 }),
    { enabled: dataWfId != null, pollInterval: live ? 2500 : undefined, silent: true, staleTime: 0 },
  );

  const pages = useMemo<CollectedPage[]>(() => {
    const rows = [...(table?.rows || [])].sort(
      (a, b) => (b.run_at ? Date.parse(b.run_at) : 0) - (a.run_at ? Date.parse(a.run_at) : 0),
    );
    const seen = new Set<string>();
    const out: CollectedPage[] = [];
    for (const r of rows) {
      const p = toCollectedPage(r);
      if (seen.has(p.key)) continue;
      seen.add(p.key);
      out.push(p);
    }
    return out;
  }, [table]);

  const composition = useMemo(() => {
    const tally = new Map<string, number>();
    for (const p of pages) {
      const kind = p.contentKind || 'html';
      tally.set(kind, (tally.get(kind) || 0) + 1);
    }
    const items = [...tally.entries()]
      .map(([kind, count]) => ({ kind, count, label: contentKindLabel(kind) || kind.toUpperCase() }))
      .sort((a, b) => b.count - a.count);
    const total = items.reduce((s, i) => s + i.count, 0);
    return { items, total };
  }, [pages]);
  const showComposition = composition.total > 0 && composition.items.length > 1;

  const schemaColumns = useMemo(() => {
    return (table?.columns || []).filter((c) => c && !c.startsWith('_'));
  }, [table]);

  const [cancelling, setCancelling] = useState(false);
  const doCancel = async () => {
    if (!crawl || cancelling) return;
    setCancelling(true);
    try {
      const updated = await crawlApi.cancel(crawl.id);
      useQueryCache.getState().set(Q.crawl(id), updated);
      useQueryCache.getState().invalidate(Q.crawls());
      toast.success(t('Stopping the crawl…'));
      refresh();
    } catch (err) {
      toast.error(apiErrorMessage(err, t('Could not stop the crawl')));
    } finally {
      setCancelling(false);
    }
  };

  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  // "Get via API" opens the dataset's API modal in place — the SAME one the Data
  // explorer's grid opens. It used to navigate to the workflow's Connect tab, which
  // is a publish-this-workflow surface, not this dataset's read API.
  //
  // MUST stay above the `loading`/`!crawl` early returns below: declared after
  // them, this useState only ran once a crawl had loaded, so the render that
  // first got data called MORE hooks than the loading render before it — React
  // error #310, which blanked the whole crawl page.
  const [apiModalOpen, setApiModalOpen] = useState(false);
  // "Call this crawl" — the CRAWL's own callable API (saved settings + max_age),
  // distinct from `apiModalOpen` above which exposes the collected DATASET's read
  // API. Two different questions: "re-run this crawl by API" vs "read these rows".
  // Same hook-placement rule: declared here with the other hooks, never beside its
  // button, because the early returns below would change the hook count per render.
  const [callModalOpen, setCallModalOpen] = useState(false);
  const [savedDefinition, setSavedDefinition] = useState<CrawlDefinition | null>(null);
  const doDelete = async () => {
    if (!crawl || deleting) return;
    setDeleting(true);
    try {
      await crawlApi.remove(crawl.id);
      useQueryCache.getState().invalidate(Q.crawls());
      toast.success(t('Crawl removed'));
      setDeleteOpen(false);
      if (onDeleted) onDeleted();
      else navigate('/crawls');
    } catch (err) {
      toast.error(apiErrorMessage(err, t('Failed to remove crawl')));
      setDeleting(false);
    }
  };
  const crawlAgain = () => {
    const q = new URLSearchParams();
    if (crawl?.seed_url) q.set('url', crawl.seed_url);
    if (crawl?.name?.trim()) q.set('name', crawl.name.trim());
    navigate(`/crawls/new?${q.toString()}`);
  };

  const total = crawl?.pages_discovered || 0;
  const done = crawl?.pages_done || 0;
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const shownDone = useCountUp(done, live);

  // Back affordance sits above the header when this view is a full page.
  const BackButton = onBack ? (
    <button
      onClick={onBack}
      className="-ml-1.5 mb-2 inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[12px] text-secondary transition-colors hover:bg-hover hover:text-ink"
    >
      <ArrowLeftIcon className="h-4 w-4" />
      {t('Harvest')}
    </button>
  ) : null;

  if (loading && !crawl) {
    return (
      <div className="crawl-cq min-h-0 flex-1 overflow-y-auto">
        <DetailSkeleton />
      </div>
    );
  }

  if (!crawl) {
    return (
      <div className="flex h-full flex-col items-center justify-center px-6 text-center">
        <ExclamationTriangleIcon className="h-7 w-7 text-tertiary" />
        <p className="mt-3 text-[14px] font-medium text-ink">{t('Crawl not found')}</p>
        <p className="mt-1 max-w-sm text-[12px] text-tertiary">
          {error || t('This crawl may have been removed, or the link is wrong.')}
        </p>
        <Button className="mt-4" size="sm" onClick={() => navigate('/crawls')}>
          <ArrowLeftIcon className="h-3.5 w-3.5" />
          {t('Back to Harvest')}
        </Button>
      </div>
    );
  }

  const terminal = isCrawlTerminal(crawl.status);
  // An empty action group would still spend the header's row gap, so the whole
  // group is conditional rather than each button inside it.
  const hasActions = isCrawlCancellable(crawl.status) || terminal;
  const hasDataset = crawl.data_workflow_id != null && done > 0;
  // A crawl is NOT a workflow: "View data" (and the "Open dataset" links below) go
  // to the Outputs explorer — crawl-aware, lens-locked via workflow_type='crawl' —
  // NOT the workflow detail page, whose Run/Publish/Steps/Connect/Edit chrome and
  // "how it runs" settings don't apply to a crawl's collected dataset.
  const dataHref = `/data?workflow=${crawl.data_workflow_id}`;
  const isSchema = crawl.extract_mode === 'schema';
  const name = crawl.name?.trim() || hostOf(crawl.seed_url);
  const renderMode = crawl.render_mode;
  const ocrMode = crawl.ocr_mode;

  return (
    <div className="crawl-cq flex h-full flex-col">
      {/* ── Identity header ──────────────────────────────────────────────────
          Two stacked bands: WHO (glyph · name · seed · mode) beside the actions,
          then one full-width meter for the state. The actions used to sit beside
          the progress bar, which made the bar's length a function of how many
          buttons happened to be showing; now the bar always spans the header. */}
      <div className="shrink-0 border-b border-border px-5 pt-4 pb-4">
        <div className="mx-auto w-full max-w-3xl">
          {BackButton}

          <div className="crawl-head">
            <div className="flex min-w-0 items-start gap-3">
              <span className={clsx(
                'relative flex h-10 w-10 shrink-0 items-center justify-center rounded-lg',
                live ? 'bg-info/10 text-info-fg' : 'bg-chrome text-ink',
              )}>
                {live && <span className="absolute inset-0 z-10 rounded-lg border-2 border-current/25 border-t-current animate-spin" />}
                <AuthImage
                  src={crawl.favicon || undefined}
                  alt=""
                  className="h-full w-full rounded-lg object-cover"
                  fallback={<GlobeAltIcon className="h-5 w-5" />}
                />
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="truncate text-[15px] font-semibold text-ink" title={name}>{name}</span>
                  <CrawlStatusChip status={crawl.status} size="sm" className="shrink-0" />
                </div>
                <a
                  href={safeHref(crawl.seed_url || '')}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-0.5 inline-flex max-w-full items-center gap-1 text-[12px] text-tertiary transition-colors hover:text-secondary"
                >
                  <span className="truncate">{crawl.seed_url}</span>
                  <ArrowTopRightOnSquareIcon className="h-3 w-3 shrink-0" />
                </a>
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  {/* The self-hosted coordinator runs deterministic crawls only. */}
                  <ModePill icon={BoltIcon}>{t('Regular')}</ModePill>
                  <ModePill icon={isSchema ? TableCellsIcon : DocumentTextIcon}>
                    {isSchema ? t('Structured') : t('Markdown')}
                  </ModePill>
                  {renderMode && renderMode !== 'auto' && (
                    <ModePill>{renderMode === 'browser' ? t('Full browser') : t('Fast HTTP')}</ModePill>
                  )}
                  {ocrMode === 'force' && <ModePill>{t('OCR on')}</ModePill>}
                </div>
              </div>
            </div>

            {hasActions && (
              <div className="crawl-head-actions">
                {isCrawlCancellable(crawl.status) && (
                  <Button variant="secondary" size="sm" loading={cancelling} onClick={doCancel}>
                    <StopIcon className="h-4 w-4" />
                    {t('Stop crawl')}
                  </Button>
                )}
                {terminal && hasDataset && (
                  <Button size="sm" onClick={() => navigate(dataHref)}>
                    <CircleStackIcon className="h-4 w-4" />
                    {t('View data')}
                  </Button>
                )}
                {terminal && hasDataset && (
                  <Button variant="secondary" size="sm" onClick={() => setApiModalOpen(true)}>
                    <CodeBracketIcon className="h-4 w-4" />
                    {t('Get via API')}
                  </Button>
                )}
                <Button variant="secondary" size="sm" onClick={() => setCallModalOpen(true)}>
                  <BoltIcon className="h-4 w-4" />
                  {savedDefinition ? t('Callable') : t('Call this crawl')}
                </Button>
                {terminal && (
                  <Button variant="secondary" size="sm" onClick={crawlAgain}>
                    <ArrowPathIcon className="h-4 w-4" />
                    {t('Crawl again')}
                  </Button>
                )}
              </div>
            )}
          </div>

          {/* State band — headline count, then the meter it belongs to. */}
          <div className="mt-4">
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <span className="text-[26px] font-semibold leading-none tabular-nums text-ink">{formatNumber(shownDone)}</span>
              <span className="text-[12px] text-tertiary">
                {total > 0
                  ? t('/ {{total}} pages · {{pct}}%', { total: formatNumber(total), pct })
                  : t('pages collected')}
              </span>
              <span className="ml-auto flex items-center gap-2 text-[11px] text-tertiary">
                {live && <><AgentsWorking crawl={crawl} /><span aria-hidden className="text-tertiary/50">·</span></>}
                {live ? (
                  <span className="tabular-nums">{formatElapsed(crawl.created_at)}</span>
                ) : crawl.completed_at ? (
                  <span>{t('Finished {{when}}', { when: formatRelativeTime(crawl.completed_at) })}</span>
                ) : (
                  <span>{formatRelativeTime(crawl.created_at)}</span>
                )}
              </span>
            </div>
            <CrawlProgressBar crawl={crawl} className="mt-2.5" />
          </div>
        </div>
      </div>

      {/* ── Body (scrolls) ── */}
      <div className="min-h-0 flex-1 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        <div className="mx-auto w-full max-w-3xl space-y-6 px-5 py-5">
          {/* Overview stats */}
          <div>
            <SectionLabel>{t('Overview')}</SectionLabel>
            <div className="crawl-stats">
              <Stat label={t('Discovered')} value={formatNumber(total)} />
              <Stat label={t('Collected')} value={formatNumber(done)} />
              <Stat label={t('Failed')} value={formatNumber(crawl.pages_failed)} tone={crawl.pages_failed > 0 ? 'danger' : 'muted'} />
              <Stat label={t('Depth')} value={<>{formatNumber(crawl.current_depth)}<span className="text-[12px] font-normal text-tertiary"> / {formatNumber(crawl.max_depth)}</span></>} />
              {live
                ? <Stat label={t('Agents')} value={<>{formatNumber(crawlAgentsWorking(crawl))}<span className="text-[12px] font-normal text-tertiary"> {t('working')}</span></>} />
                : <Stat label={t('Skipped')} value={formatNumber(crawl.pages_skipped)} tone="muted" />}
              {crawl.records_total != null
                ? <Stat label={t('Records')} value={formatNumber(crawl.records_total)} />
                : <Stat label={t('Shards done')} value={formatNumber(crawl.shards_done)} />}
              {crawl.duplicates_removed != null && crawl.duplicates_removed > 0 && (
                <Stat label={t('Duplicates')} value={formatNumber(crawl.duplicates_removed)} tone="muted" />
              )}
            </div>
          </div>

          {/* Composition */}
          {showComposition && (
            <div>
              <SectionLabel>{t('Composition')}</SectionLabel>
              <div className="border-t border-border pt-3">
                <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-canvas">
                  {composition.items.map((it, i) => (
                    <span
                      key={it.kind}
                      className="h-full first:rounded-l-full last:rounded-r-full"
                      style={{
                        width: `${(it.count / composition.total) * 100}%`,
                        backgroundColor: `color-mix(in srgb, var(--color-ink, #1a1a1a) ${Math.max(18, 78 - i * 16)}%, transparent)`,
                      }}
                      title={`${it.label} · ${it.count}`}
                    />
                  ))}
                </div>
                <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5">
                  {composition.items.map((it, i) => (
                    <span key={it.kind} className="inline-flex items-center gap-1.5 text-[12px] text-secondary">
                      <span className="h-2.5 w-2.5 rounded-sm"
                        style={{ backgroundColor: `color-mix(in srgb, var(--color-ink, #1a1a1a) ${Math.max(18, 78 - i * 16)}%, transparent)` }} />
                      <span className="font-medium text-ink">{it.label}</span>
                      <span className="tabular-nums text-tertiary">{formatNumber(it.count)}</span>
                    </span>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* Collected pages feed */}
          {(pages.length > 0 || live) && (
            <div>
              <SectionLabel
                right={
                  hasDataset ? (
                    <Link to={dataHref} className="inline-flex items-center gap-1 text-[10px] font-medium text-tertiary transition-colors hover:text-ink">
                      <TableCellsIcon className="h-3 w-3" />
                      {t('Open dataset')}
                    </Link>
                  ) : undefined
                }
              >
                {live ? t('Collecting pages') : t('Collected pages')}
              </SectionLabel>
              <div className="overflow-hidden rounded-xl border border-border bg-surface">
                {isSchema && schemaColumns.length > 0 && (
                  <div className="flex flex-wrap items-center gap-1.5 border-b border-border bg-canvas/40 px-3 py-2">
                    <span className="text-[10px] font-semibold uppercase tracking-wider text-tertiary">{t('Fields')}</span>
                    {schemaColumns.slice(0, 12).map((c) => (
                      <span key={c} className="rounded-md border border-border bg-surface px-1.5 py-0.5 font-mono text-[10px] text-secondary">{c}</span>
                    ))}
                    {schemaColumns.length > 12 && (
                      <span className="text-[10px] text-tertiary">{t('+{{n}} more', { n: schemaColumns.length - 12 })}</span>
                    )}
                  </div>
                )}

                {pages.length === 0 ? (
                  <div className="flex items-center gap-2.5 px-3 py-6 text-[12px] text-tertiary">
                    <span className="relative h-1.5 w-24 overflow-hidden rounded-full bg-canvas">
                      <span className="dragnet-indeterminate absolute inset-y-0 left-0 w-1/3 rounded-full bg-info/80" />
                    </span>
                    {t('Mapping the site and dispatching agents…')}
                  </div>
                ) : (
                  <ul className="divide-y divide-border">
                    {pages.slice(0, FEED_CAP).map((p) => (
                      <FeedRow key={p.key} page={p} isSchema={isSchema} />
                    ))}
                    {pages.length > FEED_CAP && (
                      <li className="px-3 py-2">
                        {hasDataset ? (
                          <Link to={dataHref} className="inline-flex items-center gap-1 text-[11px] font-medium text-tertiary transition-colors hover:text-ink">
                            {t('+{{n}} more · Open dataset', { n: pages.length - FEED_CAP })}
                          </Link>
                        ) : (
                          <span className="text-[11px] text-tertiary">{t('+{{n}} more', { n: pages.length - FEED_CAP })}</span>
                        )}
                      </li>
                    )}
                  </ul>
                )}
              </div>
            </div>
          )}

          {/* Site map */}
          <SiteMapSection crawl={crawl} />

          {/* Terminal outcome — dataset actions / failure / cancelled note */}
          {crawl.status === 'failed' && (
            <div className="flex items-start gap-2.5 rounded-xl border border-danger/30 bg-danger-bg px-3 py-3">
              <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
              <div className="min-w-0">
                <p className="text-[13px] font-medium text-danger-fg">{t('Crawl failed')}</p>
                <p className="mt-0.5 break-words text-[12px] text-danger-fg/80">
                  {crawl.error || t('The crawl stopped before it could finish.')}
                </p>
              </div>
            </div>
          )}

          {crawl.status === 'cancelled' && !hasDataset && (
            <div className="flex items-center justify-between gap-3 border-t border-border pt-3">
              <p className="text-[13px] text-secondary">{t('This crawl was cancelled before collecting any pages.')}</p>
              <Button size="sm" variant="secondary" onClick={() => navigate('/crawls/new')}>
                <GlobeAltIcon className="h-4 w-4" />
                {t('New crawl')}
              </Button>
            </div>
          )}

          {/* Danger zone — quiet destructive action at the very bottom. */}
          {terminal && (
            <div className="border-t border-border pt-3">
              <button
                onClick={() => setDeleteOpen(true)}
                className="-mx-1 inline-flex items-center gap-1.5 rounded px-1 py-0.5 text-[11px] font-medium text-tertiary outline-none transition-colors hover:text-danger-fg focus-visible:ring-2 focus-visible:ring-danger/40"
              >
                <TrashIcon className="h-3.5 w-3.5" />
                {t('Remove crawl')}
              </button>
            </div>
          )}
        </div>
      </div>

      <ConfirmDialog
        isOpen={deleteOpen}
        onClose={() => (deleting ? undefined : setDeleteOpen(false))}
        onConfirm={doDelete}
        title={t('Remove this crawl?')}
        message={t('This permanently removes “{{name}}” and the dataset it collected. This cannot be undone.', {
          name,
        })}
        confirmText={deleting ? t('Removing…') : t('Remove crawl')}
        variant="danger"
      />

      {/* The dataset's API surface, in place — no `viewSnippet` here: there is no
          live grid to reproduce, so the modal leads with a plain read. */}
      {crawl.data_workflow_id != null && (
        <DatasetApiModal
          isOpen={apiModalOpen}
          onClose={() => setApiModalOpen(false)}
          datasetId={crawl.data_workflow_id}
        />
      )}

      {/* The CRAWL's own callable API: save these settings, then call them with a
          key — with `max_age` deciding whether a call reuses the collected data or
          re-crawls. Not gated on `terminal`: making a crawl callable is a decision
          about its CONFIG, which is just as valid mid-crawl. */}
      <CrawlApiModal
        isOpen={callModalOpen}
        onClose={() => setCallModalOpen(false)}
        crawl={crawl}
        existing={savedDefinition}
        onSaved={setSavedDefinition}
      />
    </div>
  );
};

export default CrawlDetailView;
