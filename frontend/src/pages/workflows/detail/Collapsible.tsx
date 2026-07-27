import React, { useState } from 'react';
import clsx from 'clsx';
import { ChevronRightIcon } from '@heroicons/react/24/outline';

/**
 * Collapsible "Advanced" section for the detail page. Header reads like the
 * standard Section (uppercase title + one-line description) but toggles its body.
 *
 * Children are kept mounted once first opened (so form state / queries inside
 * survive a collapse) but are NOT mounted until the first expand — heavy bodies
 * (a data table, a script editor) stay cheap when the section is closed.
 */
export const Collapsible: React.FC<{
  title: string;
  description?: string;
  /** Open on first render (e.g. arrived via a deep-link to this section). */
  defaultOpen?: boolean;
  /** Controlled-open override — when it flips to true the section opens. */
  open?: boolean;
  right?: React.ReactNode;
  /** `data-tour` hook so the guided walkthrough can spotlight this section. */
  anchor?: string;
  children: React.ReactNode;
}> = ({ title, description, defaultOpen = false, open, right, anchor, children }) => {
  const [internalOpen, setInternalOpen] = useState(defaultOpen || !!open);
  const [everOpened, setEverOpened] = useState(defaultOpen || !!open);

  // Let a parent force the section open (deep-link) without taking over control.
  // Adjusted DURING render off the previous value of `open` rather than from an
  // effect: an effect would paint the section closed for one frame first, and
  // the deep-link lands mid-scroll where that reads as a flicker.
  const [wasForced, setWasForced] = useState(!!open);
  if (wasForced !== !!open) {
    setWasForced(!!open);
    if (open) {
      setInternalOpen(true);
      setEverOpened(true);
    }
  }

  const toggle = () => {
    setInternalOpen((o) => {
      if (!o) setEverOpened(true);
      return !o;
    });
  };

  return (
    <div data-tour={anchor} className="border border-ink/20 rounded-xl bg-surface overflow-hidden shadow-sm">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={internalOpen}
        className="w-full flex items-center gap-2.5 px-4 py-3 text-left hover:bg-chrome transition-colors"
      >
        <ChevronRightIcon
          className={clsx('w-3.5 h-3.5 text-tertiary shrink-0 transition-transform', internalOpen && 'rotate-90')}
        />
        <div className="min-w-0 flex-1">
          <h2 className="text-xs font-semibold text-secondary uppercase tracking-wider">{title}</h2>
          {description && <p className="text-[11px] text-tertiary mt-0.5">{description}</p>}
        </div>
        {right}
      </button>
      {internalOpen && (
        <div className="px-4 pb-4 pt-1 border-t border-border space-y-6">
          {everOpened ? children : null}
        </div>
      )}
    </div>
  );
};

export default Collapsible;
