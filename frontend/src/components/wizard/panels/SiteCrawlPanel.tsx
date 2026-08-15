import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import {
  GlobeAltIcon,
  DocumentTextIcon,
  AdjustmentsHorizontalIcon,
  ChevronDownIcon,
  WindowIcon,
  PlayIcon,
  MapIcon,
  ViewfinderCircleIcon,
  CheckCircleIcon,
  CheckIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import { useWizard } from '../WizardContext';
import { NumberInput, Switch, Expand } from '../../ui';
import { PersonaPicker } from '../../workflows/PersonaPicker';
import { PersonaLoginWorkflow } from '../../workflows/PersonaLoginWorkflow';
import { personasApi } from '../../../api/endpoints';
import { useQuery } from '../../../hooks/useQuery';
import { Q } from '../../../stores/queryKeys';
import { StageBackdrop } from '../shared/StageBackdrop';
import { UrlInput } from '../steps/ModeSelectionStep';
import { crawlApi, type MapUrlItem, type PreviewCrawlResponse } from '../../../api/crawl';

/**
 * Site Crawl (Dragnet) — the ONE screen of the crawl flow on the self-hosted
 * coordinator, shaped like a playground, not a form:
 *
 *   [ https://…                                    ] [▶ Start crawl]
 *   [Crawl|Scrape]  hint sentence
 *   ── essentials ──────────────────────────────────────────
 *   Collect as         [Markdown | Structured table]
 *   Parallel agents    [────●──────────────]  ≈ pages/s · eta
 *   ── everything else, stacked & collapsed ────────────────
 *   ▸ Fetching & documents  ▸ Content  ▸ Limits & boundaries
 *
 * The coordinator runs DETERMINISTIC crawls only — no AI executor — so a
 * beginner types a URL and presses Start; two quiet rows are all they see.
 *
 * Palette: flat muted tiles, ink accent (no Signal/AI accent in the
 * self-hosted app).
 */

type Output = 'markdown' | 'schema';

// The three verbs: crawl a site, scrape one page, or map it first and hand-pick.
type Verb = 'crawl' | 'scrape' | 'map';
const VERBS: { id: Verb; label: string; icon: React.FC<any> }[] = [
  { id: 'crawl', label: 'Crawl', icon: GlobeAltIcon },
  { id: 'scrape', label: 'Scrape', icon: DocumentTextIcon },
  { id: 'map', label: 'Map', icon: MapIcon },
];
const VERB_HINT: Record<Verb, string> = {
  crawl: 'Follow links from the start page and collect every page in scope.',
  scrape: 'Fetch just the entry URL — no link-following.',
  map: 'List the site’s pages and pick exactly which ones to crawl.',
};

/**
 * Where the parallel-agents SLIDER tops out. This is a comfortable range for
 * dragging, NOT a limit — the number beside the slider is editable and unbounded.
 * Self-hosted there is no plan ladder to enforce; the operator's own fleet size is
 * the real ceiling, and the queue processor only hands a shard to an agent with a
 * free slot, so an over-ambitious number queues rather than stampedes.
 */
const AGENT_SLIDER_MAX = 64;

/** The path (+query) of a URL — what the scope patterns actually match on. */
const pathOf = (url: string): string => {
  try {
    const u = new URL(url);
    return (u.pathname || '/') + (u.search || '');
  } catch {
    return url;
  }
};

/** Compact pill toggle for a small enum choice. Neutral ink palette. */
const Segmented: React.FC<{
  value: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
}> = ({ value, options, onChange }) => (
  <div className="inline-flex rounded-full border border-border bg-hover/30 p-0.5">
    {options.map((o) => (
      <button
        key={o.value}
        type="button"
        onClick={() => onChange(o.value)}
        aria-pressed={value === o.value}
        className={clsx(
          'rounded-full px-3 py-1.5 text-[12px] font-medium transition-colors',
          value === o.value ? 'bg-surface text-ink shadow-sm' : 'text-secondary hover:text-ink',
        )}
      >
        {o.label}
      </button>
    ))}
  </div>
);

const PathsField: React.FC<{
  label: string;
  hint: string;
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
}> = ({ label, hint, placeholder, value, onChange }) => (
  <div>
    <div className="mb-1.5 flex items-baseline justify-between gap-3">
      <label className="text-sm text-secondary">{label}</label>
      <span className="text-[11px] text-tertiary">{hint}</span>
    </div>
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      rows={2}
      placeholder={placeholder}
      spellCheck={false}
      className="w-full rounded-lg border border-border bg-surface px-3.5 py-2.5 font-mono text-[12px] text-ink placeholder:text-tertiary transition-all duration-200 focus:border-ink/30 focus:shadow-sm focus:outline-none focus:ring-2 focus:ring-ink/5"
    />
  </div>
);

/** Collapsed-by-default drawer for secondary settings, with a summary readout. */
const Drawer: React.FC<{
  icon: React.FC<any>;
  title: string;
  summary: string;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}> = ({ icon: Icon, title, summary, open, onToggle, children }) => (
  <div className="mt-3 overflow-hidden rounded-xl border border-border first:mt-0">
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      className="flex w-full items-center gap-2.5 px-4 py-3 text-left transition-colors hover:bg-hover/40"
    >
      <Icon className="h-4 w-4 text-tertiary" />
      <span className="flex-1 text-[13px] font-medium text-ink">{title}</span>
      <span className="max-w-[45%] truncate text-[12px] text-tertiary">{summary}</span>
      <ChevronDownIcon className={clsx('h-4 w-4 shrink-0 text-tertiary transition-transform duration-200', open && 'rotate-180')} />
    </button>
    <Expand open={open} mountOnEnter>
      <div className="space-y-5 border-t border-border px-4 py-5">{children}</div>
    </Expand>
  </div>
);

/** Small label used inside drawers. */
const SectionLabel: React.FC<{ children: React.ReactNode; hint?: string }> = ({ children, hint }) => (
  <div className="mb-2 flex items-baseline justify-between gap-3">
    <label className="text-sm font-medium text-ink">{children}</label>
    {hint && <span className="text-[11px] text-tertiary">{hint}</span>}
  </div>
);

export const SiteCrawlPanel: React.FC<{ onSubmit?: () => void }> = ({ onSubmit }) => {
  const { t } = useTranslation();
  const { state, updateConfig } = useWizard();
  const c = state.config;
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [contentOpen, setContentOpen] = useState(false);
  const [fetchOpen, setFetchOpen] = useState(false);
  const [goalOpen, setGoalOpen] = useState(false);

  const verb = c.crawlVerb as Verb;

  const parseLines = (text: string): string[] =>
    text.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);

  const seedUrl = (): string => {
    let s = (c.url || '').trim();
    if (!s) return '';
    if (!/^https?:\/\//i.test(s)) s = `https://${s}`;
    return s;
  };

  // Scope dry-run: what the crawl WOULD keep and drop. Pure read — creates no
  // crawl and dispatches no agent, so it is safe to run as often as you like.
  const [scopeBusy, setScopeBusy] = useState(false);
  const [scopeError, setScopeError] = useState<string | null>(null);
  const [preview, setPreview] = useState<PreviewCrawlResponse | null>(null);

  const runPreview = async () => {
    const url = seedUrl();
    if (!url) { setScopeError(t('Enter a URL first.')); return; }
    setScopeBusy(true);
    setScopeError(null);
    try {
      const hasIntent = !!c.crawlIntent.trim();
      const res = await crawlApi.preview({
        url,
        intent: hasIntent ? c.crawlIntent.trim() : undefined,
        // With a goal, let the coordinator derive the paths; without one, preview
        // the operator's own hand-written scope.
        include_paths: hasIntent ? undefined : parseLines(c.crawlIncludePaths),
        exclude_paths: hasIntent ? undefined : parseLines(c.crawlExcludePaths),
        max_depth: hasIntent ? undefined : c.crawlMaxDepth,
        relevance_threshold: c.crawlRelevanceThreshold || undefined,
        same_domain: c.crawlSameDomain,
        allow_subdomains: c.crawlAllowSubdomains,
      });
      setPreview(res);
      // Apply the derived scope to the form so it stays editable and the crawl uses it.
      if (hasIntent) {
        updateConfig({
          crawlIncludePaths: res.effective.include_paths.join('\n'),
          crawlExcludePaths: res.effective.exclude_paths.join('\n'),
          crawlMaxDepth: res.effective.max_depth,
        });
      }
    } catch (e: any) {
      setScopeError(e?.response?.data?.detail || t('Could not preview the scope.'));
    } finally {
      setScopeBusy(false);
    }
  };

  // Map: list the site's URLs so the operator can pick which to crawl. Pure read.
  const [mapBusy, setMapBusy] = useState(false);
  const [mapUrls, setMapUrls] = useState<MapUrlItem[] | null>(null);
  const [mapError, setMapError] = useState<string | null>(null);

  const runMap = async () => {
    const url = seedUrl();
    if (!url) { setMapError(t('Enter a URL first.')); return; }
    setMapBusy(true);
    setMapError(null);
    try {
      const res = await crawlApi.map({
        url,
        search: c.crawlIntent.trim() || undefined,
        limit: 200,
        // Map as the chosen login identity: the picker must offer the pages the
        // crawl will actually reach, not a logged-out visitor's view.
        persona_id: c.crawlPersonaId ?? undefined,
      });
      setMapUrls(res.urls);
    } catch (e: any) {
      setMapError(e?.response?.data?.detail || t('Could not map the site.'));
    } finally {
      setMapBusy(false);
    }
  };

  const toggleSeed = (url: string) => {
    const next = new Set(c.crawlSeedUrls);
    if (next.has(url)) next.delete(url); else next.add(url);
    updateConfig({ crawlSeedUrls: Array.from(next) });
  };
  const selectAllMapped = () => updateConfig({ crawlSeedUrls: (mapUrls || []).map((u) => u.url) });

  // Hand the map selection (or, if none picked, the whole map) to the crawl.
  const crawlSelection = () => {
    const sel = c.crawlSeedUrls.length ? c.crawlSeedUrls : (mapUrls || []).map((u) => u.url);
    updateConfig({ crawlVerb: 'crawl', crawlSeedUrls: sel });
  };
  const output = c.crawlOutput as Output;
  const render = (c.crawlRenderMode ?? 'auto') as 'auto' | 'http' | 'browser';
  const ocr = (c.crawlOcrMode ?? 'auto') as 'auto' | 'off' | 'force';

  const host = (() => {
    try {
      return c.url ? new URL(/^https?:\/\//i.test(c.url) ? c.url : `https://${c.url}`).hostname.replace(/^www\./, '') : undefined;
    } catch {
      return c.url || undefined;
    }
  })();

  // The chosen persona, for the sign-in row below. Shares PersonaPicker's cache
  // entry (same key), so selecting one doesn't cost a second fetch — and the
  // picker's own refresh after an inline create re-primes this too.
  const { data: crawlPersonas, refresh: refreshCrawlPersonas } = useQuery(
    Q.personas(host),
    () => personasApi.list(host),
  );
  const selectedPersona = React.useMemo(
    () => (crawlPersonas || []).find((p) => p.id === c.crawlPersonaId) || null,
    [crawlPersonas, c.crawlPersonaId],
  );

  // Parallel-agents speed estimate (mirrors the website's Dragnet demo): each
  // agent pulls pages concurrently, so throughput scales with the fleet. The
  // per-agent rate is a rough, honest guess keyed to what actually dominates:
  // render mode (HTTP vs warm browser) and the politeness delay.
  const agents = Math.max(1, c.crawlConcurrency);
  const perAgentRate = Math.min(
    render === 'http' ? 3.4 : render === 'browser' ? 0.6 : 2.5,
    1000 / Math.max(c.crawlDelayMs, 1),
  );
  const pps = agents * perAgentRate;
  const ppsLabel = pps >= 10 ? Math.round(pps).toString() : pps.toFixed(1);
  const etaPages = verb === 'scrape' ? 1 : c.crawlPageBudget;
  const etaSec = Math.max(1, Math.round(etaPages / Math.max(pps, 0.1)));
  const etaLabel =
    etaSec < 90
      ? t('{{s}}s', { s: etaSec })
      : etaSec < 5400
        ? t('{{m}} min', { m: Math.round(etaSec / 60) })
        : t('{{h}} h', { h: (etaSec / 3600).toFixed(1) });

  const goLabel = state.isSubmitting
    ? t('Starting…')
    : verb === 'map' ? (mapBusy ? t('Mapping…') : t('Map site'))
      : verb === 'scrape' ? t('Start scrape') : t('Start crawl');

  // Map never creates anything, so its button runs the map itself rather than
  // submitting the wizard.
  const onGo = () => (verb === 'map' ? void runMap() : onSubmit?.());

  return (
    <StageBackdrop className="flex justify-center">
      <div className="w-full max-w-3xl px-4 sm:px-6 py-8">
        <div className="rounded-2xl border border-ink/20 bg-surface/90 px-5 py-6 shadow-sm backdrop-blur-sm sm:px-6">
          {/* Header */}
          <div className="flex items-center gap-3">
            <div className="rounded-lg bg-hover p-2">
              <GlobeAltIcon className="h-5 w-5 text-secondary" />
            </div>
            <div className="min-w-0">
              <h2 className="text-base font-semibold tracking-tight text-ink">{t('Site Crawl')}</h2>
              <p className="truncate text-sm text-secondary">
                {host ? t('Crawl {{host}} into one dataset', { host }) : t('Turn a whole site into one dataset')}
              </p>
            </div>
          </div>

          {/* Command bar — URL and go. The whole flow for a beginner. */}
          <div className="mt-6 flex flex-wrap items-center gap-2">
            <div className="min-w-[240px] flex-1">
              <UrlInput value={c.url} onChange={(url) => updateConfig({ url })} />
            </div>
            <button
              type="button"
              onClick={onGo}
              disabled={state.isSubmitting || mapBusy}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-accent-strong px-4 py-2.5 text-[12px] font-semibold text-accent-on transition-colors hover:bg-accent-strong/90 disabled:opacity-50"
            >
              {verb === 'map' ? <MapIcon className="h-3.5 w-3.5" /> : <PlayIcon className="h-3.5 w-3.5" />}
              {goLabel}
            </button>
            {/* Scope dry-run — crawl only, and free: it creates nothing. */}
            {verb === 'crawl' && (
              <button
                type="button"
                onClick={() => void runPreview()}
                disabled={scopeBusy}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-border px-3.5 py-2.5 text-[12px] font-semibold text-ink transition-colors hover:bg-hover/40 disabled:opacity-50"
              >
                <ViewfinderCircleIcon className="h-3.5 w-3.5" />
                {scopeBusy ? t('Checking…') : t('Preview scope')}
              </button>
            )}
          </div>

          {/* The operation, under the bar it acts on. */}
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <div className="inline-flex rounded-full border border-border bg-hover/30 p-0.5">
              {VERBS.map((v) => (
                <button
                  key={v.id}
                  type="button"
                  onClick={() => updateConfig({ crawlVerb: v.id })}
                  aria-pressed={verb === v.id}
                  className={clsx(
                    'flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[12px] font-medium transition-colors',
                    verb === v.id ? 'bg-surface text-ink shadow-sm' : 'text-secondary hover:text-ink',
                  )}
                >
                  <v.icon className="h-3.5 w-3.5" />
                  {t(v.label)}
                </button>
              ))}
            </div>
            <p className="text-[12px] text-tertiary">{t(VERB_HINT[verb])}</p>
          </div>
          {scopeError && <p className="mt-2 text-[12px] text-danger-fg">{scopeError}</p>}

          {/* Scope dry-run result — exactly what the crawl will keep and drop. */}
          {verb === 'crawl' && preview && (
            <div className="mt-3 rounded-xl border border-border bg-hover/20 p-3.5">
              {preview.derived?.reason && (
                <p className="text-[12px] text-secondary">
                  <span className="font-medium text-ink">{t('Scope')}:</span> {preview.derived.reason}
                </p>
              )}
              <div className="mt-2 flex flex-wrap gap-1.5">
                {preview.effective.include_paths.map((p) => (
                  <span key={`i-${p}`} className="rounded-md bg-ink/[0.06] px-2 py-0.5 font-mono text-[11px] text-ink">
                    + {p}
                  </span>
                ))}
                {preview.effective.exclude_paths.map((p) => (
                  <span key={`e-${p}`} className="rounded-md border border-border bg-hover px-2 py-0.5 font-mono text-[11px] text-tertiary line-through">
                    − {p}
                  </span>
                ))}
                {preview.effective.include_paths.length === 0 && preview.effective.exclude_paths.length === 0 && (
                  <span className="text-[11px] text-tertiary">{t('Whole site in scope')}</span>
                )}
              </div>
              <p className="mt-2 text-[12px] text-secondary">
                {t('{{kept}} of {{total}} sampled pages match', {
                  kept: preview.counts.kept,
                  total: preview.counts.total,
                })}
                {' · '}
                {t('depth {{d}}', { d: preview.effective.max_depth })}
              </p>
              {preview.sample.length > 0 && (
                <div className="mt-2 max-h-40 space-y-0.5 overflow-y-auto">
                  {preview.sample.slice(0, 40).map((s) => (
                    <div key={s.url} className="flex items-center gap-2 text-[11px]">
                      {s.kept ? (
                        <CheckCircleIcon className="h-3.5 w-3.5 shrink-0 text-ink/70" />
                      ) : (
                        <XMarkIcon className="h-3.5 w-3.5 shrink-0 text-tertiary" />
                      )}
                      <span className={clsx('truncate font-mono', s.kept ? 'text-secondary' : 'text-tertiary line-through')}>
                        {pathOf(s.url)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* MAP — discovery: list, pick, hand off to the crawl. */}
          {verb === 'map' && (
            <div className="mt-4">
              {mapError && <p className="mb-2 text-[12px] text-danger-fg">{mapError}</p>}
              {!mapUrls && !mapBusy && (
                <p className="text-[12px] text-tertiary">{t('Mapping creates nothing — it just lists the site.')}</p>
              )}
              {mapUrls && (
                <>
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      onClick={selectAllMapped}
                      className="text-[11px] text-tertiary transition-colors hover:text-secondary"
                    >
                      {t('Select all')}
                    </button>
                    <span className="text-[11px] text-tertiary">
                      {t('{{sel}} of {{total}} selected', { sel: c.crawlSeedUrls.length, total: mapUrls.length })}
                    </span>
                  </div>
                  <div className="mt-2 max-h-80 overflow-y-auto rounded-xl border border-border bg-hover/20 p-2">
                    {mapUrls.length === 0 && <p className="p-2 text-[12px] text-tertiary">{t('No URLs found.')}</p>}
                    {mapUrls.map((u) => {
                      const on = c.crawlSeedUrls.includes(u.url);
                      return (
                        <button
                          key={u.url}
                          type="button"
                          onClick={() => toggleSeed(u.url)}
                          aria-pressed={on}
                          className={clsx(
                            'flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-[11px] transition-colors',
                            on ? 'bg-ink/[0.06]' : 'hover:bg-hover/40',
                          )}
                        >
                          <span
                            className={clsx(
                              'flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border',
                              on ? 'border-ink bg-ink text-surface' : 'border-border',
                            )}
                          >
                            {on && <CheckIcon className="h-2.5 w-2.5" />}
                          </span>
                          <span className="truncate font-mono text-secondary">{pathOf(u.url)}</span>
                          {u.title && <span className="ml-auto max-w-[40%] truncate text-tertiary">{u.title}</span>}
                        </button>
                      );
                    })}
                  </div>
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      onClick={crawlSelection}
                      className="inline-flex items-center gap-1.5 rounded-lg bg-accent-strong px-3.5 py-2 text-[12px] font-semibold text-accent-on transition-colors hover:bg-accent-strong/90"
                    >
                      <GlobeAltIcon className="h-3.5 w-3.5" />
                      {t('Crawl selected')}
                    </button>
                    <span className="text-[12px] text-tertiary">{t('No selection uses every mapped URL.')}</span>
                  </div>
                </>
              )}
            </div>
          )}

          {/* The essentials — quiet rows, nothing else visible. */}
          {verb !== 'map' && (
          <div className="mt-5 divide-y divide-border border-t border-border">
            {/* SIGN IN — first, not buried in Advanced. Whether a crawl can see the
                pages at all is a more fundamental question than how to format them,
                and a login-walled site returns nothing useful without it. Choosing a
                persona reveals its sign-in setup inline, so "crawl behind a login"
                is completable here instead of ending at a 422 on Start. */}
            <div className="py-3.5">
              <div className="mb-1.5 flex items-baseline justify-between gap-3">
                <span className="text-[13px] font-medium text-ink">{t('Sign in as')}</span>
                <span className="text-[11px] text-tertiary">{t('optional')}</span>
              </div>
              <PersonaPicker
                value={c.crawlPersonaId}
                onChange={(id) => updateConfig({ crawlPersonaId: id })}
                // Domain-sorts the list and prefills the create modal with the site
                // the user just typed.
                domain={host}
              />
              {selectedPersona ? (
                <div className="mt-3 rounded-xl border border-border bg-hover/20 p-3">
                  <PersonaLoginWorkflow
                    persona={selectedPersona}
                    onChanged={refreshCrawlPersonas}
                  />
                </div>
              ) : (
                <p className="mt-1.5 text-[12px] text-tertiary">
                  {t('Crawl pages behind a login using a saved persona (handles 2FA and reuses its session).')}
                </p>
              )}
            </div>

            {/* Output */}
            <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 py-3.5">
              <span className="text-[13px] font-medium text-ink">{t('Collect as')}</span>
              <Segmented
                value={output}
                onChange={(v) => updateConfig({ crawlOutput: v as Output })}
                options={[
                  { value: 'markdown', label: t('Markdown') },
                  { value: 'schema', label: t('Structured table') },
                ]}
              />
            </div>

            {/* Parallel agents — crawl only (scrape is a single page here).
                The slider is a comfortable RANGE, not a limit: the number beside it
                is directly editable and takes any value, because self-hosted there
                is no plan ladder to ration against — the operator's own fleet is the
                real ceiling, and the queue only dispatches a shard when an agent has
                a free slot. */}
            {verb === 'crawl' && (
            <div className="py-3.5">
              <div className="flex items-baseline justify-between gap-4">
                <span className="text-[13px] font-medium text-ink">{t('Parallel agents')}</span>
                <NumberInput
                  value={agents}
                  onChange={(v) => updateConfig({ crawlConcurrency: Math.max(1, v ?? 1) })}
                  min={1}
                  size="sm"
                  aria-label={t('Parallel agents')}
                  wrapperClassName="w-24"
                />
              </div>
              <input
                type="range"
                min={1}
                max={AGENT_SLIDER_MAX}
                step={1}
                value={Math.min(agents, AGENT_SLIDER_MAX)}
                onChange={(e) => updateConfig({ crawlConcurrency: Number(e.target.value) })}
                aria-label={t('Parallel agents (slider range)')}
                className="mt-2 h-1 w-full cursor-pointer accent-ink"
              />
              <p className="mt-1 text-[12px] text-tertiary">
                {t('≈ {{pps}} pages/s · about {{eta}} for {{n}} pages', { pps: ppsLabel, eta: etaLabel, n: etaPages })}
              </p>
              {agents > AGENT_SLIDER_MAX && (
                <p className="mt-1 text-[12px] text-tertiary">
                  {t('Past the slider’s range — that’s fine. Shards beyond your fleet’s free slots just queue.')}
                </p>
              )}
            </div>
            )}
          </div>
          )}

          {/* Everything else — stacked, collapsed, one drawer away. */}
          {verb !== 'map' && (
          <div className="mt-4">
            {verb === 'crawl' && (
            <Drawer
              icon={ViewfinderCircleIcon}
              title={t('Only collect what you need')}
              summary={c.crawlIntent.trim() ? c.crawlIntent.trim() : t('everything in scope')}
              open={goalOpen}
              onToggle={() => setGoalOpen((o) => !o)}
            >
              <div>
                <textarea
                  value={c.crawlIntent}
                  onChange={(e) => updateConfig({ crawlIntent: e.target.value })}
                  rows={2}
                  placeholder={t('e.g. only phone product pages with prices — skip the blog and support')}
                  className="w-full resize-none rounded-lg border border-border bg-surface px-3.5 py-2.5 text-[13px] text-ink placeholder:text-tertiary focus:border-ink/30 focus:outline-none focus:ring-2 focus:ring-ink/5"
                />
                <p className="mt-1.5 text-[12px] text-tertiary">
                  {c.crawlIntent.trim()
                    ? t('Matching pages are crawled first, so the page budget goes where you asked. Preview costs nothing.')
                    : t('Blank = every page in scope.')}
                </p>
                <div className="mt-3">
                  <SectionLabel hint={t('0 = keep everything')}>{t('Skip pages below')}</SectionLabel>
                  <div className="flex items-center gap-3">
                    <input
                      type="range"
                      min={0}
                      max={0.8}
                      step={0.05}
                      value={c.crawlRelevanceThreshold}
                      onChange={(e) => updateConfig({ crawlRelevanceThreshold: Number(e.target.value) })}
                      aria-label={t('Relevance threshold')}
                      disabled={!c.crawlIntent.trim()}
                      className="h-1 flex-1 cursor-pointer accent-ink disabled:cursor-not-allowed disabled:opacity-40"
                    />
                    <span className="w-10 text-right text-[13px] font-semibold tabular-nums text-ink">
                      {c.crawlRelevanceThreshold.toFixed(2)}
                    </span>
                  </div>
                  <p className="mt-1 text-[12px] text-tertiary">
                    {c.crawlIntent.trim()
                      ? t('Discovered pages scoring below this against your goal are skipped. Your start pages are never skipped.')
                      : t('Needs a goal above.')}
                  </p>
                </div>
              </div>
            </Drawer>
            )}

            <Drawer
              icon={WindowIcon}
              title={t('Fetching & documents')}
              summary={
                (render === 'auto' ? t('auto render') : render === 'http' ? t('HTTP only') : t('full browser')) +
                ' · ' +
                (ocr === 'auto' ? t('OCR auto') : ocr === 'off' ? t('OCR off') : t('force OCR'))
              }
              open={fetchOpen}
              onToggle={() => setFetchOpen((o) => !o)}
            >
              <div>
                <SectionLabel hint={t('how each page is fetched')}>{t('Render')}</SectionLabel>
                <Segmented
                  value={render}
                  onChange={(v) => updateConfig({ crawlRenderMode: v as 'auto' | 'http' | 'browser' })}
                  options={[
                    { value: 'auto', label: t('Auto') },
                    { value: 'http', label: t('Fast HTTP') },
                    { value: 'browser', label: t('Full browser') },
                  ]}
                />
                <p className="mt-1.5 text-[12px] text-tertiary">
                  {render === 'http'
                    ? t('Never render — pure HTTP fetch. Fastest and cheapest for static sites.')
                    : render === 'browser'
                    ? t('Always render in a warm browser (waits for the network to settle) — for JS/SPA sites.')
                    : t('HTTP-first; renders JS-heavy or thin pages in a warm browser only when needed.')}
                </p>
              </div>

              <div className="border-t border-border pt-4">
                <SectionLabel hint={t('PDFs, docs & images')}>{t('Documents & OCR')}</SectionLabel>
                <Segmented
                  value={ocr}
                  onChange={(v) => updateConfig({ crawlOcrMode: v as 'auto' | 'off' | 'force' })}
                  options={[
                    { value: 'auto', label: t('Auto') },
                    { value: 'off', label: t('Off') },
                    { value: 'force', label: t('Force OCR') },
                  ]}
                />
                <p className="mt-1.5 text-[12px] text-tertiary">
                  {ocr === 'off'
                    ? t('Skip PDFs, office docs, and images — crawl HTML pages only.')
                    : ocr === 'force'
                    ? t('OCR every page and document, even when it already has selectable text.')
                    : t('Read PDFs, office docs, and images — text layer directly, OCR only scans and screenshots.')}
                </p>
              </div>
            </Drawer>

            <Drawer
              icon={DocumentTextIcon}
              title={t('Content')}
              summary={
                (c.crawlContentPreset === 'full' ? t('full page') : c.crawlContentPreset === 'readable' ? t('readable') : t('main content')) +
                (c.crawlIncludeComments ? '' : t(' · no comments'))
              }
              open={contentOpen}
              onToggle={() => setContentOpen((o) => !o)}
            >
              <div>
                <SectionLabel hint={t('what to keep')}>{t('Scope')}</SectionLabel>
                <Segmented
                  value={c.crawlContentPreset}
                  onChange={(v) => updateConfig({ crawlContentPreset: v as 'main' | 'full' | 'readable' })}
                  options={[
                    { value: 'main', label: t('Main content') },
                    { value: 'full', label: t('Full page') },
                    { value: 'readable', label: t('Readable') },
                  ]}
                />
                <p className="mt-1.5 text-[12px] text-tertiary">
                  {c.crawlContentPreset === 'full'
                    ? t('Keep the whole page — including comment threads, discussions, and reviews.')
                    : t('Isolate the main article and drop site chrome (nav, footer, sidebars).')}
                </p>
              </div>

              <div className="space-y-3 border-t border-border pt-4">
                <Switch
                  checked={c.crawlIncludeComments}
                  onChange={(v) => updateConfig({ crawlIncludeComments: v })}
                  reverse
                  label={t('Include comments')}
                  description={t('Keep discussion / comment sections (forums, reviews, Q&A).')}
                />
                <Switch checked={c.crawlKeepImages} onChange={(v) => updateConfig({ crawlKeepImages: v })} reverse label={t('Keep images')} />
                <Switch checked={c.crawlKeepTables} onChange={(v) => updateConfig({ crawlKeepTables: v })} reverse label={t('Keep tables')} />
                <Switch checked={c.crawlKeepLinks} onChange={(v) => updateConfig({ crawlKeepLinks: v })} reverse label={t('Keep links')} />
              </div>

              <div className="grid grid-cols-1 gap-4 border-t border-border pt-4 @pair/stage:grid-cols-2">
                <PathsField
                  label={t('Exclude elements')}
                  hint={t('CSS, one per line')}
                  placeholder={'.ads\n#sidebar'}
                  value={c.crawlExcludeSelectors}
                  onChange={(v) => updateConfig({ crawlExcludeSelectors: v })}
                />
                <PathsField
                  label={t('Only these elements')}
                  hint={t('CSS, one per line')}
                  placeholder={'.comment\narticle'}
                  value={c.crawlIncludeSelectors}
                  onChange={(v) => updateConfig({ crawlIncludeSelectors: v })}
                />
              </div>
            </Drawer>

            {verb === 'crawl' && (
            <Drawer
              icon={AdjustmentsHorizontalIcon}
              title={t('Limits & boundaries')}
              summary={t('depth {{d}} · {{n}} pages', {
                d: c.crawlMaxDepth,
                n: c.crawlPageBudget,
              })}
              open={advancedOpen}
              onToggle={() => setAdvancedOpen((o) => !o)}
            >
              <div className="grid grid-cols-2 gap-4 @pair/stage:grid-cols-3">
                {/* Floors only — no ceilings. These bound the SHAPE of the crawl, and
                    self-hosted nothing is being rationed: the box, the fleet and the
                    bandwidth are all the operator's. */}
                <NumberInput label={t('Max depth')} value={c.crawlMaxDepth} onChange={(v) => updateConfig({ crawlMaxDepth: v ?? 0 })} min={0} />
                <NumberInput label={t('Page budget')} value={c.crawlPageBudget} onChange={(v) => updateConfig({ crawlPageBudget: v ?? 1 })} min={1} step={50} />
                <NumberInput label={t('Pages per agent')} value={c.crawlShardSize} onChange={(v) => updateConfig({ crawlShardSize: v ?? 1 })} min={1} />
                <NumberInput label={t('Delay')} value={c.crawlDelayMs} onChange={(v) => updateConfig({ crawlDelayMs: v ?? 0 })} min={0} step={50} suffix="ms" />
              </div>

              <div className="space-y-3 border-t border-border pt-4">
                <Switch
                  checked={c.crawlRespectRobots}
                  onChange={(v) => updateConfig({ crawlRespectRobots: v })}
                  reverse
                  label={t('Respect robots.txt')}
                  description={t('Skip paths the site asks crawlers not to visit.')}
                />
                <Switch
                  checked={c.crawlSameDomain}
                  onChange={(v) => updateConfig({ crawlSameDomain: v })}
                  reverse
                  label={t('Stay on the same domain')}
                  description={t('Only follow links within the seed domain.')}
                />
                <Switch
                  checked={c.crawlAllowSubdomains}
                  onChange={(v) => updateConfig({ crawlAllowSubdomains: v })}
                  reverse
                  disabled={!c.crawlSameDomain}
                  label={t('Include subdomains')}
                  description={t('Also crawl blog.*, docs.*, and other subdomains.')}
                />
              </div>

              <div className="grid grid-cols-1 gap-4 border-t border-border pt-4 @pair/stage:grid-cols-2">
                <PathsField
                  label={t('Include paths')}
                  hint={t('one per line')}
                  placeholder={'/docs\n/blog'}
                  value={c.crawlIncludePaths}
                  onChange={(v) => updateConfig({ crawlIncludePaths: v })}
                />
                <PathsField
                  label={t('Exclude paths')}
                  hint={t('one per line')}
                  placeholder={'/login\n/cart'}
                  value={c.crawlExcludePaths}
                  onChange={(v) => updateConfig({ crawlExcludePaths: v })}
                />
              </div>

            </Drawer>
            )}
          </div>
          )}
        </div>
      </div>
    </StageBackdrop>
  );
};

export default SiteCrawlPanel;
