import React from 'react';
import { CloudIcon, BoltIcon, ServerIcon, CubeIcon, ComputerDesktopIcon } from '@heroicons/react/24/outline';
import { useTranslation } from 'react-i18next';

interface PlatformIconProps {
  platform: 'cloudflare' | 'vercel' | 'lambda' | 'gcp' | 'macos' | 'windows' | 'linux' | 'desktop';
  size?: 'sm' | 'md' | 'lg';
  className?: string;
}

const sizeMap = {
  sm: 'h-4 w-4',
  md: 'h-6 w-6',
  lg: 'h-8 w-8',
};

// Monochrome — platform identity is carried by the icon shape + label, not color.
const platformConfig = {
  cloudflare: { icon: CloudIcon, color: 'text-secondary', label: 'Cloudflare' },
  vercel: { icon: BoltIcon, color: 'text-ink', label: 'Vercel' },
  lambda: { icon: ServerIcon, color: 'text-secondary', label: 'AWS Lambda' },
  gcp: { icon: CubeIcon, color: 'text-secondary', label: 'GCP' },
  macos: { icon: ComputerDesktopIcon, color: 'text-secondary', label: 'macOS' },
  windows: { icon: ComputerDesktopIcon, color: 'text-secondary', label: 'Windows' },
  linux: { icon: ComputerDesktopIcon, color: 'text-secondary', label: 'Linux' },
  desktop: { icon: ComputerDesktopIcon, color: 'text-secondary', label: 'Desktop' },
};

export const PlatformIcon: React.FC<PlatformIconProps> = ({
  platform,
  size = 'md',
  className = ''
}) => {
  const { t } = useTranslation();
  const config = platformConfig[platform] || platformConfig.desktop;
  const Icon = config.icon;

  return (
    <div className={`flex items-center gap-2 ${className}`} title={t(config.label)}>
      <Icon className={`${sizeMap[size]} ${config.color}`} />
    </div>
  );
};

export const PlatformIconWithLabel: React.FC<PlatformIconProps> = ({
  platform,
  size = 'md',
  className = ''
}) => {
  const { t } = useTranslation();
  const config = platformConfig[platform] || platformConfig.desktop;
  const Icon = config.icon;

  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <Icon className={`${sizeMap[size]} ${config.color}`} />
      <span className="text-sm font-medium">{t(config.label)}</span>
    </div>
  );
};
