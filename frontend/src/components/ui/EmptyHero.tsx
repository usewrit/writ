import React from 'react';
import clsx from 'clsx';

type IconType = React.ComponentType<{ className?: string }>;

export interface EmptyHeroProps {
  /** The one glyph that names what's missing (or what went wrong). */
  icon: IconType;
  title: React.ReactNode;
  description?: React.ReactNode;
  /**
   * 'md' (default) fills a page body or a shelf column. 'sm' is for the small
   * panels — popovers, dropdowns, the recorder's step rail — where a 48px tile
   * and 14px title would crowd the box.
   */
  size?: 'sm' | 'md';
  /**
   * Layout only: how the block claims its height and whether it paints a panel
   * background. The visual treatment (tile, glyph, type scale) is NOT
   * overridable — that's the whole point of this component.
   *
   * Common values: `flex-1 min-h-0 bg-surface` (shelf list column),
   * `flex-1` (detail pane), `min-h-[50vh]` (page body), `py-10` (popover).
   */
  className?: string;
  /** Action row — buttons/links. Spacing above is supplied here, don't add `mt-*`. */
  children?: React.ReactNode;
}

// One tile, one glyph color, one type scale — the empty state is the moment the
// app has nothing to show, so it's the moment its look has to be most certain.
// The tile stays neutral (colored icon boxes are decoration, and the design
// language reserves color for meaning); the glyph carries the brand red so the
// state reads as designed rather than as an absence. Change these four lines and
// every empty, error, and nothing-selected state in the app changes with them.
const TILE = {
  sm: 'h-10 w-10 mb-3',
  md: 'h-12 w-12 mb-4',
} as const;
const GLYPH = { sm: 'h-5 w-5', md: 'h-6 w-6' } as const;
const TITLE = { sm: 'text-xs', md: 'text-sm' } as const;
const DESC = { sm: 'text-[11px] max-w-[17rem]', md: 'text-xs max-w-sm' } as const;

export const EmptyHero: React.FC<EmptyHeroProps> = ({
  icon: Icon,
  title,
  description,
  size = 'md',
  className,
  children,
}) => (
  <div className={clsx('flex w-full flex-col items-center justify-center px-6 text-center', className)}>
    <div
      aria-hidden="true"
      className={clsx(
        'flex shrink-0 items-center justify-center rounded-2xl border border-border bg-hover',
        TILE[size],
      )}
    >
      <Icon className={clsx(GLYPH[size], 'text-accent')} />
    </div>
    <p className={clsx('font-medium text-ink', TITLE[size])}>{title}</p>
    {description && (
      <p className={clsx('mt-1 text-secondary', DESC[size])}>{description}</p>
    )}
    {children && (
      <div className="mt-4 flex flex-wrap items-center justify-center gap-2">{children}</div>
    )}
  </div>
);

export default EmptyHero;
