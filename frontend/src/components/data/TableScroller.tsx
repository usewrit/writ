import React, { useCallback, useEffect, useRef, useState } from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { ChevronLeftIcon, ChevronRightIcon } from '@heroicons/react/24/outline';

interface TableScrollerProps {
  /** Extra classes for the positioned wrapper (e.g. `min-h-0 flex-1` when maximized). */
  className?: string;
  /**
   * Bound the height as well, so the viewport — not the page — owns the grid's
   * vertical scroll. That is what makes `sticky top-0` on the `<thead>` actually
   * stick AND keeps the horizontal scrollbar on screen instead of parking it
   * below 50 rows of content. Used by the maximized grid.
   */
  bothAxes?: boolean;
  children: React.ReactNode;
}

/**
 * The horizontal viewport for the extracted-data grid.
 *
 * A fixed-layout grid with 8 data columns is ~1,600px wide and the detail pane
 * that holds it is often half that, so the columns past the fold have always been
 * *reachable* — the wrapper has scrolled since day one — but never *discoverable*:
 * macOS paints overlay scrollbars (invisible until you already scroll) and the
 * only one this region had sat at the very bottom of a 50-row table, off screen.
 * The grid therefore read as "cut off at the viewport edge" rather than "scroll
 * me". This adds the missing affordances — a live edge fade on whichever side has
 * more content, a click-to-page chevron, and a scrollbar that is actually drawn —
 * and keeps the scroll itself strictly inside the pane (`overscroll-x-contain`, so
 * reaching the end doesn't hand the gesture to the browser's back-swipe).
 */
export const TableScroller: React.FC<TableScrollerProps> = ({ className, bothAxes = false, children }) => {
  const { t } = useTranslation();
  const ref = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ left: false, right: false });

  const measure = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    // 1px of slack: fractional layout widths otherwise leave `right` stuck on at
    // the very end of the scroll, and the fade never clears.
    const left = el.scrollLeft > 1;
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
    setEdges((prev) => (prev.left === left && prev.right === right ? prev : { left, right }));
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    measure();
    // The overflow changes without any scroll event too — a column resize, a lens
    // switch, the Fields menu, or just the pane being dragged narrower — so watch
    // the viewport AND the table it holds.
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    if (el.firstElementChild) ro.observe(el.firstElementChild);
    return () => ro.disconnect();
  }, [measure, children]);

  const page = (dir: -1 | 1) => {
    const el = ref.current;
    if (!el) return;
    el.scrollBy({ left: dir * Math.max(160, el.clientWidth * 0.8), behavior: 'smooth' });
  };

  const chevron =
    'absolute top-1/2 z-20 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-full border border-border bg-surface text-secondary shadow-sm transition-all duration-150 hover:text-ink hover:shadow focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/40';

  return (
    <div className={clsx('group/scroller relative min-w-0', className)}>
      <div
        ref={ref}
        onScroll={measure}
        className={clsx(
          'scrollbar-thin min-w-0 overscroll-x-contain [scrollbar-width:thin]',
          bothAxes ? 'h-full overflow-auto' : 'overflow-x-auto',
        )}
      >
        {children}
      </div>

      {/* Edge fades — the always-on "there is more this way" cue. */}
      <div
        aria-hidden="true"
        className={clsx(
          'pointer-events-none absolute inset-y-0 left-0 z-10 w-8 bg-gradient-to-r from-surface to-transparent transition-opacity duration-150',
          edges.left ? 'opacity-90' : 'opacity-0',
        )}
      />
      <div
        aria-hidden="true"
        className={clsx(
          'pointer-events-none absolute inset-y-0 right-0 z-10 w-8 bg-gradient-to-l from-surface to-transparent transition-opacity duration-150',
          edges.right ? 'opacity-90' : 'opacity-0',
        )}
      />

      {/* …and the click affordance. Dimmed at rest so it never fights the data,
          solid once the pointer is anywhere over the grid. */}
      <button
        type="button"
        onClick={() => page(-1)}
        aria-label={t('Scroll left')}
        tabIndex={edges.left ? 0 : -1}
        className={clsx(
          chevron,
          'left-1.5',
          edges.left ? 'opacity-60 group-hover/scroller:opacity-100' : 'pointer-events-none opacity-0',
        )}
      >
        <ChevronLeftIcon aria-hidden="true" className="h-4 w-4" />
      </button>
      <button
        type="button"
        onClick={() => page(1)}
        aria-label={t('Scroll right')}
        tabIndex={edges.right ? 0 : -1}
        className={clsx(
          chevron,
          'right-1.5',
          edges.right ? 'opacity-60 group-hover/scroller:opacity-100' : 'pointer-events-none opacity-0',
        )}
      >
        <ChevronRightIcon aria-hidden="true" className="h-4 w-4" />
      </button>
    </div>
  );
};

export default TableScroller;
