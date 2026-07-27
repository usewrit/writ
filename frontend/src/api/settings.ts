import client from './client';

// ─────────────────────────────────────────────────────────────────────────────
// Coordinator settings — typed client for the single-owner /api/settings/*
// surface. Every section is a small GET/PUT pair backed by the coordinator's
// `config` KV table (see coordinator/routers/settings_extra.py + settings.py).
//
// Secrets follow the vault write-only 3-way everywhere: never returned; on
// write, omit=unchanged / ''=clear / set=replace. AI-provider keys and
// notification credentials use that contract.
// ─────────────────────────────────────────────────────────────────────────────

// ── Runtime (local execution governor) ─────────────────────────────────────
export interface RuntimeSettings {
  max_concurrent_runs: number;
  max_background_runs: number;
  rss_soft_watermark_mb: number; // 0 = off
  browser_headless: boolean;
  min_content_check_interval_s: number;
  min_browser_check_interval_s: number;
}

export const getRuntimeSettings = async (): Promise<RuntimeSettings> => {
  const r = await client.get('/settings/runtime');
  return r.data;
};

export const updateRuntimeSettings = async (
  patch: Partial<RuntimeSettings>,
): Promise<RuntimeSettings> => {
  const r = await client.put('/settings/runtime', patch);
  return r.data;
};

// ── Network & Public URL ────────────────────────────────────────────────────
export interface NetworkDerived {
  agent_ws_url: string | null;
  api_base: string | null;
  mcp_base: string | null;
}

export interface NetworkSettings {
  public_url: string;
  trusted_hosts: string[];
  derived: NetworkDerived;
}

export const getNetworkSettings = async (): Promise<NetworkSettings> => {
  const r = await client.get('/settings/network');
  return r.data;
};

export const updateNetworkSettings = async (patch: {
  public_url?: string;
  trusted_hosts?: string[];
}): Promise<NetworkSettings> => {
  const r = await client.put('/settings/network', patch);
  return r.data;
};

// ── Security (session / JWT policy) ─────────────────────────────────────────
export interface SecuritySettings {
  session_ttl_min: number;
  refresh_ttl_days: number;
  idle_timeout_min: number; // 0 = no idle timeout
  require_mfa: boolean;
  encryption_key_configured: boolean;
}

export const getSecuritySettings = async (): Promise<SecuritySettings> => {
  const r = await client.get('/settings/security');
  return r.data;
};

export const updateSecuritySettings = async (patch: {
  session_ttl_min?: number;
  refresh_ttl_days?: number;
  idle_timeout_min?: number;
  require_mfa?: boolean;
}): Promise<SecuritySettings> => {
  const r = await client.put('/settings/security', patch);
  return r.data;
};

// ── Preferences (per-owner UI) ──────────────────────────────────────────────
export interface PreferencesSettings {
  /** null = never picked; the UI then follows the browser (navigator → en). */
  language: string | null; // null | en | fr | es
  theme: string; // light | dark | system
}

export const getPreferences = async (): Promise<PreferencesSettings> => {
  const r = await client.get('/settings/preferences');
  return r.data;
};

export const updatePreferences = async (
  patch: Partial<PreferencesSettings>,
): Promise<PreferencesSettings> => {
  const r = await client.put('/settings/preferences', patch);
  return r.data;
};

// ── Data & Retention ────────────────────────────────────────────────────────
export interface DataSettings {
  retention_days: number; // 0 = keep everything
}

export interface PurgeResult {
  success: boolean;
  retention_days: number;
  purged: Record<string, number>;
  skipped?: string;
}

export const getDataSettings = async (): Promise<DataSettings> => {
  const r = await client.get('/settings/data');
  return r.data;
};

export const updateDataSettings = async (
  patch: Partial<DataSettings>,
): Promise<DataSettings> => {
  const r = await client.put('/settings/data', patch);
  return r.data;
};

export const purgeOldData = async (
  retention_days?: number,
): Promise<PurgeResult> => {
  const r = await client.post(
    '/settings/data/purge',
    retention_days != null ? { retention_days } : {},
  );
  return r.data;
};

// ── AI providers (BYO keys + model + base_url) ──────────────────────────────
export interface AIProvider {
  id: number;
  provider: string;
  api_key_masked: string | null;
  base_url: string | null;
  model: string | null;
  is_active: boolean;
  priority: number;
}

export interface AIProviderInput {
  provider: string;
  api_key?: string; // omit to keep the current key
  base_url?: string;
  model?: string;
  is_active: boolean;
  priority: number;
}

export interface AIProviderTestResult {
  status: 'ok' | 'error';
  response?: string;
  model?: string;
  error?: string;
}

export const listAIProviders = async (): Promise<AIProvider[]> => {
  const r = await client.get('/settings/ai-providers');
  return r.data;
};

export const createAIProvider = async (
  input: AIProviderInput,
): Promise<AIProvider> => {
  const r = await client.post('/settings/ai-providers', input);
  return r.data;
};

export const updateAIProvider = async (
  id: number,
  input: AIProviderInput,
): Promise<{ status: string }> => {
  const r = await client.put(`/settings/ai-providers/${id}`, input);
  return r.data;
};

export const deleteAIProvider = async (id: number): Promise<void> => {
  await client.delete(`/settings/ai-providers/${id}`);
};

export const testAIProvider = async (
  id: number,
): Promise<AIProviderTestResult> => {
  const r = await client.post(`/settings/ai-providers/test/${id}`);
  return r.data;
};

// ── AI output-token ceilings (cost control) ─────────────────────────────────
export interface AILimits {
  assist: number | null;
  agent: number | null;
  optimize: number | null;
  repair: number | null;
}

export interface AILimitsResponse {
  limits: AILimits;
  defaults: AILimits;
}

export const getAILimits = async (): Promise<AILimitsResponse> => {
  const r = await client.get('/settings/ai-limits');
  return r.data;
};

export const updateAILimits = async (
  patch: Partial<AILimits>,
): Promise<AILimitsResponse> => {
  const r = await client.put('/settings/ai-limits', patch);
  return r.data;
};
