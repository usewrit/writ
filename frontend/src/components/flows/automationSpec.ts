// The contract the AI automation generator emits. It is deliberately the *existing*
// FlowBlock tree the builder already persists (see FlowBuilder.handleSave) plus authoring
// metadata used to play the automation into the canvas block-by-block. No translation layer.

import { FlowBlock } from './types';

export type UnresolvedKind = 'recipient' | 'selector' | 'value' | 'persona' | 'file' | 'workflow' | 'confirm';

/** A candidate the AI can proactively offer for an unresolved slot (a specific shortlist). */
export interface UnresolvedOption {
  id: string | number;
  label: string;
}

export interface UnresolvedItem {
  blockId: string;
  field: string;
  kind: UnresolvedKind;
  /** Human question shown inline while playback pauses on this block. */
  question: string;
  /** Optional AI-proposed shortlist to pick from; when absent the picker sources from state. */
  options?: UnresolvedOption[];
  /** When true (or kind === 'recipient'), the user may pick more than one item. */
  multi?: boolean;
}

export interface NewResourceItem {
  kind: 'monitor' | 'workflow';
  blockId: string;
  suggestion: string;
}

/**
 * A single edit the embedded AI assist can apply to the CURRENT automation when the user
 * asks it to change (not just extend) the flow. Ops reference existing block ids.
 *   add      — insert a new block (config `block`, parented via block.parentId)
 *   remove   — delete `blockId` and its descendants
 *   move     — reparent `blockId` under `parentId` (cycle-guarded, never the root trigger)
 *   update   — merge `config` into `blockId`'s existing config
 *   set_meta — rename the automation (`name` / `description`)
 */
export type BlockEditOp = 'add' | 'remove' | 'move' | 'update' | 'set_meta';

export interface BlockEdit {
  op: BlockEditOp;
  block?: FlowBlock;
  blockId?: string;
  parentId?: string;
  config?: Record<string, any>;
  name?: string;
  description?: string;
  /** One-line narration shown as the edit is applied. */
  note?: string;
}

export interface AutomationSpec {
  name: string;
  description: string;
  /** Canonical block tree — one root `event` block, children linked by parentId. */
  blocks: FlowBlock[];
  /** Why the AI built it this way — shown once, above the canvas. */
  rationale: string;
  /** blockId -> one-line narration surfaced as each block is dropped in. */
  block_notes: Record<string, string>;
  /** Things the user must fill before saving (playback pauses on each). */
  unresolved: UnresolvedItem[];
  /** Resources the AI proposes to create (a new monitor / workflow). */
  new_resources: NewResourceItem[];
  /** True if the spec used any cloud-only block. */
  requires_cloud: boolean;
  /**
   * Edit operations to apply to the CURRENT automation (augment/edit mode only). When present,
   * the assist MODIFIES the existing flow (add/remove/move/update/rename) rather than appending.
   */
  edits?: BlockEdit[];
  /** A conversational answer — used when the user ASKS about the automation (no edits). */
  message?: string;
}

export function emptySpec(): AutomationSpec {
  return {
    name: '',
    description: '',
    blocks: [],
    rationale: '',
    block_notes: {},
    unresolved: [],
    new_resources: [],
    requires_cloud: false,
  };
}
