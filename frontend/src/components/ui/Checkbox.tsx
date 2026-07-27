import React, { useId } from 'react';
import clsx from 'clsx';
import { CheckIcon, MinusIcon } from '@heroicons/react/20/solid';

/**
 * Checkbox — tokenized replacement for the native <input type="checkbox">.
 *
 * The OS-drawn checkbox breaks our DNA in dark mode (it never inverts) and
 * ignores `text-ink` / `border-border`. This version paints its own box from
 * the same tokens Input/Select use, so it stays flush with the rest of a form
 * in both themes.
 *
 * Pass `label` for the common inline "☐ Enable X" pattern (the whole row
 * becomes the click target). Omit it and the bare box works as a table-cell
 * toggle. `indeterminate` renders the tri-state minus glyph for
 * select-all rows.
 */
export interface CheckboxProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'type' | 'size'> {
  label?: React.ReactNode;
  description?: React.ReactNode;
  error?: string;
  size?: 'sm' | 'md';
  indeterminate?: boolean;
  wrapperClassName?: string;
  boxClassName?: string;
}

const BOX_SIZE = {
  sm: 'h-3.5 w-3.5',
  md: 'h-4 w-4',
} as const;

const ICON_SIZE = {
  sm: 'h-2.5 w-2.5',
  md: 'h-3 w-3',
} as const;

export const Checkbox: React.FC<CheckboxProps> = ({
  label,
  description,
  error,
  size = 'md',
  indeterminate = false,
  checked,
  disabled,
  className,
  wrapperClassName,
  boxClassName,
  id,
  ...props
}) => {
  const reactId = useId();
  const inputId = id ?? `chk-${reactId}`;
  const errorId = error ? `${inputId}-error` : undefined;

  // Native input drives the ref for indeterminate; keeping the real input in
  // the DOM preserves keyboard, form submission, and screen-reader semantics.
  const inputRef = React.useRef<HTMLInputElement | null>(null);
  React.useEffect(() => {
    if (inputRef.current) inputRef.current.indeterminate = indeterminate;
  }, [indeterminate]);

  const box = (
    <span
      aria-hidden="true"
      className={clsx(
        'relative inline-flex shrink-0 items-center justify-center rounded-[5px] border transition-colors duration-fast',
        BOX_SIZE[size],
        (checked || indeterminate)
          ? 'bg-ink border-ink'
          : 'bg-surface border-border group-hover:border-ink/30',
        error && 'border-red-400',
        disabled && 'opacity-40',
        boxClassName,
      )}
    >
      {/* Glyph stays mounted and scales/fades with the box fill — a mark that
          pops in a frame after the box darkens reads as two separate events. */}
      <span
        className={clsx(
          'flex items-center justify-center transition-[opacity,transform] duration-fast',
          checked || indeterminate ? 'opacity-100 scale-100' : 'opacity-0 scale-50',
        )}
      >
        {indeterminate ? (
          <MinusIcon className={clsx('text-surface', ICON_SIZE[size])} />
        ) : (
          <CheckIcon className={clsx('text-surface', ICON_SIZE[size])} />
        )}
      </span>
    </span>
  );

  const inputEl = (
    <input
      ref={inputRef}
      id={inputId}
      type="checkbox"
      checked={checked}
      disabled={disabled}
      aria-invalid={error ? true : undefined}
      aria-describedby={errorId}
      className="peer sr-only"
      {...props}
    />
  );

  if (!label && !description) {
    return (
      <label
        htmlFor={inputId}
        className={clsx(
          'group inline-flex shrink-0 items-center rounded-[5px]',
          'focus-within:ring-2 focus-within:ring-ink/10 focus-within:ring-offset-1 focus-within:ring-offset-surface',
          disabled ? 'cursor-not-allowed' : 'cursor-pointer',
          wrapperClassName,
          className,
        )}
      >
        {inputEl}
        {box}
      </label>
    );
  }

  return (
    <div className={clsx('flex flex-col', wrapperClassName)}>
      <label
        htmlFor={inputId}
        className={clsx(
          'group inline-flex items-start gap-2 rounded-[5px]',
          'focus-within:ring-2 focus-within:ring-ink/10 focus-within:ring-offset-1 focus-within:ring-offset-surface',
          disabled ? 'cursor-not-allowed opacity-70' : 'cursor-pointer',
          className,
        )}
      >
        {inputEl}
        <span className="mt-0.5 flex">{box}</span>
        <span className="min-w-0 flex-1">
          {label && (
            <span
              className={clsx(
                'block text-sm leading-tight',
                checked || indeterminate ? 'text-ink' : 'text-secondary',
                error && 'text-red-500',
              )}
            >
              {label}
            </span>
          )}
          {description && (
            <span className="mt-0.5 block text-xs text-tertiary">{description}</span>
          )}
        </span>
      </label>
      {error && (
        <p id={errorId} className="mt-1 text-xs text-red-500">
          {error}
        </p>
      )}
    </div>
  );
};
