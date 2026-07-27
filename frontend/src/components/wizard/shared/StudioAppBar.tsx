import React from 'react';
import clsx from 'clsx';
import {
  ChevronLeftIcon,
  ChevronDownIcon,
  CheckIcon,
  ArrowRightIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import { useTranslation } from 'react-i18next';
import { Button } from '../../ui/Button';
import { WizardStepId } from '../WizardContext';

const STEP_LABEL_KEYS: Record<WizardStepId, string> = {
  mode: 'Setup',
  configure: 'Configure',
  finalize: 'Finalize',
};

interface StudioAppBarProps {
  /** Ordered steps for the active mode. */
  steps: WizardStepId[];
  /** The step currently shown. */
  currentStep: WizardStepId;
  /** Steps the user has already completed (clickable in the crumb popover). */
  completedSteps: Set<WizardStepId>;
  /** Jump to a completed step — wired to the same GO_TO_STEP dispatch the indicator used. */
  onStepClick: (step: WizardStepId) => void;

  /** Inline-editable workflow name (state.config.name). */
  name: string;
  onNameChange: (name: string) => void;
  /** Validation message shown as a tooltip anchored to the name input (set when
   *  the user tries to advance without a name; cleared when they type). */
  nameError?: string | null;

  /** Back affordance: backs up a step, else cancels the whole shell. */
  onBack: () => void;
  canGoBack: boolean;

  /** The single morphing primary button. */
  isLastStep: boolean;
  isSubmitting: boolean;
  /** Active wizard mode — lets the primary button label match the action
   *  (e.g. "Start crawl" for site_crawl instead of the generic "Create"). */
  mode?: string | null;
  /** Forward navigation for non-last steps (carries per-mode validation). */
  onNext: () => void;
  /** Commit on the finalize step. */
  onSubmit: () => void;

  /** Optional close (X) when not embedded. */
  onCancel?: () => void;
}

/**
 * StudioAppBar — the slim, always-standing top chrome of the Studio shell.
 *
 * It replaces the old WizardStepIndicator header. This phase carries:
 *  - a Back chevron (backs up a step, else cancels),
 *  - a step-crumb popover ("Record · 2 of 3") that jumps to completed steps,
 *  - an inline-editable workflow name,
 *  - the SINGLE morphing primary button (Continue / Done recording / Create).
 *
 * URL+status promotion and live status chip land in later phases.
 */
export const StudioAppBar: React.FC<StudioAppBarProps> = ({
  steps,
  currentStep,
  completedSteps,
  onStepClick,
  name,
  onNameChange,
  nameError,
  onBack,
  canGoBack,
  isLastStep,
  isSubmitting,
  mode,
  onNext,
  onSubmit,
  onCancel,
}) => {
  const { t } = useTranslation();
  const [crumbOpen, setCrumbOpen] = React.useState(false);
  const crumbRef = React.useRef<HTMLDivElement>(null);
  const nameInputRef = React.useRef<HTMLInputElement>(null);

  // Pull focus to the name field when its validation tooltip appears.
  React.useEffect(() => {
    if (nameError) nameInputRef.current?.focus();
  }, [nameError]);

  const currentIndex = steps.indexOf(currentStep);

  // Close the crumb popover on outside click.
  React.useEffect(() => {
    if (!crumbOpen) return;
    const onDocClick = (e: MouseEvent) => {
      if (crumbRef.current && !crumbRef.current.contains(e.target as Node)) {
        setCrumbOpen(false);
      }
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [crumbOpen]);

  // Morphing primary label by step. The Record step ("configure") saves the
  // workflow/monitor and advances to Finalize (so its wiring works there), so it
  // shows "Saving…" while that create runs. Finalize just routes onward — the
  // entity already exists — so its button reads "Done", not "Create".
  const primaryLabel = isLastStep
    ? isSubmitting
      ? mode === 'site_crawl'
        ? t('Starting…')
        : t('Finishing…')
      : mode === 'site_crawl'
        ? t('Start crawl')
        : t('Done')
    : currentStep === 'configure'
      ? (isSubmitting ? t('Saving…') : t('Done recording'))
      : t('Continue');

  const currentStepLabel = t(STEP_LABEL_KEYS[currentStep]);

  return (
    <div className="px-2 sm:px-3 h-12 border-b border-border bg-chrome flex items-center gap-2 shrink-0">
      {/* Back chevron */}
      <button
        type="button"
        onClick={onBack}
        aria-label={canGoBack ? t('Back') : t('Cancel')}
        className="p-1.5 text-tertiary hover:text-ink hover:bg-surface/60 rounded-lg transition-colors shrink-0"
      >
        <ChevronLeftIcon className="w-5 h-5" />
      </button>

      {/* Step-crumb popover — "Record · 2 of 3" */}
      <div className="relative shrink-0" ref={crumbRef}>
        <button
          type="button"
          onClick={() => setCrumbOpen((o) => !o)}
          className="flex items-center gap-1.5 px-2 py-1 rounded-lg text-xs text-secondary hover:text-ink hover:bg-surface/60 transition-colors"
        >
          <span className="font-medium text-ink">{currentStepLabel}</span>
          <span className="text-tertiary">
            {t('{{current}} of {{total}}', { current: currentIndex + 1, total: steps.length })}
          </span>
          <ChevronDownIcon
            className={clsx('w-3.5 h-3.5 text-tertiary transition-transform', crumbOpen && 'rotate-180')}
          />
        </button>

        {crumbOpen && (
          <div className="absolute left-0 top-full mt-1 z-50 w-48 rounded-xl border border-border bg-surface shadow-lg py-1 animate-wizard-field-in">
            {steps.map((step, index) => {
              const isCompleted = completedSteps.has(step);
              const isCurrent = step === currentStep;
              const isPast = index < currentIndex;
              const isClickable = (isCompleted || isPast) && !isCurrent;
              return (
                <button
                  key={step}
                  type="button"
                  disabled={!isClickable}
                  onClick={() => {
                    if (!isClickable) return;
                    onStepClick(step);
                    setCrumbOpen(false);
                  }}
                  className={clsx(
                    'w-full flex items-center gap-2 px-3 py-2 text-xs text-left transition-colors',
                    isCurrent && 'bg-hover text-ink font-medium',
                    isClickable && 'text-ink hover:bg-chrome cursor-pointer',
                    !isClickable && !isCurrent && 'text-tertiary cursor-default',
                  )}
                >
                  <span
                    className={clsx(
                      'w-4 h-4 rounded-full text-[10px] flex items-center justify-center font-medium shrink-0',
                      isCurrent && 'bg-ink text-white',
                      isCompleted && !isCurrent && 'bg-hover text-ink',
                      !isCurrent && !isCompleted && 'bg-hover text-tertiary',
                    )}
                  >
                    {isCompleted && !isCurrent ? <CheckIcon className="w-2.5 h-2.5" /> : index + 1}
                  </span>
                  <span>{t(STEP_LABEL_KEYS[step])}</span>
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* Inline-editable workflow name (+ anchored validation tooltip). Pins to a
          fixed width at the same point the step indicator unfurls its labels —
          below that the labels are icons only, so the name may take the slack. */}
      <div className="relative min-w-0 flex-1 @rail/stage:flex-none @rail/stage:w-56">
        <input
          ref={nameInputRef}
          type="text"
          data-tour="wizard-name"
          value={name}
          onChange={(e) => onNameChange(e.target.value)}
          placeholder={t('Untitled Workflow')}
          aria-label={t('Workflow name')}
          aria-invalid={nameError ? true : undefined}
          className={clsx(
            'w-full bg-transparent border rounded-lg px-2 py-1 text-sm text-ink placeholder:text-tertiary outline-none transition-colors',
            nameError
              ? 'border-red-300 bg-surface ring-2 ring-red-500/10'
              : 'border-transparent hover:border-border focus:border-border focus:bg-surface',
          )}
        />
        {nameError && (
          <div role="tooltip" className="absolute left-2 top-full mt-2 z-50 pointer-events-none animate-wizard-field-in">
            <div className="absolute -top-1 left-4 w-2 h-2 rotate-45 bg-ink" />
            <div className="rounded-lg bg-ink text-white text-[11px] font-medium px-2.5 py-1.5 shadow-lg whitespace-nowrap">
              {nameError}
            </div>
          </div>
        )}
      </div>

      {/* Flex spacer */}
      <div className="flex-1 min-w-0" />

      {/* The single morphing primary button */}
      {isLastStep ? (
        <Button
          data-tour="wizard-create"
          variant="primary"
          size="sm"
          onClick={onSubmit}
          loading={isSubmitting}
        >
          <CheckIcon className="w-4 h-4" />
          {primaryLabel}
        </Button>
      ) : (
        <Button data-tour="wizard-next" variant="primary" size="sm" onClick={onNext} loading={isSubmitting}>
          {primaryLabel}
          {!isSubmitting && <ArrowRightIcon className="w-3.5 h-3.5" />}
        </Button>
      )}

      {/* Close (X) — only when not embedded */}
      {onCancel && (
        <button
          type="button"
          onClick={onCancel}
          aria-label={t('Close')}
          className="p-1.5 text-tertiary hover:text-ink hover:bg-surface/60 rounded-lg transition-colors shrink-0"
        >
          <XMarkIcon className="w-5 h-5" />
        </button>
      )}
    </div>
  );
};
