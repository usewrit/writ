import React, { Fragment } from 'react';
import { Dialog, Transition } from '@headlessui/react';
import { XMarkIcon } from '@heroicons/react/24/outline';

interface ModalProps {
  isOpen: boolean;
  onClose: () => void;
  title?: string;
  /** Optional one-line subtitle under the title (orients the dialog). */
  subtitle?: string;
  children: React.ReactNode;
  size?: 'sm' | 'md' | 'lg' | 'xl' | '2xl';
  /**
   * Optional pinned action bar. When provided, the panel is height-capped and
   * the body scrolls internally so the footer stays reachable on long forms.
   * Omit it and the modal behaves exactly as before (grows with content).
   */
  footer?: React.ReactNode;
}

export const Modal: React.FC<ModalProps> = ({
  isOpen,
  onClose,
  title,
  subtitle,
  children,
  size = 'md',
  footer,
}) => {
  const sizeClass = {
    sm: 'max-w-sm',
    md: 'max-w-lg',
    lg: 'max-w-2xl',
    xl: 'max-w-4xl',
    '2xl': 'max-w-5xl',
  }[size];

  return (
    <Transition appear show={isOpen} as={Fragment}>
      <Dialog as="div" className="relative z-50" onClose={onClose}>
        <Transition.Child
          as={Fragment}
          enter="ease-out duration-200"
          enterFrom="opacity-0"
          enterTo="opacity-100"
          leave="ease-in duration-150"
          leaveFrom="opacity-100"
          leaveTo="opacity-0"
        >
          <div className="fixed inset-0 bg-black/20 backdrop-blur-sm" />
        </Transition.Child>

        <div className="fixed inset-0 overflow-y-auto">
          <div className="flex min-h-full items-center justify-center p-4">
            <Transition.Child
              as={Fragment}
              enter="ease-out duration-200"
              enterFrom="opacity-0 scale-[0.98]"
              enterTo="opacity-100 scale-100"
              leave="ease-in duration-150"
              leaveFrom="opacity-100 scale-100"
              leaveTo="opacity-0 scale-[0.98]"
            >
              <Dialog.Panel
                className={`w-full ${sizeClass} bg-surface rounded-xl shadow-lg ${
                  footer ? 'flex flex-col max-h-[90vh] overflow-hidden' : ''
                }`}
              >
                {title && (
                  <div className="flex items-start justify-between gap-4 px-6 py-4 border-b border-border shrink-0">
                    <div className="min-w-0">
                      <Dialog.Title className="text-base font-semibold text-ink">
                        {title}
                      </Dialog.Title>
                      {subtitle && (
                        <p className="text-xs text-secondary mt-0.5 leading-snug">{subtitle}</p>
                      )}
                    </div>
                    <button
                      onClick={onClose}
                      className="p-1.5 -mr-1 text-secondary hover:text-ink hover:bg-chrome rounded-lg transition-colors duration-150 shrink-0"
                    >
                      <XMarkIcon className="h-5 w-5" />
                    </button>
                  </div>
                )}
                <div className={footer ? 'flex-1 overflow-y-auto p-6' : 'p-6'}>{children}</div>
                {footer && (
                  <div className="shrink-0 border-t border-border bg-surface px-6 py-3.5">
                    {footer}
                  </div>
                )}
              </Dialog.Panel>
            </Transition.Child>
          </div>
        </div>
      </Dialog>
    </Transition>
  );
};
