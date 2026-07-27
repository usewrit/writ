import React, { useCallback } from 'react';
import { useWizard } from '../WizardContext';
import { BrowserRecorder } from '../../BrowserRecorder';

export const StreamingWorkflowPanel: React.FC = () => {
  const { state, updateConfig } = useWizard();

  // The recorder no longer has its own Save button: the Studio app-bar's
  // "Done recording" primary advances to Finalize. Steps, credentials, form data
  // and the streaming handler/script config all live-sync into the wizard config
  // continuously via the onChange callbacks below.
  const handleSave = useCallback((
    steps: any[],
    name: string,
    credentials?: Record<string, string>,
    formData?: Record<string, string>,
    segments?: any[],
  ) => {
    // Retained for back-compat with any non-app-bar Save trigger; folds the
    // payload into the live config (no auto-advance — the app-bar owns nav now).
    const streamingSegment = segments?.find((s: any) => s.segment_type === 'streaming_config');
    updateConfig({
      recordedSteps: steps,
      name: name || state.config.name,
      credentials: credentials || {},
      formData: formData || {},
      streamingHandlers: streamingSegment?.handlers || [],
      advancedScriptEnabled: streamingSegment?.advancedScriptEnabled || false,
      advancedScript: streamingSegment?.advancedScript || '',
      setupStepsCount: streamingSegment?.setupStepsCount ?? steps.length,
    });
  }, [updateConfig, state.config.name]);

  return (
    <div className="h-full flex flex-col">
      <div className="flex-1 min-h-0">
        <BrowserRecorder
            isOpen={true}
            embedded={true}
            streamingMode={true}
            initialUrl={state.config.url}
            autoConnect={!!state.config.url}
            preferredAgentId={state.config.executionTarget}
            onClose={() => {}}
            onSave={handleSave}
            onStepsChange={(steps) => updateConfig({ recordedSteps: steps as any[] })}
            onCredentialsChange={(credentials) => updateConfig({ credentials })}
            onFormDataChange={(formData) => updateConfig({ formData })}
            onStreamingConfigChange={(cfg) => updateConfig({
              streamingHandlers: cfg.handlers as any[],
              advancedScriptEnabled: cfg.advancedScriptEnabled,
              advancedScript: cfg.advancedScript,
              setupStepsCount: cfg.setupStepsCount,
            })}
            onPersonaCreated={(personaId) => updateConfig({ defaultPersonaId: personaId })}
          />
      </div>
    </div>
  );
};
