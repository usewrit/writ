import React, { useState, useRef, useEffect } from 'react';
import { createPortal } from 'react-dom';
import clsx from 'clsx';

interface PopoverProps {
  /** Trigger content; clicking it toggles the panel. */
  trigger: React.ReactNode;
  triggerClassName?: string;
  /** Whether the trigger should look "active" (e.g. a filter is set). */
  active?: boolean;
  width?: number;
  children: (close: () => void) => React.ReactNode;
}

/**
 * A small portal popover: opens a viewport-clamped panel below its trigger,
 * closes on outside-click or Escape. Used for the grid's per-column menus.
 */
export const Popover: React.FC<PopoverProps> = ({
  trigger,
  triggerClassName,
  active,
  width = 264,
  children,
}) => {
  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ top: 0, left: 0 });

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (
        panelRef.current &&
        !panelRef.current.contains(e.target as Node) &&
        !btnRef.current?.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    };
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onClick);
    document.addEventListener('keydown', onEsc);
    return () => {
      document.removeEventListener('mousedown', onClick);
      document.removeEventListener('keydown', onEsc);
    };
  }, [open]);

  const toggle = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!open && btnRef.current) {
      const rect = btnRef.current.getBoundingClientRect();
      const left = Math.min(Math.max(8, rect.left), window.innerWidth - width - 8);
      setPos({ top: rect.bottom + 4, left });
    }
    setOpen((v) => !v);
  };

  return (
    <>
      <button
        ref={btnRef}
        onClick={toggle}
        className={clsx(
          'inline-flex items-center justify-center rounded transition-colors',
          active ? 'text-ink' : 'text-tertiary hover:text-ink',
          triggerClassName,
        )}
      >
        {trigger}
      </button>
      {open &&
        createPortal(
          <div
            ref={panelRef}
            style={{ position: 'fixed', top: pos.top, left: pos.left, width }}
            className="z-[9999] origin-top animate-fade-in-scale rounded-xl border border-border bg-surface p-2 shadow-lg"
          >
            {children(() => setOpen(false))}
          </div>,
          document.body,
        )}
    </>
  );
};

export default Popover;
