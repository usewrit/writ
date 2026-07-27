import { useEffect, useState } from 'react';

export interface AnchorRect {
  top: number;
  left: number;
  width: number;
  height: number;
}

const eq = (a: AnchorRect | null, b: AnchorRect | null): boolean =>
  !!a && !!b && a.top === b.top && a.left === b.left && a.width === b.width && a.height === b.height;

/**
 * Resolve a `data-surface` key to a live element and track its viewport rect —
 * but only report `stable: true` once the rect has held still for a few frames
 * AND the element is on-screen. The card must not reveal against an un-settled
 * rect (scroll-into-view + entrance animations move it for a beat), which was
 * the old engine's "appear then jump". If the anchor is off-screen we scroll it
 * into view once, then wait for it to settle.
 */
export function useStableAnchor(
  anchorKey: string | null,
  active: boolean,
): { rect: AnchorRect | null; stable: boolean } {
  const [rect, setRect] = useState<AnchorRect | null>(null);
  const [stable, setStable] = useState(false);
  // Resolving the anchor is a plain DOM read, so it happens DURING render
  // rather than from an effect — switching anchor (or dropping out of `active`)
  // drops the old element in the same pass, instead of leaving the card
  // measured against it for a commit. The counter's VALUE is never read;
  // bumping it from the observer below is simply how a late-mounting anchor
  // asks for the read to be redone.
  const [, retryLookup] = useState(0);

  const target = active ? anchorKey : null;
  const element = target ? document.querySelector<HTMLElement>(`[data-surface="${target}"]`) : null;

  // ── Retry via MutationObserver — the anchor may mount late ──
  useEffect(() => {
    if (!target || element) return;
    const selector = `[data-surface="${target}"]`;
    const observer = new MutationObserver(() => {
      if (document.querySelector(selector)) {
        observer.disconnect();
        retryLookup((n) => n + 1);
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [target, element]);

  // ── Track the rect + compute stability ──
  useEffect(() => {
    if (!element) return;
    let raf = 0;
    let last: AnchorRect | null = null;
    let still = 0;
    let scrolled = false;
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;

    const tick = () => {
      if (!element.isConnected) {
        setStable(false);
        setRect(null);
        // Forget the last measurement too, or a re-attached element whose rect
        // happens to be unchanged would never re-commit past the guard below.
        last = null;
        still = 0;
        raf = requestAnimationFrame(tick);
        return;
      }
      const r = element.getBoundingClientRect();
      const next: AnchorRect = { top: r.top, left: r.left, width: r.width, height: r.height };
      if (!scrolled && (r.top < 0 || r.bottom > window.innerHeight)) {
        scrolled = true;
        try {
          element.scrollIntoView({ block: 'center', behavior: reduce ? 'auto' : 'smooth' });
        } catch {
          element.scrollIntoView();
        }
      }
      const same = eq(last, next);
      still = same ? still + 1 : 0;
      last = next;
      // Commit ONLY on change — `next` is a fresh object every frame, so an
      // unconditional setState here would re-render the card 60× a second for
      // as long as the hint is up.
      if (!same) setRect(next);
      const onScreen = next.width > 0 && next.top >= 0 && next.top <= window.innerHeight - 8;
      setStable((prev) => {
        const now = still >= 3 && onScreen;
        return prev === now ? prev : now;
      });
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [element]);

  // `rect`/`stable` are written only from the tick above, so they outlive the
  // element they were measured on. Gating the RESULT on a live element is what
  // the old reset-in-an-effect did, one commit earlier and without the extra
  // render pass.
  return element ? { rect, stable } : { rect: null, stable: false };
}
