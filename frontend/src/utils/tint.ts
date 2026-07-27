import type { CSSProperties } from 'react';

/**
 * Category tints — "color from content".
 *
 * The app chrome is warm-neutral monochrome; color is carried by content tiles
 * (the soft thumbnail behind an item's icon). Each tint maps to a flat, muted soft
 * fill + an icon color defined per-theme in styles/theme.css, so a tile reads
 * in both light and dark mode. Use {@link tintStyle} to paint any surface, or the
 * {@link IconTile} component for the common rounded icon tile.
 */
export type Tint = 'blue' | 'green' | 'amber' | 'violet' | 'teal' | 'rose' | 'neutral';

/** The colorful tints, in a stable order (used by {@link seedTint}). */
export const TINTS: readonly Tint[] = ['blue', 'green', 'amber', 'violet', 'teal', 'rose'] as const;

/** Inline style for a tinted surface: soft gradient fill + AA icon/text color. */
export function tintStyle(tint: Tint = 'neutral'): CSSProperties {
  if (tint === 'neutral') {
    return { backgroundColor: 'rgb(var(--hover))', color: 'rgb(var(--secondary))' };
  }
  return {
    backgroundColor: `rgb(var(--tint-${tint}-a))`,
    color: `rgb(var(--tint-${tint}-fg))`,
  };
}

/**
 * Deterministic tint from any seed (id, name, slug). Same seed → same tint, so an
 * item keeps a stable color identity across renders and pages without storing it.
 */
export function seedTint(seed: string | number | null | undefined): Tint {
  const s = String(seed ?? '');
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return TINTS[h % TINTS.length];
}

/** Semantic tint for a workflow type (falls back to a stable seed by type). */
const WORKFLOW_TYPE_TINT: Record<string, Tint> = {
  pre_check: 'blue',
  on_change: 'amber',
  ai_navigate: 'violet',
  recorded: 'teal',
  scheduled: 'green',
  api_recorded: 'rose',
  streaming: 'blue',
};
export function workflowTint(type?: string | null): Tint {
  return (type && WORKFLOW_TYPE_TINT[type]) || 'teal';
}
