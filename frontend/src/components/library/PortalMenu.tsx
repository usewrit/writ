import React, { useState, useRef, useEffect } from 'react';
import { createPortal } from 'react-dom';
import clsx from 'clsx';

interface PortalMenuProps {
  trigger: React.ReactNode;
  children: (close: () => void) => React.ReactNode;
}

export function PortalMenu({ trigger, children }: PortalMenuProps) {
  const [open, setOpen] = useState(false);
  const [animIn, setAnimIn] = useState(false);
  const [closing, setClosing] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ top: 0, left: 0 });

  // Handle open/close with animation. The flags flip off the previous `open`
  // DURING render — from an effect the portal would mount a commit late, and
  // `animIn` would still be true on the first closed frame, so the leave
  // transition would have nothing to run from.
  const [wasOpen, setWasOpen] = useState(open);
  if (wasOpen !== open) {
    setWasOpen(open);
    setAnimIn(false); // every enter starts from the closed style
    setClosing(!open);
  }

  const shouldRender = open || closing;

  // Two frames after the closed style is committed, flip to the open style.
  useEffect(() => {
    if (!open) return;
    let inner = 0;
    const outer = requestAnimationFrame(() => {
      inner = requestAnimationFrame(() => setAnimIn(true));
    });
    return () => {
      cancelAnimationFrame(outer);
      cancelAnimationFrame(inner);
    };
  }, [open]);

  // Unmount the portal once the leave transition has run.
  useEffect(() => {
    if (!closing) return;
    const timer = setTimeout(() => setClosing(false), 150);
    return () => clearTimeout(timer);
  }, [closing]);

  useEffect(() => {
    if (!open) return;
    const handleClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node) && !btnRef.current?.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const handleEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', handleClick);
    document.addEventListener('keydown', handleEsc);
    return () => { document.removeEventListener('mousedown', handleClick); document.removeEventListener('keydown', handleEsc); };
  }, [open]);

  const handleToggle = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!open && btnRef.current) {
      const rect = btnRef.current.getBoundingClientRect();
      setPos({ top: rect.bottom + 4, left: rect.right });
    }
    setOpen(!open);
  };

  return (
    <>
      <button ref={btnRef} onClick={handleToggle} className="p-1.5 rounded-lg hover:bg-hover text-secondary hover:text-ink transition-all duration-150">
        {trigger}
      </button>
      {shouldRender && createPortal(
        <div
          ref={menuRef}
          style={{ position: 'fixed', top: pos.top, left: pos.left, transform: 'translateX(-100%)' }}
          className={clsx(
            'w-44 bg-surface rounded-xl border border-border shadow-lg z-[9999] p-1',
            'transition-all duration-150 ease-out origin-top-right',
            animIn
              ? 'opacity-100 scale-100'
              : 'opacity-0 scale-95',
          )}
        >
          {children(() => setOpen(false))}
        </div>,
        document.body
      )}
    </>
  );
}
