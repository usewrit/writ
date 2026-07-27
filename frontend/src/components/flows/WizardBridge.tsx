import React, { useEffect, useRef } from 'react';
import { WizardProvider, useWizard } from '../wizard/WizardContext';
import type { WizardState, WizardMode } from '../wizard/WizardContext';

/**
 * Bridge that wraps real wizard panels (ContentMonitorPanel, AIWorkflowPanel, etc.)
 * in a WizardProvider, syncing config state between the flow builder and the wizard.
 */

interface WizardBridgeProps {
  mode: WizardMode;
  config: Partial<WizardState['config']>;
  onConfigChange: (updates: Partial<WizardState['config']>) => void;
  children: React.ReactNode;
}

export const WizardBridge: React.FC<WizardBridgeProps> = ({ mode, config, onConfigChange, children }) => {
  return (
    <WizardProvider>
      <WizardBridgeSync mode={mode} config={config} onConfigChange={onConfigChange} />
      {children}
    </WizardProvider>
  );
};

const WizardBridgeSync: React.FC<{
  mode: WizardMode;
  config: Partial<WizardState['config']>;
  onConfigChange: (updates: Partial<WizardState['config']>) => void;
}> = ({ mode, config, onConfigChange }) => {
  const { state, setMode, updateConfig, goToStep } = useWizard();
  const initialized = useRef(false);
  const prevConfigRef = useRef<string>('');

  useEffect(() => {
    if (!initialized.current) {
      setMode(mode);
      goToStep('configure');
      if (config) {
        updateConfig(config);
      }
      initialized.current = true;
    }
  }, []);

  useEffect(() => {
    if (!initialized.current) return;
    const configJson = JSON.stringify(state.config);
    if (configJson !== prevConfigRef.current) {
      prevConfigRef.current = configJson;
      onConfigChange(state.config);
    }
  }, [state.config, onConfigChange]);

  return null;
};
