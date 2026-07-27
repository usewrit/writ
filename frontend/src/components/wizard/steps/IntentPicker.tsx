import React from 'react';
import {
  CursorArrowRaysIcon,
  SparklesIcon,
  ArrowRightIcon,
} from '@heroicons/react/24/outline';
import { useTranslation } from 'react-i18next';
import { WizardMode } from '../WizardContext';

// The workflow build picker — the wizard's first screen. A workflow is the one
// thing built here (monitors live on /checks, crawls on /crawls, each their own
// flow), so the only real question is HOW to build it: drive a live browser and
// record the steps, or hand a goal to the AI. The short-vs-long-running choice
// is NOT asked here — it's a switch on the setup form (one-shot ↔ live session),
// where there's room to explain it. Recording defaults to a one-shot workflow.
interface BuildMethodDef {
  /** The wizard mode this method starts in. Record defaults to one-shot
   *  (manual_workflow); the setup form's run-style switch flips it to streaming. */
  mode: WizardMode;
  title: string;
  subtitle: string;
  icon: React.FC<any>;
  /** Show this method only when one of these modes is allowed by the host page. */
  requiresModes: string[];
}

interface IntentPickerProps {
  allowedModes?: string[];
  /** Selecting a build method sets the underlying wizard mode and stays in-flow. */
  onPickMode: (mode: WizardMode) => void;
  /** Kept for API compat with the onboarding sandbox (no nav-out here anymore). */
  hideNavOut?: boolean;
}

export const IntentPicker: React.FC<IntentPickerProps> = ({ allowedModes, onPickMode }) => {
  const { t } = useTranslation();

  const METHODS: BuildMethodDef[] = [
    {
      mode: 'manual_workflow',
      title: t('Record the steps'),
      subtitle: t('Drive a live browser — click, type, navigate. We capture every action as a workflow you can replay, schedule, or call as an API.'),
      icon: CursorArrowRaysIcon,
      // Recording covers both one-shot and live-session workflows; the run-style
      // switch on the next screen chooses between them.
      requiresModes: ['manual_workflow', 'streaming_workflow'],
    },
    {
      mode: 'ai_workflow',
      title: t('Let AI do it'),
      subtitle: t('Describe your goal in plain English. The AI navigates the site, fills forms, and extracts data on its own — then saves the steps it took as a reusable workflow.'),
      icon: SparklesIcon,
      requiresModes: ['ai_workflow'],
    },
  ];

  const isAllowed = (m: BuildMethodDef) =>
    !allowedModes || m.requiresModes.some((mode) => allowedModes.includes(mode));
  const visible = METHODS.filter(isAllowed);

  return (
    <div className="min-h-full flex flex-col items-center justify-center py-10 px-4 sm:px-6">
      <div className="w-full max-w-xl rounded-2xl border border-ink/20 bg-surface/90 backdrop-blur-sm shadow-sm px-6 sm:px-8 py-8">
        <h2 className="text-xl font-semibold text-ink tracking-tight text-center mb-1.5">
          {t('How do you want to build it?')}
        </h2>
        <p className="text-[12px] text-tertiary text-center mb-7">
          {t('Both produce a workflow you can run, schedule, or call as an API.')}
        </p>

        <div className="grid grid-cols-1 gap-3">
          {visible.map((m) => {
            const Icon = m.icon;
            return (
              <button
                key={m.mode}
                data-tour={`build-method-${m.mode}`}
                onClick={() => onPickMode(m.mode)}
                className="group flex items-start gap-3.5 p-4 text-left rounded-xl border border-border hover:border-ink/25 hover:bg-chrome transition-all"
              >
                <div className="w-10 h-10 rounded-lg bg-hover flex items-center justify-center shrink-0 transition-colors group-hover:bg-ink">
                  <Icon className="w-5 h-5 text-secondary group-hover:text-white" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="text-[14px] font-medium text-ink">{m.title}</div>
                  <div className="text-[12px] text-tertiary mt-1 leading-relaxed">{m.subtitle}</div>
                </div>
                <ArrowRightIcon className="w-4 h-4 text-tertiary shrink-0 mt-1 opacity-0 group-hover:opacity-100 transition-opacity" />
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
};
