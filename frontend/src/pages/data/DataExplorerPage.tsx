import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useSearchParams } from 'react-router-dom';
import { TableCellsIcon, CursorArrowRaysIcon } from '@heroicons/react/24/outline';
import { useDocumentTitle } from '../../hooks/useDocumentTitle';
import { useRequireAuth } from '../../hooks/useAuth';
import { useQuery } from '../../hooks/useQuery';
import { Q } from '../../stores/queryKeys';
import { workflowDataApi, type DataWorkflowSummary, type PickerLastDelta } from '../../api/workflowData';
import { ExtractedDataTable } from '../../components/data/ExtractedDataTable';
import { Select, EmptyHero, buttonClass } from '../../components/ui';
import { tintStyle } from '../../utils/tint';
import { SHELF_LIST_COL, ShelfAccentBar, shelfRowClass, shelfRowMouseDown, ShelfListSearch, ShelfSkeleton } from '../../components/library/shelf';
import { ScrollArea } from '../../components/ui/ScrollArea';
import { SwapFade } from '../../components/ui/Animated';
import { formatRelativeTime } from '../../utils/format';

/**
 * The picker row's change hint — one plain-text segment at most, priority
 * new > changed > removed, only when nonzero (the server sends last_delta =
 * null when unknown, e.g. a single snapshot or a pre-lineage backend).
 */
function lastDeltaHint(d: PickerLastDelta | null | undefined, t: (k: string, o?: Record<string, unknown>) => string): string | null {
  if (!d) return null;
  if (d.new > 0) return t('+{{count}} new', { count: d.new });
  if (d.changed > 0) return t('{{count}} changed', { count: d.changed });
  if (d.removed > 0) return t('−{{count}} gone', { count: d.removed });
  return null;
}

/**
 * Global Outputs explorer: a picker of every workflow that has produced extracted
 * data, with the selected workflow's data table beside it. Reuses the same
 * <ExtractedDataTable> as the per-workflow Data tab.
 */
export const DataExplorerPage: React.FC = () => {
  useRequireAuth();
  const { t } = useTranslation();
  useDocumentTitle(t('Outputs'));
  const [searchParams, setSearchParams] = useSearchParams();

  const { data: workflows, loading } = useQuery<DataWorkflowSummary[]>(
    Q.dataWorkflows(),
    () => workflowDataApi.listWorkflows(),
    { pollInterval: 60_000 },
  );

  const list = useMemo(() => workflows || [], [workflows]);

  // Top-of-list search over the workflow picker (mirrors the Workflows list).
  const [search, setSearch] = useState('');
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return q ? list.filter((w) => w.workflow_name?.toLowerCase().includes(q)) : list;
  }, [list, search]);

  const selectedParam = searchParams.get('workflow');
  const selectedId = selectedParam ? Number(selectedParam) : null;

  // Default the selection to the most-recent workflow once data loads.
  useEffect(() => {
    if (!selectedId && list.length > 0) {
      setSearchParams({ workflow: String(list[0].workflow_id) }, { replace: true });
    }
  }, [selectedId, list, setSearchParams]);

  const selected = list.find((w) => w.workflow_id === selectedId) || null;

  return (
    <div className="flex h-full flex-col">
      {/* Toolbar */}
      <div className="flex items-center gap-3 h-12 px-4 sm:px-6 bg-chrome chrome-topbar border-b border-border shrink-0">
        <TableCellsIcon className="h-4 w-4 text-tertiary shrink-0" />
        <span className="text-[13px] font-semibold text-ink shrink-0">{t('Outputs')}</span>
        <span className="hidden @pair/stage:inline text-[11px] text-tertiary">
          {t('Extracted data from your workflow runs')}
        </span>
      </div>

      {loading && list.length === 0 ? (
        <ShelfSkeleton withSearch label={t('Loading data')} />
      ) : !loading && list.length === 0 ? (
        <EmptyHero
          icon={TableCellsIcon}
          title={t('No extracted data yet')}
          description={t('Run a workflow that extracts data and it will show up here — sortable, searchable, and exportable.')}
          className="flex-1"
        >
          <Link to="/workflows" className={buttonClass({ size: 'sm' })}>
            <CursorArrowRaysIcon className="h-3.5 w-3.5" />
            {t('Go to workflows')}
          </Link>
        </EmptyHero>
      ) : (
        <div className="flex min-h-0 min-w-0 flex-1 overflow-hidden">
          {/* Workflow picker — shelf master-list tone (chrome), so it recedes into
              the nav frame while the data table beside it is the bright content. */}
          <aside className={SHELF_LIST_COL}>
            <div className="shrink-0 p-2">
              <ShelfListSearch
                value={search}
                onChange={setSearch}
                placeholder={t('Search workflows…')}
                ariaLabel={t('Search workflows')}
              />
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
              <div className="px-3 py-2 text-[10px] font-semibold uppercase tracking-wide text-tertiary">
                {t('Workflows')}
              </div>
              <nav className="space-y-0.5 px-2 pb-4">
                {filtered.map((w) => (
                <div
                  key={w.workflow_id}
                  role="button"
                  tabIndex={0}
                  aria-pressed={w.workflow_id === selectedId}
                  aria-label={w.workflow_name}
                  onClick={() => setSearchParams({ workflow: String(w.workflow_id) }, { replace: true })}
                  onMouseDown={shelfRowMouseDown}
                  onKeyDown={(e) => {
                    if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) {
                      e.preventDefault();
                      setSearchParams({ workflow: String(w.workflow_id) }, { replace: true });
                    }
                  }}
                  // A real <button> sizes to its content (a long name grows the row and
                  // never truncates); the shelf rows are <div role="button">, which
                  // fills the column so the name clips + marquees. Match that exactly.
                  className={shelfRowClass(w.workflow_id === selectedId)}
                >
                  {w.workflow_id === selectedId && <ShelfAccentBar />}
                  <div
                    style={tintStyle('neutral')}
                    aria-hidden="true"
                    className="w-8 h-8 shrink-0 rounded-lg flex items-center justify-center"
                  >
                    <CursorArrowRaysIcon className="h-4 w-4" />
                  </div>
                  <div className="flex-1 min-w-0">
                    {/* Name: clips at rest, marquees to reveal the overflow on row
                        hover (container-query units make it a no-op when it fits) —
                        same treatment as the WorkflowList rows. */}
                    <div
                      className="text-[13px] font-medium text-ink overflow-hidden"
                      style={{ containerType: 'inline-size' }}
                      title={w.workflow_name}
                    >
                      <span className="inline-block whitespace-nowrap min-w-full group-hover:[animation:marquee-hover_8s_ease-in-out_infinite]">
                        {w.workflow_name}
                      </span>
                    </div>
                    <div className="flex items-center gap-1 text-[11px] text-tertiary truncate mt-0.5">
                      <span className="shrink-0">{t('{{n}} runs', { n: w.run_count })}</span>
                      {w.last_data_at && (
                        <>
                          <span className="text-tertiary/50 shrink-0">·</span>
                          <span className="shrink-0">{formatRelativeTime(w.last_data_at)}</span>
                        </>
                      )}
                      {(() => {
                        const hint = lastDeltaHint(w.last_delta, t);
                        return hint ? (
                          <>
                            <span className="text-tertiary/50 shrink-0">·</span>
                            <span className="shrink-0 text-secondary">{hint}</span>
                          </>
                        ) : null;
                      })()}
                    </div>
                  </div>
                </div>
                ))}
                {filtered.length === 0 && (
                  <p className="px-3 py-8 text-center text-[12px] text-tertiary">{t('No workflows match')}</p>
                )}
              </nav>
            </div>
          </aside>

          {/* Narrow-stage picker (dropdown) + selected table */}
          <ScrollArea className="flex min-h-0 min-w-0 flex-1" viewportClassName="flex flex-col">
            {/* Mirrors SHELF_LIST_COL: below `split` the shelf goes full-width, so this is the picker. */}
            <div className="border-b border-border px-4 py-2 @split/stage:hidden">
              <Select<number>
                value={selectedId}
                onChange={(v) => setSearchParams({ workflow: String(v) }, { replace: true })}
                size="sm"
                aria-label={t('Workflow')}
                options={list.map((w) => ({
                  value: w.workflow_id,
                  label: `${w.workflow_name} (${w.run_count})`,
                }))}
              />
            </div>

            <SwapFade swapKey={selectedId} className="min-w-0 flex-1 px-4 py-4 sm:px-6">
              {selected && (
                <div className="mb-3 flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <h2 className="line-clamp-2 break-words text-[15px] font-semibold leading-snug text-ink">
                      {selected.workflow_name}
                    </h2>
                    <p className="mt-0.5 text-[11px] text-tertiary">
                      {t('{{n}} runs with data', { n: selected.run_count })}
                      {selected.last_data_at && <> · {t('updated {{time}}', { time: formatRelativeTime(selected.last_data_at) })}</>}
                    </p>
                  </div>
                  {/* A crawl dataset is not a workflow — link back to its crawl
                      detail, not the workflow page (Run/Publish/Steps chrome). */}
                  <Link
                    to={selected.workflow_type === 'crawl' && selected.crawl_id != null
                      ? `/crawls/${selected.crawl_id}`
                      : `/workflows/${selected.workflow_id}`}
                    className="shrink-0 rounded-lg border border-border px-2.5 py-1.5 text-[12px] font-medium text-secondary hover:text-ink"
                  >
                    {selected.workflow_type === 'crawl' && selected.crawl_id != null
                      ? t('Open crawl')
                      : t('Open workflow')}
                  </Link>
                </div>
              )}
              {selectedId ? (
                <ExtractedDataTable key={selectedId} workflowId={selectedId} isCrawl={selected?.workflow_type === 'crawl'} />
              ) : (
                <div className="py-12 text-center text-[12px] text-tertiary">
                  {t('Select a workflow to view its data.')}
                </div>
              )}
            </SwapFade>
          </ScrollArea>
        </div>
      )}
    </div>
  );
};

export default DataExplorerPage;
