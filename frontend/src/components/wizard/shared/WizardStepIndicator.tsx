import React from 'react';
import clsx from 'clsx';
import { CheckIcon } from '@heroicons/react/24/outline';
import { WizardStepId } from '../WizardContext';
import { useTranslation } from 'react-i18next';

const STEP_LABELS: Record<WizardStepId, string> = {
  mode: 'Setup',
  configure: 'Configure',
  finalize: 'Finalize',
};

interface WizardStepIndicatorProps {
  steps: WizardStepId[];
  currentStep: WizardStepId;
  completedSteps: Set<WizardStepId>;
  onStepClick?: (step: WizardStepId) => void;
}

export const WizardStepIndicator: React.FC<WizardStepIndicatorProps> = ({
  steps,
  currentStep,
  completedSteps,
  onStepClick,
}) => {
  const { t } = useTranslation();
  const currentIndex = steps.indexOf(currentStep);

  return (
    <nav className="flex items-center gap-1">
      {steps.map((step, index) => {
        const isCompleted = completedSteps.has(step);
        const isCurrent = step === currentStep;
        const isPast = index < currentIndex;
        const isClickable = isCompleted || isPast;

        return (
          <React.Fragment key={step}>
            {index > 0 && (
              <div className={clsx('h-px w-6 transition-colors duration-300', isPast || isCompleted ? 'bg-ink' : 'bg-border')} />
            )}
            <button
              onClick={() => isClickable && onStepClick?.(step)}
              disabled={!isClickable}
              className={clsx(
                'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs transition-all duration-200',
                isCurrent && 'bg-ink text-white font-medium',
                isCompleted && !isCurrent && 'text-ink cursor-pointer hover:bg-chrome',
                !isCurrent && !isCompleted && 'text-tertiary',
                !isClickable && 'cursor-default',
              )}
            >
              {isCompleted && !isCurrent ? (
                <CheckIcon className="w-3.5 h-3.5" />
              ) : (
                <span className={clsx(
                  'w-4 h-4 rounded-full text-[10px] flex items-center justify-center font-medium',
                  isCurrent && "bg-surface text-ink",
                  !isCurrent && !isCompleted && 'bg-hover text-tertiary',
                  isCompleted && !isCurrent && 'bg-hover text-ink',
                )}>
                  {index + 1}
                </span>
              )}
              <span className="hidden @rail/stage:inline">{t(STEP_LABELS[step])}</span>
            </button>
          </React.Fragment>
        );
      })}
    </nav>
  );
};
