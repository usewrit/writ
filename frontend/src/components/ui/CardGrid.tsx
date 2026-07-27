import React from 'react';
import clsx from 'clsx';

/**
 * Width-filling card layout for read-only "overview"/detail pages.
 *
 * Replaces the single narrow left-pinned `space-y-*` stack (which left a dead
 * band before the right rail) with cards that flow into balanced columns and
 * use the available width. Each child stays intact across the column break.
 *
 * Use for overview/dashboard content. Keep FORMS in a single comfortable-width
 * column (don't wrap forms in this) — wide form fields hurt readability.
 */
export const CardGrid: React.FC<{
  children: React.ReactNode;
  /** Max columns on wide screens (default 2). */
  columns?: 2 | 3;
  className?: string;
}> = ({ children, columns = 2, className }) => (
  <div
    className={clsx(
      columns === 3 ? 'columns-1 @rail/stage:columns-2 @wide/stage:columns-3' : 'columns-1 @split/stage:columns-2',
      'gap-5 [&>*]:mb-5 [&>*]:break-inside-avoid',
      className,
    )}
  >
    {children}
  </div>
);
