import React from 'react';
import { useTranslation } from 'react-i18next';
import { useParams, useSearchParams, Link } from 'react-router-dom';
import { useRequireAuth } from '../../hooks/useAuth';
import { useQuery } from '../../hooks/useQuery';
import { Q } from '../../stores/queryKeys';
import { targetsApi, selectorsApi, triggersApi } from '../../api/endpoints';
import { ArrowLeftIcon, GlobeAltIcon, ExclamationTriangleIcon, PencilSquareIcon, ClockIcon, ArrowPathIcon, SignalIcon, PlayIcon } from '@heroicons/react/24/outline';
import { CheckFastActions } from '../../components/checks/CheckFastActions';
import { EditTargetModal } from '../../components/checks/EditTargetModal';
import { LiveElapsed } from '../../components/checks/LiveSince';
import { PersonaPicker } from '../../components/workflows/PersonaPicker';
import { formatRelativeTime, cleanTitle, uiLocale } from '../../utils/format';
import { statusStyle } from '../../utils/statusStyle';
import { SectionHead } from '../../components/common/SectionHead';
import { AuthImage } from '../../components/common/AuthImage';
import { Select, Switch } from '../../components/ui';
import { FleetCapacityHint } from '../../components/checks/FleetCapacityHint';
import { useFleetCapacity } from '../../hooks/useFleetCapacity';
import toast from 'react-hot-toast';
import { toastIcon } from '../../components/ui/toastIcon';
import clsx from 'clsx';

function hostOf(url?: string): string | undefined {
  if (!url) return undefined;
  try { return new URL(url).hostname; } catch { return undefined; }
}
/** Compact host label (drops scheme + www.) for the toolbar title. */
function hostLabel(url?: string): string {
  if (!url) return '';
  try { return new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(url) ? url : 'https://' + url).hostname.replace(/^www\./, ''); } catch { return url; }
}

const PERIOD_LABELS: Record<number, string> = {
  10000: '10s', 30000: '30s', 60000: '1m', 300000: '5m',
  900000: '15m', 3600000: '1h', 86400000: '24h',
};

// Surfaced as primary check settings (interval + geo + JS). Kept in sync with
// the creation wizard's ContentMonitorPanel options.
const INTERVAL_PRESETS = [
  { ms: 10000, label: '10s' }, { ms: 30000, label: '30s' }, { ms: 60000, label: '1m' },
  { ms: 300000, label: '5m' }, { ms: 900000, label: '15m' }, { ms: 3600000, label: '1h' },
  { ms: 86400000, label: '24h' },
];
const REGION_OPTIONS = [
  { value: '', label: 'Any region' },
  { value: 'us-east', label: 'US East' }, { value: 'us-west', label: 'US West' },
  { value: 'eu-west', label: 'EU West' }, { value: 'eu-central', label: 'EU Central' },
  { value: 'ap-southeast', label: 'AP SE' },
];
const TIMEOUT_PRESETS = [
  { ms: 5000, label: '5s' }, { ms: 10000, label: '10s' },
  { ms: 30000, label: '30s' }, { ms: 60000, label: '60s' },
];

type TabId = 'overview' | 'changes' | 'settings';
const VALID_TABS: TabId[] = ['overview', 'changes', 'settings'];

export const CheckDetailPage: React.FC = () => {
  useRequireAuth();
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();

  // Tabbed shell (mirrors WorkflowDetailPage): ?tab= drives the active tab,
  // default "overview" keeps a clean URL.
  const [searchParams, setSearchParams] = useSearchParams();
  const rawTab = searchParams.get('tab');
  const activeTab: TabId = (VALID_TABS as string[]).includes(rawTab || '') ? (rawTab as TabId) : 'overview';
  const setActiveTab = (tab: TabId) => setSearchParams(tab === 'overview' ? {} : { tab }, { replace: true });

  // Adaptive polling: poll the live target + its change/error feeds only while
  // the check is enabled (actively running). Start enabled so the first load
  // polls; the effect below stops it once we learn the check is paused.
  const [livePoll, setLivePoll] = React.useState<number | undefined>(15000);
  const [changesPoll, setChangesPoll] = React.useState<number | undefined>(30000);
  const { data: target, loading, error, refresh } = useQuery(Q.target(id!), () => targetsApi.getById(id!), { pollInterval: livePoll });
  // Selectors + triggers are static config (only change on edit / trigger
  // changes) — don't poll them; refresh on the edit-modal save instead.
  const { data: selectors, refresh: refreshSelectors } = useQuery(Q.targetSelectors(id!), () => selectorsApi.listForTarget(Number(id)));
  const [editing, setEditing] = React.useState(false);
  const { data: triggers, refresh: refreshTriggers } = useQuery(Q.targetTriggers(id!), () => triggersApi.listForTarget(Number(id)));
  // Live result data (changes feed + error feed) — only worth polling while the
  // check is actively running (enabled). Paused = nothing new lands.
  const { data: changes } = useQuery(Q.targetChanges(id!), () => targetsApi.getChanges(id!), { pollInterval: changesPoll });
  const { data: checkErrors } = useQuery(Q.targetErrors(id!), () => targetsApi.getErrors(id!), { pollInterval: changesPoll });

  // Stop polling once we know the check is paused; resume when re-enabled. The
  // cadence depends on the very payload the queries return, so it can't be
  // computed before the calls above — it's reconciled DURING RENDER (React's
  // adjust-state escape hatch) instead of from an effect, which would spend a
  // second render pass on every poll. `target` null = keep the initial intervals
  // until the first load lands.
  if (target) {
    const active = (target as any).enabled !== false;
    const wantLive = active ? 15000 : undefined;
    const wantChanges = active ? 30000 : undefined;
    if (livePoll !== wantLive) setLivePoll(wantLive);
    if (changesPoll !== wantChanges) setChangesPoll(wantChanges);
  }

  // Inline editing of the primary check settings (interval / region / JS).
  const [savingSetting, setSavingSetting] = React.useState<string | null>(null);
  const fleetCap = useFleetCapacity();
  const saveSetting = async (patch: Record<string, any>, key: string) => {
    setSavingSetting(key);
    try {
      const updated: any = await targetsApi.update(id!, patch as any);
      // Surface the server's capacity advisory when the interval was changed to a
      // cadence the current fleet can't meet (it will run slower until more agents).
      const warn = updated?.capacityWarning ?? updated?.data?.capacityWarning;
      if (key === 'period' && warn) {
        toast(warn, { icon: toastIcon(ExclamationTriangleIcon, 'text-amber-500'), duration: 7000 });
      } else {
        toast.success(t('Saved'));
      }
      refresh();
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || t('Failed to save setting'));
    } finally {
      setSavingSetting(null);
    }
  };

  const initialLoading = loading && !target;
  const loadFailed = !target && !initialLoading && Boolean(error);

  if (initialLoading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <div className="text-sm text-secondary">{t('Loading monitor...')}</div>
      </div>
    );
  }

  if (loadFailed || !target) {
    return (
      <div className="px-4 sm:px-6 py-6">
        <div className="flex flex-col @pair/stage:flex-row @pair/stage:items-center @pair/stage:justify-between gap-3 rounded-xl border border-ink/20 bg-surface shadow-sm px-4 py-3.5">
          <div className="flex items-start gap-3 min-w-0">
            <ExclamationTriangleIcon className="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
            <div className="min-w-0">
              <p className="text-[13px] font-medium text-ink">{t("Couldn't load monitor")}</p>
              <p className="text-xs text-secondary mt-0.5">{error || t('This monitor may have been deleted.')}</p>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={() => refresh()}
              className="px-3 py-1.5 bg-accent-strong text-accent-on text-[12px] font-semibold rounded-lg shadow-sm hover:bg-accent-strong/90 transition-colors"
            >
              {t('Retry')}
            </button>
            <Link
              to="/checks"
              className="px-3 py-1.5 border border-border text-ink text-[12px] font-medium rounded-lg hover:bg-chrome transition-colors"
            >
              {t('Back to monitors')}
            </Link>
          </div>
        </div>
      </div>
    );
  }

  const selectorList = selectors || [];
  const triggerList = triggers || [];
  const changeList = changes || [];
  const errorList = checkErrors?.errors || [];

  // ── Health + key facts for the status line (how this monitor is doing) ──
  const enabled = target.enabled !== false;
  const state = (target as any).state ?? (target as any).health_state;
  const health = !enabled
    ? { label: t('Paused'), dot: 'bg-active', text: 'text-tertiary' }
    : state === 'up' || state === 'ok' ? { label: t('Healthy'), dot: 'bg-green-500', text: 'text-emerald-600' }
    : state === 'down' ? { label: t('Down'), dot: 'bg-red-500', text: 'text-red-600' }
    : state === 'stale' ? { label: t('Stale'), dot: 'bg-amber-500', text: 'text-amber-600' }
    : { label: t('Pending'), dot: 'bg-active', text: 'text-tertiary' };
  const lastChecked = (target as any).lastCheckedAt ?? (target as any).last_checked_at;
  const lastCheckedClock = lastChecked ? new Date(lastChecked).toLocaleTimeString(uiLocale()) : null;
  const periodMs = target.checkPeriodMs || 60000;
  const intervalLabel = PERIOD_LABELS[periodMs] || `${Math.round(periodMs / 1000)}s`;
  const changesCount = (target as any).changesCount ?? (target as any).changes_count ?? changeList.length;
  const lastStatusCode = (target as any).statusCode ?? (target as any).status_code;
  const region = (target as any).preferredRegion;
  const regionLabel = REGION_OPTIONS.find((r) => r.value === region)?.label;
  const isBrowser = (target as any).requiresPlaywright === true;

  const TABS: { id: TabId; label: string; count?: number }[] = [
    { id: 'overview', label: t('Overview') },
    { id: 'changes', label: t('Changes'), count: changesCount || undefined },
    { id: 'settings', label: t('Settings') },
  ];
  const tabButton = (tab: { id: TabId; label: string; count?: number }) => (
    <button
      key={tab.id}
      onClick={() => setActiveTab(tab.id)}
      className={clsx(
        'px-2.5 py-1 rounded-md text-[12px] font-medium transition-colors',
        activeTab === tab.id ? 'bg-surface text-ink shadow-sm' : 'text-tertiary hover:text-secondary hover:bg-surface/60',
      )}
    >
      {tab.label}
      {tab.count !== undefined && (
        <span className={clsx('ml-1 text-[10px] tabular-nums', activeTab === tab.id ? 'text-secondary' : 'text-tertiary')}>{tab.count}</span>
      )}
    </button>
  );

  // ── Recent changes list (shared by Overview preview + Changes tab) ──
  const renderChanges = (limit: number) => (
    <div className="bg-surface border border-ink/20 rounded-xl divide-y divide-border shadow-sm">
      {changeList.slice(0, limit).map((change: any, i: number) => {
        const when = change.timestamp || change.detected_at;
        const before = change.screenshotBefore;
        const after = change.screenshotAfter;
        const diff = change.screenshotDiff;
        const isVisual = Boolean(before || after || diff);
        return (
          <div key={change.id || i} className="px-4 py-3">
            <div className="flex items-center justify-between">
              <span className="text-[13px] text-ink">{change.selectorName || change.selector_name || t('Change detected')}</span>
              <span className="text-[11px] text-tertiary">{when ? formatRelativeTime(when) : ''}</span>
            </div>
            {isVisual ? (
              <div className="flex gap-3 mt-2">
                {[{ label: t('Before'), src: before }, { label: t('After'), src: after }, { label: t('Changed'), src: diff }].map((img) => (
                  <div key={img.label} className="flex-1 min-w-0">
                    <div className="text-[10px] text-tertiary mb-1">{img.label}</div>
                    {img.src ? (
                      <AuthImage
                        src={img.src}
                        alt={img.label}
                        className="w-full rounded-lg border border-border bg-canvas"
                        fallbackClassName="w-full h-20 rounded-lg border border-dashed border-border bg-canvas"
                      />
                    ) : (
                      <div className="w-full h-20 rounded-lg border border-dashed border-border bg-canvas flex items-center justify-center text-[10px] text-tertiary">{t('n/a')}</div>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              (change.diff || change.diff_snippet) && (
                <p className="text-[12px] text-secondary mt-0.5 truncate">{change.diff || change.diff_snippet}</p>
              )
            )}
          </div>
        );
      })}
    </div>
  );

  const renderProblems = (limit: number) => (
    <div className="bg-surface border border-ink/20 rounded-xl divide-y divide-border shadow-sm">
      {errorList.slice(0, limit).map((err) => (
        <div key={err.id} className="px-4 py-2.5">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[12px] font-medium text-ink">{err.error_type}{err.http_status ? ` · HTTP ${err.http_status}` : ''}</span>
            <span className="text-[11px] text-tertiary whitespace-nowrap">{err.occurred_at ? formatRelativeTime(err.occurred_at) : ''}</span>
          </div>
          <p className="text-[12px] text-secondary mt-0.5 break-all line-clamp-2">{err.error_message}</p>
        </div>
      ))}
    </div>
  );

  const wiredAutomations = triggerList.length > 0 && (
    <section>
      <SectionHead title={t('What happens on a change')} description={t('Automations that run when this monitor detects a change.')} />
      <div className="bg-surface border border-ink/20 rounded-xl divide-y divide-border shadow-sm">
        {triggerList.map((trigger: any) => (
          <Link
            key={trigger.id}
            to={`/automations/${trigger.id}`}
            className="flex items-center justify-between gap-2 px-4 py-2.5 hover:bg-chrome transition-colors"
          >
            <span className="text-[13px] text-ink truncate">{cleanTitle(trigger.name, t('Untitled'))}</span>
            <div className={clsx('w-2 h-2 rounded-full shrink-0', statusStyle(trigger.enabled ? 'active' : 'paused').dot)} title={trigger.enabled ? t('Active') : t('Paused')} />
          </Link>
        ))}
      </div>
    </section>
  );

  return (
    <>
      <div className="flex flex-col h-full">
        {/* Toolbar — identity + tabs + primary action (mirrors WorkflowDetailPage) */}
        <div className="flex items-center gap-3 h-12 px-4 sm:px-6 bg-chrome chrome-topbar border-b border-border shrink-0">
          <Link to="/checks" className="p-1 text-tertiary hover:text-ink transition-colors">
            <ArrowLeftIcon className="w-3.5 h-3.5" />
          </Link>
          <span className="relative shrink-0">
            <GlobeAltIcon className="w-4 h-4 text-tertiary" />
            <span className={clsx('absolute -bottom-0.5 -right-0.5 w-2 h-2 rounded-full border border-chrome', health.dot)} />
          </span>
          <span className="text-[13px] font-semibold text-ink truncate" title={target.url}>{hostLabel(target.url)}</span>
          <span className="hidden @rail/stage:inline text-[11px] text-tertiary">
            {t('Every {{period}}', { period: intervalLabel })} · {selectorList.length === 1 ? t('{{n}} selector', { n: selectorList.length }) : t('{{n}} selectors', { n: selectorList.length })}
          </span>

          <div className="hidden @pair/stage:flex items-center gap-0.5 ml-2">
            {TABS.map(tabButton)}
          </div>

          <div className="flex-1" />

          <div className="flex items-center gap-1.5 shrink-0">
            {!enabled && (
              <button
                onClick={() => saveSetting({ enabled: true }, 'enabled')}
                disabled={savingSetting === 'enabled'}
                className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-semibold bg-accent-strong text-accent-on rounded-lg shadow-sm hover:bg-accent-strong/90 disabled:opacity-50 transition-colors"
              >
                <PlayIcon className="w-3.5 h-3.5" />
                {t('Resume')}
              </button>
            )}
            <button
              onClick={() => setEditing(true)}
              className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium text-ink border border-border rounded-lg hover:bg-surface/60 transition-colors"
            >
              <PencilSquareIcon className="w-3.5 h-3.5" />
              <span className="hidden @pair/stage:inline">{t('Edit')}</span>
            </button>
          </div>
        </div>

        {/* Mobile tab row */}
        <div className="@pair/stage:hidden flex items-center gap-0.5 px-3 h-11 bg-chrome border-b border-border overflow-x-auto shrink-0">
          {TABS.map(tabButton)}
        </div>

        <div className="flex-1 overflow-auto">
          <div className="max-w-5xl mx-auto px-4 sm:px-6 py-6">

            {/* ══ OVERVIEW ══ */}
            {activeTab === 'overview' && (
              <div className="space-y-8">
                {/* Health hero — live "time since last check" as the principal metric. */}
                <section>
                  <span className="text-[11px] font-medium uppercase tracking-wide text-tertiary">{t('Time since last check')}</span>
                  <div className="flex items-baseline gap-2.5 mt-1.5">
                    <LiveElapsed since={lastChecked} live={enabled} className={clsx('text-[44px] leading-none font-semibold', health.text)} />
                    <span className="inline-flex items-center gap-1.5 text-[13px] text-secondary pb-1">
                      {enabled && lastChecked && (
                        <span className="relative flex h-1.5 w-1.5" aria-hidden="true">
                          <span className={clsx('absolute inline-flex h-full w-full rounded-full opacity-60 animate-ping', health.dot)} />
                          <span className={clsx('relative inline-flex h-1.5 w-1.5 rounded-full', health.dot)} />
                        </span>
                      )}
                      {health.label}
                    </span>
                  </div>
                  {lastCheckedClock && (
                    <p className="text-[13px] text-secondary mt-2">{t('Last checked at {{time}}', { time: lastCheckedClock })}</p>
                  )}

                  {/* Facts strip — one divided bar */}
                  <div className="mt-6 bg-surface rounded-xl border border-ink/20 shadow grid grid-cols-1 @rail/stage:grid-cols-3 divide-y @rail/stage:divide-y-0 @rail/stage:divide-x divide-border">
                    <div className="p-5">
                      <span className="text-[11px] uppercase tracking-wide text-tertiary font-medium inline-flex items-center gap-1.5"><ClockIcon className="w-3.5 h-3.5" />{t('Check interval')}</span>
                      <p className="text-xl font-semibold text-ink mt-1.5 tabular-nums">{intervalLabel}</p>
                      <p className="text-[11px] text-tertiary mt-0.5">{t('between checks')}</p>
                    </div>
                    <div className="p-5">
                      <span className="text-[11px] uppercase tracking-wide text-tertiary font-medium inline-flex items-center gap-1.5"><ArrowPathIcon className="w-3.5 h-3.5" />{t('Changes')}</span>
                      <p className="text-xl font-semibold text-ink mt-1.5 tabular-nums">{changesCount}</p>
                      <p className="text-[11px] text-tertiary mt-0.5">{t('detected so far')}</p>
                    </div>
                    <div className="p-5">
                      <span className="text-[11px] uppercase tracking-wide text-tertiary font-medium inline-flex items-center gap-1.5"><SignalIcon className="w-3.5 h-3.5" />{t('Last response')}</span>
                      <p className="text-xl font-semibold text-ink mt-1.5 tabular-nums">{lastStatusCode != null ? lastStatusCode : '—'}</p>
                      <p className="text-[11px] text-tertiary mt-0.5">{isBrowser ? t('Browser render') : t('HTTP fetch')}{regionLabel ? ` · ${t(regionLabel)}` : ''}</p>
                    </div>
                  </div>
                </section>

                {/* Automate on change */}
                <CheckFastActions
                  targetId={Number(id)}
                  targetUrl={target.url}
                  triggers={triggerList}
                  onCreated={() => { refreshTriggers(); }}
                />

                {/* Recent changes preview → deep-links into the Changes tab */}
                {changeList.length > 0 && (
                  <section>
                    <div className="flex items-end justify-between mb-3">
                      <div>
                        <h2 className="text-base font-semibold text-ink tracking-tight">{t('Recent changes')}</h2>
                        <p className="text-[12px] text-secondary mt-0.5">{t('What changed on the page, newest first.')}</p>
                      </div>
                      {changeList.length > 5 && (
                        <button onClick={() => setActiveTab('changes')} className="text-xs text-tertiary hover:text-ink transition-colors">{t('View all')}</button>
                      )}
                    </div>
                    {renderChanges(5)}
                  </section>
                )}

                {wiredAutomations}

                {/* Recent problems */}
                {errorList.length > 0 && (
                  <section>
                    <SectionHead title={t('Recent problems')} description={t("Times this monitor couldn't check the page. Use these to spot a broken page or a needed login.")} />
                    {renderProblems(5)}
                  </section>
                )}
              </div>
            )}

            {/* ══ CHANGES ══ */}
            {activeTab === 'changes' && (
              <div className="space-y-8">
                {/* What this monitor watches */}
                <section>
                  <SectionHead
                    title={t('What this monitor watches')}
                    description={t('The parts of the page tracked for changes — an alert fires when any of these change.')}
                  />
                  {selectorList.length === 0 ? (
                    <p className="text-[13px] text-secondary">{t('Watching the whole page for any change.')}</p>
                  ) : (
                    <div className="bg-surface border border-ink/20 rounded-xl divide-y divide-border shadow-sm">
                      {selectorList.map((s: any) => {
                        const isVisual = s.content_type === 'visual' || Boolean(s.baseline_image_url) || Boolean(s.visual_region);
                        return (
                          <div key={s.id} className="px-4 py-2.5">
                            <div className="flex items-baseline justify-between gap-3">
                              <span className="text-[13px] text-ink shrink-0">{s.name || s.label || t('Tracked element')}</span>
                              {isVisual ? (
                                <span className="text-[11px] text-tertiary shrink-0">{t('Visual zone')}</span>
                              ) : (
                                <code className="text-[11px] text-tertiary font-mono break-all text-right">{s.selector || s.cssSelector || s.css_selector || '—'}</code>
                              )}
                            </div>
                            {isVisual && (
                              <div className="mt-2">
                                <div className="text-[10px] text-tertiary mb-1">{t('Baseline')}</div>
                                {s.baseline_image_url ? (
                                  <AuthImage
                                    src={s.baseline_image_url}
                                    alt={t('Baseline')}
                                    className="max-w-[240px] w-full rounded-lg border border-border bg-canvas"
                                    fallbackClassName="w-[240px] h-20 rounded-lg border border-dashed border-border bg-canvas"
                                  />
                                ) : (
                                  <div className="w-[240px] h-20 rounded-lg border border-dashed border-border bg-canvas flex items-center justify-center text-[10px] text-tertiary">{t('No baseline captured yet')}</div>
                                )}
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </section>

                {/* Full recent changes */}
                <section>
                  <SectionHead title={t('Recent changes')} description={t('What changed on the page, newest first.')} />
                  {changeList.length === 0 ? (
                    <p className="text-[13px] text-secondary">{t('No changes detected yet — you’ll see them here.')}</p>
                  ) : renderChanges(20)}
                </section>

                {/* Recent problems */}
                {errorList.length > 0 && (
                  <section>
                    <SectionHead title={t('Recent problems')} description={t("Times this monitor couldn't check the page. Use these to spot a broken page or a needed login.")} />
                    {renderProblems(10)}
                  </section>
                )}
              </div>
            )}

            {/* ══ SETTINGS ══ */}
            {activeTab === 'settings' && (
              <div className="max-w-2xl space-y-8">
                {/* Settings — frequency, region, JS, advanced */}
                <section>
                  <SectionHead title={t('Settings')} description={t('How often, and from where, this monitor runs.')} />
                  <div className="bg-surface border border-ink/20 rounded-xl p-4 divide-y divide-border shadow-sm">
                    {/* Active / pause */}
                    <div className="flex items-center justify-between gap-4 pb-4">
                      <div>
                        <div className="text-xs font-medium text-secondary">{target.enabled ? t('Active') : t('Paused')}</div>
                        <div className="text-[11px] text-tertiary">{t('Pause to stop running this monitor without deleting it.')}</div>
                      </div>
                      <Switch size="sm" disabled={savingSetting === 'enabled'} checked={target.enabled} onChange={() => saveSetting({ enabled: !target.enabled }, 'enabled')} aria-label={t('Active')} />
                    </div>
                    {/* Check frequency */}
                    <div className="py-4">
                      <div className="text-xs font-medium text-secondary mb-1.5">{t('Check frequency')}</div>
                      <div className="flex flex-wrap gap-1">
                        {INTERVAL_PRESETS.map((p) => {
                          const active = (target.checkPeriodMs || 60000) === p.ms;
                          // Faster than the connected fleet can deliver (needs more staggered agents).
                          const infeasible = !!fleetCap && fleetCap.agents_online > 0 && p.ms < fleetCap.min_interval_ms;
                          const agentsNeeded = fleetCap?.presets.find((pr) => pr.interval_ms === p.ms)?.agents_required;
                          return (
                            <button key={p.ms} disabled={savingSetting === 'period'} title={infeasible && agentsNeeded ? t('Needs {{n}} connected agents', { n: agentsNeeded }) : undefined} onClick={() => !active && saveSetting({ check_period_ms: p.ms }, 'period')} className={clsx('px-2.5 py-1.5 rounded-lg text-[11px] font-medium transition-all disabled:opacity-50', active ? 'bg-ink text-white shadow-sm' : 'bg-chrome text-secondary hover:text-ink', infeasible && !active && 'opacity-45')}>{p.label}</button>
                          );
                        })}
                      </div>
                      <FleetCapacityHint checkPeriodMs={target.checkPeriodMs ?? null} />
                    </div>
                    {/* Region (geo) */}
                    <div className="py-4">
                      <div className="text-xs font-medium text-secondary mb-1.5">{t('Region')}</div>
                      <Select value={(target as any).preferredRegion || ''} disabled={savingSetting === 'region'} onChange={(v) => saveSetting({ preferred_region: v || null }, 'region')} size="sm" options={REGION_OPTIONS.map((r) => ({ value: r.value, label: t(r.label) }))} />
                    </div>
                    {/* JavaScript rendering */}
                    <div className="flex items-center justify-between gap-4 py-4">
                      <div>
                        <div className="text-xs font-medium text-secondary">{t('JavaScript rendering')}</div>
                        <div className="text-[11px] text-tertiary">{t('Run in a full browser for JS-heavy pages.')}</div>
                      </div>
                      <Switch size="sm" disabled={savingSetting === 'js'} checked={(target as any).requiresPlaywright} onChange={() => saveSetting({ requires_playwright: !(target as any).requiresPlaywright }, 'js')} aria-label={t('JavaScript rendering')} />
                    </div>
                    {/* Verify SSL */}
                    <div className="flex items-center justify-between gap-4 py-4">
                      <div>
                        <div className="text-xs font-medium text-secondary">{t('Verify SSL certificate')}</div>
                        <div className="text-[11px] text-tertiary">{t('Fail the check on an invalid or expired certificate.')}</div>
                      </div>
                      <Switch size="sm" disabled={savingSetting === 'ssl'} checked={(target as any).checkSsl ?? true} onChange={() => saveSetting({ check_ssl: !((target as any).checkSsl ?? true) }, 'ssl')} aria-label={t('Verify SSL certificate')} />
                    </div>
                    {/* Request timeout */}
                    <div className="pt-4">
                      <div className="text-xs font-medium text-secondary mb-1.5">{t('Request timeout')}</div>
                      <div className="flex flex-wrap gap-1">
                        {TIMEOUT_PRESETS.map((p) => {
                          const active = ((target as any).timeoutMs || 10000) === p.ms;
                          return (
                            <button key={p.ms} disabled={savingSetting === 'timeout'} onClick={() => !active && saveSetting({ timeout_ms: p.ms }, 'timeout')} className={clsx('px-2.5 py-1.5 rounded-lg text-[11px] font-medium transition-all disabled:opacity-50', active ? 'bg-ink text-white shadow-sm' : 'bg-chrome text-secondary hover:text-ink')}>{p.label}</button>
                          );
                        })}
                      </div>
                    </div>
                  </div>
                </section>

                {/* Sign-in identity */}
                <section>
                  <SectionHead title={t('Sign-in identity')} description={t('Attach a saved identity (persona) so this monitor can check a page that needs a login.')} />
                  <PersonaPicker
                    value={(target as any).persona_id ?? null}
                    domain={hostOf(target.url)}
                    allowClear
                    label={t('Persona')}
                    onChange={async (pid) => {
                      try {
                        const r = await targetsApi.setPersona(id!, pid);
                        toast.success(pid ? (r.detail || t('Persona attached')) : t('Persona detached'));
                        refresh();
                      } catch { toast.error(t('Failed to update persona')); }
                    }}
                  />
                </section>
              </div>
            )}
          </div>
        </div>
      </div>

      <EditTargetModal
        targetId={Number(id)}
        isOpen={editing}
        onClose={() => setEditing(false)}
        onSaved={() => { refresh(); refreshSelectors(); refreshTriggers(); }}
      />
    </>
  );
};
