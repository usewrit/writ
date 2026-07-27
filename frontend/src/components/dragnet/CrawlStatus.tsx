import React from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import i18n from '../../i18n';
import { crawlAgentsWorking, type CrawlStatus, type CrawlView } from '../../api/crawl';

/**
 * Shared Dragnet status vocabulary — the status chip, the live progress bar and
 * the "agents working" pill, so the list and detail surfaces read identically.
 *
 * Self-hosted palette: this frontend has no Signal accent, so the RUNNING state
 * uses the semantic `info` (blue) token. Every other state uses the app's
 * neutral + semantic palette — no accent on idle, done, or cancelled.
 */

export type CrawlTone = 'live' | 'warning' | 'success' | 'danger' | 'neutral';

interface StatusMeta {
  label: string;
  tone: CrawlTone;
  /** In-flight: drives the pulse + running progress motion. */
  live: boolean;
}

export function crawlStatusMeta(status: CrawlStatus): StatusMeta {
  switch (status) {
    case 'queued':
      return { label: i18n.t('Queued'), tone: 'neutral', live: false };
    case 'mapping':
      return { label: i18n.t('Mapping'), tone: 'live', live: true };
    case 'crawling':
      return { label: i18n.t('Crawling'), tone: 'live', live: true };
    case 'stopping':
      return { label: i18n.t('Stopping'), tone: 'warning', live: true };
    case 'completed':
      return { label: i18n.t('Completed'), tone: 'success', live: false };
    case 'failed':
      return { label: i18n.t('Failed'), tone: 'danger', live: false };
    case 'cancelled':
      return { label: i18n.t('Cancelled'), tone: 'neutral', live: false };
    default:
      return { label: status, tone: 'neutral', live: false };
  }
}

// Soft-fill pill + colored dot; live states pulse (mirrors StatusBadge).
const TONE: Record<CrawlTone, { pill: string; dot: string; text: string }> = {
  live: { pill: 'bg-info-bg', dot: 'bg-info', text: 'text-info-fg' },
  warning: { pill: 'bg-warning-bg', dot: 'bg-warning', text: 'text-warning-fg' },
  success: { pill: 'bg-success-bg', dot: 'bg-success', text: 'text-success-fg' },
  danger: { pill: 'bg-danger-bg', dot: 'bg-danger', text: 'text-danger-fg' },
  neutral: { pill: 'bg-hover', dot: 'bg-tertiary', text: 'text-secondary' },
};

const CHIP_SIZE = {
  sm: 'px-2 py-0.5 text-[11px] gap-1.5',
  md: 'px-2.5 py-1 text-xs gap-1.5',
} as const;

export const CrawlStatusChip: React.FC<{
  status: CrawlStatus;
  size?: keyof typeof CHIP_SIZE;
  className?: string;
}> = ({ status, size = 'md', className }) => {
  const meta = crawlStatusMeta(status);
  const tone = TONE[meta.tone];
  return (
    <span
      className={clsx(
        'inline-flex items-center rounded-full font-medium transition-colors',
        tone.pill,
        tone.text,
        CHIP_SIZE[size],
        className,
      )}
    >
      {meta.live ? (
        <span className="relative flex h-1.5 w-1.5">
          <span className={clsx('animate-status-pulse absolute inline-flex h-full w-full rounded-full opacity-60', tone.dot)} />
          <span className={clsx('relative inline-flex h-1.5 w-1.5 rounded-full', tone.dot)} />
        </span>
      ) : (
        <span className={clsx('inline-flex h-1.5 w-1.5 rounded-full', tone.dot)} />
      )}
      {meta.label}
    </span>
  );
};

/**
 * The crawl's page progress. Determinate once the site map has a page count
 * (pages_done / pages_discovered); an indeterminate glide while still mapping.
 * Running fill = info with a slow sheen; completed = ink; failed = danger.
 */
export const CrawlProgressBar: React.FC<{
  crawl: CrawlView;
  size?: 'sm' | 'md';
  className?: string;
}> = ({ crawl, size = 'md', className }) => {
  const meta = crawlStatusMeta(crawl.status);
  const total = crawl.pages_discovered || 0;
  const done = crawl.pages_done || 0;
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const indeterminate = meta.live && total === 0;

  const fill =
    crawl.status === 'completed'
      ? 'bg-ink'
      : crawl.status === 'failed'
        ? 'bg-danger'
        : crawl.status === 'cancelled'
          ? 'bg-tertiary'
          : 'bg-info';

  const height = size === 'sm' ? 'h-1.5' : 'h-2';

  return (
    <div
      className={clsx('relative w-full overflow-hidden rounded-full bg-canvas', height, className)}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={total || undefined}
      aria-valuenow={indeterminate ? undefined : done}
    >
      {indeterminate ? (
        <span className="dragnet-indeterminate absolute inset-y-0 left-0 w-1/3 rounded-full bg-info/80" />
      ) : (
        <div
          className={clsx('relative h-full overflow-hidden rounded-full transition-[width] duration-500 ease-out', fill)}
          style={{ width: `${pct}%` }}
        >
          {meta.live && (
            <span className="dragnet-sheen absolute inset-y-0 left-0 w-1/3 bg-gradient-to-r from-transparent via-white/55 to-transparent" />
          )}
        </div>
      )}
    </div>
  );
};

/**
 * Live "agents working" pill — the shards currently out on the fleet
 * (dispatched − done). Info dot while agents are active; a quiet waiting
 * state before the first shard lands.
 */
export const AgentsWorking: React.FC<{ crawl: CrawlView; className?: string }> = ({ crawl, className }) => {
  const { t } = useTranslation();
  const working = crawlAgentsWorking(crawl);
  const active = working > 0;
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1.5 text-[12px] font-medium tabular-nums',
        active ? 'text-info-fg' : 'text-tertiary',
        className,
      )}
    >
      <span className="relative flex h-1.5 w-1.5">
        {active && <span className="animate-status-pulse absolute inline-flex h-full w-full rounded-full bg-info opacity-60" />}
        <span className={clsx('relative inline-flex h-1.5 w-1.5 rounded-full', active ? 'bg-info' : 'bg-tertiary')} />
      </span>
      {active
        ? working === 1
          ? t('{{count}} agent working', { count: working })
          : t('{{count}} agents working', { count: working })
        : t('Spinning up agents…')}
    </span>
  );
};
