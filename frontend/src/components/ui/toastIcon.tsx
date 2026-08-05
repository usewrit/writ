import React from 'react';
import clsx from 'clsx';

/**
 * Leading glyph for a caller-supplied toast `icon`.
 *
 * The app never uses emoji in UI copy or affordances, so toasts that want a
 * custom leading mark pass a heroicon through this helper instead of an emoji
 * string. It renders at the same 18px as the built-in type glyphs in
 * `ui/Toast.tsx`, so a custom toast lines up with a success/error one.
 *
 * Lives in its own module (rather than in `ui/Toast.tsx`, which is kept in sync
 * with the desktop app's copy) so plain `.ts` call sites — hooks, stores — can
 * build an icon without JSX.
 *
 *   toast(t('Reconnecting…'), { icon: toastIcon(ArrowPathIcon) });
 *   toast(t('Heads up'), { icon: toastIcon(ExclamationTriangleIcon, 'text-amber-500') });
 */
export function toastIcon(
  Icon: React.ComponentType<React.SVGProps<SVGSVGElement>>,
  tone = 'text-secondary',
): React.ReactElement {
  return <Icon className={clsx('h-[18px] w-[18px]', tone)} aria-hidden="true" />;
}

export default toastIcon;
