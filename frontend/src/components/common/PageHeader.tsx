import React from 'react';
import { Link } from 'react-router-dom';
import { Button } from '../ui/Button';

export interface ActionButton {
  label: string;
  icon?: React.ElementType;
  onClick?: () => void;
  to?: string;
  variant?: 'primary' | 'secondary' | 'ghost';
  disabled?: boolean;
}

interface PageHeaderProps {
  title: string;
  subtitle?: string;
  actions?: ActionButton[];
}

export const PageHeader: React.FC<PageHeaderProps> = ({
  title,
  subtitle,
  actions,
}) => {
  return (
    <div className="flex flex-col @pair/stage:flex-row @pair/stage:items-center @pair/stage:justify-between gap-3 @pair/stage:gap-4 mb-6 sm:mb-8">
      <div>
        <h1 className="text-display text-ink">{title}</h1>
        {subtitle && <p className="text-sm text-secondary mt-1">{subtitle}</p>}
      </div>

      {actions && actions.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          {actions.map((action, index) => {
            if (action.to) {
              return (
                <Link key={index} to={action.to}>
                  <Button
                    variant={action.variant || 'primary'}
                    size="sm"
                    disabled={action.disabled}
                  >
                    {action.icon && <action.icon className="h-4 w-4" />}
                    {action.label}
                  </Button>
                </Link>
              );
            }

            return (
              <Button
                key={index}
                onClick={action.onClick}
                disabled={action.disabled}
                variant={action.variant || 'primary'}
                size="sm"
              >
                {action.icon && <action.icon className="h-4 w-4" />}
                {action.label}
              </Button>
            );
          })}
        </div>
      )}
    </div>
  );
};
