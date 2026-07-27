import React, { Suspense } from 'react';
import { Outlet } from 'react-router-dom';
import { Layout } from './Layout';
import { RecorderActivityProvider } from './RecorderActivityContext';

const PageFallback: React.FC = () => (
  <div className="flex h-full min-h-[50vh] w-full items-center justify-center">
    <div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-ink" />
  </div>
);

/**
 * Top-level layout route for all authenticated pages. Renders the shared
 * <Layout> chrome once and projects the matched child route through <Outlet>,
 * instead of every page wrapping itself in <Layout>.
 *
 * The <Suspense> sits INSIDE <Layout> so navigating to a lazily-loaded page
 * only shows the fallback in the content area — the sidebar/chrome stays
 * mounted and doesn't flash out on every navigation.
 */
export const AppLayout: React.FC = () => (
  <RecorderActivityProvider>
    <Layout>
      <Suspense fallback={<PageFallback />}>
        <Outlet />
      </Suspense>
    </Layout>
  </RecorderActivityProvider>
);

export default AppLayout;
