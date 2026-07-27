import clsx from 'clsx';

interface ModeToggleProps<T extends string> {
  value: T;
  onChange: (v: T) => void;
  options: { id: T; label: string }[];
}

/** A tiny segmented control for switching a value's view mode (Table/Raw, Rendered/Raw). */
export function ModeToggle<T extends string>({ value, onChange, options }: ModeToggleProps<T>) {
  return (
    <div className="inline-flex overflow-hidden rounded-md border border-border">
      {options.map((o, idx) => (
        <button
          key={o.id}
          onClick={(e) => {
            e.stopPropagation();
            onChange(o.id);
          }}
          className={clsx(
            'px-2 py-0.5 text-[10px] font-medium transition-colors',
            idx > 0 && 'border-l border-border',
            value === o.id ? 'bg-hover text-ink' : 'text-tertiary hover:text-secondary',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export default ModeToggle;
