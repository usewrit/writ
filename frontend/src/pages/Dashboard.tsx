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
import {
  ArrowRightIcon,
  EyeIcon,
  CodeBracketIcon,
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

      {/* The two doors. On the managed app this row is the assistant command bar;
          self-host has no concierge, so rather than leave the slot empty (or hide
          the actions in the header as two small buttons), the create paths take
          it over at full width. They are the point of the product, so they get
          the weight: convert a website into an API, or put one under watch. The
          rail below stays the exhaustive menu — this is just the two headline
          verbs. */}
      <div className="mt-3.5 grid grid-cols-1 @pair/stage:grid-cols-2 gap-3">
        <button
          data-tour="home-workflow"
          onClick={() => navigate('/workflows/new')}
          className="group flex items-center gap-3 rounded-xl bg-accent-strong px-4 py-3.5 text-left text-accent-on shadow-sm transition-colors hover:bg-accent-strong/90"
        >
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-white/15">
            <CodeBracketIcon className="h-[18px] w-[18px]" />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block text-[14.5px] font-semibold leading-tight">{t('Convert a website into an API')}</span>
            <span className="mt-0.5 block truncate text-[12px] leading-relaxed text-accent-on/75">
              {t('Record it once, then call it as REST or MCP.')}
            </span>
          </span>
          <ArrowRightIcon className="h-4 w-4 shrink-0 transition-transform duration-200 group-hover:translate-x-0.5 motion-reduce:transition-none" />
        </button>

        <button
          onClick={() => navigate('/checks/new')}
          className="group flex items-center gap-3 rounded-xl border border-ink/30 bg-surface px-4 py-3.5 text-left transition-colors hover:border-ink/45 hover:bg-hover/40"
        >
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-chrome transition-colors group-hover:bg-ink">
            <EyeIcon className="h-[18px] w-[18px] text-ink transition-colors group-hover:text-white" />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block text-[14.5px] font-semibold leading-tight text-ink">{t('Create a monitor')}</span>
            <span className="mt-0.5 block truncate text-[12px] leading-relaxed text-tertiary transition-colors group-hover:text-secondary">
              {t('Watch a page and get notified when it changes.')}
            </span>
          </span>
          <ArrowRightIcon className="h-4 w-4 shrink-0 text-tertiary transition-transform duration-200 group-hover:translate-x-0.5 motion-reduce:transition-none" />
        </button>
      </div>

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
