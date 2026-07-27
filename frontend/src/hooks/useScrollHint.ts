import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * useScrollHint — drives the floating "more above / more below" scroll affordance.
 *
 * Attach `scrollRef` to a scroll container; the hook reports whether the content
 * is scrollable up (`canUp`) / down (`canDown`) and offers `scrollByPage` to nudge
 * it a viewport at a time. It re-measures on scroll, on viewport resize, AND on
 * content mutation (rows loading in, tabs switching, etc.) so the affordance stays
 * accurate without the caller wiring observers itself.
 *
 * Generic over the element so it works for a <main> landmark as well as a <div>.
 */
export function useScrollHint<T extends HTMLElement = HTMLDivElement>() {
  const scrollRef = useRef<T | null>(null);
  const [state, setState] = useState<{ canUp: boolean; canDown: boolean }>({ canUp: false, canDown: false });
  const rafRef = useRef<number | null>(null);

  const measure = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    // 4px slack so a sub-pixel layout never flickers the affordance on/off.
    const canUp = el.scrollTop > 4;
    const canDown = el.scrollTop + el.clientHeight < el.scrollHeight - 4;
    setState(prev => (prev.canUp === canUp && prev.canDown === canDown ? prev : { canUp, canDown }));
  }, []);

  // Coalesce bursts (scroll + mutation + resize can all fire in one frame) into a
  // single rAF measure, so a long list loading doesn't thrash layout reads.
  const schedule = useCallback(() => {
    if (rafRef.current != null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      measure();
    });
  }, [measure]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    measure();
    el.addEventListener('scroll', schedule, { passive: true });
    const ro = new ResizeObserver(schedule);
    ro.observe(el);
    const mo = new MutationObserver(schedule);
    mo.observe(el, { childList: true, subtree: true, characterData: true });
    window.addEventListener('resize', schedule);
    return () => {
      el.removeEventListener('scroll', schedule);
      ro.disconnect();
      mo.disconnect();
      window.removeEventListener('resize', schedule);
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    };
  }, [measure, schedule]);

  const scrollByPage = useCallback((dir: 1 | -1) => {
    const el = scrollRef.current;
    if (!el) return;
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    el.scrollBy({ top: dir * Math.max(160, el.clientHeight * 0.85), behavior: reduced ? 'auto' : 'smooth' });
  }, []);

  return { scrollRef, canUp: state.canUp, canDown: state.canDown, measure, scrollByPage };
}
