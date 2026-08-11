import type { FlowBlock } from './types';
import { safeInternalPath } from '../../utils/safeRedirect';

/**
 * Hand-off draft for the automation builder.
 *
 * Blocks that need a workflow don't embed a recorder any more — they hand off to
 * the unified creation wizard, which is the one surface that knows how to build a
 * workflow (record, secure data, streaming, execution target). Leaving the builder
 * would drop an unsaved automation, so the whole draft is stashed here first; the
 * wizard then returns to `returnTo` with `?resume=1` (plus the new id) and the
 * builder rehydrates, binding the created entity onto `pendingBlockId`.
 *
 * sessionStorage, not localStorage: the draft is a single navigation round-trip,
 * not a document to keep across tabs or restarts.
 */
export interface FlowDraft {
  /** Builder route to come back to ('/automations/new' or '/automations/<id>'). */
  returnTo: string;
  /** Block that receives the created entity's id on return. */
  pendingBlockId: string;
  /** Config key on that block the new id is written to. */
  pendingField: 'workflow_id';
  flowId: number | null;
  name: string;
  description: string;
  enabled: boolean;
  blocks: FlowBlock[];
}

const KEY = 'writ.flowBuilder.handoffDraft';

export function saveFlowDraft(draft: FlowDraft): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(draft));
  } catch {
    /* private mode / quota — the hand-off still works, it just won't restore */
  }
}

/** Where a draft with an unusable `returnTo` sends the builder instead. */
const DEFAULT_RETURN_TO = '/automations/new';

/** Read without consuming — the wizard needs `returnTo` while it's still running. */
export function peekFlowDraft(): FlowDraft | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed.returnTo !== 'string' || !Array.isArray(parsed.blocks)) return null;
    // `returnTo` is interpolated straight into a `navigate()` target by the wizard,
    // so being a string is not enough — it has to be a path on THIS origin. It is
    // written as `location.pathname` and so is same-origin by construction, but this
    // value survives in sessionStorage and is only ever re-read, never re-derived.
    // Anything that can write that key therefore chooses where "Back to builder"
    // lands, including an absolute URL or the `\\evil.com` form that CVE-2026-53669
    // gets past react-router's own check. Re-validating on the way OUT means the
    // guarantee holds no matter how the value got there.
    return { ...parsed, returnTo: safeInternalPath(parsed.returnTo, DEFAULT_RETURN_TO) } as FlowDraft;
  } catch {
    return null;
  }
}

/** Read and consume — the builder restores a draft exactly once. */
export function takeFlowDraft(): FlowDraft | null {
  const draft = peekFlowDraft();
  clearFlowDraft();
  return draft;
}

export function clearFlowDraft(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
}
