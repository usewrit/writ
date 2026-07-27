import React, { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { isHttpUrl, classifyValue, previewText } from '../../utils/valueType';
import { ModeToggle } from './ModeToggle';

/**
 * Render a JSON value for a human reading extracted data. An array of records
 * (the common "list of posts / prospects / products" shape) renders as a
 * scannable CARD list by default — each record a card with a prominent title,
 * its link, and its fields, with long text clamped so one giant `content` blob
 * can't swallow the view. A TABLE and pretty-printed RAW JSON are available too.
 * Objects become key/value tables; arrays of scalars become a list. Nested
 * structures recurse, depth-bounded so a deeply nested blob can't blow up the DOM.
 */

const MAX_DEPTH = 4;

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function isImageUrl(v: unknown): v is string {
  return typeof v === 'string' && classifyValue(v).kind === 'image';
}

// Field-name hints used to pick which key heads a record card.
const TITLE_HINT = /\b(name|title|headline|company|product|subject|label|heading|position|role|full_?name)\b|name$|title$/i;
const IMAGE_HINT = /\b(image|img|photo|avatar|thumb|thumbnail|logo|picture|pic|icon|banner|cover)\b/i;

interface RecordRoles {
  titleKey: string | null;
  urlKey: string | null;
  imageKey: string | null;
}

/** A leaf (scalar) value: links become anchors, everything else plain text. */
const Scalar: React.FC<{ value: unknown }> = ({ value }) => {
  if (value === null || value === undefined || value === '') {
    return <span className="text-tertiary/60">—</span>;
  }
  if (typeof value === 'string' && isHttpUrl(value)) {
    return (
      <a
        href={value}
        target="_blank"
        rel="noopener noreferrer"
        onClick={(e) => e.stopPropagation()}
        className="text-ink underline underline-offset-2 break-all"
      >
        {value}
      </a>
    );
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return <span className="tabular-nums">{String(value)}</span>;
  }
  return <span className="break-words">{String(value)}</span>;
};

/**
 * Long text clamped to a few lines with a Show more / Show less toggle, so a
 * large article-length field stays scannable but is fully readable on demand.
 */
const ClampedText: React.FC<{ text: string }> = ({ text }) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const isLong = text.length > 180 || text.split('\n').length > 4;
  if (!isLong) {
    return <span className="whitespace-pre-wrap break-words">{text}</span>;
  }
  return (
    <div>
      <div className={clsx('whitespace-pre-wrap break-words', !open && 'line-clamp-3')}>{text}</div>
      <button
        onClick={(e) => {
          e.stopPropagation();
          setOpen((o) => !o);
        }}
        className="mt-0.5 text-[11px] font-medium text-tertiary underline-offset-2 hover:text-ink hover:underline"
      >
        {open ? t('Show less') : t('Show more')}
      </button>
    </div>
  );
};

/** Pick the title / link / image columns for a list of records. */
function pickRecordRoles(rows: Record<string, unknown>[], cols: string[]): RecordRoles {
  // First non-empty sample per column, to classify its type.
  const sample: Record<string, unknown> = {};
  for (const c of cols) {
    for (const r of rows) {
      const v = r[c];
      if (v !== null && v !== undefined && v !== '') {
        sample[c] = v;
        break;
      }
    }
  }
  const kindOf = (c: string) => classifyValue(sample[c]).kind;
  const isShortText = (c: string) => {
    const k = kindOf(c);
    return (k === 'text' || k === 'markdown') && String(sample[c] ?? '').length <= 120;
  };

  const imageKey = cols.find((c) => kindOf(c) === 'image') || cols.find((c) => IMAGE_HINT.test(c) && kindOf(c) === 'url') || null;
  const urlKey = cols.find((c) => kindOf(c) === 'url' && c !== imageKey) || null;
  const titleKey =
    cols.find((c) => c !== imageKey && TITLE_HINT.test(c) && (kindOf(c) === 'text' || kindOf(c) === 'markdown')) ||
    cols.find((c) => c !== imageKey && c !== urlKey && isShortText(c)) ||
    cols.find((c) => c !== imageKey && (kindOf(c) === 'text' || kindOf(c) === 'markdown')) ||
    null;

  return { titleKey, urlKey, imageKey };
}

/** One field inside a record card: a label plus its type-aware value. */
const CardField: React.FC<{ name: string; value: unknown; depth: number }> = ({ name, value, depth }) => {
  if (value === null || value === undefined || value === '') return null;
  const c = classifyValue(value);

  // Containers / images / long bodies render full-width below the label.
  const isContainer = c.kind === 'json';
  const isBody = c.kind === 'text' || c.kind === 'markdown';
  const isImage = c.kind === 'image';

  if (isImage) {
    return (
      <div>
        <FieldLabel>{name}</FieldLabel>
        <a href={String(value)} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()} className="mt-1 inline-block">
          <img src={String(value)} alt="" loading="lazy" className="max-h-32 rounded-md border border-border object-contain" />
        </a>
      </div>
    );
  }
  if (isContainer) {
    return (
      <div>
        <FieldLabel>{name}</FieldLabel>
        <div className="mt-1">
          <JsonNode value={c.json} depth={depth + 1} />
        </div>
      </div>
    );
  }
  if (isBody) {
    return (
      <div>
        <FieldLabel>{name}</FieldLabel>
        <div className="mt-0.5 text-[12px] leading-relaxed text-ink">
          <ClampedText text={String(value)} />
        </div>
      </div>
    );
  }
  // Short scalar / url / number / boolean -> inline "label  value".
  return (
    <div className="flex items-baseline gap-2">
      <FieldLabel inline>{name}</FieldLabel>
      <div className="min-w-0 flex-1 truncate text-[12px] text-ink" title={c.kind === 'url' ? String(value) : undefined}>
        <Scalar value={value} />
      </div>
    </div>
  );
};

const FieldLabel: React.FC<{ children: React.ReactNode; inline?: boolean }> = ({ children, inline }) => (
  <span
    className={clsx(
      'text-[10px] font-semibold uppercase tracking-wide text-tertiary',
      inline ? 'shrink-0' : 'block',
    )}
  >
    {children}
  </span>
);

/** A single record rendered as a scannable card. */
const RecordCard: React.FC<{ row: Record<string, unknown>; index: number; roles: RecordRoles; cols: string[]; depth: number }> = ({
  row,
  index,
  roles,
  cols,
  depth,
}) => {
  const { t } = useTranslation();
  const title = roles.titleKey ? previewText(row[roles.titleKey]) : '';
  const url = roles.urlKey ? row[roles.urlKey] : null;
  const imageSrc = roles.imageKey && isImageUrl(row[roles.imageKey]) ? String(row[roles.imageKey]) : null;
  // Body fields = everything not already shown in the header.
  const headerKeys = new Set([roles.titleKey, roles.imageKey, roles.urlKey].filter(Boolean) as string[]);
  const fieldKeys = cols.filter((c) => !headerKeys.has(c));

  return (
    <div className="rounded-lg border border-border bg-surface p-3">
      <div className="flex items-start gap-3">
        {imageSrc && (
          <img src={imageSrc} alt="" loading="lazy" className="h-12 w-12 shrink-0 rounded-md border border-border object-cover" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              {title ? (
                <div className="break-words text-[13px] font-semibold leading-snug text-ink line-clamp-2">{title}</div>
              ) : (
                <div className="text-[12px] font-medium text-secondary">{t('Record {{n}}', { n: index })}</div>
              )}
              {url != null && isHttpUrl(String(url)) && (
                <a
                  href={String(url)}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={(e) => e.stopPropagation()}
                  className="mt-0.5 block truncate text-[11px] text-ink/70 underline underline-offset-2 hover:text-ink"
                  title={String(url)}
                >
                  {String(url)}
                </a>
              )}
            </div>
            <span className="shrink-0 text-[10px] tabular-nums text-tertiary">{index}</span>
          </div>

          {fieldKeys.length > 0 && (
            <div className="mt-2 space-y-1.5">
              {fieldKeys.map((c) => (
                <CardField key={c} name={c} value={row[c]} depth={depth} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

/** An array of record objects rendered as a scannable card list. */
const RecordCards: React.FC<{ rows: Record<string, unknown>[]; depth: number }> = ({ rows, depth }) => {
  const cols = useMemo(() => {
    const out: string[] = [];
    for (const r of rows) for (const k of Object.keys(r)) if (!out.includes(k)) out.push(k);
    return out;
  }, [rows]);
  const roles = useMemo(() => pickRecordRoles(rows, cols), [rows, cols]);
  return (
    <div className="space-y-2">
      {rows.map((r, i) => (
        <RecordCard key={i} row={r} index={i + 1} roles={roles} cols={cols} depth={depth} />
      ))}
    </div>
  );
};

/** A table cell value: scalars truncate (with full value on hover), containers recurse. */
const TableCellValue: React.FC<{ value: unknown; depth: number }> = ({ value, depth }) => {
  if (isPlainObject(value) || Array.isArray(value)) {
    return <JsonNode value={value} depth={depth + 1} />;
  }
  if (value === null || value === undefined || value === '') {
    return <span className="text-tertiary/60">—</span>;
  }
  const text = previewText(value);
  return (
    <div className="max-w-[280px] truncate" title={text.length > 48 ? text : undefined}>
      <Scalar value={value} />
    </div>
  );
};

/** An array of records as a bounded column table (long cells truncate). */
const RecordTable: React.FC<{ rows: Record<string, unknown>[]; depth: number }> = ({ rows, depth }) => {
  const cols: string[] = [];
  for (const row of rows) for (const k of Object.keys(row)) if (!cols.includes(k)) cols.push(k);
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full border-collapse text-[11px]">
        <thead className="bg-canvas/60">
          <tr>
            <th className="px-2 py-1 text-left font-medium text-tertiary">#</th>
            {cols.map((c) => (
              <th key={c} className="px-2 py-1 text-left font-medium text-tertiary whitespace-nowrap">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {rows.map((row, ri) => (
            <tr key={ri} className="align-top">
              <td className="px-2 py-1 text-tertiary tabular-nums">{ri + 1}</td>
              {cols.map((c) => (
                <td key={c} className="px-2 py-1 align-top text-ink">
                  <TableCellValue value={row[c]} depth={depth} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

/** An array of records with a Cards / Table switch (cards by default). */
const RecordArray: React.FC<{ rows: Record<string, unknown>[]; depth: number }> = ({ rows, depth }) => {
  const { t } = useTranslation();
  const [mode, setMode] = useState<'cards' | 'table'>('cards');
  return (
    <div className="space-y-1.5">
      <ModeToggle
        value={mode}
        onChange={setMode}
        options={[
          { id: 'cards', label: t('Cards') },
          { id: 'table', label: t('Table') },
        ]}
      />
      {mode === 'cards' ? <RecordCards rows={rows} depth={depth} /> : <RecordTable rows={rows} depth={depth} />}
    </div>
  );
};

const JsonNode: React.FC<{ value: unknown; depth: number }> = ({ value, depth }) => {
  if (!isPlainObject(value) && !Array.isArray(value)) {
    return <Scalar value={value} />;
  }
  if (depth >= MAX_DEPTH) {
    return (
      <code className="block max-w-full overflow-x-auto rounded bg-canvas px-1.5 py-0.5 text-[11px] text-secondary">
        {JSON.stringify(value)}
      </code>
    );
  }

  // Array
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-tertiary/60">[]</span>;
    const allObjects = value.every((it) => isPlainObject(it));
    if (allObjects) {
      // Array of records -> scannable cards by default, with a Cards/Table switch.
      return <RecordArray rows={value as Record<string, unknown>[]} depth={depth} />;
    }
    // Array of scalars / mixed -> indexed rows.
    return (
      <ol className="space-y-0.5">
        {value.map((it, idx) => (
          <li key={idx} className="flex gap-2">
            <span className="select-none text-tertiary tabular-nums">{idx + 1}.</span>
            <span className="min-w-0">
              <JsonNode value={it} depth={depth + 1} />
            </span>
          </li>
        ))}
      </ol>
    );
  }

  // Object -> 2-column key/value table. Auto layout sizes the key column to its
  // content, which is what makes it readable — but it also means a wide value
  // (a nested record table, a long unbroken token) grows the table past its
  // parent. The scroller keeps that growth inside this node instead of letting
  // it push the whole expanded record sideways.
  const entries = Object.entries(value);
  if (entries.length === 0) return <span className="text-tertiary/60">{'{}'}</span>;
  return (
    <div className="max-w-full overflow-x-auto">
      <table className="w-full border-collapse text-[11px]">
        <tbody className="divide-y divide-border">
          {entries.map(([k, v]) => (
            <tr key={k} className="align-top">
              <td className="w-px whitespace-nowrap py-1 pr-3 font-medium text-tertiary">{k}</td>
              <td className="min-w-0 py-1 text-ink">
                {typeof v === 'string' && !isHttpUrl(v) ? <ClampedText text={v} /> : <JsonNode value={v} depth={depth + 1} />}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

type JsonViewMode = 'cards' | 'table' | 'raw';

export const JsonView: React.FC<{ value: unknown; defaultMode?: 'table' | 'raw' }> = ({ value, defaultMode }) => {
  const { t } = useTranslation();
  const isContainer = isPlainObject(value) || Array.isArray(value);
  // An array of records gets the scannable card view as its default.
  const isRecordList = Array.isArray(value) && value.length > 0 && value.every((it) => isPlainObject(it));
  const [mode, setMode] = useState<JsonViewMode>(defaultMode ?? (isRecordList ? 'cards' : 'table'));

  const options: { id: JsonViewMode; label: string }[] = isRecordList
    ? [
        { id: 'cards', label: t('Cards') },
        { id: 'table', label: t('Table') },
        { id: 'raw', label: t('JSON') },
      ]
    : [
        { id: 'table', label: t('Table') },
        { id: 'raw', label: t('JSON') },
      ];

  return (
    <div className="space-y-1.5">
      {isContainer && <ModeToggle value={mode} onChange={(m) => setMode(m)} options={options} />}
      {!isContainer || mode === 'raw' ? (
        <pre className="max-h-80 overflow-auto rounded-lg border border-border bg-canvas p-2.5 text-[11px] leading-relaxed text-ink">
          {JSON.stringify(value, null, 2)}
        </pre>
      ) : isRecordList ? (
        mode === 'cards' ? (
          <RecordCards rows={value as Record<string, unknown>[]} depth={0} />
        ) : (
          <RecordTable rows={value as Record<string, unknown>[]} depth={0} />
        )
      ) : (
        <div className="overflow-x-auto">
          <JsonNode value={value} depth={0} />
        </div>
      )}
    </div>
  );
};

export default JsonView;
