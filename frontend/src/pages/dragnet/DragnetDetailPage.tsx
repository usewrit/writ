import React from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useRequireAuth } from '../../hooks/useAuth';
import { CrawlDetailView } from '../../components/dragnet/CrawlDetailView';

/**
 * Standalone crawl detail route (`/crawls/:id`) — a thin frame around the shared
 * `CrawlDetailView`, the SAME component the `/crawls` shelf renders in its detail
 * pane. Deep links, in-flight toasts, and narrow screens land here; the shelf
 * embeds the identical view inline.
 */
export const DragnetDetailPage: React.FC = () => {
  useRequireAuth();
  const navigate = useNavigate();
  const { id = '' } = useParams();

  return (
    <CrawlDetailView crawlId={id} onBack={() => navigate('/crawls')} onDeleted={() => navigate('/crawls')} isPage />
  );
};

export default DragnetDetailPage;
