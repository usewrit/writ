import React from 'react';
import { useTranslation } from 'react-i18next';
import { ShieldCheckIcon, ServerStackIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';

/**
 * Reusable EXECUTION-TIER chip (Phase 9).
 *
 * Tier is the isolation sub-decision INSIDE the Writ-Cloud venue (plan §"venue +
 * tier decision"). It is auto-derived from run sensitivity — the UI only SHOWS it,
 * never lets the user pick it:
 *   - 'isolated' → a SENSITIVE cloud run (injects credentials / secrets /
 *     persona-auth, or the workflow is unverified/drifted). Runs on the gVisor
 *     ephemeral-process pool; never reuses a warm browser between tenants.
 *   - 'shared'   → a non-sensitive cloud run on the warm-browser shared pool.
 *   - null/undefined → tier is not applicable or not known yet (e.g. own-BYO
 *     venue never tiers, or the backend hasn't stamped it). Renders nothing
 *     unless `placeholder` is set, in which case a neutral em-dash chip shows.
 *
 * Design: monochrome only (zinc ramp); the isolated chip is the inverted
 * (bg-zinc-900) emphasis treatment — mirrors the SkuBadge/premium chip — because
 * isolated is the "stronger guarantee + higher cost" signal. No colored accents.
 */

export type ExecutionTier = 'shared' | 'isolated';

export interface TierBadgeProps {
  /** The execution tier; null/undefined renders nothing (unless `placeholder`). */
  tier?: ExecutionTier | null;
  /** Compact mode drops the text label and shows the icon only (table cells). */
  compact?: boolean;
  /** Render a neutral em-dash chip when the tier is unknown (table columns). */
  placeholder?: boolean;
  className?: string;
}

/** Plain-text label for a tier (also reused outside the chip). */
export function tierLabel(tier: ExecutionTier | null | undefined, t: (k: string) => string): string {
  if (tier === 'isolated') return t('Isolated');
  if (tier === 'shared') return t('Shared');
  return '—';
}

/**
 * PROSPECTIVE cloud tier for a workflow the UI is about to run (FastRunModal /
 * wizard / workflow detail). Mirrors the backend classify_sensitivity intent
 * client-side so the badge + isolated-cost note land where the user decides —
 * the AUTHORITATIVE tier is still computed server-side at dispatch and stamped
 * onto the run (which is what the run-history surfaces show).
 *
 * Returns the tier ONLY when the run could land on Writ Cloud. An own-BYO / local
 * venue never tiers, so we return null there (no badge — tier is a cloud concept).
 *
 *   sensitive    = injects credentials / secrets / persona-auth.
 *   couldRunCloud= the venue is (or may resolve to) Writ Cloud. 'local' forces
 *                  the own agent (no tier); 'cloud'/'auto'/'customer'/undefined
 *                  can all reach cloud.
 */
export function deriveProspectiveTier(opts: {
  sensitive: boolean;
  /** execution_target | exec_policy | buyer run target. 'local' ⇒ no tier. */
  venueHint?: string | null;
}): ExecutionTier | null {
  const v = (opts.venueHint || '').toLowerCase();
  const forcedLocal = v === 'local';
  if (forcedLocal) return null; // own/local venue never tiers
  return opts.sensitive ? 'isolated' : 'shared';
}

export const TierBadge: React.FC<TierBadgeProps> = ({
  tier,
  compact = false,
  placeholder = false,
  className,
}) => {
  const { t } = useTranslation();

  if (tier !== 'isolated' && tier !== 'shared') {
    if (!placeholder) return null;
    return (
      <span
        className={clsx(
          'inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium text-tertiary',
          className,
        )}
      >
        —
      </span>
    );
  }

  const isolated = tier === 'isolated';
  const Icon = isolated ? ShieldCheckIcon : ServerStackIcon;
  const label = isolated ? t('Isolated') : t('Shared');
  const title = isolated
    ? t('Runs isolated on Writ Cloud (fresh sandboxed process per run) because it injects your credentials or secrets. Higher cost; stronger isolation.')
    : t('Runs on the shared Writ Cloud pool (warm browser, fresh context per run). For non-sensitive runs.');

  return (
    <span
      title={title}
      className={clsx(
        'inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide',
        isolated ? 'bg-zinc-900 text-white' : 'border border-border text-secondary',
        className,
      )}
    >
      <Icon className={clsx('w-3 h-3 shrink-0', !isolated && 'text-tertiary')} />
      {!compact && label}
    </span>
  );
};

export default TierBadge;
