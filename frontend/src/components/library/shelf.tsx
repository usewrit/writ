import React from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { MagnifyingGlassIcon } from '@heroicons/react/24/outline';

/**
 * Shelf-pattern primitives — shared building blocks for the admin master–detail
 * list pages (Workflows / Monitors / Personas / …). Mirrors
 * `the desktop app’s shelf component` but LIGHT-ONLY (this app has
 * no dark mode), so there are no `dark:` variants.
 *
 * Tone model: `chrome` (#EDEBE8, the tone the sidebar already paints with) = the
 * nav/frame that recedes (sidebar + master-list column); `surface` (white) =
 * content (detail pane). The selected row borrows `surface` and butts flush into
 * the pane so the two read as one continuous surface.
 */

/** Outer two-pane container (chrome frame). */
export const SHELF_CONTAINER = 'flex flex-1 min-w-0 min-h-0 bg-chrome';
/** Master-list column — joins the sidebar's chrome tone (no divider; the
 *  content-card border in Layout carries the seam, following the card shape). */
export const SHELF_LIST_COL = 'shelf-list-col flex flex-col min-h-0 w-full @split/stage:w-[var(--shelf-list-width,340px)] shrink-0 bg-chrome';

// One delegated splitter powers every shelf list without making each page own
// drag state. Width is shared and persisted so Workflows, Outputs, Monitors,
// Personas, Secrets, API keys, and Automations keep a consistent list size.
if (typeof window !== 'undefined' && !(window as any).__writShelfResizeInstalled) {
  (window as any).__writShelfResizeInstalled = true;
  const saved = window.localStorage.getItem('writ:shelf-list-width');
  if (saved) document.documentElement.style.setProperty('--shelf-list-width', saved + 'px');
  document.addEventListener('pointerdown', (event) => {
    const target = event.target as HTMLElement | null;
    const pane = target?.closest?.('.shelf-list-col') as HTMLElement | null;
    if (!pane) return;
    const rect = pane.getBoundingClientRect();
    // Measure against the shelf itself, not the window: a narrower CONTAINER
    // (not just a narrower window) drops the list to full width with no seam to
    // drag, so gate on the pane's actual width rather than a viewport mediaquery.
    const shelf = pane.parentElement?.getBoundingClientRect();
    if (!shelf || rect.width >= shelf.width - 1) return;
    if (Math.abs(event.clientX - rect.right) > 8) return;
    event.preventDefault();
    if (event.detail > 1) {
      document.documentElement.style.removeProperty('--shelf-list-width');
      window.localStorage.removeItem('writ:shelf-list-width');
      pane.style.removeProperty('width');
      return;
    }
    const startX = event.clientX;
    const startWidth = rect.width;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    const move = (e: PointerEvent) => {
      const max = Math.min(560, shelf.width * 0.5);
      const width = Math.round(Math.max(260, Math.min(max, startWidth + e.clientX - startX)));
      document.documentElement.style.setProperty('--shelf-list-width', width + 'px');
      window.localStorage.setItem('writ:shelf-list-width', String(width));
    };
    const end = () => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', end);
      document.removeEventListener('pointercancel', end);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', end);
    document.addEventListener('pointercancel', end);
  });
}
/** Detail-pane column — the bright content surface. */
export const SHELF_DETAIL_COL = 'hidden @split/stage:flex flex-1 min-w-0 bg-surface';

/**
 * Row container className. The SELECTED row is a full-bleed `surface` slab
 * (`-mx-2` cancels the ScrollArea's `px-2` so it spans the column edge-to-edge
 * and butts flush into the pane); `pl-5 pr-4` keeps content aligned with the
 * `pl-3 pr-2` resting rows so nothing shifts on select. The focus ring is INSET
 * so it never bleeds past the slab edges into the sidebar / pane.
 */
export const shelfRowClass = (selected: boolean) =>
  clsx(
    'group relative flex items-center gap-3 py-2 cursor-pointer transition-colors min-w-0',
    'outline-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ink/30',
    selected
      ? 'bg-surface -mx-2 pl-5 pr-4 rounded-none'
      : 'pl-3 pr-2 rounded-lg hover:bg-chrome',
  );

/**
 * Row `onMouseDown` handler — suppresses focus-on-click for MOUSE users so the
 * keyboard focus ring (`:focus-visible`) never shows on click. Keyboard Tab still
 * focuses + rings. Skips inner controls so their native click behaviour is
 * untouched.
 */
export const shelfRowMouseDown = (e: React.MouseEvent) => {
  if (!(e.target as HTMLElement).closest('button')) {
    e.preventDefault();
    // Selecting via mouse: drop focus from whatever held it (often a control on
    // another row) so its :focus-visible ring doesn't linger on the WRONG item.
    const active = document.activeElement as HTMLElement | null;
    if (active && active !== document.body) active.blur();
  }
};

/** Selected-row left accent bar. Render inside the `relative` row when selected.
 *  `animate-accent-in` grows it out of the row edge instead of popping it in a
 *  frame after the selected-slab background has eased. */
export const ShelfAccentBar: React.FC = () =>
  React.createElement('span', {
    className: 'absolute left-0 top-0 bottom-0 w-[3px] rounded-r-full bg-accent animate-accent-in',
    'aria-hidden': 'true',
  });

/**
 * Filter-chip className. Active is a SOFT ink-tint chip (a selected STATE, not an
 * action — so it doesn't compete with solid-ink primary buttons); inactive is a
 * surface bordered chip so it reads against the chrome list header. Border on
 * BOTH keeps the width stable when toggling. Wrap chips in
 * `flex flex-wrap items-center gap-1` so they wrap rather than horizontally
 * scroll.
 */
export const shelfFilterChipClass = (active: boolean) =>
  clsx(
    'inline-flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] font-medium whitespace-nowrap border transition-colors',
    'outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
    active
      // `accent-strong` on the label: it carries type, so it needs the AA step.
      // A tint, never a solid fill — selection is a state, not an action, so it
      // must not compete with a primary button.
      ? 'bg-accent/10 border-accent/40 text-accent-strong'
      : 'bg-surface border-border text-secondary hover:text-accent-strong hover:border-accent/40',
  );

/** Count-pill color inside a filter chip. */
export const shelfFilterCountClass = (active: boolean) =>
  clsx('tabular-nums', active ? 'text-accent-strong/70' : 'text-tertiary');

/**
 * Search input at the TOP of the master-list column — it lives with the list it
 * filters, not in the page topbar. Full-width and `surface`-carded so it reads
 * on the chrome list header (a `bg-canvas` fill is ~invisible there).
 */
export const ShelfListSearch: React.FC<{
  value: string;
  onChange: (v: string) => void;
  /** Accessible name, e.g. t('Search workflows'). Falls back to the placeholder. */
  ariaLabel?: string;
  placeholder?: string;
}> = ({ value, onChange, ariaLabel, placeholder }) => {
  const { t } = useTranslation();
  const hint = placeholder ?? t('Search...');
  return (
    <div className="relative">
      <MagnifyingGlassIcon aria-hidden="true" className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-tertiary" />
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={hint}
        aria-label={ariaLabel ?? hint}
        className="w-full pl-8 pr-3 py-1.5 text-[12px] bg-surface border border-border rounded-lg outline-none focus:ring-2 focus:ring-ink/10 transition-colors placeholder:text-tertiary"
      />
    </div>
  );
};

// ── Topbar (per-page inline toolbar) ─────────────────────────────────────────
// Admin has no shared Layout header (unlike desktop), so each list page renders
// its own compact `h-12` toolbar. These give it the shelf's chrome frame tone +
// carded controls, matching the sidebar + list column so the nav frame reads as
// one. Same token collision as the list header: on `chrome`, `bg-hover`/`bg-canvas`
// fills are ~invisible, so controls must be carded with a `surface` fill.

/** The page toolbar container — chrome tone, matching the sidebar + shelf list. */
export const SHELF_TOPBAR = 'flex items-center gap-3 h-12 px-4 sm:px-6 bg-chrome chrome-topbar border-b border-border shrink-0';

/**
 * A segmented tab in the topbar (e.g. Recorded / AI / Devices). Active is a raised
 * white pill — a control CARDED onto the chrome toolbar (`bg-hover` would be
 * ~invisible on chrome). Inactive stays weightless text with a translucent-white
 * hover so it still registers on chrome.
 */
export const shelfTabClass = (active: boolean) =>
  clsx(
    'inline-flex items-center gap-1.5 px-2.5 py-1 text-[12px] font-medium rounded-md transition-colors',
    'outline-none focus-visible:ring-2 focus-visible:ring-ink/40',
    active ? 'bg-surface text-ink shadow-sm' : 'text-tertiary hover:text-secondary hover:bg-surface/60',
  );

/**
 * Search input on the chrome topbar — white fill + full border + focus ring so it
 * reads as an input (a `bg-canvas` fill is ~invisible on chrome). Pages add their
 * own width (e.g. `w-44`) and the leading `pl-8` is included for the search icon.
 */
export const SHELF_SEARCH_INPUT =
  'pl-8 pr-3 py-1.5 text-[12px] bg-surface border border-border rounded-lg outline-none focus:ring-2 focus:ring-ink/10 transition-colors placeholder:text-tertiary';

/**
 * A bordered secondary button on the chrome topbar (e.g. Collections). `hover:bg-hover`
 * is invisible on chrome, so hover lifts to a translucent white instead.
 */
export const SHELF_TOPBAR_BTN =
  'inline-flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium text-secondary rounded-lg border border-border hover:bg-surface/60 hover:text-ink outline-none focus-visible:ring-2 focus-visible:ring-ink/40 transition-colors shrink-0';

/**
 * Full-height loading skeleton for the two-pane shelf list pages (Monitors /
 * Personas / Workflows / API Keys / Secrets / Automations / …). Paints the real
 * page shape immediately — a `chrome`-toned master column with a header stub +
 * pulsing rows, and a soft `surface` detail-pane skeleton — so the first fetch
 * never flashes a blank white pane or shifts the layout when data lands.
 *
 * This is the ONE place the loading treatment lives; per-page inline skeletons
 * and bare `animate-spin` / tiny-dot loaders should route through here so every
 * list loads the same way. Deterministic row widths (indexed, no RNG) keep the
 * skeleton stable across renders.
 */
export const ShelfSkeleton: React.FC<{
  /** Placeholder rows in the master column. */
  rows?: number;
  /** Accessible busy label, e.g. t('Loading monitors'). */
  label?: string;
  /** Reserve the search-input slot in the header stub (pages that show a search). */
  withSearch?: boolean;
}> = ({ rows = 7, label, withSearch = false }) => {
  const { t } = useTranslation();
  return (
    <div className={SHELF_CONTAINER} aria-busy="true" aria-label={label ?? t('Loading…')}>
      {/* ── Master list ── */}
      <div className={SHELF_LIST_COL}>
        {/* Header stub — matches the filter-chip + sort bar so rows don't jump. */}
        <div className="shrink-0 border-b border-border px-3 py-2.5 space-y-2">
          {withSearch && <div className="h-7 w-full rounded-lg bg-hover animate-pulse" />}
          <div className="flex flex-wrap items-center gap-1">
            <div className="h-6 w-16 rounded-lg bg-hover animate-pulse" />
            <div className="h-6 w-14 rounded-lg bg-hover animate-pulse" />
            <div className="h-6 w-12 rounded-lg bg-hover animate-pulse" />
          </div>
          <div className="flex items-center justify-between">
            <div className="h-3 w-14 rounded bg-hover animate-pulse" />
            <div className="h-6 w-28 rounded-lg bg-hover animate-pulse" />
          </div>
        </div>
        {/* Rows */}
        <div className="flex-1 min-h-0 overflow-hidden px-2 py-2 space-y-0.5">
          {Array.from({ length: rows }).map((_, i) => (
            <div key={i} className="flex items-center gap-3 px-2.5 py-2">
              <div className="w-8 h-8 rounded-lg bg-hover animate-pulse shrink-0" />
              <div className="flex-1 space-y-1.5">
                <div className="h-3 rounded bg-hover animate-pulse" style={{ width: `${52 + ((i * 13) % 34)}%` }} />
                <div className="h-2 w-1/3 rounded bg-hover animate-pulse" />
              </div>
            </div>
          ))}
        </div>
      </div>
      {/* ── Detail pane — a soft skeleton, never a blank white void ── */}
      <div className={clsx(SHELF_DETAIL_COL, 'flex-col gap-4 p-6')}>
        <div className="h-6 w-2/5 rounded bg-hover animate-pulse" />
        <div className="h-4 w-1/2 rounded bg-hover animate-pulse" />
        <div className="h-32 w-full rounded-xl bg-hover/60 animate-pulse mt-1" />
        <div className="h-4 w-2/3 rounded bg-hover animate-pulse" />
        <div className="h-4 w-1/2 rounded bg-hover animate-pulse" />
      </div>
    </div>
  );
};

/**
 * Single-column / full-width loading skeleton — pulsing carded rows for list and
 * table pages that aren't two-pane shelves (run feeds, notification lists). Drop
 * it inside the page's existing scroll body.
 */
export const RowsSkeleton: React.FC<{
  rows?: number;
  label?: string;
  className?: string;
}> = ({ rows = 6, label, className }) => {
  const { t } = useTranslation();
  return (
    <div className={clsx('space-y-2', className)} aria-busy="true" aria-label={label ?? t('Loading…')}>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex items-center gap-3 rounded-xl border border-ink/20 bg-surface px-4 py-3.5">
          <div className="w-8 h-8 rounded-lg bg-hover animate-pulse shrink-0" />
          <div className="flex-1 space-y-1.5">
            <div className="h-3 rounded bg-hover animate-pulse" style={{ width: `${48 + ((i * 11) % 36)}%` }} />
            <div className="h-2 w-1/4 rounded bg-hover animate-pulse" />
          </div>
          <div className="h-6 w-16 rounded-lg bg-hover animate-pulse shrink-0 hidden @pair/stage:block" />
        </div>
      ))}
    </div>
  );
};
