import React, { useId } from 'react';
import clsx from 'clsx';

/**
 * Radio — tokenized replacement for the native <input type="radio">.
 *
 * Same rationale as Checkbox: the OS control doesn't respect our tokens or
 * dark mode. This paints an outer ring from `border-border` and an inner dot
 * from `bg-ink`, so it lives inside the DNA. Use bare or with a label; group
 * by giving instances the same `name`.
 *
 * `RadioGroup` is a thin controlled wrapper for the common "list of options"
 * pattern — pass an `options` array and it renders a stack of Radios.
 */
export interface RadioProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'type' | 'size'> {
  label?: React.ReactNode;
  description?: React.ReactNode;
  size?: 'sm' | 'md';
  error?: string;
  wrapperClassName?: string;
  boxClassName?: string;
}

const RING_SIZE = {
  sm: 'h-3.5 w-3.5',
  md: 'h-4 w-4',
} as const;

const DOT_SIZE = {
  sm: 'h-1.5 w-1.5',
  md: 'h-2 w-2',
} as const;

export const Radio: React.FC<RadioProps> = ({
  label,
  description,
  size = 'md',
  error,
  checked,
  disabled,
  className,
  wrapperClassName,
  boxClassName,
  id,
  ...props
}) => {
  const reactId = useId();
  const inputId = id ?? `rad-${reactId}`;
  const errorId = error ? `${inputId}-error` : undefined;

  const dot = (
    <span
      aria-hidden="true"
      className={clsx(
        'relative inline-flex shrink-0 items-center justify-center rounded-full border transition-colors duration-150',
        RING_SIZE[size],
        checked ? 'border-ink' : 'border-border group-hover:border-ink/30',
        error && 'border-red-400',
        disabled && 'opacity-40',
        'bg-surface',
        boxClassName,
      )}
    >
      {checked && <span className={clsx('rounded-full bg-ink', DOT_SIZE[size])} />}
    </span>
  );

  const inputEl = (
    <input
      id={inputId}
      type="radio"
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
          'group inline-flex shrink-0 items-center rounded-full',
          'focus-within:ring-2 focus-within:ring-ink/10 focus-within:ring-offset-1 focus-within:ring-offset-surface',
          disabled ? 'cursor-not-allowed' : 'cursor-pointer',
          wrapperClassName,
          className,
        )}
      >
        {inputEl}
        {dot}
      </label>
    );
  }

  return (
    <div className={clsx('flex flex-col', wrapperClassName)}>
      <label
        htmlFor={inputId}
        className={clsx(
          'group inline-flex items-start gap-2 rounded-md',
          'focus-within:ring-2 focus-within:ring-ink/10 focus-within:ring-offset-1 focus-within:ring-offset-surface',
          disabled ? 'cursor-not-allowed opacity-70' : 'cursor-pointer',
          className,
        )}
      >
        {inputEl}
        <span className="mt-0.5 flex">{dot}</span>
        <span className="min-w-0 flex-1">
          {label && (
            <span
              className={clsx(
                'block text-sm leading-tight',
                checked ? 'text-ink' : 'text-secondary',
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

export interface RadioGroupOption<T extends string | number = string> {
  value: T;
  label: React.ReactNode;
  description?: React.ReactNode;
  disabled?: boolean;
}

export interface RadioGroupProps<T extends string | number = string> {
  name?: string;
  value: T | null | undefined;
  onChange: (value: T) => void;
  options: RadioGroupOption<T>[];
  label?: string;
  error?: string;
  disabled?: boolean;
  size?: 'sm' | 'md';
  orientation?: 'vertical' | 'horizontal';
  className?: string;
  wrapperClassName?: string;
}

export function RadioGroup<T extends string | number = string>({
  name,
  value,
  onChange,
  options,
  label,
  error,
  disabled = false,
  size = 'md',
  orientation = 'vertical',
  className,
  wrapperClassName,
}: RadioGroupProps<T>) {
  const reactId = useId();
  const groupName = name ?? `rgroup-${reactId}`;
  const errorId = error ? `${groupName}-error` : undefined;

  return (
    <div
      role="radiogroup"
      aria-labelledby={label ? `${groupName}-label` : undefined}
      aria-invalid={error ? true : undefined}
      aria-describedby={errorId}
      className={wrapperClassName}
    >
      {label && (
        <div id={`${groupName}-label`} className="mb-1.5 block text-sm text-secondary">
          {label}
        </div>
      )}
      <div
        className={clsx(
          orientation === 'vertical' ? 'flex flex-col gap-2' : 'flex flex-wrap gap-4',
          className,
        )}
      >
        {options.map((opt) => (
          <Radio
            key={String(opt.value)}
            name={groupName}
            value={String(opt.value)}
            checked={value === opt.value}
            onChange={() => onChange(opt.value)}
            disabled={disabled || opt.disabled}
            size={size}
            label={opt.label}
            description={opt.description}
          />
        ))}
      </div>
      {error && (
        <p id={errorId} className="mt-1 text-xs text-red-500">
          {error}
        </p>
      )}
    </div>
  );
}
