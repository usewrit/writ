import React from 'react';
import toast, { Toaster, resolveValue, type Toast } from 'react-hot-toast';
import clsx from 'clsx';
import i18n from '../../i18n';
import {
  CheckCircleIcon,
  ExclamationTriangleIcon,
  InformationCircleIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';

/**
 * App-wide toast surface — a single design-token card used for EVERY toast
 * (success / error / loading / blank), so the whole app reads with one voice.
 *
 * Why a custom renderer instead of react-hot-toast's default pill: the default
 * ships a hard-coded white/dark pill that ignores our theme tokens and sits
 * top-center. We render our own card off the toast object via the <Toaster>
 * render-prop, which keeps all existing `toast.success(...)` / `toast.error(...)`
 * / bare `toast(...)` call sites working untouched — they just look different now.
 *
 * Kept byte-for-byte in sync with the desktop app's Toast.tsx (only the bottom
 * offset differs — the cloud app has no window status bar). Design: neutral
 * `surface` card with a subtle status glyph (green check / red triangle — the
 * same -500 shades the status dots use) and a dismiss affordance, anchored
 * bottom-right. A caller-supplied `icon` always wins over the type glyph.
 */

const BOTTOM_OFFSET = 16;

const Spinner: React.FC = () => (
  <svg className="h-[18px] w-[18px] animate-spin text-secondary" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
    <path className="opacity-80" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
  </svg>
);

const ToastCard: React.FC<{ t: Toast }> = ({ t }) => {
  const message = resolveValue(t.message, t);

  // Leading visual: an explicit caller icon wins, otherwise a glyph by type.
  let leading: React.ReactNode;
  if (t.type === 'loading') {
    leading = <Spinner />;
  } else if (t.icon) {
    leading = <span className="text-[16px] leading-none">{t.icon}</span>;
  } else if (t.type === 'success') {
    leading = <CheckCircleIcon className="h-[18px] w-[18px] text-green-500" aria-hidden="true" />;
  } else if (t.type === 'error') {
    leading = <ExclamationTriangleIcon className="h-[18px] w-[18px] text-red-500" aria-hidden="true" />;
  } else {
    leading = <InformationCircleIcon className="h-[18px] w-[18px] text-secondary" aria-hidden="true" />;
  }

  return (
    <div
      className={clsx(
        'pointer-events-auto flex w-[360px] max-w-[calc(100vw-2rem)] items-start gap-2.5',
        'rounded-lg border border-border bg-surface px-3.5 py-3 shadow-lg',
        'transition-all duration-300 ease-out motion-reduce:transition-none',
        t.visible ? 'translate-x-0 opacity-100' : 'translate-x-3 opacity-0',
      )}
    >
      <span className="mt-px shrink-0">{leading}</span>
      <div
        {...t.ariaProps}
        className="min-w-0 flex-1 self-center text-[13px] leading-snug text-ink [overflow-wrap:anywhere]"
      >
        {message}
      </div>
      {t.type !== 'loading' && (
        <button
          type="button"
          onClick={() => toast.dismiss(t.id)}
          aria-label={i18n.t('Dismiss')}
          className="-mr-1 -mt-0.5 shrink-0 self-start rounded-md p-1 text-tertiary transition-colors hover:bg-hover hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/40"
        >
          <XMarkIcon className="h-4 w-4" aria-hidden="true" />
        </button>
      )}
    </div>
  );
};

/** The single <Toaster> for the app. Mount once near the root. */
export const AppToaster: React.FC = () => (
  <Toaster
    position="bottom-right"
    gutter={10}
    containerStyle={{ bottom: BOTTOM_OFFSET, right: 16 }}
    toastOptions={{
      duration: 4000,
      success: { duration: 3000 },
      error: { duration: 6000 },
    }}
  >
    {(t) => <ToastCard t={t} />}
  </Toaster>
);

export default AppToaster;
