import React, { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import {
  GlobeAltIcon,
  UserIcon,
  CalendarDaysIcon,
  ClockIcon,
  BuildingLibraryIcon,
} from '@heroicons/react/24/outline';
import { Markdown, safeHref } from './Markdown';
import { parseFrontmatter, type DocMeta } from '../../utils/frontmatter';
import { uiLocale } from '../../utils/format';

/** Host of a URL for a compact source chip (falls back to the raw string). */
function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return url;
  }
}

/** Format an ISO (or any parseable) date as a short absolute day; passthrough on
 *  failure so we never show "Invalid Date". */
function fmtDate(value: string): string {
  const d = new Date(value);
  if (isNaN(d.getTime())) return value;
  return d.toLocaleDateString(uiLocale(), { year: 'numeric', month: 'short', day: 'numeric' });
}

const MetaItem: React.FC<{ icon: React.ElementType; title?: string; children: React.ReactNode }> = ({
  icon: Icon,
  title,
  children,
}) => (
  <span className="inline-flex max-w-full items-center gap-1 truncate" title={title}>
    <Icon className="h-3.5 w-3.5 shrink-0 text-tertiary" />
    <span className="truncate">{children}</span>
  </span>
);

/** The document header built from crawl frontmatter — title, description, and a
 *  meta row (source · author · site · published · crawled). Metadata never leaks
 *  into the rendered body. */
const DocHeader: React.FC<{ meta: DocMeta }> = ({ meta }) => {
  const { t } = useTranslation();
  // meta.url comes from the crawled document's own frontmatter (a permissive
  // `key: value` scan over the leading `---` block), so it is attacker-controlled
  // content: a hostile page can begin its extracted text with `---\nurl:
  // javascript:…\n---`. Same scheme allowlist the body links go through.
  const url = safeHref(meta.url?.trim() || '');
  const hasMetaRow = !!(url || meta.author || meta.site || meta.published || meta.crawled_at);
  const title = meta.title?.trim();
  const description = meta.description?.trim();
  if (!title && !description && !hasMetaRow) return null;

  return (
    <header className="mb-4 border-b border-border pb-3">
      {title && <h1 className="text-[18px] font-semibold leading-snug tracking-tight text-ink">{title}</h1>}
      {description && (
        <p className="mt-1.5 text-[12.5px] leading-relaxed text-secondary">{description}</p>
      )}
      {hasMetaRow && (
        <div className="mt-2.5 flex flex-wrap items-center gap-x-3.5 gap-y-1.5 text-[11px] text-tertiary">
          {url && (
            <a
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="inline-flex max-w-full items-center gap-1 truncate font-medium text-secondary underline-offset-2 transition-colors hover:text-ink hover:underline"
              title={url}
            >
              <GlobeAltIcon className="h-3.5 w-3.5 shrink-0" />
              <span className="truncate">{hostOf(url)}</span>
            </a>
          )}
          {meta.author && <MetaItem icon={UserIcon}>{meta.author}</MetaItem>}
          {meta.site && meta.site !== meta.author && <MetaItem icon={BuildingLibraryIcon}>{meta.site}</MetaItem>}
          {meta.published && (
            <MetaItem icon={CalendarDaysIcon} title={meta.published}>
              {t('Published {{date}}', { date: fmtDate(meta.published) })}
            </MetaItem>
          )}
          {meta.crawled_at && (
            <MetaItem icon={ClockIcon} title={meta.crawled_at}>
              {t('Crawled {{date}}', { date: fmtDate(meta.crawled_at) })}
            </MetaItem>
          )}
        </div>
      )}
    </header>
  );
};

/**
 * A crawled/markdown document: frontmatter parsed into a labelled header, the
 * body rendered by <Markdown> with links/images resolved against the page URL.
 * Falls back to plain <Markdown> when the source has no frontmatter.
 */
export const MarkdownDocument: React.FC<{ source: string }> = ({ source }) => {
  const { meta, body } = useMemo(() => parseFrontmatter(source), [source]);
  return (
    <div>
      {meta && <DocHeader meta={meta} />}
      <Markdown source={body} baseUrl={meta?.url} />
    </div>
  );
};

export default MarkdownDocument;
