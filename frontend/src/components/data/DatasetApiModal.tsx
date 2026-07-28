import React from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import { ClipboardDocumentIcon } from '@heroicons/react/24/outline';
import { Modal } from '../ui/Modal';
import { docsUrl, DOCS_LINK_PROPS } from '../../utils/docs';

/**
 * "Get via API" — a dataset's whole REST surface in one modal: records, schema,
 * export (+ the change-tracking routes when the backend has them).
 *
 * Extracted from the Data explorer's grid so every "Get via API" affordance opens
 * the SAME modal instead of some of them redirecting elsewhere: the grid renders
 * it with a `viewSnippet` (a curl reproducing the on-screen search/filters/sort),
 * while a page with no live grid — the crawl detail view — renders it without one
 * and it simply leads with a plain read of the dataset.
 *
 * A dataset id IS its workflow id, so `datasetId` is all either caller needs.
 *
 * EVERY path here must be one the COORDINATOR actually mounts. This modal is a
 * descendant of the cloud's, whose surface is the `/api/v1/datasets/…` tree; that
 * tree does not exist in the self-host carve, so the paths below are the
 * coordinator's own `/api/automation/workflows/{id}/data…` routes. Copy a row
 * into a terminal before changing one — a row that 404s is worse than no row.
 */
export interface DatasetApiModalProps {
  isOpen: boolean;
  onClose: () => void;
  /** The dataset (== workflow) id to build every endpoint from. */
  datasetId: number;
  /**
   * A curl reproducing the CURRENT on-screen view. Omit when there is no grid to
   * reproduce (the crawl detail page) — the modal then opens on the plain read.
   */
  viewSnippet?: string;
  /**
   * Whether the backend serves the change-tracking routes. `undefined`/`false`
   * hides them rather than advertising endpoints that would 404.
   */
  lineageSupported?: boolean;
}

export const DatasetApiModal: React.FC<DatasetApiModalProps> = ({
  isOpen,
  onClose,
  datasetId,
  viewSnippet,
  lineageSupported,
}) => {
  const { t } = useTranslation();

  // The public REST surface for this dataset. The coordinator mounts the
  // automation router under `/api` (routers/automation.py: APIRouter(prefix=
  // "/automation")), so a dataset's routes live at `/api/automation/workflows/
  // {id}/data…`. This modal used to advertise the CLOUD's `/api/v1/datasets/…`
  // tree — search, records, schema, the datasets index — none of which is
  // mounted here, so every row it listed 404'd on a self-host instance. Same
  // class of drift already corrected in ConnectTab.tsx for `/api/v1/.../run`.
  // A dataset id == its workflow id, and every route below is reachable with a
  // `workflows:read` API key.
  const apiBase = `${typeof window !== 'undefined' ? window.location.origin : 'https://your-instance.com'}/api/automation`;
  const keyPlaceholder = 'wt_YOUR_KEY';

  // When there's no on-screen view to reproduce, lead with a plain read so the
  // modal still opens on something copy-pasteable.
  const leadSnippet =
    viewSnippet ??
    `curl -G "${apiBase}/workflows/${datasetId}/data" \\\n` +
      `  -H "Authorization: Bearer ${keyPlaceholder}" \\\n` +
      `  --data-urlencode 'limit=50'`;

  // The rest of the dataset surface, one compact copyable row each. Search is
  // absent on purpose: the FTS index behind the cloud's /datasets/{id}/search is
  // not part of the self-host carve, and listing it would be another 404 row.
  // `q` on the read below is the substring filter over the bounded scan.
  const apiEndpoints: { method: string; label: string; path: string; hint?: string }[] = [
    { method: 'GET', label: t('List records'), path: `/workflows/${datasetId}/data`, hint: '?q=&sort_by=&limit=50' },
    { method: 'GET', label: t('Schema & facets'), path: `/workflows/${datasetId}/data/facets` },
    { method: 'GET', label: t('Export'), path: `/workflows/${datasetId}/data/export`, hint: '?format=csv|json|markdown|html' },
    ...(lineageSupported === true
      ? [
          { method: 'GET', label: t('Snapshots'), path: `/workflows/${datasetId}/data/runs` },
          { method: 'GET', label: t('Record history'), path: `/workflows/${datasetId}/data/records/{uid}/history` },
        ]
      : []),
  ];

  const copyText = (text: string) => {
    navigator.clipboard.writeText(text);
    toast.success(t('Copied'));
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={t('Get via API')}
      subtitle={t('Everything you can do with this dataset over the API — search, list, schema and export.')}
      size="lg"
    >
      <div className="space-y-4">
        {/* 1 — the lead read (the on-screen view when there is one) */}
        <div>
          <div className="mb-1.5 flex items-center justify-between">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-tertiary">
              {viewSnippet ? t('This exact view') : t('Read the dataset')}
            </span>
            <button
              onClick={() => copyText(leadSnippet)}
              className="inline-flex items-center gap-1 text-[11px] font-medium text-tertiary transition-colors hover:text-ink"
            >
              <ClipboardDocumentIcon className="h-3.5 w-3.5" />
              {t('Copy')}
            </button>
          </div>
          <pre className="overflow-x-auto rounded-lg bg-ink px-4 py-3 font-mono text-[11px] leading-relaxed text-green-400">
            {leadSnippet}
          </pre>
          {viewSnippet && (
            <p className="mt-1 text-[11px] text-tertiary">
              {t('Reproduces the current search, filters, sort and lens.')}
            </p>
          )}
        </div>

        {/* 2 — the rest of the surface, one compact copyable row each */}
        <div>
          <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-tertiary">{t('Everything else')}</span>
          <div className="divide-y divide-border overflow-hidden rounded-lg border border-border">
            {apiEndpoints.map((e) => (
              <div key={e.path + (e.hint ?? '')} className="flex items-center gap-2 px-2.5 py-1.5">
                <span className="shrink-0 rounded bg-hover px-1.5 py-0.5 font-mono text-[10px] font-semibold text-secondary">{e.method}</span>
                <span className="w-28 shrink-0 text-[11px] text-secondary">{e.label}</span>
                <code className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink">
                  {e.path}
                  {e.hint ? <span className="text-tertiary">{e.hint}</span> : null}
                </code>
                <button
                  onClick={() => copyText(`curl "${apiBase}${e.path}${e.hint ?? ''}" -H "Authorization: Bearer ${keyPlaceholder}"`)}
                  className="shrink-0 text-tertiary transition-colors hover:text-ink"
                  title={t('Copy')}
                >
                  <ClipboardDocumentIcon className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
        </div>

        {/* 3 — auth + shared-params footer */}
        <div className="space-y-1.5 border-t border-border pt-3 text-[12px] leading-relaxed text-secondary">
          <p>
            {t('Authenticate with an API key that has the')}{' '}
            <code className="rounded border border-border bg-hover px-1 py-0.5 font-mono text-[11px] text-ink">workflows:read</code>{' '}
            {t('scope — create one on the')}{' '}
            <Link to="/keys" className="text-ink underline underline-offset-2 hover:text-secondary">{t('API Keys')}</Link>{' '}
            {t('page.')}{' '}
            <a href={docsUrl('api')} {...DOCS_LINK_PROPS} className="text-ink underline underline-offset-2 hover:text-secondary">{t('Full reference')}</a>
          </p>
          <p className="text-[11px] text-tertiary">
            {t('Every read above also takes')}{' '}
            <code className="font-mono text-[10px] text-secondary">?format=json|csv|markdown|html</code>{' '}
            — {t('markdown/html render a crawl’s pages as documents, structured data as a table.')}
          </p>
          <p className="text-[11px] text-tertiary">
            {t('The list, records and export endpoints share the same params —')}{' '}
            <code className="font-mono text-[10px] text-secondary">q, filter, filters, sort_by, sort_dir, limit, offset</code>.{' '}
            {t('Run inputs are omitted by default; add')}{' '}
            <code className="font-mono text-[10px] text-secondary">include_inputs=true</code>{' '}
            {t('to get them as')} <code className="font-mono text-[10px] text-secondary">input.&lt;name&gt;</code>{' '}
            {t('columns.')}
          </p>
        </div>
      </div>
    </Modal>
  );
};

export default DatasetApiModal;
