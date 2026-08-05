import React from 'react';
import clsx from 'clsx';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost';
export type ButtonSize = 'sm' | 'md' | 'lg';

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
}

/**
 * The button look, as a class string — so an `<a>`/`<Link>` that acts as a CTA
 * renders identically to a real `<Button>` instead of re-deriving the padding,
 * weight and shadow by hand (which is how the three apps' CTAs drifted apart).
 * Reach for `<Button>` for actions; use this only when the element must stay an
 * anchor so middle-click / open-in-new-tab keep working.
 */
export function buttonClass({
  variant = 'primary',
  size = 'md',
  disabled = false,
  className,
}: { variant?: ButtonVariant; size?: ButtonSize; disabled?: boolean; className?: string } = {}) {
  return clsx(
    // Hover eases in on the app's expo curve; the press is near-instant
    // (active:duration-75) then settles back smoothly on release — the
    // micro-timing that makes a button feel responsive rather than mushy.
    'inline-flex items-center justify-center font-medium rounded-lg transition-all duration-200 ease-out active:scale-[0.97] active:duration-75',
    size === 'sm' && 'px-3 py-1.5 text-sm gap-1.5',
    size === 'md' && 'px-4 py-2.5 text-sm gap-2',
    size === 'lg' && 'px-5 py-3 text-base gap-2',
    // Primary carries the brand red (tape DNA). `accent-strong`, not
    // `accent`: this fill sits under type, so it needs the AA-safe step
    // (6.23:1 with `accent-on`). `accent-on` is white on light and ink on
    // dark — white on the dark red would only reach 3.36:1.
    variant === 'primary' && 'bg-accent-strong text-accent-on font-semibold shadow-sm hover:bg-accent-strong/90 hover:shadow-md',
    variant === 'secondary' && 'bg-surface text-secondary border border-border shadow-sm hover:text-ink hover:border-ink/20 hover:shadow',
    variant === 'ghost' && 'text-secondary hover:text-ink hover:bg-chrome',
    disabled && 'opacity-40 pointer-events-none',
    className,
  );
}

export const Button: React.FC<ButtonProps> = ({
  children,
  variant = 'primary',
  size = 'md',
  loading = false,
  className,
  disabled,
  ...props
}) => {
  return (
    <button
      className={buttonClass({ variant, size, disabled: disabled || loading, className })}
      disabled={disabled || loading}
      {...props}
    >
      {loading && (
        <svg className="animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
        </svg>
      )}
      {children}
    </button>
  );
};
