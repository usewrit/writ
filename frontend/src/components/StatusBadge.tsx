import React from 'react';
import clsx from 'clsx';
import i18n from '../i18n';

interface StatusBadgeProps {
  status: 'online' | 'offline' | 'error' | 'active' | 'inactive' | 'revoked' | 'suspended' | 'enabled' | 'disabled';
  size?: 'sm' | 'md' | 'lg';
}

// Scannable status palette (see design memory): green = live/positive,
// red = failure/offline, amber = attention, gray = neutral. Soft-fill badge
// + a status-colored dot; live states pulse. Matches the inline status badges
// standardized across the app so shared and inline badges look identical.
const statusConfig = {
  online: { color: 'bg-green-50 text-green-700', dot: 'bg-green-500', label: 'Online' },
  active: { color: 'bg-green-50 text-green-700', dot: 'bg-green-500', label: 'Active' },
  enabled: { color: 'bg-green-50 text-green-700', dot: 'bg-green-500', label: 'Enabled' },
  offline: { color: 'bg-red-50 text-red-700', dot: 'bg-red-500', label: 'Offline' },
  error: { color: 'bg-red-50 text-red-700', dot: 'bg-red-500', label: 'Error' },
  revoked: { color: 'bg-red-50 text-red-700', dot: 'bg-red-500', label: 'Revoked' },
  suspended: { color: 'bg-amber-50 text-amber-700', dot: 'bg-amber-500', label: 'Suspended' },
  inactive: { color: 'bg-hover text-secondary', dot: 'bg-tertiary', label: 'Inactive' },
  disabled: { color: 'bg-hover text-secondary', dot: 'bg-tertiary', label: 'Disabled' },
};

const sizeConfig = {
  sm: 'px-2 py-0.5 text-xs',
  md: 'px-2.5 py-1 text-sm',
  lg: 'px-3 py-1.5 text-base',
};

const liveStatuses = new Set(['online', 'active', 'enabled']);

export const StatusBadge: React.FC<StatusBadgeProps> = ({ status, size = 'md' }) => {
  const config = statusConfig[status] || { color: 'bg-hover text-secondary', dot: 'bg-tertiary', label: status || i18n.t('Unknown') };
  const isLive = liveStatuses.has(status);

  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1.5 rounded-full font-medium transition-colors duration-200',
        config.color,
        sizeConfig[size]
      )}
    >
      {isLive ? (
        <span className="relative flex h-1.5 w-1.5">
          <span className={clsx('animate-status-pulse absolute inline-flex h-full w-full rounded-full opacity-60', config.dot)} />
          <span className={clsx('relative inline-flex rounded-full h-1.5 w-1.5', config.dot)} />
        </span>
      ) : (
        <span className={clsx('inline-flex h-1.5 w-1.5 rounded-full', config.dot)} />
      )}
      {i18n.t(config.label)}
    </span>
  );
};
