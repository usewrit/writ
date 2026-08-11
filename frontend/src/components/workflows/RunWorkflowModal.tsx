import React, { useState } from 'react';
import { Modal } from '../ui/Modal';
import { automationApi } from '../../api/endpoints';
import { ExecutionTargetPicker } from './ExecutionTargetPicker';
import { PersonaPicker } from './PersonaPicker';
import { FilePicker, PickedFile } from '../FilePicker';
import { Checkbox } from '../ui';
import { isStorageQuotaError } from '../../utils/storageQuota';
import type { DataManifest } from '../../api/marketplace';
import { collectPlaceholders } from '../steps/stepMeta';

/** Backend convention: secret VALUES live in `workflow.credentials_encrypted` (a
 *  separate sealed column, NOT `form_data`). The API redacts that blob and hands
 *  the frontend a single `has_credentials` boolean — we can't see which specific
 *  names are inside. Sent back to the run body under `__secret_<name>` keys is a
 *  distinct transport convention; the modal uses this prefix only when POSTing
 *  the user's just-typed secret values, never when reading state. */
const SECRET_KEY_PREFIX = '__secret_';

/** Names of secrets a workflow references (`{{secret:X}}` anywhere in its steps)
 *  but has NO stored value for. Two accuracy tiers:
 *   • `credential_keys: string[]` (the modern signal — names only, no values):
 *     compare each `{{secret:X}}` name against that set directly, so a workflow
 *     that has `api_key` linked but references a NEW `{{secret:new_key}}` shows
 *     only `new_key` as prompt-worthy.
 *   • Fallback to the coarse `has_credentials` boolean (older responses / paths
 *     that don't emit `credential_keys` yet): truthy → assume ALL linked; falsy
 *     → NONE linked. A partial mismatch surfaces as a runtime error.
 *  Order is insertion order from the step scan (stable per workflow definition). */
function collectUnlinkedSecrets(workflow: {
  has_credentials?: boolean;
  credential_keys?: string[];
  steps?: unknown;
}): string[] {
  const referenced = collectPlaceholders(workflow.steps ?? []);
  // Per-name accuracy when the API supplies it. An empty array is meaningful
  // (no credentials linked); we only fall through when `credential_keys` is
  // undefined (unknown, treat as legacy).
  const perName = Array.isArray(workflow.credential_keys)
    ? new Set(workflow.credential_keys)
    : null;
  if (!perName && workflow.has_credentials) return [];
  const out: string[] = [];
  for (const raw of referenced) {
    const m = /^secret\s*:\s*(.+)$/i.exec(raw);
    if (!m) continue;
    const name = m[1].trim();
    if (!name || out.includes(name)) continue;
    if (perName && perName.has(name)) continue;
    out.push(name);
  }
  return out;
}
import {
  PlayIcon,
  LockClosedIcon,
  DocumentIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';

type ExecutionTarget = 'auto' | 'local' | 'cloud';

function hostOf(url?: string): string | undefined {
  if (!url) return undefined;
  try {
    return new URL(url).hostname;
  } catch {
    return undefined;
  }
}

interface Placeholder {
  key: string;
  label: string;
  field_type?: string | null;
}

/** A declared file input the runner binds their own stored file to (§7.3). */
interface FileSlot {
  slot: string;
  label: string;
  is_multiple?: boolean;
  /** File pinned on the step — pre-selected here, and used when the runner
   *  doesn't choose another. Absent for a data-less marketplace slot. */
  default_file_id?: string;
  default_filename?: string;
}

/**
 * Extract the file input slots a workflow declares — the slots a runner binds
 * their OWN stored file to before a run (§4.2/§7.3). Sources, in priority order:
 *  - the backend-serialized `file_slots` field (computed from upload-step
 *    config.file_slot even in the polled list where `steps` is omitted — the
 *    reliable source for both the list and detail views);
 *  - upload steps that carry a `config.file_slot` (fallback when only the steps
 *    blob is present; a concrete `config.file_id` is already pre-bound in the
 *    recipe and resolved server-side, so it needs no run-time binding);
 *  - an installed proxy's `data_manifest.file_slots`.
 * De-duped by slot name, order-preserving. The slot NAME is the key sent in the
 * run body's `files` map — the backend binds it to the matching step's file_slot.
 */
function extractFileSlots(workflow: {
  file_slots?: Array<{
    slot: string; label?: string | null; is_multiple?: boolean;
    default_file_id?: string | null; default_filename?: string | null;
  }>;
  steps?: Array<{ id?: string; type?: string; config?: Record<string, any> | null; options?: Record<string, any> | null }>;
  data_manifest?: DataManifest | null;
}): FileSlot[] {
  const slots: FileSlot[] = [];
  const seen = new Set<string>();
  const add = (
    slot: string, label?: string | null, isMultiple?: boolean,
    defaultFileId?: string | null, defaultFilename?: string | null,
  ) => {
    const name = (slot || '').trim();
    if (!name || seen.has(name)) return;
    seen.add(name);
    slots.push({
      slot: name,
      label: label || name.replace(/_/g, ' '),
      is_multiple: !!isMultiple,
      default_file_id: defaultFileId || undefined,
      default_filename: defaultFilename || undefined,
    });
  };
  for (const fs of workflow.file_slots || []) {
    add(fs.slot, fs.label, fs.is_multiple, fs.default_file_id, fs.default_filename);
  }
  // Fallback for a workflow served WITHOUT the backend's computed file_slots (summary
  // payloads, older caches): derive the same inputs from the steps. Every upload step
  // is an input — one with a pinned file simply arrives pre-filled — and an unslotted
  // step is keyed on its stable step id, matching the backend and the agent.
  for (const step of workflow.steps || []) {
    if (step?.type !== 'upload') continue;
    const cfg = step.config || {};
    const o = step.options || {};
    const declared = (typeof cfg.file_slot === 'string' && cfg.file_slot.trim())
      || (typeof o.file_slot === 'string' && o.file_slot.trim());
    const slot = declared || (step.id ? `step:${step.id}` : '');
    add(
      String(slot || ''),
      cfg.label || o.label || cfg.file_name || o.filename || o.file_name,
      cfg.is_multiple || o.is_multiple,
      cfg.file_id || o.file_id,
      cfg.file_name || o.filename || o.file_name,
    );
  }
  for (const fs of workflow.data_manifest?.file_slots || []) {
    add(fs.slot, fs.label, fs.is_multiple);
  }
  return slots;
}

interface RunWorkflowModalProps {
  workflow: {
    id: number;
    name: string;
    form_data?: Record<string, string>;
    placeholders?: Placeholder[];
    has_credentials?: boolean;
    /** Names of the workflow's stored credential-blob keys — no values. Present
     *  on newer API responses so the modal can prompt per-name for unlinked
     *  `{{secret:X}}` refs instead of falling back to the coarse `has_credentials`. */
    credential_keys?: string[];
    default_persona_id?: number | null;
    entry_url?: string;
    has_login?: boolean;
    has_twofa?: boolean;
    file_slots?: Array<{
      slot: string; label?: string | null; is_multiple?: boolean;
      default_file_id?: string | null; default_filename?: string | null;
    }>;
    steps?: Array<{ id?: string; type?: string; config?: Record<string, any> | null; options?: Record<string, any> | null }>;
    data_manifest?: DataManifest | null;
  };
  isOpen: boolean;
  onClose: () => void;
  onDispatched?: (taskId: number) => void;
}

/**
 * Inline control that binds one stored file to a run-time file slot — mirrors the
 * step editor's FileLink so binding a file is one consistent surface. The shared
 * FilePicker enforces ownership/quota/size server-side; only {file_id, filename}
 * is kept locally and sent as `files: {slot: file_id}` in the run request.
 */
const SlotFileLink: React.FC<{
  value?: PickedFile;
  onPick: (f: PickedFile) => void;
  onClear: () => void;
}> = ({ value, onPick, onClear }) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <>
      {value ? (
        <div className="flex items-center gap-2 px-3 py-2 bg-canvas border border-border rounded-lg">
          <DocumentIcon className="w-4 h-4 text-tertiary shrink-0" />
          <span className="flex-1 min-w-0 truncate text-sm text-ink" title={value.filename}>
            {value.filename}
          </span>
          <button type="button" onClick={() => setOpen(true)} className="text-[11px] font-medium text-secondary hover:text-ink">
            {t('Change')}
          </button>
          <button type="button" onClick={onClear} className="text-tertiary hover:text-ink" title={t('Clear')}>
            <XMarkIcon className="w-3.5 h-3.5" />
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="w-full px-3 py-2 text-sm text-secondary bg-canvas border border-dashed border-border rounded-lg hover:border-ink/30 hover:text-ink transition-colors text-left"
        >
          {t('Choose a stored file…')}
        </button>
      )}
      <FilePicker
        isOpen={open}
        onClose={() => setOpen(false)}
        onSelect={(f) => { onPick(f); setOpen(false); }}
        selectedId={value?.file_id ?? null}
        title={t('Attach a file for this run')}
      />
    </>
  );
};

/**
 * Modal shown before running a workflow that requires input data.
 * Lets the user review/edit input values and choose execution target.
 */
export const RunWorkflowModal: React.FC<RunWorkflowModalProps> = ({
  workflow,
  isOpen,
  onClose,
  onDispatched,
}) => {
  const { t } = useTranslation();
  const [formValues, setFormValues] = useState<Record<string, string>>({});
  const [executionTarget, setExecutionTarget] = useState<ExecutionTarget>('auto');
  // Optional device pin — set only when the user picks a specific device (2+
  // online). Cleared whenever the target moves away from Local.
  const [agentId, setAgentId] = useState<string | undefined>(undefined);
  const [personaId, setPersonaId] = useState<number | null>(null);
  const [running, setRunning] = useState(false);
  // When the workflow already has input data saved, default to reusing it.
  const [useSaved, setUseSaved] = useState(true);
  // FILE ASSETS (§7.3): the runner's own files bound to declared file slots,
  // keyed by slot name. Sent as `files: {slot: file_id}` in the run request.
  const [fileBindings, setFileBindings] = useState<Record<string, PickedFile>>({});

  const fileSlots = React.useMemo(() => extractFileSlots(workflow), [workflow]);

  // Each step's pinned file IS that slot's default, so a plain Run works untouched
  // and picking another simply overrides it for this run.
  //
  // DERIVED, not seeded into state by an effect. `fileBindings` holds only what the
  // runner deliberately chose; the default is folded in here at read time. Copying
  // the defaults into state instead costs an extra render pass on open, and — because
  // such an effect can only ever ADD — a binding from a previously shown workflow
  // would survive into the next one, offering to upload a file that belongs to
  // something else.
  const effectiveFiles = React.useMemo(() => {
    const out: Record<string, PickedFile> = { ...fileBindings };
    for (const s of fileSlots) {
      if (!out[s.slot] && s.default_file_id) {
        out[s.slot] = { file_id: s.default_file_id, filename: s.default_filename || s.label };
      }
    }
    return out;
  }, [fileSlots, fileBindings]);

  const stored = workflow.form_data || {};
  // Secrets the workflow references via `{{secret:X}}` but has NO saved value for.
  // Prompted alongside plain placeholders in the modal — the user's ask: "detect
  // secret: placeholder and ask user for secrets when run modal ONLY IF no
  // input data / secret are already linked".
  const unlinkedSecrets = React.useMemo(() => collectUnlinkedSecrets(workflow), [workflow]);
  // Merge backend-declared plain placeholders with client-detected unlinked
  // secrets. Secrets are keyed `__secret_<name>` so the collected value drops
  // straight into `form_data` and the backend resolves `{{secret:<name>}}` at
  // run time exactly as it does for a saved secret.
  const effectivePlaceholders = React.useMemo(() => {
    const list: Placeholder[] = [...(workflow.placeholders || [])];
    const seen = new Set(list.map((p) => p.key));
    for (const name of unlinkedSecrets) {
      const key = `${SECRET_KEY_PREFIX}${name}`;
      if (seen.has(key)) continue;
      list.push({ key, label: name, field_type: 'password' });
      seen.add(key);
    }
    return list;
  }, [workflow.placeholders, unlinkedSecrets]);

  // Saved plain fields = placeholders that already have a non-empty stored value.
  const savedPlainCount = (workflow.placeholders || [])
    .filter((p) => (stored[p.key] ?? '').trim() !== '').length;
  // Secrets live in the workflow's SEALED `credentials_encrypted` column, not in
  // `form_data`. The API redacts the blob and returns `has_credentials: bool`
  // (no key names) — so we can't count them here, only detect presence.
  const hasSavedSecure = !!workflow.has_credentials;
  const hasSavedData = savedPlainCount > 0 || hasSavedSecure;

  // Initialize form values only when the modal opens (not on every poll).
  // Adjusted DURING RENDER (React's derive-state-from-props escape hatch) rather
  // than from an effect: the modal is already visible on the frame that flips
  // `isOpen`, so an effect would paint the PREVIOUS run's answers first.
  const [initialized, setInitialized] = useState(false);
  if (isOpen && !initialized) {
    const initial: Record<string, string> = {};
    for (const p of effectivePlaceholders) {
      initial[p.key] = stored[p.key] ?? '';
    }
    setFormValues(initial);
    setPersonaId(workflow.default_persona_id ?? null);
    // If any referenced secret has NO saved value, the user must enter it —
    // "reuse saved" would run with the missing secret unresolved. Force
    // manual entry (the reuse toggle stays available if the user checks it,
    // but they'll re-see the unlinked-secret note explaining what's missing).
    setUseSaved(hasSavedData && unlinkedSecrets.length === 0);
    setFileBindings({});
    setAgentId(undefined);
    setInitialized(true);
  }
  if (!isOpen && initialized) {
    setInitialized(false);
  }

  const handleRun = async () => {
    // When reusing saved data we send no form_data override — the backend falls
    // back to the workflow's stored form_data + encrypted credentials.
    if (!useSaved) {
      const empty = Object.entries(formValues).filter(([_, v]) => !v.trim());
      if (empty.length > 0) {
        toast.error(t('Please fill in: {{fields}}', { fields: empty.map(([k]) => k).join(', ') }));
        return;
      }
    }

    // FILE ASSETS (§7.3): a file input with NO pinned default must be bound to one of
    // the runner's own files — the recipe ships no bytes, so it can't fall back to
    // saved data and the run needs it bound here. A step with a pinned file is
    // already satisfied.
    const missingFiles = fileSlots.filter((s) => !effectiveFiles[s.slot]);
    if (missingFiles.length > 0) {
      toast.error(t('Attach a file for: {{fields}}', {
        fields: missingFiles.map((s) => s.label).join(', '),
      }));
      return;
    }

    // { slot: file_id } — the run-body files map (§4.5). Each id is the runner's
    // own file; the backend ownership-checks it (resolve_for_run fail-closes 404).
    // Falls back to the step's pinned file so an untouched slot still runs.
    const filesMap: Record<string, string> = {};
    for (const s of fileSlots) {
      const fid = effectiveFiles[s.slot]?.file_id;
      if (fid) filesMap[s.slot] = fid;
    }

    setRunning(true);
    try {
      // A persona forces cloud execution (managed trusted agent + 2FA).
      const effectiveTarget = personaId ? 'cloud' : executionTarget;
      const result = await automationApi.runWorkflowWithData(
        workflow.id,
        effectiveTarget,
        useSaved ? undefined : formValues,
        personaId ?? undefined,
        filesMap,
        // Only pin a device when we're actually running locally.
        effectiveTarget === 'local' ? agentId : undefined,
      );
      toast.success(result.message || t('Workflow dispatched — Task #{{taskId}}', { taskId: result.task_id }), { duration: 4000 });
      onDispatched?.(result.task_id);
      onClose();
    } catch (err: any) {
      // A storage/file-count quota denial is owned by the central storage modal
      // (global 402/409 handler) — don't double-surface it as a toast here.
      if (!isStorageQuotaError(err)) {
        const detail = err?.response?.data?.detail;
        toast.error(typeof detail === 'string' ? detail : t('Failed to run workflow'));
      }
    } finally {
      setRunning(false);
    }
  };

  const fields = Object.entries(formValues);

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={t('Run "{{name}}"', { name: workflow.name })}>
      <div className="space-y-5">
        {/* Saved-data toggle — only when the workflow already has input saved */}
        {hasSavedData && (
          <Checkbox
            wrapperClassName="px-3 py-2.5 bg-hover border border-border rounded-lg"
            checked={useSaved}
            onChange={(e) => setUseSaved(e.target.checked)}
            label={<span className="text-sm font-medium text-ink">{t('Use saved input data')}</span>}
            description={
              <>
                {[
                  savedPlainCount > 0
                    ? t('{{n}} saved field', { n: savedPlainCount, count: savedPlainCount })
                    : null,
                  hasSavedSecure ? t('encrypted credentials') : null,
                ].filter(Boolean).join(' · ')}
                {' — '}
                {t('runs with the values saved on this workflow.')}
              </>
            }
          />
        )}

        {/* Input fields — hidden while reusing saved data */}
        {fields.length > 0 && !useSaved && (
          <div className="space-y-3">
            <div>
              <h3 className="text-sm font-medium text-ink">{t('Input Data')}</h3>
              <p className="text-xs text-tertiary mt-0.5">
                {unlinkedSecrets.length > 0
                  ? t('Fill in the required values before running. This workflow references {{n}} secret(s) with no saved value.', { n: unlinkedSecrets.length })
                  : t('Fill in the required values before running.')}
              </p>
            </div>
            <div className="space-y-2">
              {fields.map(([key, value]) => {
                const placeholder = effectivePlaceholders.find(p => p.key === key);
                const label = placeholder?.label || key;
                const isPassword = placeholder?.field_type === 'password';
                const isSecret = key.startsWith(SECRET_KEY_PREFIX);
                return (
                  <div key={key} className="space-y-1">
                    <label className="text-xs font-medium text-secondary flex items-center gap-1">
                      {isSecret && <LockClosedIcon className="w-3 h-3 text-tertiary" />}
                      {label}
                      {isSecret && (
                        <span className="text-[10px] font-normal text-tertiary uppercase tracking-wide">
                          {t('secret')}
                        </span>
                      )}
                    </label>
                    <input
                      type={isPassword ? 'password' : 'text'}
                      value={value}
                      onChange={(e) => setFormValues(prev => ({ ...prev, [key]: e.target.value }))}
                      placeholder={isSecret ? `{{secret:${label}}}` : key}
                      className="w-full px-3 py-2 text-sm border border-border rounded-lg bg-surface text-ink font-mono placeholder:text-tertiary focus:outline-none focus:ring-1 focus:ring-ink/10"
                    />
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* File inputs — bind the runner's own stored files to declared slots (§7.3).
            Always shown (a slot has no saved file to reuse) and required to run. */}
        {fileSlots.length > 0 && (
          <div className="space-y-3">
            <div>
              <h3 className="text-sm font-medium text-ink">{t('Files')}</h3>
              <p className="text-xs text-tertiary mt-0.5">
                {t('Attach a file from your library for each input below.')}
              </p>
            </div>
            <div className="space-y-2">
              {fileSlots.map((s) => (
                <div key={s.slot} className="space-y-1">
                  <label className="text-xs font-medium text-secondary">
                    {s.label}
                    {s.is_multiple && (
                      <span className="ml-1.5 text-tertiary font-normal">{t('(multiple)')}</span>
                    )}
                  </label>
                  <SlotFileLink
                    value={effectiveFiles[s.slot]}
                    onPick={(f) => setFileBindings((prev) => ({ ...prev, [s.slot]: f }))}
                    onClear={() => setFileBindings((prev) => {
                      const next = { ...prev };
                      delete next[s.slot];
                      return next;
                    })}
                  />
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Secret fields indicator — when entering data manually but creds are stored */}
        {hasSavedSecure && !useSaved && (
          <div className="flex items-center gap-2 px-3 py-2 bg-hover border border-border rounded-lg">
            <LockClosedIcon className="w-4 h-4 text-tertiary" />
            <span className="text-xs text-secondary">
              {t('Encrypted credentials will be included automatically')}
            </span>
          </div>
        )}

        {/* Persona (auth identity) — only when the workflow logs in; forces cloud when set */}
        {(workflow.has_login || workflow.default_persona_id) && (
          <>
            <PersonaPicker
              value={personaId}
              onChange={setPersonaId}
              domain={hostOf(workflow.entry_url)}
              allowClear
            />
            {workflow.has_twofa && !personaId && !workflow.default_persona_id && (
              <p className="text-[11px] text-secondary -mt-1">
                {t('This workflow enters a 2FA code — without a persona holding the 2FA secret, unattended runs will stop at the challenge.')}
              </p>
            )}
          </>
        )}

        {/* Execution target — disabled when a persona forces cloud */}
        {!personaId && (
          <ExecutionTargetPicker
            value={executionTarget}
            onChange={(v) => { setExecutionTarget(v); if (v !== 'local') setAgentId(undefined); }}
            compact
            agentId={agentId}
            onAgentIdChange={setAgentId}
          />
        )}

        {/* Actions */}
        <div className="flex items-center justify-end gap-2 pt-2 border-t border-border">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg hover:bg-hover transition-colors"
          >
            {t('Cancel')}
          </button>
          <button
            onClick={handleRun}
            disabled={running}
            className="flex items-center gap-2 px-4 py-2 text-sm font-medium bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 active:scale-[0.97] transition-all disabled:opacity-50"
          >
            <PlayIcon className="w-4 h-4" />
            {running ? t('Dispatching...') : t('Run Workflow')}
          </button>
        </div>
      </div>
    </Modal>
  );
};


/**
 * Check if a workflow requires user input before running.
 * Uses the same placeholder detection as the backend.
 */
export function workflowNeedsInput(workflow: {
  placeholders?: Placeholder[];
  form_data?: Record<string, string>;
  file_slots?: Array<{
    slot: string; label?: string | null; is_multiple?: boolean;
    default_file_id?: string | null; default_filename?: string | null;
  }>;
  steps?: Array<{ id?: string; type?: string; config?: Record<string, any> | null; options?: Record<string, any> | null }>;
  data_manifest?: DataManifest | null;
  has_credentials?: boolean;
  credential_keys?: string[];
}): boolean {
  // If backend returned placeholders, use those
  if (workflow.placeholders && workflow.placeholders.length > 0) {
    return true;
  }
  // A declared file slot must be bound at run time → route through the modal (§7.3).
  if (extractFileSlots(workflow).length > 0) {
    return true;
  }
  // A `{{secret:X}}` reference with no saved `__secret_X` must prompt — otherwise
  // we'd silently dispatch a run that has no chance of resolving the secret.
  if (collectUnlinkedSecrets(workflow).length > 0) {
    return true;
  }
  // Fallback: check form_data for keys (form_data holds ONLY plain fields —
  // secrets live in credentials_encrypted, not here — so any key at all counts).
  const formData = workflow.form_data || {};
  return Object.keys(formData).length > 0;
}
