import containerQueries from '@tailwindcss/container-queries'

/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      // ── Container-query scale: the CARD's width, not the window's ──────
      // Every page renders inside the content card, which is never the window —
      // the sidebar sits beside it — so `sm:`/`lg:` misreport how much room a
      // page actually has. Pages lay out against the `@stage` container declared
      // on that card (Layout.tsx). Names describe what the card can HOLD, so a
      // threshold is a decision rather than a magic px:
      //   pair  — two comfortable columns
      //   rail  — a fixed side rail still leaves the main column readable
      //   split — a main column + a real sidebar, both comfortable
      //   wide  — three columns / the full dense treatment
      // Kept identical to the other Writ frontends: these three trees
      // share component copies, so a token must mean the same thing in each.
      containers: {
        pair: '520px',
        rail: '700px',
        split: '860px',
        wide: '1100px',
      },
      fontFamily: {
        sans: ['Inter', '-apple-system', 'BlinkMacSystemFont', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      colors: {
        // Warm neutral scale. canvas is intentionally a step darker than the
        // white surface so cards/panels separate from the page (depth without
        // shadows). The text ramp (ink → secondary → tertiary) is tuned for a
        // clear 3-tier hierarchy; tertiary still passes AA on white (was #ABABAB
        // / 2.6:1 which failed and made the whole UI read faint and "flat").
        canvas: '#EFEFF1',        // Page background — a touch darker so white cards pop
        surface: '#FFFFFF',       // Elevated content (cards, panels)
        sidebar: '#F4F4F6',       // Sidebar background — distinct from canvas + surface
        chrome: '#EDEBE8',        // Nav/frame tone for the shelf master-list column — the warm neutral the sidebar already paints via bg-[#EDEBE8]; recedes so the white surface reads as content
        ink: '#0D0D0D',           // Primary text
        secondary: '#565656',    // Secondary text (~7:1) — readable, clear step below ink
        tertiary: '#767676',     // Tertiary text / labels (~4.5:1, AA) — was #ABABAB
        border: '#D6D7DC',       // Borders — darkened from #E3E3E6 (mirrors desktop): the old value was nearly invisible on white surfaces over the #EFEFF1 canvas, so the content-card edge + section dividers never read as distinct (sidebar↔content separation looked weak)
        'border-strong': '#C5C6CC', // Emphasized rule for section dividers / card headers
        hover: '#EAEAEC',        // Hover states
        active: '#E2E2E5',       // Active/pressed states
        // ── Accent: the brand red, ported from the marketing site's tape DNA ──
        // Red means ONE thing across site + app: live / active / changed /
        // primary action. Two values, split by WCAG threshold — do not collapse
        // them (selfhost is light-only, so there is no dark variant to mirror):
        //
        //   accent        GRAPHICAL ONLY — status dots, progress fills, washes
        //                 (`bg-accent/10`), borders, selection bars. Never under
        //                 type. #E23A14 is the site's exact brand red and clears
        //                 the 3:1 non-text bar (WCAG 1.4.11) on every surface we
        //                 paint on: 4.33 surface / 3.77 canvas / 3.64 chrome.
        //   accent-strong ANY ROLE INVOLVING TEXT — red type, red icons beside
        //                 type, and filled controls carrying `accent-on`.
        //                 #B4300F clears AA 4.5:1 everywhere: 6.23 surface /
        //                 5.43 canvas / 5.24 chrome / 5.19 hover, and 6.23 as a
        //                 fill under white text. The raw brand red FAILS both as
        //                 a fill with white text (4.33) and as body type (3.77),
        //                 which is why this darker step exists.
        //   accent-on     Text/icon sitting ON an accent-strong fill.
        accent: '#E23A14',
        'accent-strong': '#B4300F',
        'accent-on': '#FFFFFF',
        // Brand execution accent. selfhost had no `signal` token at all, so the
        // ported master-detail lists had nothing to paint a running state with;
        // it is an alias of `accent` for parity with admin + desktop.
        signal: '#E23A14',
        // ── Semantic status palette (the ONLY color in the app) — mirrors the
        //    desktop token system so the ported master-detail lists render their
        //    status dots / chips. Each role: base (dot/icon) + `-bg` soft fill +
        //    `-fg` AA text on white or on the soft tint.
        success: '#16A34A', 'success-bg': '#E7F6EC', 'success-fg': '#15803D',
        warning: '#D97706', 'warning-bg': '#FBF0DD', 'warning-fg': '#B45309',
        danger: '#DC2626', 'danger-bg': '#FCEBEB', 'danger-fg': '#B91C1C',
        info: '#2563EB', 'info-bg': '#E8F0FE', 'info-fg': '#1D4ED8',
      },
      borderRadius: {
        DEFAULT: '12px',
        sm: '8px',
        lg: '16px',
        xl: '24px',
        full: '9999px',
      },
      boxShadow: {
        'sm': '0 1px 2px rgba(0,0,0,0.04)',
        'md': '0 2px 8px rgba(0,0,0,0.06)',
        'lg': '0 8px 24px rgba(0,0,0,0.08)',
      },
      // Motion tokens (src/index.css) surfaced as Tailwind utilities so
      // JSX and hand-written CSS share ONE vocabulary. Overriding DEFAULT makes
      // every bare `transition-*` utility settle on the expo curve at 180ms
      // (instead of Tailwind's stock 150ms cubic-bezier(0.4,0,0.2,1)), and
      // `ease-out` now IS the token curve — the app no longer runs two
      // different "ease-out"s. `ease-in` keeps Tailwind's default: exits
      // accelerate away and are too short (100–150ms) to read a custom curve.
      transitionTimingFunction: {
        DEFAULT: 'var(--ease-out)',
        out: 'var(--ease-out)',
        'in-out': 'var(--ease-in-out)',
        spring: 'var(--ease-spring)',
      },
      transitionDuration: {
        DEFAULT: 'var(--dur)',
        fast: 'var(--dur-fast)',
        base: 'var(--dur)',
        slow: 'var(--dur-slow)',
      },
      keyframes: {
        'wizard-fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        'wizard-scale-in': {
          '0%': { opacity: '0', transform: 'scale(0.97) translateY(8px)' },
          '100%': { opacity: '1', transform: 'scale(1) translateY(0)' },
        },
        'wizard-slide-up': {
          '0%': { opacity: '0', transform: 'translateY(16px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'wizard-field-in': {
          '0%': { opacity: '0', transform: 'translateY(12px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'fade-in': {
          '0%': { opacity: '0.5' },
          '100%': { opacity: '1' },
        },
        'fade-in-up': {
          '0%': { opacity: '0.4', transform: 'translateY(6px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'fade-in-scale': {
          '0%': { opacity: '0.5', transform: 'scale(0.98)' },
          '100%': { opacity: '1', transform: 'scale(1)' },
        },
        'slide-down': {
          '0%': { opacity: '0.5', transform: 'translateY(-3px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'status-pulse': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.4' },
        },
        'page-enter': {
          // Starts at 0.5 (not 0) so the always-mounted PageTransition wrapper never
          // blanks the content area to the white `--surface` mid-navigation — the same
          // anti-flash floor used by fade-in / fade-in-scale.
          '0%': { opacity: '0.5', transform: 'translateY(4px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'step-enter': {
          '0%': { opacity: '0', transform: 'translateY(12px) scale(0.97)' },
          '100%': { opacity: '1', transform: 'translateY(0) scale(1)' },
        },
        'step-line-grow': {
          '0%': { transform: 'scaleY(0)' },
          '100%': { transform: 'scaleY(1)' },
        },
        'summary-enter': {
          '0%': { opacity: '0', transform: 'translateY(20px) scale(0.96)' },
          '100%': { opacity: '1', transform: 'translateY(0) scale(1)' },
        },
        'overlay-enter': {
          '0%': { opacity: '0', transform: 'translateY(16px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        // One-time entrance for genuinely-new page content. Opacity only (no
        // transform → no layout shift) and a high floor so it reads as a soft
        // settle, never a dim or a jump.
        'content-in': {
          '0%': { opacity: '0.6' },
          '100%': { opacity: '1' },
        },
        // Selected-row accent bar (shelf lists): grows out from the row edge
        // instead of popping in a frame after the slab bg has eased.
        'accent-in': {
          '0%': { opacity: '0', transform: 'scaleY(0.4)' },
          '100%': { opacity: '1', transform: 'scaleY(1)' },
        },
      },
      animation: {
        // All entrance animations settle on the shared expo token curve — the
        // literal cubic-bezier(0.16,1,0.3,1) copies are gone so the curve can
        // never drift per-keyframe again.
        'wizard-fade-in': 'wizard-fade-in 0.2s var(--ease-out)',
        'wizard-scale-in': 'wizard-scale-in 0.3s var(--ease-out)',
        'wizard-slide-up': 'wizard-slide-up 0.35s var(--ease-out)',
        'wizard-field-in': 'wizard-field-in 0.3s var(--ease-out)',
        'fade-in': 'fade-in 0.2s var(--ease-out)',
        'fade-in-up': 'fade-in-up 0.3s var(--ease-out)',
        'fade-in-scale': 'fade-in-scale 0.2s var(--ease-out)',
        'slide-down': 'slide-down 0.15s var(--ease-out)',
        'status-pulse': 'status-pulse 2s var(--ease-in-out) infinite',
        // `backwards` (not `both`): the enter animation must NOT forward-fill, or
        // the final `transform: translateY(0)` is retained forever, pinning the
        // always-present PageTransition wrapper onto a compositor layer (heavy
        // re-composite on every scroll frame). Dropping forward-fill releases the
        // transform after 0.28s (base state is visually identical, so no flash).
        'page-enter': 'page-enter 0.28s var(--ease-out) backwards',
        'step-enter': 'step-enter 0.35s var(--ease-out) both',
        'step-line-grow': 'step-line-grow 0.25s var(--ease-out) both',
        'summary-enter': 'summary-enter 0.4s var(--ease-out) both',
        'overlay-enter': 'overlay-enter 0.3s var(--ease-out) both',
        'content-in': 'content-in 0.16s var(--ease-out)',
        'accent-in': 'accent-in 0.18s var(--ease-out) both',
      },
    },
  },
  plugins: [containerQueries],
}
