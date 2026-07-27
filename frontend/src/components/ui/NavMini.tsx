import React from 'react';

/**
 * NavMini — the sidebar's hover-reveal micro-animation, ported from the
 * marketing site's mega-menu (`.fl-mini` in website/src/components/Nav.astro).
 *
 * The site's idea, kept intact: a flagship nav row hides a tiny mono strip that
 * appears on hover and animates ONLY while hovered — the animation sits at
 * `animation-play-state: paused` until the row is hovered. Nothing moves in a
 * resting sidebar, so this costs no attention until you go looking.
 *
 * ONE adaptation. On the site the mini EXPANDS the row (`max-height: 0 → 24px`)
 * because the mega-menu is a transient overlay — nothing below it matters. The
 * sidebar is persistent, so expanding a row would shove every item under it
 * down on every hover. Here the mini instead fades into the row's trailing
 * space at a fixed 14px height: same reveal, zero layout shift.
 *
 * Reserved for the BUILD flagships, mirroring the site, where only `flagshipLinks`
 * carry a mini and the secondary columns don't. If everything animates, the
 * signal is gone.
 *
 * Red here is `bg-accent` — these are pure graphics (bars/dots, never type), so
 * the 3:1 non-text bar applies and the true brand red is the right token.
 * The whole strip is hidden under `prefers-reduced-motion`; it is decorative and
 * duplicates nothing, so removing it loses no information.
 */

export type NavMiniKind = 'collapse' | 'spark' | 'pops' | 'frontier';

export const NavMini: React.FC<{ kind: NavMiniKind }> = ({ kind }) => (
  <span className="navmini ml-auto shrink-0 motion-reduce:hidden" aria-hidden="true">
    {/* Workflows — the site's `mn-collapse`: N browser steps fold down to a
        couple of direct HTTP calls. Gray bar shrinks, red bar takes over. */}
    {kind === 'collapse' && (
      <span className="navmini-collapse">
        <i className="a" />
        <i className="b" />
      </span>
    )}
    {/* Monitors — the site's `mn-spark`: a flat series, then one bar jumps. */}
    {kind === 'spark' && (
      <span className="navmini-spark">
        <i /><i /><i /><i className="h" />
      </span>
    )}
    {/* Automations — the site's `mn-pops`: trigger fires a chain, in order. */}
    {kind === 'pops' && (
      <span className="navmini-pops">
        <i /><i /><i />
      </span>
    )}
    {/* Harvest — the site's frontier fan-out: agents pulling in parallel. */}
    {kind === 'frontier' && (
      <span className="navmini-frontier">
        <i /><i /><i />
      </span>
    )}
  </span>
);
