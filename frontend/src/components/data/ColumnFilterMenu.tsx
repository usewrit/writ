import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import {
  ChevronDownIcon,
  BarsArrowUpIcon,
  BarsArrowDownIcon,
  MagnifyingGlassIcon,
  CheckIcon,
} from '@heroicons/react/24/outline';
import { Popover } from './Popover';
import type { ColumnFacet, FilterClause } from '../../api/workflowData';
import { NumberInput } from '../ui';

type SortDir = 'asc' | 'desc';

interface Props {
  col: string;
  facet?: ColumnFacet;
  clause?: FilterClause;
  /** Current sort direction if this column is the active sort, else null. */
  sortDir: SortDir | null;
  onSort: (dir: SortDir) => void;
  onApply: (clause: FilterClause | null) => void;
  /** Meta columns like run_id/run_at are sort-only. */
  filterable?: boolean;
}

const SortRow: React.FC<{
  icon: React.ElementType;
  label: string;
  active: boolean;
  onClick: () => void;
}> = ({ icon: Icon, label, active, onClick }) => (
  <button
    onClick={onClick}
    className={clsx(
      'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12px] transition-colors',
      active ? 'bg-hover text-ink' : 'text-secondary hover:bg-hover hover:text-ink',
    )}
  >
    <Icon className="h-3.5 w-3.5" />
    {label}
  </button>
);

const Footer: React.FC<{ onClear?: () => void; onApply: () => void; canClear: boolean }> = ({
  onClear,
  onApply,
  canClear,
}) => {
  const { t } = useTranslation();
  return (
    <div className="mt-2 flex items-center justify-between">
      <button
        onClick={onClear}
        disabled={!canClear}
        className="text-[11px] font-medium text-tertiary hover:text-ink disabled:opacity-40"
      >
        {t('Clear')}
      </button>
      <button
        onClick={onApply}
        className="rounded-md bg-accent-strong px-2.5 py-1 text-[11px] font-medium text-accent-on hover:bg-accent-strong/90"
      >
        {t('Apply')}
      </button>
    </div>
  );
};

const inputCls =
  'w-full rounded-md border border-border bg-canvas px-2 py-1 text-[12px] text-ink placeholder:text-tertiary focus:border-border-strong focus:outline-none';

const ContainsEditor: React.FC<{
  col: string;
  clause?: FilterClause;
  onApply: (c: FilterClause | null) => void;
}> = ({ col, clause, onApply }) => {
  const { t } = useTranslation();
  const [v, setV] = useState(clause?.op === 'contains' ? clause.value : '');
  const apply = () => onApply(v.trim() ? { col, op: 'contains', value: v.trim() } : null);
  return (
    <div>
      <input
        autoFocus
        value={v}
        onChange={(e) => setV(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && apply()}
        placeholder={t('contains…')}
        className={inputCls}
      />
      <Footer canClear={!!clause} onClear={() => onApply(null)} onApply={apply} />
    </div>
  );
};

const RangeEditor: React.FC<{
  col: string;
  facet?: ColumnFacet;
  clause?: FilterClause;
  onApply: (c: FilterClause | null) => void;
}> = ({ col, facet, clause, onApply }) => {
  const { t } = useTranslation();
  const [mn, setMn] = useState<number | null>(
    clause?.op === 'between' && clause.min != null ? Number(clause.min) : null,
  );
  const [mx, setMx] = useState<number | null>(
    clause?.op === 'between' && clause.max != null ? Number(clause.max) : null,
  );
  const apply = () => {
    const min = mn;
    const max = mx;
    if ((min == null || Number.isNaN(min)) && (max == null || Number.isNaN(max))) {
      onApply(null);
      return;
    }
    onApply({
      col,
      op: 'between',
      ...(min != null && !Number.isNaN(min) ? { min } : {}),
      ...(max != null && !Number.isNaN(max) ? { max } : {}),
    });
  };
  return (
    <div>
      <div className="flex items-center gap-1.5">
        <NumberInput
          autoFocus
          value={mn}
          onChange={setMn}
          onKeyDown={(e) => e.key === 'Enter' && apply()}
          placeholder={facet?.min != null ? t('min {{v}}', { v: facet.min }) : t('min')}
          size="sm"
          hideSteppers
        />
        <span className="text-tertiary">–</span>
        <NumberInput
          value={mx}
          onChange={setMx}
          onKeyDown={(e) => e.key === 'Enter' && apply()}
          placeholder={facet?.max != null ? t('max {{v}}', { v: facet.max }) : t('max')}
          size="sm"
          hideSteppers
        />
      </div>
      <Footer canClear={!!clause} onClear={() => onApply(null)} onApply={apply} />
    </div>
  );
};

const BooleanEditor: React.FC<{
  col: string;
  clause?: FilterClause;
  onApply: (c: FilterClause | null) => void;
}> = ({ col, clause, onApply }) => {
  const { t } = useTranslation();
  const init: 'any' | 'true' | 'false' =
    clause?.op === 'eq' ? (clause.value === 'true' ? 'true' : 'false') : 'any';
  const [v, setV] = useState<'any' | 'true' | 'false'>(init);
  const opts: { id: 'any' | 'true' | 'false'; label: string }[] = [
    { id: 'any', label: t('Any') },
    { id: 'true', label: t('True') },
    { id: 'false', label: t('False') },
  ];
  return (
    <div>
      <div className="flex flex-col gap-0.5">
        {opts.map((o) => (
          <button
            key={o.id}
            onClick={() => setV(o.id)}
            className={clsx(
              'flex items-center gap-2 rounded-md px-2 py-1 text-left text-[12px]',
              v === o.id ? 'bg-hover text-ink' : 'text-secondary hover:bg-hover',
            )}
          >
            <span className={clsx('h-3 w-3 rounded-full border', v === o.id ? 'border-ink bg-ink' : 'border-border')} />
            {o.label}
          </button>
        ))}
      </div>
      <Footer
        canClear={!!clause}
        onClear={() => onApply(null)}
        onApply={() => onApply(v === 'any' ? null : { col, op: 'eq', value: v })}
      />
    </div>
  );
};

const ChecklistEditor: React.FC<{
  col: string;
  facet: ColumnFacet;
  clause?: FilterClause;
  onApply: (c: FilterClause | null) => void;
}> = ({ col, facet, clause, onApply }) => {
  const { t } = useTranslation();
  const [sel, setSel] = useState<Set<string>>(
    new Set(clause?.op === 'in' ? clause.values : []),
  );
  const [search, setSearch] = useState('');
  const options = (facet.distinct || []).filter((d) =>
    d.value.toLowerCase().includes(search.toLowerCase()),
  );
  const toggle = (val: string) => {
    setSel((prev) => {
      const next = new Set(prev);
      if (next.has(val)) next.delete(val); else next.add(val);
      return next;
    });
  };
  return (
    <div>
      {(facet.distinct || []).length > 8 && (
        <div className="relative mb-1.5">
          <MagnifyingGlassIcon className="pointer-events-none absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-tertiary" />
          <input
            autoFocus
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('Search values…')}
            className={clsx(inputCls, 'pl-7')}
          />
        </div>
      )}
      <div className="max-h-52 space-y-0.5 overflow-y-auto">
        {options.length === 0 ? (
          <p className="px-2 py-1 text-[11px] text-tertiary">{t('No values')}</p>
        ) : (
          options.map((d) => {
            const on = sel.has(d.value);
            return (
              <button
                key={d.value}
                onClick={() => toggle(d.value)}
                className="flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left hover:bg-hover"
              >
                <span
                  className={clsx(
                    'flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border',
                    on ? 'border-ink bg-ink text-white' : 'border-border',
                  )}
                >
                  {on && <CheckIcon className="h-2.5 w-2.5" />}
                </span>
                <span className="min-w-0 flex-1 truncate text-[12px] text-ink" title={d.value}>
                  {d.value}
                </span>
                <span className="shrink-0 text-[10px] tabular-nums text-tertiary">{d.count}</span>
              </button>
            );
          })
        )}
      </div>
      <Footer
        canClear={!!clause}
        onClear={() => onApply(null)}
        onApply={() => onApply(sel.size ? { col, op: 'in', values: [...sel] } : null)}
      />
    </div>
  );
};

const FilterEditor: React.FC<{
  col: string;
  facet?: ColumnFacet;
  clause?: FilterClause;
  onApply: (c: FilterClause | null) => void;
}> = ({ col, facet, clause, onApply }) => {
  const type = facet?.type;
  const hasDistinct = !!facet?.distinct?.length;
  if (type === 'boolean') return <BooleanEditor col={col} clause={clause} onApply={onApply} />;
  if (type === 'number') return <RangeEditor col={col} facet={facet} clause={clause} onApply={onApply} />;
  if (hasDistinct && facet) return <ChecklistEditor col={col} facet={facet} clause={clause} onApply={onApply} />;
  return <ContainsEditor col={col} clause={clause} onApply={onApply} />;
};

/** Per-column header menu: sort asc/desc + a data-aware filter editor. */
export const ColumnFilterMenu: React.FC<Props> = ({
  col,
  facet,
  clause,
  sortDir,
  onSort,
  onApply,
  filterable = true,
}) => {
  const { t } = useTranslation();
  return (
    <Popover
      active={!!clause}
      width={facet?.distinct ? 280 : 232}
      trigger={<ChevronDownIcon className="h-3 w-3" />}
      triggerClassName="h-4 w-4"
    >
      {(close) => (
        <div className="space-y-1">
          <SortRow
            icon={BarsArrowUpIcon}
            label={t('Sort ascending')}
            active={sortDir === 'asc'}
            onClick={() => {
              onSort('asc');
              close();
            }}
          />
          <SortRow
            icon={BarsArrowDownIcon}
            label={t('Sort descending')}
            active={sortDir === 'desc'}
            onClick={() => {
              onSort('desc');
              close();
            }}
          />
          {filterable && (
            <>
              <div className="my-1 border-t border-border" />
              <div className="px-1 pb-0.5 text-[10px] font-semibold uppercase tracking-wide text-tertiary">
                {t('Filter')}
              </div>
              <div className="px-1 pb-1">
                <FilterEditor
                  col={col}
                  facet={facet}
                  clause={clause}
                  onApply={(c) => {
                    onApply(c);
                    close();
                  }}
                />
              </div>
            </>
          )}
        </div>
      )}
    </Popover>
  );
};

export default ColumnFilterMenu;
