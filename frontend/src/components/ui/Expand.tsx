import React from 'react';
import clsx from 'clsx';

/**
 * Expand — smoothly animates content open/closed by HEIGHT using the CSS grid
 * `0fr → 1fr` trick: the outer grid row grows from 0 to the content's AUTO
 * height (no JS measuring, so it handles dynamic content for free), while the
 * inner wrapper clips the overflow and cross-fades. This is the exact technique
 * WorkflowList's expanded rows already shipped — lifted into one primitive so
 * every accordion / collapsible / expandable row in the app animates the same
 * way instead of half of them snapping open.
 *
 * Children stay mounted while closed so the COLLAPSE has something to animate;
 * pass `mountOnEnter` to defer first mount until the first open (a heavy body —
 * a data table, a script editor — stays cheap until the section is opened once).
 * While closed the body is `inert` (a no-op on engines that lack it, so it just
 * degrades) which drops its controls out of the tab order and the a11y tree.
 * The global `prefers-reduced-motion` rule flattens the timing to ~instant.
 */
export const Expand: React.FC<{
  open: boolean;
  children: React.ReactNode;
  /** Defer mounting children until the first open (heavy bodies). */
  mountOnEnter?: boolean;
  /**
   * Animate the FIRST mount too. For bodies that conditionally MOUNT already
   * open (an expanded table `<tr>`, `{isOpen && …}` blocks) the wrapper has no
   * closed state to transition from, so it would pop to full height; `appear`
   * renders the first frame closed and flips open on the next, so the body
   * GROWS in. (The collapse side of such sites unmounts instantly — that stays
   * abrupt by design; attention has already moved on dismissal.)
   */
  appear?: boolean;
  /** Classes for the inner content wrapper (padding, borders, spacing). */
  className?: string;
  /** Open/close duration in ms (default 260). */
  durationMs?: number;
}> = ({ open, children, mountOnEnter = false, appear = false, className, durationMs = 260 }) => {
  const [everOpened, setEverOpened] = React.useState(open);
  const [appeared, setAppeared] = React.useState(!appear);
  const innerRef = React.useRef<HTMLDivElement>(null);

  // Latch the first open DURING render (not from an effect): `everOpened` gates
  // whether `mountOnEnter` children exist at all, and an effect would mount them
  // one commit after the row started growing — the body would slide open empty
  // and then pop its content in.
  if (open && !everOpened) setEverOpened(true);

  // One frame after mount, release the `appear` gate so the 0fr→1fr transition
  // plays (same rAF idiom as Select's enter).
  React.useEffect(() => {
    if (appeared) return;
    const raf = requestAnimationFrame(() => setAppeared(true));
    return () => cancelAnimationFrame(raf);
  }, [appeared]);

  const effOpen = open && appeared;

  // Keep the collapsed body out of the tab order / a11y tree. `inert` is set
  // imperatively (so we don't fight React 18's prop typing) and is simply
  // ignored on engines that don't support it.
  React.useEffect(() => {
    const el = innerRef.current;
    if (el) (el as unknown as { inert: boolean }).inert = !effOpen;
  }, [effOpen]);

  return (
    <div
      className="grid"
      style={{
        gridTemplateRows: effOpen ? '1fr' : '0fr',
        transition: `grid-template-rows ${durationMs}ms var(--ease-out)`,
      }}
    >
      <div
        ref={innerRef}
        className="min-h-0 overflow-hidden"
        style={{
          opacity: effOpen ? 1 : 0,
          transition: `opacity ${Math.round(durationMs * 0.85)}ms var(--ease-out)`,
        }}
      >
        <div className={clsx(className)}>
          {mountOnEnter ? (everOpened ? children : null) : children}
        </div>
      </div>
    </div>
  );
};

export default Expand;
