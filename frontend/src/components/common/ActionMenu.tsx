import React, { Fragment } from 'react';
import { Menu, Transition } from '@headlessui/react';
import { EllipsisVerticalIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

export interface ActionMenuItem {
  label: string;
  icon?: React.ElementType;
  onClick?: () => void;
  to?: string;
  variant?: 'default' | 'danger';
  disabled?: boolean;
}

interface ActionMenuProps {
  items: ActionMenuItem[];
  disabled?: boolean;
}

export const ActionMenu: React.FC<ActionMenuProps> = ({ items, disabled }) => {
  const { t } = useTranslation();
  return (
    <Menu as="div" className="relative inline-block text-left">
      <div>
        <Menu.Button
          disabled={disabled}
          className="flex items-center justify-center rounded-lg p-2 text-tertiary hover:text-secondary hover:bg-hover focus:outline-none disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          <span className="sr-only">{t('Open options')}</span>
          <EllipsisVerticalIcon className="h-5 w-5" aria-hidden="true" />
        </Menu.Button>
      </div>

      <Transition
        as={Fragment}
        enter="transition ease-out duration-150"
        enterFrom="transform opacity-0 scale-95 -translate-y-1"
        enterTo="transform opacity-100 scale-100 translate-y-0"
        leave="transition ease-in duration-100"
        leaveFrom="transform opacity-100 scale-100 translate-y-0"
        leaveTo="transform opacity-0 scale-95 -translate-y-1"
      >
        <Menu.Items className="absolute right-0 z-10 mt-1 w-48 origin-top-right rounded-lg bg-surface shadow-lg border border-border focus:outline-none">
          <div className="p-1">
            {items.map((item, index) => (
              <Menu.Item key={index} disabled={item.disabled}>
                {({ active }) => {
                  const className = clsx(
                    'group flex w-full items-center px-3 py-2 text-sm rounded-md transition-colors',
                    active && 'bg-hover',
                    item.variant === 'danger' ? 'text-red-600' : 'text-secondary',
                    active && item.variant !== 'danger' && 'text-ink',
                    item.disabled && 'opacity-50 cursor-not-allowed',
                  );

                  const content = (
                    <>
                      {item.icon && (
                        <item.icon className="mr-2.5 h-4 w-4" aria-hidden="true" />
                      )}
                      {t(item.label)}
                    </>
                  );

                  if (item.to) {
                    return (
                      <Link to={item.to} className={className}>
                        {content}
                      </Link>
                    );
                  }

                  return (
                    <button onClick={item.onClick} className={className} disabled={item.disabled}>
                      {content}
                    </button>
                  );
                }}
              </Menu.Item>
            ))}
          </div>
        </Menu.Items>
      </Transition>
    </Menu>
  );
};
