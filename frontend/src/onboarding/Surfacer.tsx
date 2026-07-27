import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { XMarkIcon } from '@heroicons/react/24/outline';
import { hasSurfaced, isOnboardingActive, markSurfaced } from './storage';
import { SURFACE_EVENT } from './surfaceTrigger';
import { getHint } from './hints';
import { useStableAnchor } from './useStableAnchor';

interface SurfacerCtx {
  /** Request a hint to surface (no-op if already seen / onboarding inactive / one on screen). */
  surface: (id: string) => void;
}

const Ctx = createContext<SurfacerCtx>({ surface: () => {} });
export const useSurfacer = (): SurfacerCtx => useContext(Ctx);

const CARD_W = 268;
const GAP = 12;
const MARGIN = 10;

/**
 * The single first-run hint primitive. Replaces the old spotlight/coachmark
 * tour engine. It never dims the screen, never blocks the app, never animates a
 * layout property, and never reveals against an un-settled anchor — it fades in
 * (opacity + scale only) once the anchor rect is stable, self-dismisses, and is
 * remembered so it shows exactly once. One hint on screen at a time.
 */
export const SurfacerProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [activeId, setActiveId] = useState<string | null>(null);

  const surface = useCallback((id: string) => {
    if (!getHint(id)) return;
    if (!isOnboardingActive() || hasSurfaced(id)) return;
    // one-at-a-time: keep whatever is already showing.
    setActiveId((prev) => prev ?? id);
  }, []);

  useEffect(() => {
    const onReq = (e: Event) => {
      const id = (e as CustomEvent).detail?.id;
      if (typeof id === 'string') surface(id);
    };
    window.addEventListener(SURFACE_EVENT, onReq);
    return () => window.removeEventListener(SURFACE_EVENT, onReq);
  }, [surface]);

  const dismiss = useCallback(() => {
    setActiveId((cur) => {
      if (cur) markSurfaced(cur);
      return null;
    });
  }, []);

  return (
    <Ctx.Provider value={{ surface }}>
      {children}
      {activeId && <SurfacerCard key={activeId} hintId={activeId} onDismiss={dismiss} />}
    </Ctx.Provider>
  );
};

const SurfacerCard: React.FC<{ hintId: string; onDismiss: () => void }> = ({ hintId, onDismiss }) => {
  const { t } = useTranslation();
  const hint = getHint(hintId)!;
  const { rect, stable } = useStableAnchor(hint.anchor, true);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  const [shown, setShown] = useState(false);
  const reduce =
    typeof window !== 'undefined' &&
    (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false);

  // The card's own size feeds the top/left it renders at, so it has to be a
  // rendered value — measuring it by reading a ref while positioning would be a
  // ref read in the render path. A callback ref measures at COMMIT instead and
  // commits the size to state; it fires once per mount (it is stable), and the
  // guard keeps an identical re-measure from scheduling a render.
  const measureRef = useCallback((el: HTMLDivElement | null) => {
    if (!el) return;
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    setSize((prev) => (prev && prev.w === w && prev.h === h ? prev : { w, h }));
  }, []);

  // Reveal ONLY once the anchor rect is stable — never before, and only once:
  // `stable` drops back to false every time the anchor moves again, and the card
  // must not fade out from under the user. The latch lands on the NEXT frame, so
  // the card has already been painted at opacity 0 in its measured position
  // (a transition needs that first paint to animate from).
  useEffect(() => {
    if (!stable || shown) return;
    const raf = requestAnimationFrame(() => setShown(true));
    return () => cancelAnimationFrame(raf);
  }, [stable, shown]);

  // Esc dismisses.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onDismiss();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onDismiss]);

  // Anchor removed after we revealed → nothing to point at, so dismiss.
  useEffect(() => {
    if (shown && !rect) onDismiss();
  }, [shown, rect, onDismiss]);

  if (!rect) return null;

  const cw = size?.w || CARD_W;
  const ch = size?.h || 100;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  // The anchor is a zero-height marker at the TOP of the target (the endpoint /
  // the Repairs header). Prefer a callout just ABOVE it; drop just below only if
  // there's no room. Never sit below a whole tall panel (that floats it into
  // empty space).
  let top = rect.top - ch - GAP;
  if (top < MARGIN) top = rect.top + rect.height + GAP;
  top = Math.min(Math.max(top, MARGIN), vh - ch - MARGIN);
  const left = Math.max(MARGIN, Math.min(rect.left, vw - cw - MARGIN));

  return createPortal(
    <div
      ref={measureRef}
      role="dialog"
      aria-live="polite"
      aria-label={t(hint.title)}
      className="fixed z-[80] w-[268px] max-w-[calc(100vw-20px)] bg-ink text-white rounded-xl shadow-lg p-3.5"
      style={{
        // top/left are set directly (never transitioned) so the card tethers to
        // the anchor; only opacity + transform are animated, and only on reveal.
        top,
        left,
        transformOrigin: 'top left',
        opacity: shown ? 1 : 0,
        transform: shown ? 'scale(1)' : 'scale(0.97)',
        pointerEvents: shown ? 'auto' : 'none',
        transition: reduce ? 'none' : 'opacity 190ms var(--ease-out), transform 190ms var(--ease-out)',
      }}
    >
      <h3 className="text-[13px] font-semibold leading-snug pr-5">{t(hint.title)}</h3>
      <p className="text-[12px] text-white/70 leading-relaxed mt-1">{t(hint.body)}</p>
      <div className="flex items-center justify-between mt-3">
        <span className="text-[11px] text-white/45">{t('Shown once')}</span>
        <button
          type="button"
          onClick={onDismiss}
          className="text-[12px] font-medium bg-white/15 hover:bg-white/25 transition rounded-lg px-3 py-1.5"
        >
          {t('Got it')}
        </button>
      </div>
      <button
        type="button"
        onClick={onDismiss}
        aria-label={t('Dismiss')}
        className="absolute top-2 right-2 p-1 rounded-md text-white/50 hover:text-white hover:bg-white/15 transition"
      >
        <XMarkIcon className="w-3.5 h-3.5" />
      </button>
    </div>,
    document.body,
  );
};
