import React from 'react';
import clsx from 'clsx';

export interface StatCardProps {
  title: string;
  value: string | number | React.ReactNode;
  icon?: React.ElementType;
  status?: 'normal' | 'warning' | 'critical' | 'success';
  loading?: boolean;
  onClick?: () => void;
}

export const StatCard: React.FC<StatCardProps> = ({
  title,
  value,
  icon: Icon,
  status = 'normal',
  loading,
  onClick,
}) => {
  return (
    <div
      onClick={onClick}
      className={clsx(
        'rounded-lg border bg-surface p-5 transition-all duration-200',
        status === 'normal' && 'border-border',
        status === 'warning' && 'border-amber-200',
        status === 'critical' && 'border-red-200',
        status === 'success' && 'border-green-200',
        onClick && 'cursor-pointer hover:border-ink/20 hover:shadow-md hover:-translate-y-0.5',
      )}
    >
      <div className="flex items-start justify-between">
        <div>
          <p className="text-sm text-secondary">{title}</p>
          <div className="mt-1.5">
            {loading ? (
              <div className="h-7 w-16 animate-pulse rounded bg-hover" />
            ) : (
              <span className="text-2xl font-semibold text-ink">{value}</span>
            )}
          </div>
        </div>
        {Icon && (
          <div className="p-2 rounded-lg bg-hover">
            <Icon className="h-5 w-5 text-secondary" aria-hidden="true" />
          </div>
        )}
      </div>
    </div>
  );
};
