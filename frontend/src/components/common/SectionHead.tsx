import React from 'react';

/**
 * SectionHead — the canonical de-carded section header.
 *
 * A scannable title + one-line description (the page-intent text belongs HERE
 * or in the toolbar subtitle — never as a loose floating <p> above content),
 * with an optional right-aligned action/meta slot. Pair it with the section
 * rhythm convention used across content pages: each <section> separated by a
 * hairline rule + even vertical padding (e.g. `pt-7 mt-7 border-t border-border`,
 * first section without the top rule) instead of stacking boxed cards.
 *
 * Reserve real card containers (`bg-surface border rounded-xl`) for tabular /
 * list data inside a section — not for the section itself.
 */
export const SectionHead: React.FC<{
  title: string;
  description?: string;
  right?: React.ReactNode;
}> = ({ title, description, right }) => (
  <div className="flex items-start justify-between gap-4 mb-4">
    <div className="min-w-0">
      <h2 className="text-base font-semibold text-ink">{title}</h2>
      {description && (
        <p className="text-[13px] text-secondary mt-1 max-w-2xl leading-relaxed">{description}</p>
      )}
    </div>
    {right && <div className="flex items-center gap-2 shrink-0">{right}</div>}
  </div>
);

export default SectionHead;
