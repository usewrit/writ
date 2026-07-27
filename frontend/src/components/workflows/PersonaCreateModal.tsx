import React from 'react';
import { PersonaWizard } from './PersonaWizard';
import type { Persona } from '../../types/api';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  onCreated: (persona: Persona) => void;
  /** Prefill the site domain (e.g. from the workflow's entry_url host). */
  defaultDomain?: string;
}

/**
 * Shared "create a persona" dialog. Used by the Personas page and inline by the
 * PersonaPicker so users can spin up an identity without leaving a workflow/automation.
 * Thin wrapper around the full PersonaWizard (kept for prop compatibility).
 */
export const PersonaCreateModal: React.FC<Props> = ({ isOpen, onClose, onCreated, defaultDomain }) => (
  <PersonaWizard isOpen={isOpen} onClose={onClose} onSaved={onCreated} defaultDomain={defaultDomain} />
);

export default PersonaCreateModal;
