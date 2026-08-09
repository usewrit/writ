import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { XMarkIcon, ArrowLeftIcon, ArrowRightIcon, CheckIcon } from '@heroicons/react/24/outline';
import { isAnchorVisible, useTourAnchor } from './useTourAnchor';

export interface TourStep {
  id: string;
  /**
   * `data-tour` value of the element this step is about. Omit for a free-floating
   * card (the opening and closing steps, which describe the page as a whole).
   * A step whose anchor isn't painted when the tour starts is dropped — that's
   * how one step list covers the regular / streaming / narrow-stage variants of
   * a page without any branching.
   */
  anchor?: string;
  /** Already-translated heading. */
  title: string;
  /** Already-translated body copy. */
  body: string;
  /** Optional demo / illustration rendered under the body. */
  extra?: React.ReactNode;
  /** Widen the card (a step carrying a demo needs the room). */
  wide?: boolean;
}

interface GuidedTourProps {
  steps: TourStep[];
  open: boolean;
  /** `completed` is true when the user reached the end, false when they skipped. */
  onClose: (completed: boolean) => void;
}

const GAP = 14;
const MARGIN = 12;

/**
 * A stepped, spotlighted walkthrough for ONE dense screen.
 *
 * Deliberately distinct from the Surfacer (`onboarding/Surfacer`), which is the
 * ambient one-line hint primitive: it never dims, never blocks, and fires on its
 * own. A tour is the opposite contract — it is explicitly entered (first visit,
 * or the "How this page works" button), it takes over the screen so the user can
 * read, and it exits on Skip/Esc/Done. Use a hint for a single non-obvious
 * control; use a tour when the screen has several parts whose RELATIONSHIP is
 * the thing to teach.
 *
 * Motion rules follow the rest of the app: the spotlight glides on the expo
 * curve, the card never animates its position (it re-keys and fades in at its
 * final coordinates), and `prefers-reduced-motion` drops both.
 */
export const GuidedTour: React.FC<GuidedTourProps> = ({ steps, open, onClose }) => {
  const { t } = useTranslation();
  const [index, setIndex] = useState(0);
  // Steps are filtered ONCE, when the tour opens — the DOM is stable for the
  // life of a tour, and re-filtering mid-run would renumber the steps under the
  // user's feet. `steps` is rebuilt every render by the caller (it carries
  // translated copy), so the filter is driven by `open` flipping, never by the
  // array's identity.
  const [visibleSteps, setVisibleSteps] = useState<TourStep[]>([]);

  const reduce =
    typeof window !== 'undefined' &&
    (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false);

  // Opening/closing is an identity change, so the reset is adjusted DURING
  // render off the previous `open`. From an effect the list would be empty for
  // exactly one render after opening — which the "nothing to show" guard below
  // used to read as a finished tour, and which needed a second `prepared` flag
  // to paper over. Filtering in the same pass removes the transient entirely.
  const [wasOpen, setWasOpen] = useState(false);
  if (wasOpen !== open) {
    setWasOpen(open);
    setIndex(0);
    setVisibleSteps(open ? steps.filter((s) => !s.anchor || isAnchorVisible(s.anchor)) : []);
  }

  const total = visibleSteps.length;
  const step = visibleSteps[index];
  const isLast = index === total - 1;

  // Closing is entirely the parent's business: `index` resets itself when `open`
  // flips (above), so there is no local state left to unwind here.
  const finish = useCallback((completed: boolean) => {
    onClose(completed);
  }, [onClose]);

  const next = useCallback(() => {
    if (index >= total - 1) { finish(true); return; }
    setIndex(index + 1);
  }, [index, total, finish]);

  const back = useCallback(() => setIndex((i) => Math.max(0, i - 1)), []);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); finish(false); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); next(); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); back(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, next, back, finish]);

  // A tour that ends up with nothing to point at must not paint an empty scrim.
  // This one stays in an effect on purpose: it calls OUT to the parent, and a
  // parent may not be told to close mid-render.
  useEffect(() => {
    if (open && total === 0) finish(true);
  }, [open, total, finish]);

  if (!open || !step) return null;

  // NOT keyed on the step: the layer persists for the whole tour, so stepping
  // never unmounts the scrim (which would flash the un-dimmed page for a frame).
  return createPortal(
    <TourLayer
      step={step}
      index={index}
      total={total}
      isLast={isLast}
      reduce={reduce}
      onNext={next}
      onBack={back}
      onSkip={() => finish(false)}
      t={t}
    />,
    document.body,
  );
};

interface LayerProps {
  step: TourStep;
  index: number;
  total: number;
  isLast: boolean;
  reduce: boolean;
  onNext: () => void;
  onBack: () => void;
  onSkip: () => void;
  t: (k: string, o?: Record<string, unknown>) => string;
}

const TourLayer: React.FC<LayerProps> = ({ step, index, total, isLast, reduce, onNext, onBack, onSkip, t }) => {
  const { rect, stable } = useTourAnchor(step.anchor ?? null, true);
  const cardRef = useRef<HTMLDivElement>(null);
  const nextRef = useRef<HTMLButtonElement>(null);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);

  // Measure the card before placing it — a card placed from a guessed height
  // lands wrong and then jumps, which is exactly what the motion rules forbid.
  useLayoutEffect(() => {
    const el = cardRef.current;
    if (!el) return;
    const measure = () => setSize({ w: el.offsetWidth, h: el.offsetHeight });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [step.id]);

  useEffect(() => { nextRef.current?.focus(); }, [step.id]);

  const anchored = !!step.anchor;
  const ready = size !== null && (!anchored || stable);
  // The card reveals ONCE, against a settled anchor. After that it stays up and
  // simply re-tethers on each step — fading it out and back in per step would
  // read as a flicker, not as motion. The latch is flipped during render: from
  // an effect the reveal would land a commit after the anchor settled, i.e. one
  // frame further into the scroll it is supposed to arrive with.
  const [revealed, setRevealed] = useState(false);
  if (ready && !revealed) setRevealed(true);
  const visible = ready || revealed;

  const placement = useMemo(() => {
    const vw = typeof window !== 'undefined' ? window.innerWidth : 1280;
    const vh = typeof window !== 'undefined' ? window.innerHeight : 800;
    const w = size?.w ?? (step.wide ? 380 : 300);
    const h = size?.h ?? 200;

    if (!rect) {
      // Free-floating step — centred, slightly high so it reads as a title card.
      return { top: Math.max(MARGIN, vh / 2 - h / 2 - 24), left: Math.max(MARGIN, vw / 2 - w / 2) };
    }
    const left = Math.max(MARGIN, Math.min(rect.left, vw - w - MARGIN));
    const below = rect.top + rect.height + GAP;
    if (below + h <= vh - MARGIN) return { top: below, left };
    const above = rect.top - h - GAP;
    if (above >= MARGIN) return { top: above, left };
    // Neither side fits (a section taller than the viewport): pin to the bottom
    // so the card is always fully readable and the spotlight stays above it.
    return { top: Math.max(MARGIN, vh - h - MARGIN), left };
  }, [rect, size, step.wide]);

  const spotlight = rect
    ? {
        top: rect.top - 6,
        left: rect.left - 8,
        width: rect.width + 16,
        height: rect.height + 12,
      }
    : null;

  return (
    // The full-bleed layer itself swallows pointer events, so a stray click can
    // never fire the app control the tour is currently talking about (the
    // spotlight below is painted with a box-shadow, which is not hit-testable).
    // Advancing is always explicit.
    <div className="fixed inset-0 z-[200]">
      {/* Scrim + spotlight in ONE element: a huge outward box-shadow paints the
          dim everywhere EXCEPT the cut-out, so there is a single box to move
          instead of four edge panels to keep in sync.

          Its geometry is never transitioned. The rect is re-read every frame
          while the step's scroll-into-view settles, and a transition restarted
          on every frame does not glide — it smears and lags behind the element
          it is supposed to be glued to. The scroll IS the motion. */}
      {spotlight ? (
        <div
          aria-hidden
          className="absolute rounded-xl"
          style={{
            top: spotlight.top,
            left: spotlight.left,
            width: spotlight.width,
            height: spotlight.height,
            boxShadow: '0 0 0 9999px rgb(0 0 0 / 0.45)',
            outline: '2px solid rgb(255 255 255 / 0.85)',
            outlineOffset: '-1px',
          }}
        />
      ) : (
        /* No anchor → the scrim still has to cover the screen. */
        <div aria-hidden className="absolute inset-0 bg-black/45" />
      )}

      <div
        ref={cardRef}
        role="dialog"
        aria-modal="true"
        aria-label={step.title}
        className={clsx(
          'absolute bg-surface text-ink rounded-xl shadow-2xl border border-border-strong p-4',
          // 340, not 312: the footer's three actions are ~237px wide in French
          // (vs ~194 in English), and at 312 that left the progress track a
          // 29px stub. The extra 28px is the room the longest language needs.
          step.wide ? 'w-[400px]' : 'w-[340px]',
          'max-w-[calc(100vw-24px)]',
        )}
        style={{
          // top/left are set directly, never transitioned — the card is tethered
          // to the spotlight and has to arrive with it, not chase it.
          top: placement.top,
          left: placement.left,
          opacity: visible ? 1 : 0,
          transform: visible ? 'scale(1)' : 'scale(0.97)',
          transformOrigin: 'top left',
          pointerEvents: visible ? 'auto' : 'none',
          transition: reduce ? 'none' : 'opacity 190ms var(--ease-out), transform 190ms var(--ease-out)',
        }}
      >
        <button
          type="button"
          onClick={onSkip}
          aria-label={t('Skip the tour')}
          className="absolute top-2.5 right-2.5 p-1 rounded-md text-tertiary hover:text-ink hover:bg-hover transition-colors"
        >
          <XMarkIcon className="w-3.5 h-3.5" />
        </button>

        {/* Keyed on the step: the frame stays put and only its contents settle
            in, the same 160ms opacity swap the tab bodies use. Remounting also
            replays any demo the step carries when the user steps back to it. */}
        <div key={step.id} className={reduce ? undefined : 'animate-content-in'}>
          <p className="text-[10px] font-semibold uppercase tracking-wider text-tertiary">
            {t('Step {{n}} of {{total}}', { n: index + 1, total })}
          </p>
          <h3 className="text-[14px] font-semibold leading-snug text-ink mt-1 pr-6">{step.title}</h3>
          <p className="text-[12px] text-secondary leading-relaxed mt-1.5">{step.body}</p>

          {step.extra && <div className="mt-3">{step.extra}</div>}
        </div>

        {/* The footer is sized by the LONGEST language, not by English.
            "Ignorer / Retour / Suivant" is half again as wide as "Skip / Back /
            Next", and a per-step dot strip cannot give the difference back: its
            gaps don't shrink, so twelve dots hold ~44px of the row open no
            matter what — which pushed the primary button clean outside a 312px
            card in French (and already by 35px in English). So the three
            actions travel as ONE non-shrinking group, and the progress readout
            is a single track that flexes down to nothing. The authoritative
            count is the "Step n of N" line above; this is just the shape of it. */}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 mt-4">
          <div
            className="flex-1 min-w-0 h-1 rounded-full bg-border-strong overflow-hidden"
            role="progressbar"
            aria-valuemin={1}
            aria-valuemax={total}
            aria-valuenow={index + 1}
            aria-label={t('Step {{n}} of {{total}}', { n: index + 1, total })}
          >
            <div
              className="h-full rounded-full bg-ink"
              style={{
                width: `${((index + 1) / total) * 100}%`,
                transition: reduce ? 'none' : 'width 200ms var(--ease-out)',
              }}
            />
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={onSkip}
              className="px-2.5 py-1.5 text-[12px] font-medium whitespace-nowrap text-tertiary hover:text-ink rounded-lg hover:bg-hover transition-colors"
            >
              {t('Skip')}
            </button>
            {index > 0 && (
              <button
                type="button"
                onClick={onBack}
                className="flex items-center gap-1 px-2.5 py-1.5 text-[12px] font-medium whitespace-nowrap text-secondary hover:text-ink border border-border rounded-lg hover:bg-hover transition-colors"
              >
                <ArrowLeftIcon className="w-3 h-3 shrink-0" />
                {t('Back')}
              </button>
            )}
            <button
              ref={nextRef}
              type="button"
              onClick={onNext}
              className="flex items-center gap-1 px-3 py-1.5 text-[12px] font-semibold whitespace-nowrap bg-accent-strong text-accent-on rounded-lg shadow-sm hover:bg-accent-strong/90 transition-colors"
            >
              {isLast ? t('Done') : t('Next')}
              {isLast ? <CheckIcon className="w-3 h-3 shrink-0" /> : <ArrowRightIcon className="w-3 h-3 shrink-0" />}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default GuidedTour;
