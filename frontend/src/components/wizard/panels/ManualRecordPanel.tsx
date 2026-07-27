import React, { useCallback } from 'react';
import { useWizard } from '../WizardContext';
import { BrowserRecorder } from '../../BrowserRecorder';

export const ManualRecordPanel: React.FC = () => {
  const { state, updateConfig } = useWizard();

  // The recorder no longer has its own Save button: the Studio app-bar's
  // "Done recording" primary advances to Finalize. Everything needed to create
  // (steps, credentials, form data, segments) live-syncs into the wizard config
  // continuously via the onChange callbacks below, so the draft is always current.
  const handleSave = useCallback((steps: any[], name: string, credentials?: Record<string, string>, formData?: Record<string, string>, segments?: any[]) => {
    // Retained for back-compat with any non-app-bar Save trigger; just folds the
    // payload into the live config (no auto-advance — the app-bar owns nav now).
    updateConfig({
      recordedSteps: steps,
      name: name || state.config.name,
      credentials: credentials || {},
      formData: formData || {},
      workflowType: 'recorded',
      ...(segments && segments.length > 0 ? { segments } : {}),
    });
  }, [updateConfig, state.config.name]);

  return (
    <div className="h-full flex flex-col">
      {/* Embedded recorder — fills the entire configure step area */}
      <div className="flex-1 min-h-0">
        <BrowserRecorder
            isOpen={true}
            embedded={true}
            initialUrl={state.config.url}
            autoConnect={!!state.config.url}
            preferredAgentId={state.config.executionTarget}
            onClose={() => {}}
            onSave={handleSave}
            onStepsChange={(steps) => updateConfig({ recordedSteps: steps as any[] })}
            onSegmentsChange={(segments) => updateConfig({ segments: segments as any[] })}
            onCredentialsChange={(credentials) => updateConfig({ credentials })}
            onFormDataChange={(formData) => updateConfig({ formData })}
            onPersonaCreated={(personaId) => updateConfig({ defaultPersonaId: personaId })}
          />
      </div>
    </div>
  );
};
