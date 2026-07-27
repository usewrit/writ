import client from './client';

// Lightweight result shapes — only the fields the palette renders.
// These hit the SAME endpoints the list pages use (ChecksListPage,
// WorkflowsListPage, AutomationsListPage) so the shared useQuery cache
// (Q.targets('content') / Q.workflows() / Q.triggers()) round-trips cleanly.

export interface PaletteCheck {
  id: string;
  url: string;
  name?: string;
  enabled?: boolean;
}

export interface PaletteWorkflow {
  id: number;
  name: string;
  workflow_type?: string;
  entry_url?: string;
  is_active?: boolean;
}

export interface PaletteAutomation {
  id: number;
  name?: string;
  enabled?: boolean;
}

export const commandPaletteApi = {
  // Same call as ChecksListPage: targetsApi.getAll('content')
  listChecks: async (): Promise<PaletteCheck[]> => {
    const response = await client.get('/targets?check_type=content');
    return response.data;
  },

  // Same call as WorkflowsListPage: automationApi.listWorkflows()
  listWorkflows: async (): Promise<PaletteWorkflow[]> => {
    const response = await client.get('/automation/workflows?');
    return response.data;
  },

  // Same call as AutomationsListPage: triggersApi.listAll()
  listAutomations: async (): Promise<PaletteAutomation[]> => {
    const response = await client.get('/triggers/all?');
    return response.data;
  },
};
