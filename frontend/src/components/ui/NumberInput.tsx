import React, { useCallback, useId, useState } from 'react';
import clsx from 'clsx';
import { ChevronUpIcon, ChevronDownIcon } from '@heroicons/react/20/solid';
import { useTranslation } from 'react-i18next';

/**
 * NumberInput — number field with tokenized +/- steppers that replaces the
 * browser's native spinner UI (which is not themable and looks especially
 * broken in dark mode / Chromium on Linux).
 *
 * The visible <input> keeps `type="number"` for on-mobile numeric keypads and
 * form validation, but the native increment arrows are hidden via the CSS
 * classes in index.css (`::-webkit-inner-spin-button`, MOZ `appearance:none`).
 * The two chevron buttons on the right clamp against min/max and honor step.
 *
 * `value` may be null/undefined for a truly-empty field (the input renders as
 * blank), so callers don't have to invent a sentinel like `-1`.
 */
export interface NumberInputProps
  extends Omit<
    React.InputHTMLAttributes<HTMLInputElement>,
    'type' | 'value' | 'onChange' | 'size'
  > {
  value: number | null | undefined;
  onChange: (value: number | null) => void;
  label?: string;
  error?: string;
  size?: 'sm' | 'md';
  min?: number;
  max?: number;
  step?: number;
  /** Text shown as suffix inside the field (e.g. "ms", "px"). */
  suffix?: React.ReactNode;
  /** Hide the +/- steppers when steppers make no sense (freeform amounts). */
  hideSteppers?: boolean;
  wrapperClassName?: string;
}

const SIZES = {
  sm: { input: 'px-3 py-1.5 text-xs', stepCol: 'w-6', chevron: 'h-3 w-3' },
  md: { input: 'px-4 py-2.5 text-sm', stepCol: 'w-7', chevron: 'h-3.5 w-3.5' },
} as const;

export const NumberInput: React.FC<NumberInputProps> = ({
  value,
  onChange,
  label,
  error,
  size = 'md',
  min,
  max,
  step = 1,
  suffix,
  hideSteppers = false,
  disabled,
  className,
  wrapperClassName,
  id,
  'aria-label': ariaLabel,
  ...props
}) => {
  const { t } = useTranslation();
  const reactId = useId();
  const inputId = id ?? `num-${reactId}`;
  const errorId = error ? `${inputId}-error` : undefined;
  const sizeCls = SIZES[size];

  const clamp = useCallback(
    (n: number) => {
      let out = n;
      if (typeof min === 'number' && out < min) out = min;
      if (typeof max === 'number' && out > max) out = max;
      return out;
    },
    [min, max],
  );

  const bump = useCallback(
    (dir: 1 | -1) => {
      if (disabled) return;
      const base = typeof value === 'number' && !Number.isNaN(value) ? value : (min ?? 0);
      onChange(clamp(base + dir * step));
    },
    [disabled, value, min, step, onChange, clamp],
  );

  // We drive the visible field as a text input over an internal string buffer
  // rather than a native `type="number"`. A `type="number"` input reports an
  // EMPTY string to React for legitimate intermediate states like "0.", "1.5e"
  // or a lone "-", so decimals and negatives become impossible to type. The
  // buffer holds exactly what the user typed; the parsed number is pushed to
  // `onChange` only when the text parses to a finite value.
  const canonical = value === null || value === undefined || Number.isNaN(value) ? '' : String(value);
  const [text, setText] = useState(canonical);
  // Focus is STATE, not a ref: the sync below runs during render and a ref may
  // not be read there (it would also be one render stale in concurrent mode).
  const [focused, setFocused] = useState(false);

  // Keep the buffer in sync when the value changes from OUTSIDE (prop-driven:
  // steppers, resets, another control). While focused we don't stomp what the
  // user is mid-typing unless the numeric value genuinely diverges.
  //
  // Adjusted DURING render off the last canonical string we synced: from an
  // effect the field paints the previous buffer for one frame first, so a
  // stepper click or an external reset lands as a visible one-frame stutter.
  const [syncedCanonical, setSyncedCanonical] = useState(canonical);
  if (syncedCanonical !== canonical) {
    setSyncedCanonical(canonical);
    if (!focused || (text.trim() !== '' && Number(text) !== value)) {
      setText(canonical);
    }
  }

  const allowsNegative = typeof min !== 'number' || min < 0;

  const sanitize = useCallback(
    (raw: string) => {
      let s = raw.replace(/[^0-9.-]/g, '');
      if (!allowsNegative) s = s.replace(/-/g, '');
      else s = s.replace(/(?!^)-/g, ''); // only a leading minus
      const firstDot = s.indexOf('.');
      if (firstDot !== -1) {
        s = s.slice(0, firstDot + 1) + s.slice(firstDot + 1).replace(/\./g, '');
      }
      return s;
    },
    [allowsNegative],
  );

  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const raw = sanitize(e.target.value);
      setText(raw);
      if (raw === '' || raw === '-' || raw === '.' || raw === '-.') {
        onChange(null);
        return;
      }
      const n = Number(raw);
      if (Number.isNaN(n)) return; // partial like "1." — keep buffer, hold value
      onChange(n);
    },
    [onChange, sanitize],
  );

  const handleFocus = useCallback(
    (e: React.FocusEvent<HTMLInputElement>) => {
      setFocused(true);
      props.onFocus?.(e);
    },
    [props],
  );

  const handleBlur = useCallback(
    (e: React.FocusEvent<HTMLInputElement>) => {
      setFocused(false);
      // On blur, clamp to min/max so users can't leave the field out of range,
      // then normalise the buffer to the canonical string of the final value.
      if (typeof value === 'number' && !Number.isNaN(value)) {
        const clamped = clamp(value);
        if (clamped !== value) {
          onChange(clamped);
          setText(String(clamped));
        } else {
          setText(String(value));
        }
      } else {
        setText('');
      }
      props.onBlur?.(e);
    },
    [value, clamp, onChange, props],
  );

  const displayValue = text;

  const atMin = typeof min === 'number' && typeof value === 'number' && value <= min;
  const atMax = typeof max === 'number' && typeof value === 'number' && value >= max;

  const inputMode: React.HTMLAttributes<HTMLInputElement>['inputMode'] =
    (typeof step === 'number' && step < 1) || allowsNegative ? 'decimal' : 'numeric';

  return (
    <div className={wrapperClassName}>
      {label && (
        <label htmlFor={inputId} className="mb-1.5 block text-sm text-secondary">
          {label}
        </label>
      )}
      <div
        className={clsx(
          'relative flex items-center rounded-lg border bg-surface transition-all duration-200',
          error
            ? 'border-red-300 focus-within:ring-2 focus-within:ring-red-500/10'
            : 'border-border focus-within:border-ink/30 focus-within:ring-2 focus-within:ring-ink/5 focus-within:shadow-sm',
          disabled && 'opacity-40 pointer-events-none',
        )}
      >
        <input
          id={inputId}
          type="text"
          inputMode={inputMode}
          disabled={disabled}
          aria-label={!label ? ariaLabel : undefined}
          aria-invalid={error ? true : undefined}
          aria-describedby={errorId}
          className={clsx(
            'w-full bg-transparent text-ink placeholder:text-tertiary focus:outline-none',
            sizeCls.input,
            !hideSteppers && 'pr-9',
            suffix && 'pr-16',
            className,
          )}
          {...props}
          value={displayValue}
          onChange={handleInputChange}
          onFocus={handleFocus}
          onBlur={handleBlur}
        />
        {suffix && (
          <span
            className={clsx(
              'pointer-events-none absolute text-tertiary',
              hideSteppers ? 'right-3' : (size === 'sm' ? 'right-8' : 'right-9'),
              size === 'sm' ? 'text-xs' : 'text-sm',
            )}
          >
            {suffix}
          </span>
        )}
        {!hideSteppers && (
          // Steppers sit flush against the field's right edge — a single
          // hairline (border-l) separates them from the value, another splits
          // up/down. No floating inner box (that read as border-in-border).
          <div
            className={clsx(
              'absolute right-0 top-0 bottom-0 flex flex-col border-l border-border',
              sizeCls.stepCol,
            )}
          >
            <button
              type="button"
              tabIndex={-1}
              aria-label={t('Increment')}
              onClick={() => bump(1)}
              disabled={disabled || atMax}
              className={clsx(
                'flex flex-1 items-center justify-center rounded-tr-lg text-tertiary transition-colors',
                'hover:bg-hover hover:text-ink active:bg-hover',
                (disabled || atMax) && 'opacity-30 pointer-events-none',
              )}
            >
              <ChevronUpIcon className={sizeCls.chevron} />
            </button>
            <div className="h-px shrink-0 bg-border" />
            <button
              type="button"
              tabIndex={-1}
              aria-label={t('Decrement')}
              onClick={() => bump(-1)}
              disabled={disabled || atMin}
              className={clsx(
                'flex flex-1 items-center justify-center rounded-br-lg text-tertiary transition-colors',
                'hover:bg-hover hover:text-ink active:bg-hover',
                (disabled || atMin) && 'opacity-30 pointer-events-none',
              )}
            >
              <ChevronDownIcon className={sizeCls.chevron} />
            </button>
          </div>
        )}
      </div>
      {error && (
        <p id={errorId} className="mt-1 text-xs text-red-500">
          {error}
        </p>
      )}
    </div>
  );
};
