/**
 * Writ brand marks — the handoff construction.
 *
 * WritWordmark: lowercase `writ` on the Schibsted 600 skeleton with the three
 * proprietary cuts — square i-dot, wedge-cut drawn t, and the Signal bar that
 * completes the t's missing right arm on the crossbar line. Styling lives in
 * the global stylesheet (`.writ-wm` block); scale with `size` (px font-size).
 * Inherits text color; the bar reads `--writ-signal` (#E23A14 on light).
 *
 * WritGlyph: the compact "handoff" symbol (broken t + bar). Self-contained
 * SVG, no font dependency. `cut="optical"` is the heavier simplified version
 * for 16–20 px; it is auto-selected at small sizes.
 *
 * WritTile: the app-icon treatment — dark tile, light glyph, Signal bar —
 * for sidebars, logins, and anywhere the icon appears on arbitrary grounds.
 */

const T_MAIN = 'M43 20 L58 11 V80 L43 89 V50 H20 V34 H43 Z';
const BAR_MAIN = { x: 65, y: 34, w: 15, h: 46 };
const T_OPTICAL = 'M35 19 L57 8 V85 H35 V54 H13 V35 H35 Z';
const BAR_OPTICAL = { x: 65, y: 35, w: 22, h: 50 };

export function WritWordmark({ size = 15, className = '' }: { size?: number; className?: string }) {
  return (
    <span className={`writ-wm ${className}`} role="img" aria-label="writ" style={{ fontSize: size }}>
      wr
      <span className="wx-i">
        {'ı'}
        <span className="wx-dot" />
      </span>
      <svg className="wx-t" viewBox="0 0 34 76" aria-hidden="true" focusable="false">
        <path d="M10 7 L23 0 V76 H10 V34 H0 V23 H10 Z" fill="currentColor" />
      </svg>
      <span className="wx-bar" />
    </span>
  );
}

export function WritGlyph({
  size = 24,
  cut,
  tFill = 'currentColor',
  barFill = 'var(--writ-signal, #E23A14)',
  className = '',
}: {
  size?: number;
  cut?: 'main' | 'optical';
  tFill?: string;
  barFill?: string;
  className?: string;
}) {
  const optical = cut ? cut === 'optical' : size <= 20;
  const t = optical ? T_OPTICAL : T_MAIN;
  const bar = optical ? BAR_OPTICAL : BAR_MAIN;
  return (
    <svg
      viewBox="0 0 100 100"
      width={size}
      height={size}
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      <path d={t} fill={tFill} />
      <rect x={bar.x} y={bar.y} width={bar.w} height={bar.h} fill={barFill} />
    </svg>
  );
}

export function WritTile({ size = 24, className = '' }: { size?: number; className?: string }) {
  const glyph = Math.round(size * 0.78);
  return (
    <span
      aria-hidden="true"
      className={`inline-flex items-center justify-center shrink-0 ${className}`}
      style={{ width: size, height: size, borderRadius: Math.round(size * 0.26), background: '#0A0C10' }}
    >
      <WritGlyph size={glyph} cut={glyph <= 22 ? 'optical' : 'main'} tFill="#E9EBEF" barFill="#FF4A24" />
    </span>
  );
}
