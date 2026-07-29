import React, { useId } from 'react';

/**
 * Scribe's mascot — "the Signal".
 *
 * The brand's Signal bar (the same slab that completes the `t` in WritGlyph)
 * given a pair of eyes. Deliberately NOT an illustration: it is the mark the
 * product already owns, so it stays legible at the 14px the sidebar renders it
 * at and still reads as a character at 48px in the panel's empty state.
 *
 * Drop-in for the heroicon it replaces: no width/height attributes, so the
 * caller's Tailwind `w-*`/`h-*` classes size it exactly as they sized
 * `SparklesIcon`. By default the body does NOT read currentColor — it is the
 * Signal (`--writ-signal`, matching WritGlyph's bar), because a gray or
 * inherited-ink mascot is not the mascot. Callers therefore drop the
 * `text-accent` they used to need. The eyes are punched out of the body, so
 * they take the colour of whatever it stands on and stay legible in both
 * themes without the component knowing which one is active.
 *
 * The eyes are alive: they glance around and blink. Motion lives in the global
 * stylesheet (`.scribe-mark` block), which is also where the
 * prefers-reduced-motion opt-out sits. `animate="hover"` holds it still until
 * the row it sits in is hovered; `tone="current"` drops the red and lets it go
 * monochrome with the surrounding type.
 */

/**
 * The viewBox HUGS the slab (60×86) rather than sitting in the square 0 0 100
 * 100 the other brand marks use. A square box would letterbox this shape: in a
 * `w-4 h-4` slot the mascot would scale to fit the 16px width and come out ~9px
 * tall, visibly punier than the sparkle it replaces. Hugged, it stands the full
 * height of the slot and is simply narrower — which is what a bar should be.
 */
const BODY =
  'M21 0 H39 A21 21 0 0 1 60 21 V65 A21 21 0 0 1 39 86 H21 A21 21 0 0 1 0 65 V21 A21 21 0 0 1 21 0 Z';

/**
 * Two or three mascots share most screens (home bar + sidebar + header). Left
 * alone they would blink in perfect unison, which reads as a rendering glitch
 * rather than as characters. A deterministic per-instance hash of React's
 * `useId` offsets each one; the delays are NEGATIVE so a freshly mounted
 * mascot starts mid-cycle instead of sitting frozen for four seconds.
 */
function staggerFor(id: string): { blink: string; glance: string } {
  let h = 0;
  for (let i = 0; i < id.length; i += 1) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return {
    blink: `-${((h % 47) / 10).toFixed(1)}s`,
    glance: `-${(((h >>> 5) % 73) / 10).toFixed(1)}s`,
  };
}

const EYES: Array<{ cx: number; cy: number }> = [
  { cx: 17, cy: 34 },
  { cx: 43, cy: 34 },
];

export interface ScribeMarkProps {
  className?: string;
  /**
   * `signal` (default) — the body is the brand red.
   * `current` — the body reads `currentColor` and goes monochrome with the type
   * around it (ink on light, paper on dark). Use it where the mascot is a nav
   * affordance rather than a brand moment and a permanent spot of red would
   * out-shout its neighbours. Either way the eyes are knocked out of the body.
   */
  tone?: 'signal' | 'current';
  /**
   * `always` (default) — idles wherever it is mounted.
   * `hover` — dead still until an ancestor `.group` is hovered or focused, then
   * it wakes. Requires the `group` class on that ancestor.
   * `never` — a still frame; no keyframes attached at all.
   */
  animate?: 'always' | 'hover' | 'never';
  /**
   * Accessible name. Omit on decorative uses (the label is almost always in the
   * adjacent text) — the mark is then `aria-hidden`, as the sparkle was.
   */
  title?: string;
}

export const ScribeMark: React.FC<ScribeMarkProps> = ({
  className = '',
  tone = 'signal',
  animate = 'always',
  title,
}) => {
  const uid = useId();
  const stagger = staggerFor(uid);
  const labelled = !!title;
  const maskId = `scribe-eyes-${uid.replace(/:/g, '')}`;

  /* Only the always-on variant is staggered. A hover-woken mascot is alone on
     screen and must open on eyes-forward — a negative delay would drop it into
     the middle of a blink the instant the pointer arrives, so it squints at
     you instead of waking up. */
  const wake = animate === 'hover';
  const delay = (kind: 'blink' | 'glance') =>
    animate === 'always' ? { animationDelay: stagger[kind] } : undefined;

  return (
    <svg
      viewBox="0 0 60 86"
      className={[animate === 'never' ? '' : wake ? 'scribe-mark-wake' : 'scribe-mark', className]
        .filter(Boolean)
        .join(' ')}
      role={labelled ? 'img' : undefined}
      aria-hidden={labelled ? undefined : true}
      focusable="false"
    >
      {labelled && <title>{title}</title>}
      {/* The eyes are HOLES, not painted shapes, so whatever the mascot is
          standing on becomes the eye: white on a light page, near-black on a
          dark one, chrome-gray on a chrome-gray chip. One fixed eye colour
          cannot do that — black eyes go invisible the moment `tone="current"`
          paints the body black, and a light-mode mascot wants light eyes.

          A <mask> rather than an evenodd path because the eyes still have to
          blink and glance, and a mask's contents animate live. */}
      <mask id={maskId}>
        <path d={BODY} fill="#fff" />
        {/* Two nested transforms so they can never fight: the group glances,
            the ellipses blink. One element doing both would need the keyframes
            to restate the other's value at every stop. */}
        <g className="sm-look" style={delay('glance')}>
          {EYES.map((e) => (
            <ellipse
              key={e.cx}
              className="sm-eye"
              cx={e.cx}
              cy={e.cy}
              rx="9"
              ry="9.5"
              fill="#000"
              style={delay('blink')}
            />
          ))}
        </g>
      </mask>
      <path
        d={BODY}
        fill={tone === 'current' ? 'currentColor' : 'var(--writ-signal, #E23A14)'}
        mask={`url(#${maskId})`}
      />
    </svg>
  );
};

export default ScribeMark;
