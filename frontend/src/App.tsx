import React, { Suspense, lazy } from 'react';
import { BrowserRouter, Routes, Route, Navigate, useParams, useLocation } from 'react-router-dom';
import { AppToaster } from './components/ui/Toast';
import { isAuthenticated } from './utils/auth';
import { applyServerPreferences } from './utils/preferences';
import { NotificationProvider } from './providers/NotificationProvider';
import { AppLayout } from './components/AppLayout';
import { SurfacerProvider } from './onboarding/Surfacer';

/**
 * Route-level code splitting. Every page is loaded in its own chunk so the
 * initial bundle no longer ships the entire panel, recorder, and wizards up
 * front. Page components use *named* exports, so we adapt them to React.lazy's
 * default-export contract here.
 */
function lazyNamed<T extends Record<string, any>, K extends keyof T>(
  factory: () => Promise<T>,
  name: K,
) {
  return lazy(() => factory().then((m) => ({ default: m[name] as React.ComponentType<any> })));
}

// Public / auth-flow (single-owner login only — no signup/social/reset flows).
const Login = lazyNamed(() => import('./pages/Login'), 'Login');
// First-run onboarding: create the owner account when none exists yet.
const Setup = lazyNamed(() => import('./pages/Setup'), 'Setup');

// Protected
const Dashboard = lazyNamed(() => import('./pages/Dashboard'), 'Dashboard');
const ChecksListPage = lazyNamed(() => import('./pages/checks/ChecksListPage'), 'ChecksListPage');
const CheckCreatePage = lazyNamed(() => import('./pages/checks/CheckCreatePage'), 'CheckCreatePage');
const CheckDetailPage = lazyNamed(() => import('./pages/checks/CheckDetailPage'), 'CheckDetailPage');
const WorkflowsListPage = lazyNamed(() => import('./pages/workflows/WorkflowsListPage'), 'WorkflowsListPage');
const StreamingSessionPage = lazyNamed(() => import('./pages/streaming/StreamingSessionPage'), 'StreamingSessionPage');
const StreamingListPage = lazyNamed(() => import('./pages/streaming/StreamingListPage'), 'StreamingListPage');
const RunsPage = lazyNamed(() => import('./pages/RunsPage'), 'RunsPage');
const DataExplorerPage = lazyNamed(() => import('./pages/data/DataExplorerPage'), 'DataExplorerPage');
const DevelopersPage = lazyNamed(() => import('./pages/developers/DevelopersPage'), 'DevelopersPage');
const ConnectPage = lazyNamed(() => import('./pages/connect/ConnectPage'), 'ConnectPage');
const WorkflowCreatePage = lazyNamed(() => import('./pages/workflows/WorkflowCreatePage'), 'WorkflowCreatePage');
const WorkflowDetailPage = lazyNamed(() => import('./pages/workflows/WorkflowDetailPage'), 'WorkflowDetailPage');
const AutomationsListPage = lazyNamed(() => import('./pages/automations/AutomationsListPage'), 'AutomationsListPage');
const AutomationBuilderPage = lazyNamed(() => import('./pages/automations/AutomationBuilderPage'), 'AutomationBuilderPage');
const AutomationRunsPage = lazyNamed(() => import('./pages/automations/AutomationRunsPage'), 'AutomationRunsPage');
// Site crawl (Dragnet) is a first-class Build surface: its own shelf, dedicated
// creation flow, and live detail page.
const CrawlsPage = lazyNamed(() => import('./pages/crawls/CrawlsPage'), 'CrawlsPage');
const CrawlCreatePage = lazyNamed(() => import('./pages/crawls/CrawlCreatePage'), 'CrawlCreatePage');
const DragnetDetailPage = lazyNamed(() => import('./pages/dragnet/DragnetDetailPage'), 'DragnetDetailPage');
const OAuthDevice = lazyNamed(() => import('./pages/OAuthDevice'), 'OAuthDevice');
const FleetPage = lazyNamed(() => import('./pages/fleet/FleetPage'), 'FleetPage');
const Settings = lazyNamed(() => import('./pages/Settings'), 'Settings');
const NotFound = lazyNamed(() => import('./pages/NotFound'), 'NotFound');
const SecretsPage = lazyNamed(() => import('./pages/SecretsPage'), 'SecretsPage');
const PersonasPage = lazyNamed(() => import('./pages/PersonasPage'), 'PersonasPage');
const Files = lazyNamed(() => import('./pages/Files'), 'Files');
const NotificationsPage = lazyNamed(() => import('./components/notifications/NotificationsPage'), 'NotificationsPage');

const RouteFallback: React.FC = () => (
  <div className="fallback-in flex h-full min-h-[40vh] w-full items-center justify-center">
    <div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-zinc-800" />
  </div>
);

/**
 * DeferredRoutes — the reason page changes no longer flicker.
 *
 * Every page is a lazy chunk (see lazyNamed). Rendering the *live* location means
 * navigating to a not-yet-cached page unmounts the current page and shows the
 * Suspense fallback (a bare spinner) until the chunk loads — that content → spinner
 * → content swap IS the flicker, and no enter-fade can hide it because it happens
 * in the mount gap.
 *
 * Instead we hold the router on the CURRENT location and only advance it inside a
 * React 18 `startTransition`. During a transition React keeps the already-committed
 * page on screen and does NOT fall back to the Suspense spinner; it waits for the
 * incoming chunk, then swaps atomically. The URL bar updates immediately (history
 * is already pushed); only the painted content follows a beat later — the standard
 * "stay on the old page until the new one is ready" UX.
 *
 * Passing `location={displayLocation}` also overrides the location context for the
 * whole subtree, so `useLocation()` inside <PageTransition> sees the DEFERRED
 * location — the enter-fade fires exactly when the content swaps, never before.
 */
const DeferredRoutes: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const location = useLocation();
  const [displayLocation, setDisplayLocation] = React.useState(location);
  const [, startTransition] = React.useTransition();

  React.useEffect(() => {
    if (location === displayLocation) return;
    startTransition(() => setDisplayLocation(location));
  }, [location, displayLocation]);

  return <Routes location={displayLocation}>{children}</Routes>;
};

const ProtectedRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const location = useLocation();
  if (!isAuthenticated()) {
    // Build the post-login redirect from the ROUTER location this route is being
    // rendered for — never window.location. Under <DeferredRoutes> the address bar
    // (window.location) lags/leads the rendered route during transitions; reading
    // it here captured the in-flight "/login?redirect=…" URL and nested it into
    // itself on every render, producing the ever-growing ?redirect=%2Flogin%3F…
    // loop. Also never point the redirect back at an auth route.
    const currentPath = location.pathname + location.search;
    const isAuthRoute = location.pathname === '/login' || location.pathname === '/setup';
    const redirectParam =
      currentPath !== '/' && !isAuthRoute ? `?redirect=${encodeURIComponent(currentPath)}` : '';
    return <Navigate to={`/login${redirectParam}`} replace />;
  }
  // NotificationProvider mounts once for every authenticated surface so the
  // bell/inbox poll runs app-wide.
  return (
    <NotificationProvider>
      <PreferencesBoot />
      {children}
    </NotificationProvider>
  );
};

/**
 * Applies the owner's stored language + theme (coordinator `/settings/preferences`)
 * once per session. The preference is the cross-device source of truth; i18next's
 * localStorage cache and the `dark` class are only this browser's copy of it, so
 * without this a fresh browser would silently fall back to the navigator language.
 *
 * Renders nothing and never blocks paint: the fetch needs an authenticated session,
 * so it cannot run in main.tsx alongside `i18nReady`. A first load in a browser that
 * has no cached language therefore paints English for one round trip before
 * switching — subsequent loads read the localStorage cache and paint correctly.
 */
const PreferencesBoot: React.FC = () => {
  React.useEffect(() => {
    void applyServerPreferences();
  }, []);
  return null;
};

const PublicRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  if (isAuthenticated()) {
    return <Navigate to="/" replace />;
  }
  return <>{children}</>;
};

/**
 * Legacy deep-link redirect that substitutes the :id path param into the target
 * URL. <Navigate> does NOT interpolate params, so a literal ":id" in the `to`
 * string would otherwise produce a broken URL.
 */
const ParamRedirect: React.FC<{ to: (id: string) => string }> = ({ to }) => {
  const { id } = useParams();
  return <Navigate to={id ? to(id) : '/automations'} replace />;
};

function App() {
  return (
    <BrowserRouter>
      <AppToaster />
      <SurfacerProvider>
      <Suspense fallback={<RouteFallback />}>
      <DeferredRoutes>
        {/* Public — first-run onboarding + single-owner login */}
        <Route path="/setup" element={<PublicRoute><Setup /></PublicRoute>} />
        <Route path="/login" element={<PublicRoute><Login /></PublicRoute>} />

        {/* Redirects (no layout chrome — pure navigation) */}
        <Route path="/workflows/:id/edit" element={<ParamRedirect to={(id) => `/workflows/${id}?tab=steps`} />} />
        <Route path="/flows" element={<Navigate to="/automations" replace />} />
        <Route path="/flows/new" element={<Navigate to="/automations/new" replace />} />
        <Route path="/flows/:id/runs" element={<ParamRedirect to={(id) => `/automations/${id}/runs`} />} />
        <Route path="/flows/:id" element={<ParamRedirect to={(id) => `/automations/${id}`} />} />
        <Route path="/library" element={<Navigate to="/checks" replace />} />
        {/* Legacy /dragnet/new → the dedicated crawl creation flow. */}
        <Route path="/dragnet/new" element={<Navigate to="/crawls/new" replace />} />
        <Route path="/library/workflows/:id/edit" element={<ParamRedirect to={(id) => `/workflows/${id}?tab=steps`} />} />
        <Route path="/triggers" element={<Navigate to="/automations" replace />} />
        {/* Legacy developer surfaces → unified /developers tabs */}
        <Route path="/keys" element={<Navigate to="/developers" replace />} />
        <Route path="/developers/endpoints" element={<Navigate to="/developers?tab=endpoints" replace />} />
        <Route path="/developers/webhooks" element={<Navigate to="/developers?tab=endpoints" replace />} />
        <Route path="/developers/mcp" element={<Navigate to="/developers?tab=endpoints" replace />} />

        {/* Protected (single top-level layout route) */}
        <Route element={<ProtectedRoute><AppLayout /></ProtectedRoute>}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/checks" element={<ChecksListPage />} />
          <Route path="/checks/new" element={<CheckCreatePage />} />
          <Route path="/checks/:id" element={<CheckDetailPage />} />
          <Route path="/workflows" element={<WorkflowsListPage />} />
          <Route path="/workflows/new" element={<WorkflowCreatePage />} />
          <Route path="/workflows/:id" element={<WorkflowDetailPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/data" element={<DataExplorerPage />} />
          <Route path="/streaming" element={<StreamingListPage />} />
          <Route path="/streaming/:sessionKey" element={<StreamingSessionPage />} />
          <Route path="/automations" element={<AutomationsListPage />} />
          <Route path="/automations/new" element={<AutomationBuilderPage />} />
          <Route path="/automations/:id/runs" element={<AutomationRunsPage />} />
          <Route path="/automations/:id" element={<AutomationBuilderPage />} />
          {/* Site crawl (Dragnet) — a first-class Build surface with its own shelf
              (/crawls), dedicated creation flow (/crawls/new), and live detail page
              (/crawls/:id). /crawls/new MUST precede /crawls/:id. Legacy /dragnet*
              links redirect in. */}
          <Route path="/crawls" element={<CrawlsPage />} />
          <Route path="/crawls/new" element={<CrawlCreatePage />} />
          <Route path="/crawls/:id" element={<DragnetDetailPage />} />
          <Route path="/dragnet" element={<Navigate to="/crawls" replace />} />
          <Route path="/dragnet/:id" element={<ParamRedirect to={(id) => `/crawls/${id}`} />} />
          {/* Unified Developers surface: tabbed [API Keys | Endpoints]. */}
          <Route path="/developers" element={<DevelopersPage />} />
          {/* Connect — attach Claude / MCP clients to this coordinator. */}
          <Route path="/connect" element={<ConnectPage />} />
          {/* Fleet — connected Rust agents + connect-a-new-agent flow. */}
          <Route path="/fleet" element={<FleetPage />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/secrets" element={<SecretsPage />} />
          <Route path="/personas" element={<PersonasPage />} />
          <Route path="/files" element={<Files />} />
          {/* In-app notification inbox. The bell links here. */}
          <Route path="/notifications" element={<NotificationsPage />} />
        </Route>

        {/* Protected, but full-screen (no sidebar chrome) — agent device-flow */}
        <Route path="/oauth/device" element={<ProtectedRoute><OAuthDevice /></ProtectedRoute>} />

        <Route path="*" element={<NotFound />} />
      </DeferredRoutes>
      </Suspense>
      </SurfacerProvider>
    </BrowserRouter>
  );
}

export default App;
