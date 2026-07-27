import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { classifyValue } from '../../utils/valueType';
import { MarkdownDocument } from './MarkdownDocument';
import { JsonView } from './JsonView';
import { ModeToggle } from './ModeToggle';

/** Linkify bare URLs in otherwise-plain text (no markdown interpretation). */
const URL_SPLIT = /(https?:\/\/[^\s)]+)/g;
const isUrlPart = (s: string) => /^https?:\/\//.test(s); // non-global: stateless test
const Linkified: React.FC<{ text: string }> = ({ text }) => {
  const parts = text.split(URL_SPLIT);
  return (
    <span className="whitespace-pre-wrap break-words">
      {parts.map((p, i) =>
        isUrlPart(p) ? (
          <a
            key={i}
            href={p}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="text-ink underline underline-offset-2 break-all"
          >
            {p}
          </a>
        ) : (
          <React.Fragment key={i}>{p}</React.Fragment>
        ),
      )}
    </span>
  );
};

/** Text/markdown with a Rendered/Raw toggle (markdown defaults to Rendered). */
const TextValue: React.FC<{ source: string; markdown?: boolean }> = ({ source, markdown }) => {
  const { t } = useTranslation();
  const multiline = source.includes('\n');
  const [mode, setMode] = useState<'rendered' | 'raw'>(markdown ? 'rendered' : 'raw');

  if (!markdown) {
    // Plain text: preserve whitespace + linkify, no markdown interpretation.
    return (
      <div className={clsx(multiline && 'max-h-72 overflow-auto')}>
        <Linkified text={source} />
      </div>
    );
  }
  return (
    <div className="space-y-1.5">
      <ModeToggle
        value={mode}
        onChange={setMode}
        options={[
          { id: 'rendered', label: t('Rendered') },
          { id: 'raw', label: t('Raw') },
        ]}
      />
      {mode === 'rendered' ? (
        <div className="max-h-[32rem] overflow-auto pr-1">
          <MarkdownDocument source={source} />
        </div>
      ) : (
        <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-canvas p-2.5 text-[11px] leading-relaxed text-ink">
          {source}
        </pre>
      )}
    </div>
  );
};

const ImageValue: React.FC<{ src: string }> = ({ src }) => (
  <a href={src} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()} className="inline-block">
    <img
      src={src}
      alt=""
      loading="lazy"
      className="max-h-48 max-w-full rounded-lg border border-border object-contain"
    />
  </a>
);

/**
 * Render an extracted field value with the viewer that fits its type:
 *  - JSON object/array -> Table / Raw JSON toggle (nested tables recurse)
 *  - markdown text     -> Rendered / Raw toggle
 *  - plain text        -> whitespace-preserved, URLs linkified
 *  - url               -> link;  image url -> thumbnail
 *  - number / boolean  -> formatted
 */
export const ValueViewer: React.FC<{ value: unknown }> = ({ value }) => {
  const c = classifyValue(value);
  switch (c.kind) {
    case 'empty':
      return <span className="text-tertiary/60">—</span>;
    case 'boolean':
      return (
        <span className="rounded bg-canvas px-1.5 py-0.5 text-[11px] font-medium text-secondary">
          {String(value)}
        </span>
      );
    case 'number':
      return <span className="tabular-nums text-ink">{String(value)}</span>;
    case 'image':
      return <ImageValue src={String(value)} />;
    case 'url':
      return (
        <a
          href={String(value)}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(e) => e.stopPropagation()}
          className="text-ink underline underline-offset-2 break-all"
        >
          {String(value)}
        </a>
      );
    case 'json':
      return <JsonView value={c.json} />;
    case 'markdown':
      return <TextValue source={String(value)} markdown />;
    case 'text':
    default:
      return <TextValue source={String(value)} />;
  }
};

export default ValueViewer;
