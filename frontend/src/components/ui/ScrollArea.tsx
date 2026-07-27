import React, { useState } from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { ChevronUpIcon, ChevronDownIcon } from '@heroicons/react/24/outline';
import { useScrollHint } from '../../hooks/useScrollHint';

/**
 * The edge fade blends to the scroller's own background, so it has to name a real
 * color token. Static map (not a template string) so Tailwind's JIT scanner keeps
 * the classes. `none` drops the fade and leaves only the floating chevrons.
 */
const FADE_FROM = {
  surface: 'from-surface',
  canvas: 'from-canvas',
  chrome: 'from-chrome',
  none: '',
} as const;

export type ScrollFade = keyof typeof FADE_FROM;

interface ScrollHintOverlayProps {
  canUp: boolean;
  canDown: boolean;
  /** Reveal the interactive chevrons (the parent is hovered). */
  hovered: boolean;
  onUp: () => void;
  onDown: () => void;
  /** Background the edge fade blends to — the scroller's bg. Default: surface. */
  fade?: ScrollFade;
  /** `sm` for tight rails (sidebar), `md` for page content. */
  size?: 'sm' | 'md';
}

/**
 * The floating scroll indicator itself: a soft edge fade that's visible whenever
 * there's more content in that direction (the always-on "based on scroll position"
 * cue), plus a circular chevron button that fades in on hover/focus (the
 * interactive "on hover" affordance). Position it inside a `relative` container
 * that wraps the scroll viewport.
 */
export const ScrollHintOverlay: React.FC<ScrollHintOverlayProps> = ({
  canUp,
  canDown,
  hovered,
  onUp,
  onDown,
  fade = 'surface',
  size = 'md',
}) => {
  const { t } = useTranslation();
  const fadeFrom = FADE_FROM[fade];
  const btn = size === 'sm' ? 'h-6 w-6' : 'h-7 w-7';
  const chev = size === 'sm' ? 'h-3.5 w-3.5' : 'h-4 w-4';
  const edge = size === 'sm' ? 'h-8' : 'h-10';
  const topPos = size === 'sm' ? 'top-1.5' : 'top-2.5';
  const botPos = size === 'sm' ? 'bottom-1.5' : 'bottom-2.5';
  const base =
    'absolute left-1/2 -translate-x-1/2 z-30 rounded-full bg-surface border border-border shadow-sm flex items-center justify-center text-secondary hover:text-ink hover:shadow transition-all duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/40 focus-visible:opacity-100 focus-visible:pointer-events-auto';

  return (
    <>
      {/* Top: fade (scroll-position cue) + chevron (hover affordance) */}
      {fadeFrom && (
        <div
          aria-hidden="true"
          className={clsx(
            'pointer-events-none absolute inset-x-0 top-0 z-20 bg-gradient-to-b to-transparent transition-opacity duration-150',
            edge,
            fadeFrom,
            canUp ? 'opacity-90' : 'opacity-0',
          )}
        />
      )}
      <button
        type="button"
        onClick={onUp}
        aria-label={t('Scroll up')}
        tabIndex={canUp ? 0 : -1}
        className={clsx(
          base,
          btn,
          topPos,
          hovered && canUp ? 'opacity-100 translate-y-0 pointer-events-auto' : 'opacity-0 -translate-y-1 pointer-events-none',
        )}
      >
        <ChevronUpIcon aria-hidden="true" className={chev} />
      </button>

      {/* Bottom: fade (scroll-position cue) + chevron (hover affordance) */}
      {fadeFrom && (
        <div
          aria-hidden="true"
          className={clsx(
            'pointer-events-none absolute inset-x-0 bottom-0 z-20 bg-gradient-to-t to-transparent transition-opacity duration-150',
            edge,
            fadeFrom,
            canDown ? 'opacity-90' : 'opacity-0',
          )}
        />
      )}
      <button
        type="button"
        onClick={onDown}
        aria-label={t('Scroll for more')}
        tabIndex={canDown ? 0 : -1}
        className={clsx(
          base,
          btn,
          botPos,
          hovered && canDown ? 'opacity-100 translate-y-0 pointer-events-auto' : 'opacity-0 translate-y-1 pointer-events-none',
        )}
      >
        <ChevronDownIcon aria-hidden="true" className={chev} />
      </button>
    </>
  );
};

export interface ScrollAreaProps extends React.HTMLAttributes<HTMLDivElement> {
  /** Classes for the OUTER positioned wrapper — sizing within the page (e.g. "flex-1"). */
  className?: string;
  /** Classes for the INNER scroll viewport — padding, bg, flex layout for children. */
  viewportClassName?: string;
  /** Background the edge fade blends to (the viewport's bg). Default: surface. */
  fade?: ScrollFade;
  /** Keep the native scrollbar (default false — the floating indicator replaces it). */
  showScrollbar?: boolean;
  children: React.ReactNode;
}

/**
 * ScrollArea — a drop-in scroll container with the floating scroll indicator baked
 * in. Replaces a raw `<div className="flex-1 overflow-auto">` page scroller:
 *
 *   <ScrollArea className="flex-1" viewportClassName="px-6 py-4"> …content… </ScrollArea>
 *
 * The outer wrapper is the flex/layout box (it gets `className`); the inner element
 * is the actual scroll viewport (it gets `viewportClassName` + any passed-through
 * props like `onScroll`). The viewport is `relative` so existing `absolute inset-0`
 * page backdrops (the dot grid) keep working unchanged.
 */
export const ScrollArea: React.FC<ScrollAreaProps> = ({
  className,
  viewportClassName,
  fade,
  showScrollbar = false,
  children,
  ...rest
}) => {
  const { scrollRef, canUp, canDown, scrollByPage } = useScrollHint<HTMLDivElement>();
  const [hovered, setHovered] = useState(false);
  return (
    <div
      // `min-w-0` is load-bearing: this wrapper is the flex item that sits next
      // to a fixed right rail (ToolRail) on every list page. Its overflow is
      // visible (the scroll lives on the inner viewport), so without min-w-0 a
      // flex item keeps its content-based `min-width: auto` and refuses to
      // shrink — wide rows then push the rail off-screen. The raw
      // `flex-1 overflow-auto` scroller this replaced got an automatic min-width
      // of 0 for free (overflow ≠ visible); we restore that here explicitly.
      className={clsx('relative flex min-h-0 min-w-0 flex-col', className)}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <div
        ref={scrollRef}
        className={clsx(
          'relative flex-1 overflow-auto',
          // The floating indicator is the affordance, so the native scrollbar is
          // hidden by default (set `showScrollbar` to keep it, e.g. wide data tables).
          !showScrollbar && '[scrollbar-width:none] [&::-webkit-scrollbar]:hidden',
          viewportClassName,
        )}
        {...rest}
      >
        {children}
      </div>
      <ScrollHintOverlay
        canUp={canUp}
        canDown={canDown}
        hovered={hovered}
        onUp={() => scrollByPage(-1)}
        onDown={() => scrollByPage(1)}
        fade={fade}
      />
    </div>
  );
};
