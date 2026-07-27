import React from 'react';
import { ArrowLeftIcon, ArrowRightIcon, CheckIcon } from '@heroicons/react/24/outline';
import { useTranslation } from 'react-i18next';
import { Button } from '../../ui/Button';

interface WizardFooterProps {
  canGoBack: boolean;
  canGoNext: boolean;
  isLastStep: boolean;
  isSubmitting?: boolean;
  onBack: () => void;
  onNext: () => void;
  onSkip?: () => void;
  onSubmit?: () => void;
  skipLabel?: string;
  nextLabel?: string;
}

export const WizardFooter: React.FC<WizardFooterProps> = ({
  canGoBack,
  canGoNext,
  isLastStep,
  isSubmitting = false,
  onBack,
  onNext,
  onSkip,
  onSubmit,
  skipLabel,
  nextLabel,
}) => {
  const { t } = useTranslation();
  return (
    <div className="px-6 py-4 border-t border-border bg-surface flex items-center justify-between">
      <div>
        {canGoBack && (
          <Button variant="ghost" size="sm" onClick={onBack}>
            <ArrowLeftIcon className="w-3.5 h-3.5" />
            {t('Back')}
          </Button>
        )}
      </div>

      <div className="flex items-center gap-2">
        {onSkip && (
          <Button variant="ghost" size="sm" onClick={onSkip}>
            {skipLabel || t('Skip')}
          </Button>
        )}

        {isLastStep ? (
          <Button data-tour="wizard-create" variant="primary" size="md" onClick={onSubmit} loading={isSubmitting}>
            <CheckIcon className="w-4 h-4" />
            {isSubmitting ? t('Creating...') : t('Create')}
          </Button>
        ) : (
          canGoNext && (
            <Button data-tour="wizard-next" variant="primary" size="md" onClick={onNext}>
              {nextLabel || t('Continue')}
              <ArrowRightIcon className="w-3.5 h-3.5" />
            </Button>
          )
        )}
      </div>
    </div>
  );
};
