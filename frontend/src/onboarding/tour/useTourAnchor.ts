import { useEffect, useState } from 'react';

export interface AnchorRect {
  top: number;
  left: number;
  width: number;
  height: number;
}

const eq = (a: AnchorRect | null, b: AnchorRect | null): boolean =>
  !!a && !!b && a.top === b.top && a.left === b.left && a.width === b.width && a.height === b.height;

/** The selector a tour step's `anchor` resolves to. */
export const tourSelector = (anchor: string): string => `[data-tour="${anchor}"]`;

/**
 * True when the anchor exists AND is actually painted. Elements hidden by a
 * container query (`hidden @rail/stage:flex` — the at-a-glance rail on a narrow
 * stage) stay in the DOM with a zero-size rect, so `querySelector` alone would
 * happily spotlight nothing. Steps are filtered through this at tour start.
 */
export function isAnchorVisible(anchor: string): boolean {
  const el = document.querySelector<HTMLElement>(tourSelector(anchor));
  if (!el) return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}

/**
 * Track a tour anchor's viewport rect, reporting `stable: true` only once the
 * rect has held still for a few frames.
 *
 * Same contract as the Surfacer's `useStableAnchor` (never reveal against an
 * un-settled rect — scroll-into-view and entrance animations move it for a
 * beat), with two differences a stepped tour needs: it resolves `data-tour`
 * rather than `data-surface`, and it re-scrolls on every step change instead of
 * once per element, because the next step is usually below the fold.
 */
export function useTourAnchor(
  anchor: string | null,
  active: boolean,
): { rect: AnchorRect | null; stable: boolean } {
  const [rect, setRect] = useState<AnchorRect | null>(null);
  const [stable, setStable] = useState(false);
  // Resolving the anchor is a plain DOM read, so it happens DURING render
  // rather than from an effect: stepping swaps the element in the same pass the
  // anchor changes, instead of leaving the previous step's element live for a
  // commit. The counter's VALUE is never read — bumping it from the observer
  // below is simply how a late-mounting anchor asks for the read to be redone.
  const [, retryLookup] = useState(0);

  const target = active ? anchor : null;
  const element = target ? document.querySelector<HTMLElement>(tourSelector(target)) : null;

  // ── Retry via MutationObserver — a step may open a collapsed section, so its
  //    anchor can mount a frame late ──
  useEffect(() => {
    if (!target || element) return;
    const selector = tourSelector(target);
    const observer = new MutationObserver(() => {
      if (document.querySelector(selector)) {
        observer.disconnect();
        retryLookup((n) => n + 1);
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [target, element]);

  // ── Bring it into view, then track the rect + compute stability ──
  useEffect(() => {
    if (!element) return;
    let raf = 0;
    let last: AnchorRect | null = null;
    let still = 0;
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;

    // Always centre the step's target: unlike a one-off hint, a tour walks DOWN
    // a long page, so every step after the first starts off-screen.
    try {
      element.scrollIntoView({ block: 'center', behavior: reduce ? 'auto' : 'smooth' });
    } catch {
      // Older engines reject the options object; jsdom has no scrollIntoView at
      // all. Neither is a reason to take the tour down.
      try { element.scrollIntoView(); } catch { /* not scrollable here */ }
    }

    const tick = () => {
      if (!element.isConnected) {
        setStable(false);
        setRect(null);
        // Forget the last measurement too, or a re-attached element whose rect
        // happens to be unchanged would never re-commit past the `!same` guard.
        last = null;
        still = 0;
        raf = requestAnimationFrame(tick);
        return;
      }
      const r = element.getBoundingClientRect();
      const next: AnchorRect = { top: r.top, left: r.left, width: r.width, height: r.height };
      const same = eq(last, next);
      still = same ? still + 1 : 0;
      last = next;
      // Commit ONLY on change. A per-frame setState would re-render the whole
      // tour layer — demo animation included — 60× a second for the entire life
      // of the step.
      if (!same) setRect(next);
      // A tall section can overflow the viewport — only part of it has to be on
      // screen for the spotlight and card to make sense.
      const onScreen = next.width > 0 && next.top < window.innerHeight - 8 && next.top + next.height > 8;
      setStable((prev) => {
        const now = still >= 3 && onScreen;
        return prev === now ? prev : now;
      });
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [element]);

  // `rect`/`stable` are written only from the tick above, so they survive a step
  // change until the next step's first measurement lands. That is deliberate and
  // is what the old reset-from-an-effect also did: the spotlight stays glued to
  // the step you are leaving until the new rect is READ, instead of blinking the
  // cut-out off for a frame. Gate on the target so a step with no anchor (the
  // opening / closing cards) reports nothing at all, immediately.
  return target ? { rect, stable } : { rect: null, stable: false };
}
