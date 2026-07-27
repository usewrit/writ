import React from 'react';
import { Button } from '../ui/Button';

interface EmptyStateProps {
  icon?: React.ElementType;
  title: string;
  description?: string;
  actionLabel?: string;
  onAction?: () => void;
}

// Compact action row — NOT a giant centered min-h box. Icon + title + one-line
// description on the left, optional CTA on the right. (Design-language: empty /
// CTA states are compact bordered rows, never wasted-space hero boxes.)
export const EmptyState: React.FC<EmptyStateProps> = ({
  icon: Icon,
  title,
  description,
  actionLabel,
  onAction,
}) => {
  return (
    <div className="flex flex-col @pair/stage:flex-row @pair/stage:items-center @pair/stage:justify-between gap-3 rounded-xl border border-border bg-surface px-4 py-3.5">
      <div className="flex items-start gap-3 min-w-0">
        {Icon && <Icon className="w-5 h-5 text-tertiary shrink-0 mt-0.5" />}
        <div className="min-w-0">
          <p className="text-[13px] font-medium text-ink">{title}</p>
          {description && (
            <p className="text-xs text-secondary mt-0.5 max-w-xl">{description}</p>
          )}
        </div>
      </div>
      {actionLabel && onAction && (
        <div className="shrink-0">
          <Button onClick={onAction} size="sm">
            {actionLabel}
          </Button>
        </div>
      )}
    </div>
  );
};
