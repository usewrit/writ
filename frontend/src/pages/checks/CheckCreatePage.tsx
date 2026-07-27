import React, { useEffect } from 'react';
import { useRequireAuth } from '../../hooks/useAuth';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { UnifiedCreationWizard } from '../../components/wizard/UnifiedCreationWizard';
import { triggerMini } from '../../onboarding/miniTrigger';

export const CheckCreatePage: React.FC = () => {
  useRequireAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();

  // Deep-link seeds (e.g. ?intent=monitor&url=…&name=… from the intent picker /
  // dashboard quick-starts). Monitor always preselects content_monitor below.
  const url = params.get('url') || undefined;
  const name = params.get('name') || undefined;
  // Launched from the automation source picker's "Create new monitor": on completion
  // redirect back into the automation builder with this monitor auto-selected.
  const fromAutomation = params.get('intent') === 'automation_source';

  // First real monitor creation → offer the contextual mini-tutorial
  // (suppressed if the user already did the full "?" walkthrough).
  useEffect(() => { triggerMini('content_monitor'); }, []);

  return (
    <>
      <div className="flex flex-col h-full">
        <UnifiedCreationWizard
          initialMode="content_monitor"
          initialUrl={url}
          initialName={name}
          forceAutomateReturn={fromAutomation}
          onComplete={() => navigate(fromAutomation ? '/automations/new' : '/checks')}
          onCancel={() => navigate(fromAutomation ? '/automations/new' : '/checks')}
          embedded
        />
      </div>
    </>
  );
};
