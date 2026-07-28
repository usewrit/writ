import React, { useEffect } from 'react';
import { useRequireAuth } from '../hooks/useAuth';
import { useTour } from '../onboarding/TourProvider';
import { getOnboarding } from '../onboarding/storage';
import { useDocumentTitle } from '../hooks/useDocumentTitle';
import { Link, useNavigate } from 'react-router-dom';
import client from '../api/client';
import { useQuery } from '../hooks/useQuery';
import { Q } from '../stores/queryKeys';
import { NeedsAttention } from '../components/home/NeedsAttention';
import { useDismissedFailures } from '../components/home/useDismissedFailures';
import { RecentActivity } from '../components/home/RecentActivity';
import { CreateRail, FleetStatus } from '../components/home/HomeRail';
import { homeHealthApi } from '../api/homeHealth';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { Stagger } from '../components/ui/Animated';
import { NavMini, type NavMiniKind } from '../components/ui/NavMini';
import {
  ArrowRightIcon,
} from '@heroicons/react/24/outline';

// Small inline status pill for the header. Optionally a link (counts → their list
// page). Fill is rationed so the pills don't all compete: only the meaningful status
// pill is subtly filled (`filled`) and attention is amber; neutral count pills are
// outline-only and pick up a chrome tint on hover.
const Pill: React.FC<{ label: string; dot?: 'ok' | 'idle'; warn?: boolean; filled?: boolean; to?: string }> = ({ label, dot, warn, filled, to }) => {
  const cls = clsx(
    'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11.5px] font-medium transition-colors',
    warn ? 'border-amber-300 bg-amber-50 text-amber-700'
      : filled ? 'border-border bg-chrome text-ink'
      : 'border-border text-secondary',
    to && (warn ? 'hover:bg-amber-100' : 'hover:text-ink hover:border-ink/20 hover:bg-chrome'),
  );
  const inner = (
    <>
      {dot && <span className={clsx('w-1.5 h-1.5 rounded-full', dot === 'ok' ? 'bg-emerald-500' : 'bg-zinc-300')} />}
      {label}
    </>
  );
  return to ? <Link to={to} className={cls}>{inner}</Link> : <span className={cls}>{inner}</span>;
};

// The direct start doors — one per core surface. Same shape and vocabulary as the
// managed app's empty home so a self-hoster and a cloud user learn ONE home.
//
// These were two filled/outlined CARD buttons that named only two of the three
// surfaces: HARVEST was missing entirely, so the crawl product had no door on the
// page that is supposed to be the map of the product. Authored as RULED COLUMNS,
// not cards (the design language's "de-card by default" rule): the top hairline IS
// the frame, it asserts to the brand accent on hover, and the mono glyph reuses the
// same `▸API` / `MON` / `CRWL` vocabulary the nav uses for these surfaces.
const START_DOORS: Array<{
  id: string; glyph: string; mini: NavMiniKind; tour?: string;
  title: string; description: string; to: string;
}> = [
  {
    id: 'record',
    glyph: '▸API',
    mini: 'collapse',
    tour: 'home-workflow',
    title: 'Record a workflow',
    description: 'Capture it once, replay it as an API.',
    to: '/workflows/new',
  },
  {
    id: 'monitor',
    glyph: 'MON',
    mini: 'spark',
    title: 'Monitor a target',
    description: 'Watch a page, get alerted when it moves.',
    to: '/checks/new',
  },
  {
    id: 'harvest',
    glyph: 'CRWL',
    mini: 'frontier',
    title: 'Harvest a site',
    description: 'An AI agent crawls a whole site into data.',
    to: '/crawls/new',
  },
];

export const Dashboard: React.FC = () => {
  const { t } = useTranslation();
  useRequireAuth();
  useDocumentTitle(t('Home'));
  const navigate = useNavigate();
  const { data: targets } = useQuery(Q.targets('content'), () => client.get('/targets?check_type=content').then(r => r.data || []));
  const { data: workflows } = useQuery(Q.workflows(), () => client.get('/automation/workflows').then(r => r.data || []));
  const { data: triggers } = useQuery(Q.triggers(), () => client.get('/triggers/all').then(r => r.data || []));
  const { data: failures, loading: failuresLoading } = useQuery(
    Q.key('home:recent-failures'),
    () => homeHealthApi.listRecentFailures(10),
    { pollInterval: 30000 },
  );

  const { dismissed, dismiss } = useDismissedFailures();
  const targetList = targets || [];
  const workflowList = workflows || [];
  const triggerList = triggers || [];
  // What still needs a human. Drops rows the user dismissed (persisted locally) and
  // rows AI already resolved — either this run's own in-session repair, or a later
  // repair of the same workflow. A repair that FAILED is never dropped: that's exactly
  // the case a human has to pick up, so it stays and gets a "Repair failed" badge.
  const failureList = React.useMemo(
    () => (failures || []).filter(f => (
      !dismissed.has(f.id) &&
      (f.repair_failed || !(f.repaired || f.repaired_since))
    )),
    [failures, dismissed],
  );
  const needAttention = failureList.length;

  // ── Auto-start the Tier-1 interactive tour once for brand-new accounts ──
  // Persistence (globalStatus) — not a mount-scoped ref — is what prevents the
  // tour from re-showing, which keeps it correct under React StrictMode's
  // double-invoke.
  const { startTour } = useTour();
  useEffect(() => {
    const ob = getOnboarding();
    if (ob.dismissedForever || ob.globalStatus !== 'pending') return;
    const timer = setTimeout(() => startTour('global'), 500);
    return () => clearTimeout(timer);
  }, [startTour]);

  // ── Command center — the one and only Home, empty account or not. A new
  //    install lands here too: the counts read zero, the two create doors are
  //    the same ones a first-run poster would have shown, and the feed/rail
  //    carry their own empty copy. No separate first-run screen to fall out of
  //    the moment the first workflow exists. ──
  return (
    <div className="px-7 sm:px-8 py-7 max-w-[1400px] mx-auto">
      {/* Title + positioning + inline status pills (counts folded in) · create actions */}
      <div className="flex flex-col @split/stage:flex-row @split/stage:items-start @split/stage:justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-[24px] font-semibold text-ink tracking-tight leading-tight">{t('Self-hosted website APIs')}</h1>
          <p className="text-[13px] text-secondary mt-1 max-w-2xl">
            {t('Operate website APIs and your execution fleet entirely inside your infrastructure.')}
          </p>
          <div className="flex flex-wrap items-center gap-1.5 mt-2.5">
            <Pill dot="ok" filled label={t('Self-hosted')} />
            <Pill to="/checks" label={t('{{n}} monitors', { n: targetList.length })} />
            <Pill to="/workflows" label={t('{{n}} workflows', { n: workflowList.length })} />
            <Pill to="/automations" label={t('{{n}} automations', { n: triggerList.length })} />
            {needAttention > 0 && <Pill warn to="/automations" label={t('{{n}} need attention', { n: needAttention })} />}
          </div>
        </div>
      </div>

      {/* The direct doors — one per core surface.
          Ruled columns (the managed app's empty home) were tried here and read as
          three unanchored text blocks: that treatment needs the airy hero page it
          was designed for, and this is a POPULATED command center with a dense
          header directly above and the working grid directly below. Per the design
          language, an element cannot just be "placed there" — so the three doors
          share ONE container and become a single deliberate band, divided rather
          than boxed so it still reads as columns and not as three cards. The band
          spans the full width, which also lets it sit flush with the grid below.
          The mono glyph vocabulary (`▸API` / `MON` / `CRWL`) is kept — it is how
          the nav names these same surfaces. */}
      <Stagger
        className="mt-5 grid grid-cols-1 divide-y divide-border overflow-hidden rounded-xl border border-ink/15 bg-surface shadow-sm @pair/stage:grid-cols-3 @pair/stage:divide-x @pair/stage:divide-y-0"
        staggerMs={60}
      >
        {START_DOORS.map(door => (
          <button
            key={door.id}
            data-tour={door.tour}
            onClick={() => navigate(door.to)}
            className="nav-row group flex h-full flex-col justify-start px-5 py-4 text-left transition-colors duration-200 hover:bg-hover/50"
          >
            <span className="flex items-center gap-2">
              <span className="font-mono text-[10px] leading-none tracking-[0.08em] text-tertiary transition-colors duration-200 group-hover:text-accent-strong">
                {door.glyph}
              </span>
              <h2 className="text-[13.5px] font-semibold leading-tight text-ink">{t(door.title)}</h2>
              <ArrowRightIcon className="h-3 w-3 shrink-0 text-tertiary transition-transform duration-200 group-hover:translate-x-0.5 motion-reduce:transition-none" />
              <NavMini kind={door.mini} />
            </span>
            <p className="mt-1.5 text-xs leading-relaxed text-tertiary transition-colors duration-200 group-hover:text-secondary">
              {t(door.description)}
            </p>
          </button>
        ))}
      </Stagger>


      {/* Working grid: operational feed (main) · create + local status (rail) */}
      <div className="mt-5 grid grid-cols-1 @split/stage:grid-cols-[minmax(0,1fr)_320px] gap-6 items-start">
        <div className="min-w-0 space-y-5">
          {failureList.length > 0 && <NeedsAttention failures={failureList} loading={failuresLoading} onDismiss={dismiss} />}
          <RecentActivity />
        </div>
        <aside className="min-w-0 space-y-4">
          <CreateRail />
          <FleetStatus />
        </aside>
      </div>
    </div>
  );
};
