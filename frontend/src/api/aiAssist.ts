// AI-assist client — the "describe it, AI builds it" automation generator.
//
// Talks to the LOCAL daemon's `/v1/ai-assist/generate-automation`. The daemon runs the
// user's own provider (offline-first) and, when no local provider is configured but the
// app is cloud-linked, reflects the request to the cloud — so "local with cloud fallback"
// is handled server-side and the frontend just calls one endpoint.

import client from './client';
import type { AutomationSpec } from '../components/flows/automationSpec';
import type { Platform } from '../components/flows/blockCatalog';

/** This build is the desktop app.  */
export const APP_PLATFORM: Platform = 'cloud';

export interface ResourceContext {
  workflows: Array<{ id: number; name: string; type?: string; has_login?: boolean; outputs?: string[] }>;
  monitors: Array<{ id: number; url: string }>;
  ai_sessions: Array<{ id: number; name: string; goal?: string }>;
  personas: Array<{ id: number; name: string; domain?: string }>;
  files: Array<{ id: string; filename: string }>;
}

export interface GenerateAutomationRequest {
  goal: string;
  url?: string;
  platform: Platform;
  /** The Block Catalog digest — the "what blocks exist" explanation for the model. */
  catalog_digest: string;
  resource_context: ResourceContext;
  /**
   * When set, the AI EXTENDS this existing automation (returns only new blocks parented
   * onto it) instead of building from scratch — the "Ask AI in the block editor" augment flow.
   */
  current_automation?: { name: string; description: string; blocks: unknown[] };
}

export interface GenerateAutomationResponse {
  automation: AutomationSpec;
  message?: string;
  /** Which brain answered — for a subtle "generated in the cloud" hint. */
  source?: 'local' | 'cloud';
  credits_used?: number;
}

export const aiAssistApi = {
  async generateAutomation(body: GenerateAutomationRequest): Promise<GenerateAutomationResponse> {
    const { data } = await client.post('/ai-assist/generate-automation', body);
    return data as GenerateAutomationResponse;
  },
};

export default aiAssistApi;
