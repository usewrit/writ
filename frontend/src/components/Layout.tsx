import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { getUser } from '../utils/auth';
import { performLogout } from '../hooks/useAuth';
import clsx from 'clsx';
import { useRecorderActivity } from './RecorderActivityContext';
import { NotificationBell } from './notifications/NotificationBell';
import { LocalePreferencesModal } from './LocalePreferencesModal';
import { ActivityIndicator } from './activity/ActivityIndicator';
import { useRunEventStream } from '../hooks/useRunEventStream';
import { PageTransition } from './ui/Animated';
import { NavMini, type NavMiniKind } from './ui/NavMini';
import { WritWordmark, WritTile } from './brand/WritMark';
import { ScrollArea } from './ui/ScrollArea';
import { prefetchRoute, prefetchAllRoutes } from '../routePrefetch';
import {
  Bars2Icon,
  XMarkIcon,
  HomeIcon,
  EyeIcon,
  CursorArrowRaysIcon,
  BoltIcon,
  CpuChipIcon,
  Cog6ToothIcon,
  ArrowRightStartOnRectangleIcon,
  LockClosedIcon,
  IdentificationIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  MagnifyingGlassIcon,
  SignalIcon,
  QueueListIcon,
  CircleStackIcon,
  TableCellsIcon,
  DocumentDuplicateIcon,
  SparklesIcon,
  GlobeAltIcon,
  BookOpenIcon,
  ArrowTopRightOnSquareIcon,
} from '@heroicons/react/24/outline';
import { CommandPalette } from './CommandPalette';
import { docsUrl, DOCS_LINK_PROPS } from '../utils/docs';

interface LayoutProps {
  children: React.ReactNode;
}


interface NavItem {
  name: string;
  href: string;
  icon: React.ElementType;
  /** Stable anchor key (legacy tour attribute; inert). */
  tour?: string;
  /** One-line "what lives here", shown once in the first-login sidebar map. */
  desc?: string;
  /**
   * Hover-reveal micro-animation, ported from the marketing site's mega-menu.
   * BUILD flagships only — the site gives a mini to `flagshipLinks` and nothing
   * else, and that restraint is the point.
   */
  mini?: NavMiniKind;
}

// ── Sidebar information architecture ────────────────────────────────────────
// Mirrors the desktop app's IA: a small flat quick-access cluster at the top
// (Home / Runs / Outputs), then labelled SECTIONS in the same order the desktop
// shell uses — Build (the callable/monitorable assets) → Vault (the reusable
// inputs those assets consume) → Connect (programmatic access + the agent
// fleet). Settings/Docs live pinned in the footer.
const topNav: NavItem[] = [
  { name: 'Home', href: '/', icon: HomeIcon, tour: 'nav-home', desc: 'your starting point' },
  { name: 'Runs', href: '/runs', icon: QueueListIcon, desc: 'every execution & result' },
  { name: 'Outputs', href: '/data', icon: TableCellsIcon, desc: "data you've extracted" },
];

// Build — the callable/monitorable assets you author.
const buildItems: NavItem[] = [
  { name: 'Workflows', href: '/workflows', icon: CursorArrowRaysIcon, mini: 'collapse', tour: 'nav-workflows', desc: 'recorded tasks you replay' },
  { name: 'Monitors', href: '/checks', icon: EyeIcon, mini: 'spark', tour: 'nav-monitors', desc: "pages you're watching" },
  // Dragnet (site crawl) is a first-class Build surface: its own shelf at /crawls,
  // a dedicated creation flow at /crawls/new, and a live /crawls/:id detail page.
  { name: 'Harvest', href: '/crawls', icon: GlobeAltIcon, mini: 'frontier', tour: 'nav-crawls', desc: 'crawl a whole site into data' },
  { name: 'Automations', href: '/automations', icon: BoltIcon, mini: 'pops', tour: 'nav-automations', desc: 'chains, triggers & schedules' },
  { name: 'Streaming', href: '/streaming', icon: SignalIcon, desc: 'live callable sessions' },
];

// Vault — the reusable inputs the build assets consume.
const vaultItems: NavItem[] = [
  { name: 'Personas', href: '/personas', icon: IdentificationIcon, desc: 'saved sign-ins Writ reuses' },
  { name: 'Secrets', href: '/secrets', icon: LockClosedIcon, desc: 'API keys & credentials' },
  { name: 'Files', href: '/files', icon: DocumentDuplicateIcon, desc: 'uploads your runs use' },
];

// Connect — programmatic access TO your workflows + the agent fleet that runs
// them. Developers = REST/MCP/OpenAI endpoints + API keys; Fleet = the
// connected Rust agents.
const connectItems: NavItem[] = [
  { name: 'Connect', href: '/connect', icon: SparklesIcon, desc: 'call it from anywhere' },
  { name: 'Developers', href: '/developers', icon: CircleStackIcon, desc: 'your REST & MCP endpoints' },
  { name: 'Fleet', href: '/fleet', icon: CpuChipIcon, desc: 'agents that run your jobs' },
];

const bottomNav: NavItem[] = [
  { name: 'Settings', href: '/settings', icon: Cog6ToothIcon },
];

/**
 * Scrollable nav area with top/bottom fade affordances so it visibly reads as
 * scrollable. Self-contained (own ref + observers) so it works correctly in
 * both the desktop sidebar and the mobile drawer, and auto-recomputes when the
 * content height changes (section collapse/expand) via a ResizeObserver on the
 * inner content.
 */
const NavScrollArea: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { t } = useTranslation();
  const scrollRef = useRef<HTMLElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const [fade, setFade] = useState({ top: false, bottom: false });
  const [hovered, setHovered] = useState(false);

  const update = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const top = el.scrollTop > 4;
    const bottom = el.scrollTop + el.clientHeight < el.scrollHeight - 4;
    setFade(prev => (prev.top === top && prev.bottom === bottom ? prev : { top, bottom }));
  }, []);

  useEffect(() => {
    update();
    const el = scrollRef.current;
    const inner = innerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(update);
    ro.observe(el);
    if (inner) ro.observe(inner);
    window.addEventListener('resize', update);
    return () => { ro.disconnect(); window.removeEventListener('resize', update); };
  }, [update]);

  const scrollBy = (delta: number) => scrollRef.current?.scrollBy({ top: delta, behavior: 'smooth' });

  return (
    <div
      className="relative flex-1 min-h-0"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {/* Top fade + floating "more above" arrow */}
      <div className={clsx(
        'pointer-events-none absolute top-0 inset-x-0 h-8 z-10 bg-gradient-to-b from-[#EDEBE8] to-transparent transition-opacity duration-150',
        fade.top ? 'opacity-100' : 'opacity-0',
      )} />
      <button
        type="button"
        onClick={() => scrollBy(-240)}
        aria-label={t('Scroll up')}
        className={clsx(
          'absolute top-1.5 left-1/2 -translate-x-1/2 z-20 w-6 h-6 rounded-full bg-surface border border-border shadow-sm flex items-center justify-center text-secondary hover:text-ink hover:shadow transition-all',
          hovered && fade.top ? 'opacity-100 translate-y-0 pointer-events-auto' : 'opacity-0 -translate-y-1 pointer-events-none',
        )}
      >
        <ChevronUpIcon className="w-3.5 h-3.5" />
      </button>

      <nav
        ref={scrollRef}
        onScroll={update}
        className="h-full px-3 pb-2 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
      >
        <div ref={innerRef}>{children}</div>
      </nav>

      {/* Bottom fade + floating "more below" arrow — the scroll affordance */}
      <div className={clsx(
        'pointer-events-none absolute bottom-0 inset-x-0 h-8 z-10 bg-gradient-to-t from-[#EDEBE8] to-transparent transition-opacity duration-150',
        fade.bottom ? 'opacity-100' : 'opacity-0',
      )} />
      <button
        type="button"
        onClick={() => scrollBy(240)}
        aria-label={t('Scroll for more')}
        className={clsx(
          'absolute bottom-1.5 left-1/2 -translate-x-1/2 z-20 w-6 h-6 rounded-full bg-surface border border-border shadow-sm flex items-center justify-center text-secondary hover:text-ink hover:shadow transition-all',
          hovered && fade.bottom ? 'opacity-100 translate-y-0 pointer-events-auto' : 'opacity-0 translate-y-1 pointer-events-none',
        )}
      >
        <ChevronDownIcon className="w-3.5 h-3.5" />
      </button>
    </div>
  );
};

export const Layout: React.FC<LayoutProps> = ({ children }) => {
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();
  const user = getUser();
  // Single app-shell subscription to the run-events SSE stream: pushes run
  // start/end into Live activity + the runs feed instantly (see useRunEventStream).
  useRunEventStream();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  // A live browser recording is running in the page content. Clicking a main-nav
  // item would unmount that page and tear the session down, so we route the jump
  // through `guardedNavigate`, which confirms first while a recording is live
  // (react-router v6.20 has no useBlocker, so we guard the sidebar <Link>s
  // directly).
  const { active: recorderActive, guardedNavigate } = useRecorderActivity();

  // A route change closes the mobile sidebar. Reconciled DURING RENDER (React's
  // derive-state-from-props escape hatch) rather than from an effect, so the
  // overlay is already gone on the frame the new page first paints.
  const [shownPath, setShownPath] = useState(location.pathname);
  if (shownPath !== location.pathname) {
    setShownPath(location.pathname);
    if (sidebarOpen) setSidebarOpen(false);
  }

  // This shell only mounts once the user is authenticated, so this fires "just
  // after login" (and on every authenticated reload). Warm EVERY route chunk in
  // the background — it's a tool, not a website: pay one idle-time download pass
  // now to make navigating anywhere in the app instant for the rest of the
  // session (no Suspense spinner / content jump while a JS chunk downloads).
  useEffect(() => { prefetchAllRoutes(); }, []);

  // Revokes the refresh cookie server-side, not just the in-memory token —
  // see performLogout. Clearing locally alone would leave a redeemable session.
  const handleLogout = async () => { await performLogout(); navigate('/login'); };

  const isActive = (href: string) =>
    href === '/' ? location.pathname === '/' : location.pathname.startsWith(href);

  // Intercept a sidebar navigation while a recording is live. Lets no-op
  // (same-URL) and modifier/middle clicks (open-in-new-tab) through untouched;
  // otherwise hands the jump to guardedNavigate, which raises the confirm dialog.
  const guardNav = (e: React.MouseEvent, href: string) => {
    if (!recorderActive) return;
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (location.pathname === href) return;
    e.preventDefault();
    guardedNavigate(href);
  };

  const renderLink = (item: NavItem, indent = false) => {
    const Icon = item.icon;
    const active = isActive(item.href);
    return (
      <Link key={item.href} to={item.href} data-tour={item.tour}
        data-navhint={item.desc ? item.href : undefined}
        data-navhint-desc={item.desc}
        data-navhint-name={item.name}
        onClick={(e) => guardNav(e, item.href)}
        onMouseEnter={() => prefetchRoute(item.href)}
        onFocus={() => prefetchRoute(item.href)}
        className={clsx(
          // `nav-row` is the hover/focus hook the sidebar minis key off (index.css).
          'nav-row group flex items-center gap-2.5 py-[7px] rounded-md text-[13px] transition-all duration-100',
          indent ? 'pl-7 pr-2.5' : 'px-2.5',
          active
            ? 'bg-surface text-ink font-semibold shadow-sm ring-1 ring-black/5'
            : 'text-secondary font-medium hover:text-ink hover:bg-surface/60',
        )}>
        {/* Idle icons stay tertiary; hovering lifts them to the brand red. This
            is the cheap half of the tape DNA — it fires on every row, where the
            mini is deliberately rationed to the BUILD flagships. */}
        <Icon
          className={clsx(
            'w-[18px] h-[18px] shrink-0 transition-colors duration-100',
            active ? 'text-ink' : 'text-tertiary group-hover:text-accent',
          )}
        />
        <span className="min-w-0 flex-1 truncate">{t(item.name)}</span>
        {item.mini && <NavMini kind={item.mini} />}
      </Link>
    );
  };

  const renderSectionHeader = (label: string, items: NavItem[], tour?: string) => {
    if (items.length === 0) return null;
    const anyActive = items.some(c => isActive(c.href));
    return (
      <div className="mb-2">
        <div
          data-tour={tour}
          className={clsx(
            'flex items-center px-2 py-[5px]',
            anyActive ? 'text-ink' : 'text-secondary',
          )}
        >
          <span className="uppercase tracking-[0.08em] text-[11px] font-semibold">{label}</span>
        </div>
        <div className="mt-0.5 space-y-0.5">
          {items.map(item => renderLink(item, true))}
        </div>
      </div>
    );
  };

  // ── Sidebar content ──
  const sidebar = (
    <div className="flex flex-col h-full">
      {/* Logo row — the wordmark alone (the deployment's identity lives in the
          footer account block), matching the other Writ frontends' sidebars. */}
      <div className="h-[52px] flex items-center justify-center px-4 shrink-0">
        <WritWordmark size={20} className="text-ink" />
      </div>

      {/* Quick Actions */}
      <div className="px-3 mb-3">
        <button
          onClick={() => setPaletteOpen(true)}
          className="flex items-center gap-2 w-full px-2.5 py-[7px] text-[12px] text-tertiary hover:text-ink bg-surface/50 hover:bg-surface/80 border border-border rounded-lg transition-colors"
        >
          <MagnifyingGlassIcon className="w-3.5 h-3.5" />
          <span className="flex-1 text-left">{t('Search...')}</span>
          <kbd className="text-[10px] text-tertiary font-mono">⌘K</kbd>
        </button>
      </div>

      {/* Nav — scrollable, with top/bottom fade affordances */}
      <NavScrollArea>
        {/* Quick access — overview & activity */}
        <div className="space-y-0.5 mb-4">
          {topNav.map(item => renderLink(item))}
        </div>

        {/* Build — the core assets (the anchor of the nav) */}
        {renderSectionHeader(t('Build'), buildItems)}

        {/* Vault — reusable inputs those assets consume */}
        {renderSectionHeader(t('Vault'), vaultItems)}

        {/* Connect — programmatic access + the agent fleet */}
        {renderSectionHeader(t('Connect'), connectItems, 'nav-developers')}
      </NavScrollArea>

      {/* ── Pinned footer: utilities · account ── */}
      <div className="shrink-0 border-t border-border">
        {/* Utility links */}
        <div className="px-3 pt-2 space-y-0.5">
          {bottomNav.map(item => renderLink(item))}
          {/* Docs are NOT embedded in the coordinator — they live on the public
              site and open in the user's browser. Same row shape as the nav
              links above, with the external-link affordance made explicit. */}
          <a
            href={docsUrl()}
            {...DOCS_LINK_PROPS}
            data-navhint-name="Docs"
            className="nav-row group flex items-center gap-2.5 py-[7px] px-2.5 rounded-md text-[13px] text-secondary font-medium transition-all duration-100 hover:text-ink hover:bg-surface/60"
          >
            <BookOpenIcon className="w-[18px] h-[18px] shrink-0 text-tertiary group-hover:text-accent transition-colors duration-100" />
            <span className="min-w-0 flex-1 truncate">{t('Docs')}</span>
            <ArrowTopRightOnSquareIcon className="w-3.5 h-3.5 shrink-0 text-tertiary" />
          </a>
        </div>

        {/* Account */}
        <div className="px-2 py-2 mt-1 border-t border-border">
          <div className="flex items-center gap-2.5 px-2 py-1.5 rounded-md hover:bg-surface/60 transition-colors group">
            <div className="w-6 h-6 rounded-full bg-ink flex items-center justify-center text-[10px] font-semibold text-white shrink-0">
              {(user?.name || user?.email || '?')[0].toUpperCase()}
            </div>
            <div className="min-w-0 flex-1 leading-tight">
              <div className="text-[12px] font-medium text-ink truncate">{user?.name || user?.email || t('Your account')}</div>
            </div>
            <NotificationBell />
            <button
              onClick={handleLogout}
              className="p-1 text-secondary hover:text-ink rounded-md hover:bg-surface/80 transition-colors shrink-0"
              title={t('Sign out')}
            >
              <ArrowRightStartOnRectangleIcon className="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>
    </div>
  );

  return (
    <div className="h-screen bg-[#EDEBE8] overflow-hidden flex">
      {/* Mobile overlay */}
      {sidebarOpen && (
        <div className="fixed inset-0 bg-black/20 z-40 lg:hidden" onClick={() => setSidebarOpen(false)} />
      )}

      {/* Desktop sidebar */}
      <aside data-nav-rail className="hidden lg:flex w-[220px] bg-[#EDEBE8] flex-col shrink-0 overflow-hidden">
        {sidebar}
      </aside>

      {/* Mobile drawer */}
      <aside className={clsx(
        'fixed inset-y-0 left-0 w-[260px] bg-[#EDEBE8] flex flex-col z-50 transition-transform duration-200 lg:hidden',
        sidebarOpen ? 'translate-x-0' : '-translate-x-full',
      )}>
        <div className="flex items-center justify-end px-3 h-12">
          <button onClick={() => setSidebarOpen(false)} className="p-1 text-secondary hover:text-ink">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>
        {sidebar}
      </aside>

      {/* Content — white surface. The `border` outlines the rounded card (inset
          by my-2/mr-2) so the seam between the sidebar and a chrome-toned shelf
          master-list follows the CARD'S shape, not a full-height sidebar line. */}
      {/* `@container/stage` — the ONE container every page lays out against. Unlike
          the hosted app there is no Scribe workspace squeezing this card, but it is
          still not the window (the sidebar sits beside it), and this tree shares
          component copies with the other Writ frontends that already query
          `stage`. Declaring it here is what makes those copies behave the same. */}
      <div className="@container/stage relative flex-1 flex flex-col min-w-0 bg-surface border border-border-strong lg:my-2 lg:mr-2 lg:rounded-xl overflow-hidden shadow-sm">
        {/* Mobile header */}
        <div className="lg:hidden flex items-center gap-3 h-12 px-4 bg-chrome chrome-topbar border-b border-border shrink-0">
          <button onClick={() => setSidebarOpen(true)} className="p-1 text-secondary hover:text-ink">
            <Bars2Icon className="w-5 h-5" />
          </button>
          <WritTile size={20} />
          <div className="flex-1" />
          <ActivityIndicator />
          <NotificationBell />
        </div>

        {/* Scrollable content — a ScrollArea so every page gets the floating
            scroll-position indicator + chevron affordance (the desktop convention),
            in one place. */}
        <main className="flex-1 min-h-0 flex flex-col">
          <ScrollArea className="flex-1" fade="surface">
            <PageTransition>{children}</PageTransition>
          </ScrollArea>
        </main>
      </div>

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} onOpen={() => setPaletteOpen(true)} />

      {/* First-run: pick language (moved out of the sidebar) */}
      <LocalePreferencesModal />

      {/* Live-run indicator — a floating bottom-right pill on desktop (mobile keeps
          it in the top header). It only mounts a visible button when there's live
          work (ActivityIndicator returns null at total 0), so the corner stays
          clean when idle; the popover opens upward + clamps to the viewport. */}
      <div className="hidden lg:block fixed bottom-5 right-5 z-40">
        <ActivityIndicator floating />
      </div>
    </div>
  );
};
