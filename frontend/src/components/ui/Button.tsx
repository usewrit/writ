import React from 'react';
import clsx from 'clsx';

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'primary' | 'secondary' | 'ghost';
  size?: 'sm' | 'md' | 'lg';
  loading?: boolean;
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
      className={clsx(
        'inline-flex items-center justify-center rounded-lg transition-all duration-150 active:scale-[0.97]',
        size === 'sm' && 'px-3 py-1.5 text-sm gap-1.5',
        size === 'md' && 'px-4 py-2.5 text-sm gap-2',
        size === 'lg' && 'px-5 py-3 text-base gap-2',
        // Primary carries the brand red (tape DNA). `accent-strong`, not
        // `accent`: this fill sits under type, so it needs the AA-safe step
        // (6.23:1 with `accent-on`).
        variant === 'primary' && 'bg-accent-strong text-accent-on font-semibold shadow-sm hover:bg-accent-strong/90',
        variant === 'secondary' && 'bg-surface text-secondary font-medium border border-border hover:text-ink hover:border-ink/20 hover:bg-chrome',
        variant === 'ghost' && 'text-secondary font-medium hover:text-ink hover:bg-chrome',
        (disabled || loading) && 'opacity-40 pointer-events-none',
        className,
      )}
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
