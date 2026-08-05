import React, { useId } from 'react';
import clsx from 'clsx';

/**
 * Switch — tokenized on/off toggle (role="switch") for binary settings where a
 * sliding switch reads better than a checkbox (enable/disable a feature,
 * headless mode, autoscale…).
 *
 * Why it exists: the app grew ~a dozen bespoke `<button>` toggles, several with
 * hardcoded `bg-zinc-*` / `bg-surface` that don't invert in dark mode. This one
 * paints from the same tokens as the rest of the DNA (`bg-ink` on, `bg-border`
 * off, `bg-surface` knob) and inherits the theme.
 *
 * `onChange` receives the NEXT boolean value; callers that just want to flip a
 * flag can ignore the argument. Pass `label`/`description` for the common
 * "row with a switch on the right" pattern (the whole row toggles).
 */
export interface SwitchProps {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  label?: React.ReactNode;
  description?: React.ReactNode;
  size?: 'sm' | 'md';
  /** Put the label on the left and the switch on the right (row layout). */
  reverse?: boolean;
  className?: string;
  wrapperClassName?: string;
  id?: string;
  name?: string;
  title?: string;
  /**
   * Status color of the ON state. Default is monochrome `bg-ink` (the DNA
   * baseline). Use a semantic tone ONLY when the on/off state itself carries
   * meaning at a glance — e.g. maintenance mode (danger), a service being
   * enabled (success) — so color stays "status only", never decorative.
   */
  tone?: 'default' | 'success' | 'danger' | 'warning';
  'aria-label'?: string;
}

const SIZES = {
  sm: { track: 'h-5 w-9', knob: 'h-4 w-4', on: 'translate-x-4', off: 'translate-x-0.5' },
  md: { track: 'h-6 w-11', knob: 'h-5 w-5', on: 'translate-x-5', off: 'translate-x-0.5' },
} as const;

// ON-state track color + matching focus ring per tone. Off state is always the
// neutral `bg-border` so an off switch reads the same regardless of tone.
const TONE = {
  default: { on: 'bg-ink', ring: 'focus-visible:ring-ink/30' },
  success: { on: 'bg-green-500', ring: 'focus-visible:ring-green-500/30' },
  danger: { on: 'bg-red-500', ring: 'focus-visible:ring-red-500/30' },
  warning: { on: 'bg-amber-500', ring: 'focus-visible:ring-amber-500/30' },
} as const;

export const Switch: React.FC<SwitchProps> = ({
  checked,
  onChange,
  disabled = false,
  label,
  description,
  size = 'md',
  reverse = false,
  className,
  wrapperClassName,
  id,
  name,
  title,
  tone = 'default',
  'aria-label': ariaLabel,
}) => {
  const reactId = useId();
  const baseId = id ?? `switch-${reactId}`;
  const labelId = label ? `${baseId}-label` : undefined;
  const s = SIZES[size];
  const t = TONE[tone];

  const control = (
    <button
      type="button"
      role="switch"
      id={baseId}
      name={name}
      aria-checked={checked}
      aria-labelledby={label ? labelId : undefined}
      aria-label={!label ? ariaLabel : undefined}
      title={title}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={clsx(
        'relative inline-flex shrink-0 items-center rounded-full transition-colors duration-200',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 focus-visible:ring-offset-surface',
        t.ring,
        s.track,
        checked ? t.on : 'bg-border',
        disabled ? 'cursor-not-allowed opacity-40' : 'cursor-pointer',
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={clsx(
          'inline-block transform rounded-full bg-surface shadow-sm transition-transform duration-200',
          s.knob,
          checked ? s.on : s.off,
        )}
      />
    </button>
  );

  if (!label && !description) {
    return wrapperClassName ? <span className={wrapperClassName}>{control}</span> : control;
  }

  return (
    <div
      className={clsx(
        'flex items-start gap-3',
        reverse && 'flex-row-reverse justify-between',
        wrapperClassName,
      )}
    >
      {control}
      <label
        htmlFor={baseId}
        id={labelId}
        className={clsx('min-w-0 flex-1', disabled ? 'cursor-not-allowed opacity-70' : 'cursor-pointer')}
        onClick={(e) => {
          // The <label htmlFor> would toggle a form control, but our switch is a
          // <button>; forward the intent manually.
          e.preventDefault();
          if (!disabled) onChange(!checked);
        }}
      >
        {label && (
          <span className={clsx('block text-sm leading-tight', checked ? 'text-ink' : 'text-secondary')}>
            {label}
          </span>
        )}
        {description && <span className="mt-0.5 block text-xs text-tertiary">{description}</span>}
      </label>
    </div>
  );
};
