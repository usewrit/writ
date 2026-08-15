import React from 'react';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import { useRequireAuth } from '../../hooks/useAuth';
import { useNavigate, useSearchParams, Navigate } from 'react-router-dom';
import { UnifiedCreationWizard } from '../../components/wizard/UnifiedCreationWizard';
import { peekFlowDraft } from '../../components/flows/flowDraft';

// Intent → underlying wizard mode. The build intents land here; monitor/automate/
// crawl redirect out to their own create surfaces (preserving the query so url/
// name/template carry through). A stray ?intent=task falls through to the
// intent-first picker with no AI card.
const INTENT_TO_MODE: Record<string, 'manual_workflow'> = {
  callable: 'manual_workflow',
};

// Modes offered when an automation block sent the user here to build a workflow for
// it. Only these end with a workflow row the block can bind — an AI session isn't a
// workflow (that's the "Run AI Agent" block), so it's off the menu for this trip.
const AUTOMATION_ACTION_MODES = ['manual_workflow', 'streaming_workflow'];

export const WorkflowCreatePage: React.FC = () => {
  useRequireAuth();
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [params] = useSearchParams();

  const intent = params.get('intent') || undefined;
  const url = params.get('url') || undefined;
  const name = params.get('name') || undefined;

  // Launched from an automation block's "Create a new workflow": the builder stashed
  // its draft before leaving, so finishing (or cancelling) must land back there —
  // with the new workflow id on a finish so the block binds it.
  const forAutomationAction = intent === 'automation_action';
  const draftReturnTo = React.useMemo(
    () => (forAutomationAction ? peekFlowDraft()?.returnTo : undefined),
    [forAutomationAction],
  );
  const backToBuilder = (workflowId?: number | null) => {
    const q = new URLSearchParams({ resume: '1' });
    if (workflowId) q.set('bindWorkflowId', String(workflowId));
    navigate(`${draftReturnTo || '/automations/new'}?${q.toString()}`);
  };

  // Launched from a persona's "Record login": the workflow being built IS that
  // persona's sign-in, so finishing must ATTACH it (personas.login_workflow_id)
  // and return where the user came from. Without the attach the recording would
  // be just another workflow and the persona would still be unable to sign in —
  // which is the whole reason they were sent here.
  const loginForPersona = Number(params.get('login_for_persona')) || null;
  const loginReturnTo = params.get('login_return_to') || '/personas';
  const attachLoginAndReturn = async (workflowId?: number | null) => {
    if (loginForPersona && workflowId) {
      try {
        const { personasApi } = await import('../../api/endpoints');
        await personasApi.update(loginForPersona, { login_workflow_id: workflowId });
        toast.success(t('Login workflow attached to the persona'));
      } catch (e: any) {
        // The workflow itself was saved — say what didn't happen rather than
        // implying the recording was lost.
        toast.error(
          e?.response?.data?.detail ||
            t('Workflow saved, but it could not be attached to the persona. Attach it from the persona.'),
        );
      }
    }
    navigate(loginReturnTo);
  };

  // Monitor / Automate / Crawl intents live on their own pages — redirect,
  // preserving the query string (url, name, template/preset).
  if (intent === 'monitor') {
    return <Navigate to={{ pathname: '/checks/new', search: params.toString() }} replace />;
  }
  if (intent === 'automate') {
    return <Navigate to={{ pathname: '/automations/new', search: params.toString() }} replace />;
  }
  if (intent === 'crawl') {
    return <Navigate to={{ pathname: '/crawls/new', search: params.toString() }} replace />;
  }

  const initialMode = intent ? INTENT_TO_MODE[intent] : undefined;

  return (
    <>
      <div className="flex flex-col h-full">
        <UnifiedCreationWizard
          initialMode={initialMode}
          initialUrl={url}
          initialName={name}
          allowedModes={forAutomationAction
            ? AUTOMATION_ACTION_MODES
            : ['manual_workflow', 'streaming_workflow']}
          onComplete={(ids) => {
            // A missing workflowId still returns to the builder — the draft is restored,
            // just with nothing new bound.
            if (forAutomationAction) { backToBuilder(ids?.workflowId); return; }
            if (loginForPersona) { void attachLoginAndReturn(ids?.workflowId); return; }
            navigate('/workflows');
          }}
          onCancel={() => {
            if (forAutomationAction) { backToBuilder(null); return; }
            // Cancelled: nothing to attach, but still return to the persona rather
            // than dumping the user on the workflow list they never asked for.
            if (loginForPersona) { navigate(loginReturnTo); return; }
            navigate('/workflows');
          }}
          embedded
        />
      </div>
    </>
  );
};
