import React, { useEffect, useMemo, useState } from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import {
  ChevronDoubleRightIcon,
  ChevronDoubleLeftIcon,
  AdjustmentsHorizontalIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import { formatRelativeTime } from '../../utils/format';
import type { RailConfig } from './configs';
import { useToolRail } from './useToolRail';

interface ToolRailProps {
  config: RailConfig;
  /** the same array the list page loads via useQuery (shared cache) */
  items: any[];
  /** called whenever the active filter changes; pass the predicate to the list */
  onFilterChange?: (predicate: (item: any) => boolean) => void;
}

// ── section wrapper ──
const Section: React.FC<{ title: string; right?: React.ReactNode; children: React.ReactNode }> = ({ title, right, children }) => (
  <div className="px-3 py-3 border-b border-border last:border-b-0">
    <div className="flex items-center justify-between mb-2 px-1">
      <span className="uppercase tracking-wider text-[10px] font-semibold text-tertiary">{title}</span>
      {right}
    </div>
    {children}
  </div>
);

// ── status dot — restrained semantic color (failed=red, running=amber pulse,
// ok=green); see utils/statusStyle.ts ──
const Dot: React.FC<{ ok?: boolean | null }> = ({ ok }) => (
  <span
    className={clsx(
      'w-1.5 h-1.5 rounded-full shrink-0',
      ok === false ? 'bg-red-500' : ok === null ? 'bg-amber-500 animate-status-pulse' : 'bg-green-500',
    )}
  />
);

const RailBody: React.FC<{ rail: ReturnType<typeof useToolRail>; config: RailConfig; items: any[] }> = ({ rail, config, items }) => {
  const { t } = useTranslation();
  // activity() walks the full items list; recompute only when items/config change,
  // not on every rail-state (filter/view) re-render.
  const activity = useMemo(() => config.activity(items), [config, items]);
  return (
    <div className="flex-1 overflow-y-auto">
      {/* Views */}
      <Section
        title={t('Views')}
        right={rail.activeFilterCount > 0 ? (
          <button onClick={rail.clearAll} className="text-[10px] text-tertiary hover:text-ink transition-colors">{t('Clear')}</button>
        ) : undefined}
      >
        <div className="space-y-0.5">
          {config.views.map((v) => {
            const Icon = v.icon;
            const active = rail.view === v.id;
            const count = rail.viewCounts[v.id] ?? 0;
            return (
              <button
                key={v.id}
                onClick={() => rail.setView(v.id)}
                className={clsx(
                  'flex items-center gap-2.5 w-full px-2 py-[6px] rounded-md text-[13px] transition-colors',
                  active ? 'bg-hover text-ink font-medium' : 'text-secondary hover:text-ink hover:bg-hover',
                )}
              >
                <Icon className={clsx('w-4 h-4 shrink-0', active ? 'text-ink' : 'text-tertiary')} />
                <span className="flex-1 text-left truncate">{t(v.label)}</span>
                <span className={clsx('text-[11px] tabular-nums', active ? 'text-secondary' : 'text-tertiary')}>{count}</span>
              </button>
            );
          })}
        </div>
      </Section>

      {/* Facets — hide options with no results (unless selected), and skip the
          whole facet when nothing in it applies, so we never show a section of
          all-zeros (e.g. "Frequency: 10s 0 · 30s 0 · …"). */}
      {config.facets.map((f) => {
        const visibleOptions = f.options.filter((o) =>
          (rail.facetCounts[`${f.id}:${o.id}`] ?? 0) > 0 || (rail.facetState[f.id] || []).includes(o.id),
        );
        if (visibleOptions.length === 0) return null;
        return (
        <Section key={f.id} title={t(f.label)}>
          <div className="flex flex-wrap gap-1.5">
            {visibleOptions.map((o) => {
              const selected = (rail.facetState[f.id] || []).includes(o.id);
              const count = rail.facetCounts[`${f.id}:${o.id}`] ?? 0;
              return (
                <button
                  key={o.id}
                  onClick={() => rail.toggleFacet(f.id, o.id)}
                  className={clsx(
                    'inline-flex items-center gap-1.5 pl-2 pr-1.5 py-1 rounded-full text-[11px] border transition-colors',
                    selected
                      ? 'bg-ink text-white border-ink'
                      : 'bg-surface text-secondary border-border hover:border-border-strong hover:text-ink',
                  )}
                >
                  {t(o.label)}
                  <span className={clsx('tabular-nums', selected ? 'text-white/70' : 'text-tertiary')}>{count}</span>
                </button>
              );
            })}
          </div>
        </Section>
        );
      })}

      {/* Overview */}
      <Section title={t('Overview')}>
        <div className="grid grid-cols-2 gap-2">
          {config.stats.map((s) => {
            const r = s.compute(items);
            return (
              <div key={s.id} className="rounded-lg border border-border/70 bg-surface px-2.5 py-2">
                <div className="text-[10px] text-tertiary truncate">{t(s.label)}</div>
                <div className="text-[15px] font-semibold text-ink tabular-nums leading-tight mt-0.5">{r.value}</div>
                {typeof r.ratio === 'number' && (
                  <div className="mt-1.5 h-1 bg-hover rounded-full overflow-hidden">
                    <div className="h-full bg-ink rounded-full transition-all" style={{ width: `${r.ratio}%` }} />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </Section>

      {/* Activity */}
      <Section title={config.activityTitle}>
        {activity.length === 0 ? (
          <p className="text-[11px] text-tertiary px-1 py-1">{t('No recent activity')}</p>
        ) : (
          <div className="space-y-px">
            {activity.map((a) => (
              <div key={a.id} className="flex items-center gap-2 px-1 py-1.5 rounded-md">
                <Dot ok={a.ok} />
                <div className="flex-1 min-w-0">
                  <div className="text-[12px] text-ink truncate">{a.title}</div>
                  {a.subtitle && <div className="text-[10px] text-tertiary truncate">{a.subtitle}</div>}
                </div>
                {a.time && <span className="text-[10px] text-tertiary shrink-0">{formatRelativeTime(a.time)}</span>}
              </div>
            ))}
          </div>
        )}
      </Section>
    </div>
  );
};

export const ToolRail: React.FC<ToolRailProps> = ({ config, items, onFilterChange }) => {
  const { t } = useTranslation();
  const rail = useToolRail(config, items);
  const [mobileOpen, setMobileOpen] = useState(false);

  // push the active predicate up to the page so the list re-filters
  useEffect(() => { onFilterChange?.(rail.predicate); }, [rail.predicate, onFilterChange]);

  return (
    <>
      {/* Desktop rail */}
      <aside
        className={clsx(
          // @rail/stage, not lg: — this rail lives in a content card that can be
          // squeezed narrower than the window (e.g. a side panel open beside it),
          // so `lg:` kept it docked while it ate most of a narrow card. Below
          // `rail` it becomes the same floating button + sheet already used on a
          // narrow window.
          'hidden @rail/stage:flex flex-col shrink-0 border-l border-border bg-surface transition-[width] duration-200',
          rail.collapsed ? 'w-[44px]' : 'w-[264px]',
        )}
      >
        {rail.collapsed ? (
          <button
            onClick={rail.toggleCollapsed}
            className="flex flex-col items-center gap-2 pt-3 text-tertiary hover:text-ink transition-colors"
            title={t('Expand insights')}
          >
            <ChevronDoubleLeftIcon className="w-4 h-4" />
            {rail.activeFilterCount > 0 && (
              <span className="w-1.5 h-1.5 rounded-full bg-ink" />
            )}
          </button>
        ) : (
          <>
            <div className="flex items-center justify-between h-11 px-3 border-b border-border shrink-0">
              <span className="text-[12px] font-semibold text-ink">{t('Insights')}</span>
              <button onClick={rail.toggleCollapsed} className="p-1 text-tertiary hover:text-ink transition-colors" title={t('Collapse')}>
                <ChevronDoubleRightIcon className="w-4 h-4" />
              </button>
            </div>
            <RailBody rail={rail} config={config} items={items} />
          </>
        )}
      </aside>

      {/* Mobile: floating Filters button + right drawer */}
      <button
        onClick={() => setMobileOpen(true)}
        className="@rail/stage:hidden absolute bottom-4 right-4 z-30 flex items-center gap-1.5 px-3.5 py-2.5 bg-ink text-white text-[12px] font-medium rounded-full shadow-lg"
      >
        <AdjustmentsHorizontalIcon className="w-4 h-4" />
        {t('Insights')}
        {rail.activeFilterCount > 0 && (
          <span className="ml-0.5 w-4 h-4 rounded-full bg-surface text-ink text-[10px] font-bold flex items-center justify-center">{rail.activeFilterCount}</span>
        )}
      </button>

      {mobileOpen && (
        <div className="@rail/stage:hidden absolute inset-0 z-50 flex justify-end">
          <div className="absolute inset-0 bg-black/20" onClick={() => setMobileOpen(false)} />
          {/* max-w-[85%] of the CARD, not 85vw of the window — this sheet is now
              absolutely positioned inside the content card (see the drawer's
              `absolute`, not `fixed`, wrapper above). */}
          <aside className="relative w-[280px] max-w-[85%] bg-surface flex flex-col shadow-xl animate-fade-in">
            <div className="flex items-center justify-between h-12 px-3 border-b border-border shrink-0">
              <span className="text-[12px] font-semibold text-ink">{t('Insights')}</span>
              <button onClick={() => setMobileOpen(false)} className="p-1 text-tertiary hover:text-ink">
                <XMarkIcon className="w-5 h-5" />
              </button>
            </div>
            <RailBody rail={rail} config={config} items={items} />
          </aside>
        </div>
      )}
    </>
  );
};
