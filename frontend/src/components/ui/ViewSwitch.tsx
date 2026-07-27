import { useLayoutEffect, useRef, useState } from 'react';
import clsx from 'clsx';

type IconType = React.ComponentType<{ className?: string }>;

export interface ViewSwitchOption<T extends string> {
  id: T;
  label: string;
  /** Optional leading icon (e.g. laptop for Local, cloud for Cloud). */
  icon?: IconType;
  /** Optional trailing count (e.g. the Steps tab's step count). */
  count?: number;
  /** Optional `data-tour` hook for the onboarding tour spotlight. */
  dataTour?: string;
}

interface ViewSwitchProps<T extends string> {
  value: T;
  onChange: (v: T) => void;
  options: ViewSwitchOption<T>[];
  className?: string;
}

/**
 * ViewSwitch — the primary view switcher, rendered in the app's in-topbar tab
 * idiom. The topbar is `chrome`, and `bg-hover` (#EAEAEC) is nearly identical to
 * `chrome` (#EDEBE8) — a hover-filled active segment was invisible there. So the
 * active segment is a raised `surface` (white) pill with a soft shadow: it reads
 * as a control CARDED onto the chrome toolbar (the same reason the sort/Select
 * pills use a white fill). Inactive segments stay weightless text; their hover
 * uses a translucent white so it registers on chrome too.
 *
 * The thumb is measured on BOTH axes from the active button, not stretched with
 * `inset-y-0`. That mattered: `inset-y-0` resolves against the track's PADDING BOX, so
 * the thumb ignored a caller's `p-0.5` vertically while `offsetLeft` respected it
 * horizontally. Flush against a `rounded-lg` track, the thumb's smaller `rounded-md`
 * corners collided with the track's — the active segment read as a SQUARE block inside a
 * rounded pill. (Note this design system redefines the radius scale — `sm`/DEFAULT/`lg`/`xl`
 * = 8/12/16/24px — but leaves `md` at Tailwind's stock 6px.) Measuring the button keeps the
 * inset equal on all four sides, and deriving the radius as `outer − inset` keeps the
 * thumb's curve parallel to whatever track it is dropped into.
 *
 * The pill is a single SLIDING THUMB (native segmented-control feel): one
 * absolutely-positioned span glides between segments on the expo curve instead
 * of a bg/shadow snapping from one button to the next. It's measured from the
 * active button (offsetLeft/offsetWidth) and re-measured by a ResizeObserver,
 * so label changes, container reveals (display:none → block in kept-alive
 * pages) and window resizes all self-correct. While the control is hidden the
 * thumb unmounts (measure reads 0), and on reveal it re-mounts already at its
 * final position — mounting never plays a slide.
 *
 * (Distinct from the tiny inline {@link ModeToggle} used for Table/Raw data
 * sub-views, which intentionally stays a bordered micro-control.)
 */
export function ViewSwitch<T extends string>({ value, onChange, options, className }: ViewSwitchProps<T>) {
  const listRef = useRef<HTMLDivElement>(null);
  const btnRefs = useRef<Map<string, HTMLButtonElement>>(new Map());
  const [thumb, setThumb] = useState<
    { left: number; width: number; top: number; height: number; radius: number | null } | null
  >(null);

  useLayoutEffect(() => {
    const measure = () => {
      const btn = btnRefs.current.get(value);
      if (!btn || btn.offsetWidth === 0) {
        setThumb(null);
        return;
      }
      // BOTH axes come from the button. `offsetTop`/`offsetLeft` are measured from the
      // track's padding box, which is also what `absolute` resolves against — so the thumb
      // lands exactly on the active button whatever padding the caller's track has.
      //
      // The radius follows the TRACK the caller passed, because nested corners have to be
      // concentric: inner = outer − inset. A fixed `rounded-md` (6px) inside a 16px
      // `rounded-lg` track read as a square block in a rounded pill. Deriving it means any
      // track — 8px, 16px, a full pill — gets a thumb whose curve is parallel to it.
      // `null` when the track has no radius of its own (the trackless topbar usages),
      // where the flat `rounded-md` default is already right.
      const trackRadius = parseFloat(getComputedStyle(btn.parentElement!).borderTopLeftRadius) || 0;
      setThumb({
        left: btn.offsetLeft,
        width: btn.offsetWidth,
        top: btn.offsetTop,
        height: btn.offsetHeight,
        radius: trackRadius > 0 ? Math.max(0, trackRadius - btn.offsetTop) : null,
      });
    };
    measure();
    const ro = new ResizeObserver(measure);
    if (listRef.current) ro.observe(listRef.current);
    return () => ro.disconnect();
  }, [value, options]);

  return (
    <div
      ref={listRef}
      role="tablist"
      aria-orientation="horizontal"
      className={clsx('relative inline-flex items-center gap-0.5', className)}
    >
      {thumb && (
        <span
          aria-hidden="true"
          className="absolute left-0 top-0 rounded-md bg-surface shadow-sm transition-[transform,width] duration-base ease-out"
          style={{
            width: thumb.width,
            height: thumb.height,
            transform: `translate(${thumb.left}px, ${thumb.top}px)`,
            ...(thumb.radius !== null && { borderRadius: thumb.radius }),
          }}
        />
      )}
      {options.map((o) => {
        const active = value === o.id;
        const Icon = o.icon;
        return (
          <button
            key={o.id}
            ref={(el) => {
              if (el) btnRefs.current.set(o.id, el);
              else btnRefs.current.delete(o.id);
            }}
            type="button"
            role="tab"
            aria-selected={active}
            // Same derived radius as the thumb: the inactive hover fill
            // (`hover:bg-surface/60`) is drawn on THIS element, so a fixed 6px would put a
            // square hover block inside the rounded track exactly like the thumb did.
            style={thumb?.radius != null ? { borderRadius: thumb.radius } : undefined}
            data-tour={o.dataTour}
            onClick={(e) => {
              e.stopPropagation();
              onChange(o.id);
            }}
            className={clsx(
              'relative inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[12px] font-medium transition-colors whitespace-nowrap',
              'outline-none focus-visible:ring-2 focus-visible:ring-ink/40',
              active ? 'text-ink' : 'text-tertiary hover:text-secondary hover:bg-surface/60',
            )}
          >
            {Icon && <Icon className={clsx('h-3.5 w-3.5 transition-colors', active ? 'text-ink' : 'text-tertiary')} />}
            {o.label}
            {o.count !== undefined && (
              <span className={clsx('text-[10px] tabular-nums transition-colors', active ? 'text-secondary' : 'text-tertiary')}>
                {o.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

export default ViewSwitch;
