import React from 'react';
import { useTranslation } from 'react-i18next';
import { QuestionMarkCircleIcon } from '@heroicons/react/24/outline';
import { GuidedTour, type TourStep } from '../../../../onboarding/tour/GuidedTour';
import { markTourSeen, shouldAutoRunTour } from '../../../../onboarding/storage';
import { SecureFieldDemo } from './SecureFieldDemo';

const TOUR_ID = 'workflow-detail';
/** Fired by {@link WorkflowDetailTourButton}, which sits far from the tour host. */
const REPLAY_EVENT = 'writ:tour:workflow-detail';

/**
 * First-visit walkthrough of the workflow Detail tab.
 *
 * ONE step list covers every shape of the tab (regular / streaming, owner /
 * narrow stage): the engine drops any step whose `data-tour` anchor isn't
 * painted, so a streaming workflow silently skips "About" and a narrow stage
 * silently skips the at-a-glance rail. Steps are listed in DOM order.
 *
 * The Input data step is the one that earns its place: locking a field is the
 * single most consequential, least discoverable control on the page, so instead
 * of describing it the step PLAYS it — see {@link SecureFieldDemo}.
 */
export const WorkflowDetailTour: React.FC = () => {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);

  // First visit: let the tab's 160ms content settle (and the data widgets
  // resolve) before taking the screen, so nothing reflows under the spotlight.
  React.useEffect(() => {
    if (!shouldAutoRunTour(TOUR_ID)) return;
    const id = window.setTimeout(() => setOpen(true), 700);
    return () => window.clearTimeout(id);
  }, []);

  React.useEffect(() => {
    const replay = () => setOpen(true);
    window.addEventListener(REPLAY_EVENT, replay);
    return () => window.removeEventListener(REPLAY_EVENT, replay);
  }, []);

  const steps: TourStep[] = [
    {
      id: 'intro',
      title: t('Detail — what this workflow is'),
      body: t('A workflow is split across four tabs: Detail is what it IS, Steps is what it does, Connect is how it gets invoked, and Data is what it produced. This walkthrough covers Detail.'),
    },
    {
      id: 'about',
      anchor: 'wf-detail-about',
      title: t('About — the one-line answer'),
      body: t('The description plus the URL each run starts from. The description is also what an AI agent reads when this workflow is handed to it as a tool, so keep it literal.'),
    },
    {
      id: 'session',
      anchor: 'wf-detail-session',
      title: t('Session — a browser that stays open'),
      body: t('A streaming workflow is not a one-shot run. Start a session and it holds a live browser you can keep calling until you end it.'),
    },
    {
      id: 'inputs',
      anchor: 'wf-detail-inputs',
      wide: true,
      title: t('Input data — the values a run needs'),
      body: t('Anything the workflow types, searches for, or signs in with. The lock is the important part — watch:'),
      extra: <SecureFieldDemo />,
    },
    {
      id: 'persona',
      anchor: 'wf-detail-persona',
      title: t('Persona — the identity it signs in as'),
      body: t('A saved login. Attach one and every run signs in as it. It also marks the run sensitive, so in the cloud it gets a private, isolated browser instead of the shared pool.'),
    },
    {
      id: 'capabilities',
      anchor: 'wf-detail-capabilities',
      title: t('Capabilities — what it exposes'),
      body: t('A read-out of the switches that live in Connect. Click any row to jump straight to the setting behind it.'),
    },
    {
      id: 'data',
      anchor: 'wf-detail-data',
      title: t('Output — what it produced'),
      body: t('A preview of the rows recent runs extracted. The Data tab holds the full table, the search, and the API that serves it.'),
    },
    {
      id: 'runs',
      anchor: 'wf-detail-runs',
      title: t('Run history — how it is holding up'),
      body: t('Every execution, newest first; click a failed one for its full error. When a page changes and a step stops matching, AI auto-repair rewrites that step instead of failing the run.'),
    },
    {
      id: 'sessions',
      anchor: 'wf-detail-sessions',
      title: t('Recent sessions'),
      body: t('Past and current sessions for this workflow, with the number of events each one emitted.'),
    },
    {
      id: 'advanced',
      anchor: 'wf-detail-advanced',
      title: t('Advanced — how it runs'),
      body: t('Browser behaviour per run, where it executes, and how it recovers. The defaults are deliberate — open this only to pin a venue or change recovery.'),
    },
    {
      id: 'rail',
      anchor: 'wf-detail-rail',
      title: t('At a glance'),
      body: t('The rail mirrors live state — schedule, last result, and the Run button — so you keep them while you scroll.'),
    },
    {
      id: 'outro',
      title: t('That is the Detail tab'),
      body: t('Steps is the recipe, Connect turns it into a REST endpoint, an AI tool or a schedule, and Data is everything it has extracted. Reopen this walkthrough any time from the guide button.'),
    },
  ];

  return (
    <GuidedTour
      steps={steps}
      open={open}
      onClose={() => { markTourSeen(TOUR_ID); setOpen(false); }}
    />
  );
};

/**
 * Replays the Detail walkthrough. Lives in a Section header far from the tour
 * host, so it talks to it over a window event rather than shared state.
 */
export const WorkflowDetailTourButton: React.FC = () => {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={() => window.dispatchEvent(new Event(REPLAY_EVENT))}
      title={t('Replay the walkthrough of this page')}
      className="flex items-center gap-1 px-1.5 py-0.5 text-[11px] font-medium text-tertiary hover:text-ink rounded-md hover:bg-hover transition-colors"
    >
      <QuestionMarkCircleIcon className="w-3.5 h-3.5" />
      {t('How this page works')}
    </button>
  );
};

export default WorkflowDetailTour;
