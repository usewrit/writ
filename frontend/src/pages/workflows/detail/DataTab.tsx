import React from 'react';
import { useTranslation } from 'react-i18next';
import { ExtractedDataTable } from '../../../components/data/ExtractedDataTable';
import { RunOutputFiles } from '../../../components/data/RunOutputFiles';
import { Section } from './Section';

interface DataTabProps {
  workflow: any;
}

/**
 * Per-workflow Data tab: everything this workflow has extracted across all its
 * runs, as one sortable / searchable / exportable table.
 */
export const DataTab: React.FC<DataTabProps> = ({ workflow }) => {
  const { t } = useTranslation();
  return (
    <Section
      title={t('Data')}
      description={t('Everything this workflow has extracted, across all its runs. Sort, filter, expand a row to see the raw record, or export to CSV/JSON.')}
    >
      <RunOutputFiles workflowId={workflow.id} />
      <ExtractedDataTable workflowId={workflow.id} isCrawl={workflow.workflow_type === 'crawl'} />
    </Section>
  );
};

export default DataTab;
