import React from 'react';
import clsx from 'clsx';

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
}

export const Input: React.FC<InputProps> = ({
  label,
  error,
  className,
  ...props
}) => {
  return (
    <div>
      {label && (
        <label className="block text-sm text-secondary mb-1.5">{label}</label>
      )}
      <input
        className={clsx(
          'w-full px-4 py-2.5 text-sm text-ink bg-surface rounded-lg transition-all duration-200',
          'border placeholder:text-tertiary',
          'focus:outline-none focus:ring-2 focus:ring-ink/5 focus:border-ink/30 focus:shadow-sm',
          error ? 'border-red-300' : 'border-border',
          className,
        )}
        {...props}
      />
      {error && <p className="mt-1 text-xs text-red-500">{error}</p>}
    </div>
  );
};
