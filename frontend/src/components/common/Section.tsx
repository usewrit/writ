import React from 'react';
import clsx from 'clsx';

interface SectionProps {
  title?: string;
  subtitle?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  noPadding?: boolean;
}

export const Section: React.FC<SectionProps> = ({
  title,
  subtitle,
  actions,
  children,
  className,
  noPadding = false,
}) => {
  return (
    <div className={clsx('bg-surface rounded-lg border border-border overflow-hidden', className)}>
      {(title || actions) && (
        <div className="px-6 py-4 border-b border-border flex flex-col @pair/stage:flex-row @pair/stage:items-center justify-between gap-3">
          <div>
            {title && <h2 className="text-base font-semibold text-ink">{title}</h2>}
            {subtitle && <p className="text-sm text-secondary mt-0.5">{subtitle}</p>}
          </div>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </div>
      )}
      <div className={clsx(!noPadding && 'p-6')}>
        {children}
      </div>
    </div>
  );
};
