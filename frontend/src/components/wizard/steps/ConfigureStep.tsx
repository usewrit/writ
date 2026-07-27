import React from 'react';
import { useTranslation } from 'react-i18next';
import { useWizard } from '../WizardContext';
import { ContentMonitorPanel } from '../panels/ContentMonitorPanel';
import { ManualRecordPanel } from '../panels/ManualRecordPanel';
import { AIWorkflowPanel } from '../panels/AIWorkflowPanel';
import { ExtractScrapePanel } from '../panels/ExtractScrapePanel';
import { StreamingWorkflowPanel } from '../panels/StreamingWorkflowPanel';
import { SiteCrawlPanel } from '../panels/SiteCrawlPanel';

export const ConfigureStep: React.FC<{ onSubmit?: () => void }> = ({ onSubmit }) => {
  const { t } = useTranslation();
  const { state } = useWizard();

  switch (state.mode) {
    case 'content_monitor':
      return <ContentMonitorPanel />;
    case 'manual_workflow':
      return <ManualRecordPanel />;
    case 'ai_workflow':
      return <AIWorkflowPanel />;
    case 'extract_scrape':
      return <ExtractScrapePanel />;
    case 'streaming_workflow':
      return <StreamingWorkflowPanel />;
    case 'site_crawl':
      return <SiteCrawlPanel onSubmit={onSubmit} />;
    default:
      return (
        <div className="flex items-center justify-center h-full text-secondary">
          {t('Please select a mode first.')}
        </div>
      );
  }
};
