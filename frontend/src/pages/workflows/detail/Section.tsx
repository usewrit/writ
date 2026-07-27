import React from 'react';

/**
 * Standard detail-page section: uppercase header, optional one-line explanation,
 * optional right-side action, then the content. Keeps every tab scannable the
 * same way.
 */
export const Section: React.FC<{
  title: string;
  description?: string;
  right?: React.ReactNode;
  /** `data-tour` hook so the guided walkthrough can spotlight this section. */
  anchor?: string;
  children: React.ReactNode;
}> = ({ title, description, right, anchor, children }) => (
  <div data-tour={anchor}>
    <div className="flex items-center justify-between mb-0.5">
      <h2 className="text-xs font-semibold text-secondary uppercase tracking-wider">{title}</h2>
      {right}
    </div>
    {description && <p className="text-[11px] text-tertiary mb-2">{description}</p>}
    {!description && <div className="mb-2" />}
    {children}
  </div>
);
