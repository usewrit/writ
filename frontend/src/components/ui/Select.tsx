import React, {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';
import clsx from 'clsx';
import { CheckIcon, ChevronUpDownIcon } from '@heroicons/react/20/solid';

/**
 * Select — a custom, accessible dropdown that replaces the native <select>.
 *
 * Why it exists: native <select> popups can't be themed (the menu is OS-drawn),
 * so they break our design DNA — especially in dark mode, where the OS menu
 * stays light. This component renders an in-app menu styled from the same tokens
 * as Input/Button (surface / ink / border / hover / accent) and inherits the
 * theme automatically.
 *
 * Design notes:
 *  - The trigger mirrors <Input>: same radius, border, focus ring and sizing
 *    ramp, so a Select sits flush next to text inputs in a form.
 *  - The menu is portalled to <body> and positioned with fixed coordinates from
 *    the trigger's rect, so it never gets clipped inside modals or scroll areas
 *    and flips above the trigger when there isn't room below.
 *  - Full keyboard + ARIA listbox semantics: open with Enter/Space/↑/↓, navigate
 *    with arrows/Home/End, type-ahead to jump, Esc/Tab to close. Focus stays on a
 *    roving `aria-activedescendant` so screen readers announce each option.
 *
 * Generic over the value type so callers get typed values back (no manual
 * Number() coercion the way native onChange forced).
 */

export type SelectValue = string | number;

export interface SelectOption<T extends SelectValue = SelectValue> {
  value: T;
  label: React.ReactNode;
  /** Optional secondary line rendered under the label inside the menu. */
  description?: string;
  /** Optional leading glyph shown in the trigger and the menu row. */
  icon?: React.ReactNode;
  disabled?: boolean;
}

export interface SelectOptionGroup<T extends SelectValue = SelectValue> {
  label: string;
  options: SelectOption<T>[];
}

type SelectItem<T extends SelectValue> = SelectOption<T> | SelectOptionGroup<T>;

function isGroup<T extends SelectValue>(item: SelectItem<T>): item is SelectOptionGroup<T> {
  return Array.isArray((item as SelectOptionGroup<T>).options);
}

export interface SelectProps<T extends SelectValue = SelectValue> {
  value: T | null | undefined;
  onChange: (value: T) => void;
  options: SelectItem<T>[];
  placeholder?: string;
  /** Visible field label (wired to the trigger like <Input label>). */
  label?: string;
  /** Error message; also turns the trigger border red. */
  error?: string;
  disabled?: boolean;
  size?: 'sm' | 'md';
  /** Extra classes merged onto the trigger button (e.g. width). */
  className?: string;
  /** Classes for the outer wrapper (label + trigger). */
  wrapperClassName?: string;
  /** Extra classes for the floating menu panel. */
  menuClassName?: string;
  id?: string;
  name?: string;
  'aria-label'?: string;
}

interface MenuPosition {
  left: number;
  width: number;
  top?: number;
  bottom?: number;
  maxHeight: number;
}

const SIZES = {
  sm: { trigger: 'px-3 py-1.5 text-xs gap-2', chevron: 'h-4 w-4' },
  md: { trigger: 'px-4 py-2.5 text-sm gap-2', chevron: 'h-5 w-5' },
} as const;

export function Select<T extends SelectValue = SelectValue>({
  value,
  onChange,
  options,
  placeholder = 'Select…',
  label,
  error,
  disabled = false,
  size = 'md',
  className,
  wrapperClassName,
  menuClassName,
  id,
  name,
  'aria-label': ariaLabel,
}: SelectProps<T>) {
  const reactId = useId();
  const baseId = id ?? `select-${reactId}`;
  const labelId = label ? `${baseId}-label` : undefined;
  const errorId = error ? `${baseId}-error` : undefined;

  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const optionRefs = useRef<(HTMLDivElement | null)[]>([]);
  const typeahead = useRef<{ query: string; timer: number | null }>({ query: '', timer: null });

  const [open, setOpen] = useState(false);
  const [entered, setEntered] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [menu, setMenu] = useState<MenuPosition | null>(null);

  // Flatten groups into a single ordered list so keyboard navigation, the
  // active index, and option ids all index off the same sequence the menu
  // renders in.
  const flat = useMemo(() => {
    const out: SelectOption<T>[] = [];
    for (const item of options) {
      if (isGroup(item)) out.push(...item.options);
      else out.push(item);
    }
    return out;
  }, [options]);

  const selected = useMemo(
    () => (value === null || value === undefined ? null : flat.find((o) => o.value === value) ?? null),
    [flat, value],
  );

  const firstEnabled = useCallback(() => flat.findIndex((o) => !o.disabled), [flat]);

  // ---- positioning -------------------------------------------------------
  const updatePosition = useCallback(() => {
    const el = triggerRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const margin = 8;
    const spaceBelow = window.innerHeight - rect.bottom - margin;
    const spaceAbove = rect.top - margin;
    const flipUp = spaceBelow < 220 && spaceAbove > spaceBelow;
    const maxHeight = Math.min(320, Math.max(160, (flipUp ? spaceAbove : spaceBelow)));
    setMenu({
      left: rect.left,
      width: rect.width,
      top: flipUp ? undefined : rect.bottom + 6,
      bottom: flipUp ? window.innerHeight - rect.top + 6 : undefined,
      maxHeight,
    });
  }, []);

  // Exit plays the entrance in reverse (faster), THEN unmounts. A menu that
  // eases in but vanishes in one frame reads as a glitch — the 100ms fade-out
  // below (unmount at 110ms) is the missing half of the transition.
  const closeTimer = useRef<number | null>(null);
  const close = useCallback(() => {
    setEntered(false);
    if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    closeTimer.current = window.setTimeout(() => {
      closeTimer.current = null;
      setOpen(false);
    }, 110);
  }, []);
  useEffect(
    () => () => {
      if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    },
    [],
  );

  // Bumped on every open so the enter effect re-arms even when re-opening
  // mid-exit (open never flipped false in that window).
  const [openTick, setOpenTick] = useState(0);
  const openMenu = useCallback(() => {
    if (disabled) return;
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
    updatePosition();
    const sel = selected ? flat.indexOf(selected) : -1;
    setActiveIndex(sel >= 0 && !flat[sel]?.disabled ? sel : firstEnabled());
    setOpen(true);
    setOpenTick((t) => t + 1);
  }, [disabled, updatePosition, selected, flat, firstEnabled]);

  // Recompute on open and keep glued to the trigger while scrolling/resizing.
  useLayoutEffect(() => {
    if (!open) return;
    updatePosition();
    const onScrollOrResize = () => updatePosition();
    window.addEventListener('scroll', onScrollOrResize, true);
    window.addEventListener('resize', onScrollOrResize);
    return () => {
      window.removeEventListener('scroll', onScrollOrResize, true);
      window.removeEventListener('resize', onScrollOrResize);
    };
  }, [open, updatePosition]);

  // Trigger the enter transition one frame after mount, and move focus into the
  // listbox so arrow keys work immediately.
  useEffect(() => {
    if (!open) return;
    const raf = requestAnimationFrame(() => {
      setEntered(true);
      listRef.current?.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(raf);
  }, [open, openTick]);

  // Close on outside pointer down.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target as Node;
      if (triggerRef.current?.contains(target) || listRef.current?.contains(target)) return;
      close();
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    return () => document.removeEventListener('pointerdown', onPointerDown, true);
  }, [open, close]);

  // Keep the active option scrolled into view.
  useEffect(() => {
    if (!open || activeIndex < 0) return;
    optionRefs.current[activeIndex]?.scrollIntoView({ block: 'nearest' });
  }, [open, activeIndex]);

  const commit = useCallback(
    (index: number) => {
      const opt = flat[index];
      if (!opt || opt.disabled) return;
      onChange(opt.value);
      close();
      triggerRef.current?.focus({ preventScroll: true });
    },
    [flat, onChange, close],
  );

  const moveActive = useCallback(
    (dir: 1 | -1) => {
      setActiveIndex((prev) => {
        if (!flat.length) return prev;
        let i = prev < 0 ? (dir === 1 ? -1 : 0) : prev;
        for (let n = 0; n < flat.length; n++) {
          i = (i + dir + flat.length) % flat.length;
          if (!flat[i].disabled) return i;
        }
        return prev;
      });
    },
    [flat],
  );

  const edgeActive = useCallback(
    (edge: 'first' | 'last') => {
      if (edge === 'first') setActiveIndex(firstEnabled());
      else {
        for (let i = flat.length - 1; i >= 0; i--) {
          if (!flat[i].disabled) {
            setActiveIndex(i);
            return;
          }
        }
      }
    },
    [flat, firstEnabled],
  );

  const runTypeahead = useCallback(
    (char: string) => {
      const ta = typeahead.current;
      if (ta.timer) window.clearTimeout(ta.timer);
      ta.query += char.toLowerCase();
      ta.timer = window.setTimeout(() => (ta.query = ''), 600);
      const match = flat.findIndex(
        (o) => !o.disabled && typeof o.label === 'string' && o.label.toLowerCase().startsWith(ta.query),
      );
      if (match >= 0) setActiveIndex(match);
    },
    [flat],
  );

  const onTriggerKeyDown = (e: React.KeyboardEvent) => {
    if (disabled) return;
    if (open) return; // the list handles keys while open
    if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) {
      e.preventDefault();
      openMenu();
    }
  };

  const onListKeyDown = (e: React.KeyboardEvent) => {
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        moveActive(1);
        break;
      case 'ArrowUp':
        e.preventDefault();
        moveActive(-1);
        break;
      case 'Home':
        e.preventDefault();
        edgeActive('first');
        break;
      case 'End':
        e.preventDefault();
        edgeActive('last');
        break;
      case 'Enter':
      case ' ':
        e.preventDefault();
        if (activeIndex >= 0) commit(activeIndex);
        break;
      case 'Escape':
        e.preventDefault();
        close();
        triggerRef.current?.focus({ preventScroll: true });
        break;
      case 'Tab':
        close();
        break;
      default:
        if (e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey) {
          runTypeahead(e.key);
        }
    }
  };

  const sizeCls = SIZES[size];

  return (
    <div className={clsx(wrapperClassName)}>
      {label && (
        <label id={labelId} className="block text-sm text-secondary mb-1.5">
          {label}
        </label>
      )}
      <button
        ref={triggerRef}
        type="button"
        id={baseId}
        name={name}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-labelledby={label ? labelId : undefined}
        aria-label={!label ? ariaLabel : undefined}
        aria-invalid={error ? true : undefined}
        aria-describedby={errorId}
        onClick={() => (open ? close() : openMenu())}
        onKeyDown={onTriggerKeyDown}
        className={clsx(
          'inline-flex w-full items-center justify-between rounded-lg border bg-surface text-left text-ink',
          'transition-all duration-200 focus:outline-none',
          sizeCls.trigger,
          error
            ? 'border-red-300 focus:ring-2 focus:ring-red-500/10'
            : 'border-border focus:border-ink/30 focus:ring-2 focus:ring-ink/5 focus:shadow-sm',
          open && !error && 'border-ink/30 ring-2 ring-ink/5 shadow-sm',
          disabled ? 'cursor-not-allowed opacity-40' : 'cursor-pointer hover:border-ink/20',
          className,
        )}
      >
        <span className={clsx('flex min-w-0 items-center gap-2', !selected && 'text-tertiary')}>
          {selected?.icon && <span className="flex shrink-0 items-center">{selected.icon}</span>}
          <span className="truncate">{selected ? selected.label : placeholder}</span>
        </span>
        <ChevronUpDownIcon
          aria-hidden="true"
          className={clsx('ml-2 shrink-0 text-tertiary', sizeCls.chevron)}
        />
      </button>
      {error && (
        <p id={errorId} className="mt-1 text-xs text-red-500">
          {error}
        </p>
      )}

      {open &&
        menu &&
        createPortal(
          <div
            ref={listRef}
            role="listbox"
            tabIndex={-1}
            aria-labelledby={label ? labelId : undefined}
            aria-label={!label ? ariaLabel : undefined}
            aria-activedescendant={activeIndex >= 0 ? `${baseId}-opt-${activeIndex}` : undefined}
            onKeyDown={onListKeyDown}
            style={{
              position: 'fixed',
              left: menu.left,
              width: menu.width,
              top: menu.top,
              bottom: menu.bottom,
              maxHeight: menu.maxHeight,
            }}
            className={clsx(
              'z-[60] overflow-y-auto rounded-lg border border-border bg-surface p-1 shadow-lg',
              'origin-top transition-[opacity,transform] focus:outline-none',
              menu.bottom !== undefined && 'origin-bottom',
              // Asymmetric timing: enter settles on the expo curve, exit is a
              // quicker ease-in (and inert to clicks while leaving).
              entered
                ? 'duration-150 ease-out opacity-100 scale-100 translate-y-0'
                : 'duration-100 ease-in pointer-events-none opacity-0 scale-[0.98] -translate-y-1',
              menuClassName,
            )}
          >
            {(() => {
              let idx = -1;
              const renderOption = (opt: SelectOption<T>) => {
                idx += 1;
                const optionIndex = idx;
                const isSelected = selected === opt;
                const isActive = optionIndex === activeIndex;
                return (
                  <div
                    key={`${String(opt.value)}-${optionIndex}`}
                    ref={(el) => (optionRefs.current[optionIndex] = el)}
                    id={`${baseId}-opt-${optionIndex}`}
                    role="option"
                    aria-selected={isSelected}
                    aria-disabled={opt.disabled || undefined}
                    onPointerEnter={() => !opt.disabled && setActiveIndex(optionIndex)}
                    onClick={() => commit(optionIndex)}
                    className={clsx(
                      'flex cursor-pointer items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors duration-100',
                      opt.disabled && 'cursor-not-allowed opacity-40',
                      !opt.disabled && isActive && 'bg-chrome',
                      isSelected ? 'text-ink' : 'text-secondary',
                    )}
                  >
                    {opt.icon && <span className="flex shrink-0 items-center text-tertiary">{opt.icon}</span>}
                    <span className="min-w-0 flex-1">
                      <span className={clsx('block truncate', isSelected && 'font-medium text-ink')}>
                        {opt.label}
                      </span>
                      {opt.description && (
                        <span className="mt-0.5 block truncate text-xs text-tertiary">{opt.description}</span>
                      )}
                    </span>
                    {isSelected && <CheckIcon aria-hidden="true" className="h-4 w-4 shrink-0 text-ink" />}
                  </div>
                );
              };

              return options.map((item, i) =>
                isGroup(item) ? (
                  <div key={`group-${i}`} role="group" aria-label={item.label} className="py-1 first:pt-0">
                    <div className="px-3 pb-1 pt-1 text-[11px] font-medium uppercase tracking-wide text-tertiary">
                      {item.label}
                    </div>
                    {item.options.map(renderOption)}
                  </div>
                ) : (
                  renderOption(item)
                ),
              );
            })()}
          </div>,
          document.body,
        )}
    </div>
  );
}
