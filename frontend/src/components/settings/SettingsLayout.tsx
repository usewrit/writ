import React from 'react';
import clsx from 'clsx';

/**
 * Settings layout kit — the structure every Settings tab is built from.
 *
 * WHY THIS EXISTS. Each tab had hand-rolled its own page frame: its own
 * `px-4 py-6 sm:px-6` + `mx-auto max-w-2xl`, and its own
 * `divide-y divide-border overflow-hidden rounded-xl border border-border
 * bg-surface` box around the rows. Twelve of those boxes across the section.
 * That box is the problem — `SectionHead`'s own contract says card containers
 * are for tabular / list data INSIDE a section, never for the section frame
 * itself. Wrapping every group in a bordered card is the generic-SaaS settings
 * look, and it is why this surface read as a different product from the rest of
 * the app, where detail panes are de-carded and separated by hairlines.
 *
 * So: no boxes. A group is a title plus hairline-separated rows, and the rhythm
 * between groups carries the structure. Same job, one less frame.
 *
 * Also fixes an inverted type ramp. Rows used to set the LABEL at
 * `text-xs text-secondary` and the DESCRIPTION at `text-[13px] text-tertiary` —
 * the description outsized the thing it described. Here the label is ink and
 * larger; the description is the quiet one.
 */

/** Page frame for a tab body: one measure, one rhythm, applied everywhere. */
export const SettingsPane: React.FC<{ children: React.ReactNode; className?: string }> = ({
  children,
  className,
}) => (
  <div className="px-5 py-7 sm:px-8">
    <div className={clsx('mx-auto max-w-3xl space-y-9', className)}>{children}</div>
  </div>
);

/**
 * A titled group of settings rows. De-carded: the optional title sits above a
 * hairline-ruled stack, with no surrounding border or fill.
 *
 * Pass `title` for a sub-group inside a tab that already has a `SectionHead`.
 * Omit it when the tab's `SectionHead` is the only heading needed.
 */
export const SettingGroup: React.FC<{
  title?: string;
  description?: string;
  children: React.ReactNode;
  className?: string;
}> = ({ title, description, children, className }) => (
  <section className={className}>
    {title && (
      <div className="mb-1">
        <h3 className="text-[11px] font-semibold uppercase tracking-[0.07em] text-tertiary">{title}</h3>
        {description && <p className="mt-1 text-[12.5px] leading-relaxed text-tertiary">{description}</p>}
      </div>
    )}
    {/* The top rule opens the group and each row carries its own — so a group
        reads as a ruled ledger rather than a boxed card. */}
    <div className="border-t border-border">{children}</div>
  </section>
);

/**
 * One setting: label + optional description on the left, its control on the
 * right. `control` is given `shrink-0` room so a long description never squeezes
 * a select or a switch into wrapping.
 *
 * `stack` drops the control onto its own line below the text — for controls that
 * are genuinely wide (a path input, a key field, a row of buttons) and would
 * otherwise crush the description against the left edge.
 */
export const SettingRow: React.FC<{
  label: React.ReactNode;
  description?: React.ReactNode;
  /** Leading icon, for rows that benefit from being findable at a glance. */
  icon?: React.ComponentType<{ className?: string }>;
  control?: React.ReactNode;
  stack?: boolean;
  /** Extra content under the row (warnings, revealed detail, nested options). */
  children?: React.ReactNode;
  className?: string;
}> = ({ label, description, icon: Icon, control, stack = false, children, className }) => (
  <div className={clsx('border-b border-border py-3.5', className)}>
    <div className={clsx('flex gap-4', stack ? 'flex-col' : 'items-start justify-between')}>
      <div className="flex min-w-0 items-start gap-2.5">
        {Icon && <Icon className="mt-0.5 h-4 w-4 shrink-0 text-tertiary" />}
        <div className="min-w-0">
          <div className="text-[13px] font-medium leading-snug text-ink">{label}</div>
          {description && (
            <p className="mt-1 text-[12.5px] leading-relaxed text-tertiary">{description}</p>
          )}
        </div>
      </div>
      {control && <div className={clsx('flex items-center gap-2', stack ? '' : 'shrink-0')}>{control}</div>}
    </div>
    {children && <div className="mt-3">{children}</div>}
  </div>
);
