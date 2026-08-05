import client from './client';

// ============================================================================
// Transfer API — portable `.writ` import/export (self-host)
// Backend: THIS coordinator's /api/transfer/* — same paths and same bodies as the
// cloud edition on purpose, so a package and the wizard driving it behave
// identically in both. (See DATA_PORTABILITY_SPEC.md §10.)
//
// The import side is STAGED: `inspect` reads the package's cleartext header with
// no passphrase, `stage` unlocks once and parks the body server-side, `savePlan`
// is a PATCH each wizard step calls, `commit` applies, `undo` reverses. Nothing
// in the user's account changes before `commit`.
// ============================================================================

/** Kinds this edition creates. `ai_sessions` / `endpoints` appear only when READING
 *  a cloud-made package, and arrive as reported capability blocks. */
export type AssetKind =
  | 'workflows' | 'automations' | 'monitors' | 'crawls'
  | 'personas' | 'webhooks' | 'ai_sessions' | 'endpoints';

export type Resolution = 'import' | 'skip' | 'rename' | 'replace';

export interface PackageHeaderSummary {
  bundle_id: string | null;
  label: string | null;
  created_at: string | null;
  producer_app: 'cloud' | 'desktop' | 'selfhost' | 'golden' | null;
  producer_version: string | null;
  producer_edition: string | null;
  producer_schema: number | null;
  package_version: number;
  /** Counts only — the header deliberately carries no names or key names. */
  contents: Record<string, number>;
  requires: { logins?: number; keys?: number; inputs?: number; files?: number };
  has_sealed_credentials: boolean;
  sealed_credential_count: number;
}

export interface SummaryItem {
  ref: string;
  kind: AssetKind;
  name: string;
  identity: string | null;
  collides: boolean;
  default_resolution: Resolution;
  /** Non-null ⇒ this asset cannot be imported; `message` says why, in a sentence. */
  block: { reason: string; message: string } | null;
  detail: string | null;
  needs: Record<string, boolean>;
  /** Knobs the producing install could not carry across. */
  dropped: string[];
}

export interface PersonaSlot {
  slot: string;
  domain?: string | null;
  covers_fields?: string[];
  used_by?: string[];
}

export interface SecretSlot {
  key: string;
  kind?: string;
  required?: boolean;
  persona_satisfiable?: boolean;
  validation?: Record<string, any>;
  used_by?: string[];
}

export interface InputSlot {
  key: string;
  type?: string;
  label?: string;
  required?: boolean;
  used_by?: string[];
}

export interface NotifySlot {
  slot: string;
  channels: string[];
  used_by?: string[];
}

export interface Requirements {
  persona_slots?: PersonaSlot[];
  secret_slots?: SecretSlot[];
  input_slots?: InputSlot[];
  file_slots?: { slot: string; label?: string; used_by?: string[] }[];
  notify_slots?: NotifySlot[];
  webhook_slots?: { slot: string; direction: string }[];
  monitor_url_slots?: any[];
}

export interface ImportSummary {
  items: SummaryItem[];
  data: { ref: string; row_count: number; run_count: number; truncated: boolean }[];
  marketplace_refs: { slug: string | null; name: string | null; kind: string; reason: string }[];
  skipped_by_producer: { kind: string; name: string; reason: string; detail: string }[];
  unknown_kinds: string[];
  /** Kinds this EDITION has no table for, with how many the package holds. Distinct
   *  from `unknown_kinds` ("no Writ this old knows it") — these are understood, this
   *  install simply cannot create them. */
  unsupported_kinds?: Record<string, number>;
  counts: Record<string, number>;
}

export interface Readiness {
  will_create: { ref: string; kind: string; name: string }[];
  will_replace: { ref: string; kind: string; name: string }[];
  will_skip: { ref: string; kind: string; name: string }[];
  blocked: (SummaryItem & { reason: any })[];
  unbound_secrets: string[];
  unbound_personas: string[];
  /** Assets that will import PAUSED because a login/key was not attached. */
  paused_refs: string[];
  data_rows: number;
  arm_schedules: boolean;
  ready: boolean;
}

export interface TransferImportRow {
  id: string;
  status: 'staged' | 'planned' | 'committing' | 'committed' | 'failed' | 'undone' | 'expired';
  label: string | null;
  bundle_id: string | null;
  producer: { app: string | null; version: string | null; edition: string | null };
  counts: Record<string, number>;
  has_sealed_credentials: boolean;
  payload_bytes: number;
  expires_at: string | null;
  committed_at: string | null;
  undone_at: string | null;
  progress: { done: number; total: number | null; phase: string } | null;
  created_at: string | null;
}

export interface ImportState {
  import: TransferImportRow;
  summary: ImportSummary;
  requirements: Requirements;
  plan: Record<string, any>;
  readiness: Readiness;
  result: ImportResult | null;
}

export interface AssetOutcome {
  ref: string;
  kind: string;
  name: string;
  outcome: 'created' | 'replaced' | 'paused' | 'skipped' | 'blocked' | 'failed';
  id: number | null;
  reason: string;
}

export interface ImportResult {
  assets: AssetOutcome[];
  counts: Record<string, number>;
  error?: string;
  undo?: { deleted: Record<string, number>; kept: any[] };
}

export interface ExportSelection {
  workflows?: number[];
  automations?: number[];
  monitors?: number[];
  crawls?: number[];
  /** 'referenced' (default) pulls in exactly the personas the selection uses. */
  personas?: 'referenced' | 'all' | 'none' | number[];
}

export interface ExportPreview {
  counts: Record<string, number>;
  requires: Record<string, number>;
  requirements: Requirements;
  skipped: { kind: string; name: string; reason: string; detail: string }[];
  marketplace_refs: { slug: string | null; name: string | null; kind: string }[];
  data: { workflow_id: number; rows: number; runs: number; truncated: boolean }[];
  personas_included: string[];
}

export const transferApi = {
  // ── export ──────────────────────────────────────────────────────────────
  previewExport: async (
    select: ExportSelection,
    includeData: Record<string, number[]> = {},
  ): Promise<ExportPreview> => {
    const { data } = await client.post('/transfer/export/preview', {
      select,
      include_data: includeData,
    });
    return data;
  },

  /**
   * Download a package. Returns the Blob rather than triggering the download so
   * the caller controls the filename and can surface a failure in the wizard
   * instead of leaving a broken file in the user's downloads.
   */
  exportPackage: async (body: {
    label?: string;
    select: ExportSelection;
    include_data?: Record<string, number[]>;
    include_credentials?: boolean;
    reauth?: { password: string };
    passphrase: string;
  }): Promise<{ blob: Blob; filename: string }> => {
    const response = await client.post('/transfer/export', body, { responseType: 'blob' });
    const disposition = String(response.headers?.['content-disposition'] || '');
    const match = disposition.match(/filename="?([^"]+)"?/);
    return { blob: response.data as Blob, filename: match?.[1] || 'writ-export.writ' };
  },

  // ── import ──────────────────────────────────────────────────────────────
  /** Step 1: header only. No passphrase, no staging, no writes. */
  inspect: async (file: File): Promise<{ header: PackageHeaderSummary; compatible: boolean }> => {
    const form = new FormData();
    form.append('file', file);
    const { data } = await client.post('/transfer/inspect', form);
    return data;
  },

  /** Step 2: unlock and stage. Slow by design — Argon2id runs server-side. */
  stage: async (file: File, passphrase: string): Promise<ImportState> => {
    const form = new FormData();
    form.append('file', file);
    form.append('passphrase', passphrase);
    const { data } = await client.post('/transfer/stage', form);
    return data;
  },

  get: async (id: string): Promise<ImportState> => {
    const { data } = await client.get(`/transfer/imports/${id}`);
    return data;
  },

  list: async (): Promise<{ imports: TransferImportRow[] }> => {
    const { data } = await client.get('/transfer/imports');
    return data;
  },

  /** Steps 3-6. A PATCH: send only what this step owns. */
  savePlan: async (
    id: string,
    patch: {
      resolutions?: Record<string, { action: Resolution; name?: string }>;
      personas?: Record<string, number | null>;
      secrets?: Record<string, string | null>;
      inputs?: Record<string, any>;
      files?: Record<string, any>;
      notify?: Record<string, string[]>;
      webhooks?: Record<string, any>;
      schedules?: Record<string, boolean>;
      credentials?: Record<string, boolean>;
      include_data?: boolean;
      arm_schedules?: boolean;
    },
  ): Promise<{ import: TransferImportRow; readiness: Readiness }> => {
    const { data } = await client.put(`/transfer/imports/${id}/plan`, patch);
    return data;
  },

  /**
   * Step 7. `Idempotency-Key` is mandatory here, not optional: without it a
   * double-click imports the package twice and lands a set of renamed duplicates.
   */
  commit: async (id: string, idempotencyKey: string): Promise<{ import: TransferImportRow; result: ImportResult }> => {
    const { data } = await client.post(`/transfer/imports/${id}/commit`, {}, {
      headers: { 'Idempotency-Key': idempotencyKey },
    });
    return data;
  },

  undo: async (id: string): Promise<{ import: TransferImportRow; undo: ImportResult['undo'] }> => {
    const { data } = await client.post(`/transfer/imports/${id}/undo`, {});
    return data;
  },

  discard: async (id: string): Promise<{ discarded: boolean }> => {
    const { data } = await client.delete(`/transfer/imports/${id}`);
    return data;
  },
};

/** Kick off a browser download for a package Blob. */
export function downloadPackage(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Revoke on the next tick — revoking synchronously can cancel the download in
  // Safari before it has read the object.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/**
 * A memorable passphrase suggestion, generated in the BROWSER with
 * `crypto.getRandomValues` — never server-side, so the only copy is the one the
 * user is looking at. Six words from a small curated list is ~62 bits, which is
 * well beyond what Argon2id at 64 MiB makes brute-forceable.
 */
export function suggestPassphrase(words = 6): string {
  const list = [
    'anchor', 'basalt', 'cedar', 'dahlia', 'ember', 'fathom', 'granite', 'harbor',
    'indigo', 'juniper', 'kestrel', 'lantern', 'marble', 'nimbus', 'orchard', 'pewter',
    'quarry', 'ribbon', 'saffron', 'thistle', 'umber', 'velvet', 'willow', 'yarrow',
    'almond', 'bramble', 'copper', 'driftwood', 'echo', 'flint', 'glacier', 'hollow',
    'ivory', 'jasper', 'kelp', 'lichen', 'meadow', 'nectar', 'onyx', 'plume',
    'quill', 'rustle', 'slate', 'tundra', 'ursine', 'vellum', 'walnut', 'zephyr',
  ];
  const picks: string[] = [];
  const buffer = new Uint32Array(words);
  crypto.getRandomValues(buffer);
  for (let i = 0; i < words; i += 1) {
    // Rejection-free modulo bias is irrelevant at 48 entries vs 2^32, but the
    // list length is a power-of-two-friendly 48 so the skew is negligible either way.
    picks.push(list[buffer[i] % list.length]);
  }
  return picks.join('-');
}
