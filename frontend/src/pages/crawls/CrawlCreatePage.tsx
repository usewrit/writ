import React from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useRequireAuth } from '../../hooks/useAuth';
import { useDocumentTitle } from '../../hooks/useDocumentTitle';
import { UnifiedCreationWizard } from '../../components/wizard/UnifiedCreationWizard';

/**
 * Dedicated site-crawl (Dragnet) creation flow. Crawl used to be one mode among
 * several in the unified workflow wizard; it's now its own Build surface. We still
 * reuse the wizard shell — but LOCKED to `site_crawl` (single allowed mode), so
 * there's no mode picker and no "Change" affordance: the page opens straight on
 * the crawl setup form and its config panel.
 */
export const CrawlCreatePage: React.FC = () => {
  const { t } = useTranslation();
  useRequireAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();

  const url = params.get('url') || undefined;
  const name = params.get('name') || undefined;

  useDocumentTitle(t('New crawl'));

  return (
    <div className="flex flex-col h-full">
      <UnifiedCreationWizard
        initialMode="site_crawl"
        allowedModes={['site_crawl']}
        initialUrl={url}
        initialName={name}
        // The crawl submit navigates to /crawls/{id} itself; these cover cancel
        // and any non-navigating completion.
        onComplete={() => navigate('/crawls')}
        onCancel={() => navigate('/crawls')}
        embedded
      />
    </div>
  );
};

export default CrawlCreatePage;
