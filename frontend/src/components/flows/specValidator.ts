// Validate + repair an AI-generated AutomationSpec before it is played into the canvas.
// This is defense-in-depth on the client; the server validates again on save.
//
// Guarantees after repair:
//   - exactly one root `event` block, valid parentId graph (no cycles/orphans),
//   - every blockType exists in the catalog FOR THIS PLATFORM (cloud-only blocks are
//     dropped on desktop and `requiresCloud` is flagged),
//   - every resource reference (workflow_id, target_id, …) either resolves to a real id
//     or is turned into an `unresolved` picker,
//   - every `matches` regex passes a ReDoS length/shape guard.

import { FlowBlock } from './types';
import { AutomationSpec, UnresolvedItem, BlockEdit } from './automationSpec';
import { BLOCK_CATALOG, BlockDef, BlockField, Platform, RefKind } from './blockCatalog';
import i18n from '../../i18n';

export interface ResourceIndex {
  workflows: Set<number>;
  targets: Set<number>;
  selectors: Set<number>;
  aiSessions: Set<number>;
  personas: Set<number>;
  files: Set<string>;
  recipients: Set<string>;
}

export interface ValidationResult {
  name: string;
  description: string;
  blocks: FlowBlock[];
  blockNotes: Record<string, string>;
  unresolved: UnresolvedItem[];
  requiresCloud: boolean;
  dropped: Array<{ blockType: string; reason: string }>;
  warnings: string[];
}

let idCounter = 0;
function genBlockId(): string {
  // Deterministic-enough unique id; avoids Date.now collisions during a fast playback.
  idCounter += 1;
  return `block_${Date.now()}_${idCounter}_${Math.random().toString(36).slice(2, 6)}`;
}

const MAX_BLOCKS = 40;
const MAX_REGEX_LEN = 512;

/** Cheap ReDoS heuristic — rejects overlong or nested-quantifier patterns. */
export function isRegexSafe(pattern: string): boolean {
  if (typeof pattern !== 'string') return false;
  if (pattern.length > MAX_REGEX_LEN) return false;
  // Nested quantifiers like (a+)+ / (a*)* / (a+)* are the classic catastrophic shapes.
  if (/\([^)]*[+*][^)]*\)\s*[+*]/.test(pattern)) return false;
  try {
    // eslint-disable-next-line no-new
    new RegExp(pattern);
  } catch {
    return false;
  }
  return true;
}

function refPool(index: ResourceIndex, ref: RefKind): { has: (v: any) => boolean; kindLabel: string } {
  switch (ref) {
    case 'workflow': return { has: (v) => index.workflows.has(Number(v)), kindLabel: 'workflow' };
    case 'target': return { has: (v) => index.targets.has(Number(v)), kindLabel: 'monitor' };
    case 'selector': return { has: (v) => index.selectors.has(Number(v)), kindLabel: 'selector' };
    case 'ai_session': return { has: (v) => index.aiSessions.has(Number(v)), kindLabel: 'AI session' };
    case 'persona': return { has: (v) => index.personas.has(Number(v)), kindLabel: 'persona' };
    case 'file': return { has: (v) => index.files.has(String(v)), kindLabel: 'file' };
    case 'recipient': return { has: (v) => index.recipients.has(String(v)), kindLabel: 'recipient' };
    default: return { has: () => true, kindLabel: ref };
  }
}

function unresolvedKindFor(ref: RefKind): UnresolvedItem['kind'] {
  switch (ref) {
    case 'persona': return 'persona';
    case 'file': return 'file';
    case 'recipient': return 'recipient';
    case 'selector': return 'selector';
    case 'workflow': return 'workflow';
    default: return 'value';
  }
}

export function validateAndRepairSpec(
  spec: AutomationSpec,
  platform: Platform,
  index: ResourceIndex,
): ValidationResult {
  const warnings: string[] = [];
  const dropped: Array<{ blockType: string; reason: string }> = [];
  const unresolved: UnresolvedItem[] = [];
  let requiresCloud = !!spec.requires_cloud;

  const rawBlocks = Array.isArray(spec.blocks) ? spec.blocks.slice(0, MAX_BLOCKS) : [];
  if (Array.isArray(spec.blocks) && spec.blocks.length > MAX_BLOCKS) {
    warnings.push(i18n.t('Automation was truncated to {{max}} blocks.', { max: MAX_BLOCKS }));
  }

  // 1. Ensure unique ids and drop unknown / unsupported block types.
  const seenIds = new Set<string>();
  const kept: FlowBlock[] = [];
  const idRemap: Record<string, string> = {};
  for (const b of rawBlocks) {
    if (!b || typeof b !== 'object' || !b.blockType) {
      dropped.push({ blockType: String(b?.blockType ?? 'unknown'), reason: 'malformed block' });
      continue;
    }
    const def = BLOCK_CATALOG[b.blockType];
    if (!def) {
      dropped.push({ blockType: b.blockType, reason: 'unknown block type' });
      continue;
    }
    if (def.status === 'planned') {
      dropped.push({ blockType: b.blockType, reason: 'not available yet' });
      continue;
    }
    if (!def.platforms.includes(platform)) {
      requiresCloud = requiresCloud || def.platforms.includes('cloud');
      dropped.push({ blockType: b.blockType, reason: def.cloudOnlyReason || 'not available on this platform' });
      continue;
    }
    let id = typeof b.id === 'string' && b.id ? b.id : genBlockId();
    if (seenIds.has(id)) {
      const fresh = genBlockId();
      idRemap[id] = fresh;
      id = fresh;
    } else if (typeof b.id === 'string' && b.id !== id) {
      idRemap[b.id] = id;
    }
    seenIds.add(id);
    kept.push({
      id,
      type: def.category,
      blockType: b.blockType,
      config: b.config && typeof b.config === 'object' ? { ...b.config } : {},
      parentId: b.parentId,
      children: undefined,
    });
  }

  // Re-point any parentId that referenced a remapped id.
  for (const b of kept) {
    if (b.parentId && idRemap[b.parentId]) b.parentId = idRemap[b.parentId];
  }

  // 2. Establish exactly one root event block.
  const eventBlocks = kept.filter((b) => b.type === 'event' && !b.parentId);
  let root: FlowBlock | undefined = eventBlocks[0];
  if (!root) {
    // Maybe an event block exists but was given a parentId — promote the first event.
    root = kept.find((b) => b.type === 'event');
    if (root) root.parentId = undefined;
  }
  if (!root) {
    return {
      name: (spec.name || '').trim(),
      description: (spec.description || '').trim(),
      blocks: [],
      blockNotes: spec.block_notes || {},
      unresolved: [],
      requiresCloud,
      dropped,
      warnings: [...warnings, i18n.t('The generated automation had no usable trigger block.')],
    };
  }
  // Any extra roots become children of the primary root (keep them, don't drop).
  for (const b of kept) {
    if (b !== root && !b.parentId) b.parentId = root.id;
  }

  // 3. Repair the parentId graph — drop orphans/cycles by reattaching to root.
  const byId = new Map(kept.map((b) => [b.id, b]));
  for (const b of kept) {
    if (b === root) continue;
    if (!b.parentId || !byId.has(b.parentId)) {
      b.parentId = root.id;
      continue;
    }
    // Walk up; if we don't reach root within N hops, it's a cycle → reattach.
    let cur: FlowBlock | undefined = b;
    let hops = 0;
    const path = new Set<string>();
    while (cur && cur.parentId && hops < MAX_BLOCKS) {
      if (path.has(cur.id)) { b.parentId = root.id; break; }
      path.add(cur.id);
      cur = byId.get(cur.parentId);
      hops += 1;
    }
  }

  // 4. Validate config: resource refs + regex safety.
  for (const b of kept) {
    const def = BLOCK_CATALOG[b.blockType] as BlockDef;
    for (const field of def.config) {
      validateField(b, field, index, unresolved);
    }
  }

  return {
    name: (spec.name || '').trim() || 'Untitled automation',
    description: (spec.description || '').trim(),
    blocks: kept,
    blockNotes: spec.block_notes || {},
    unresolved: dedupeUnresolved([...(spec.unresolved || []), ...unresolved]),
    requiresCloud,
    dropped,
    warnings,
  };
}

/**
 * Validate + repair the NEW blocks the AI returns when EXTENDING an existing automation
 * (the "Ask AI in the block editor" augment flow). Unlike validateAndRepairSpec, this does
 * NOT expect a root event block: the new blocks hang off the existing tree. Each new block's
 * parentId must resolve to an existing block id or to another (kept) new block — otherwise
 * the block is dropped. Ids that collide with existing blocks are re-minted.
 */
export function validateAndRepairAugment(
  spec: AutomationSpec,
  platform: Platform,
  index: ResourceIndex,
  existingIds: Set<string>,
): ValidationResult {
  const warnings: string[] = [];
  const dropped: Array<{ blockType: string; reason: string }> = [];
  const unresolved: UnresolvedItem[] = [];
  let requiresCloud = !!spec.requires_cloud;

  const rawBlocks = Array.isArray(spec.blocks) ? spec.blocks.slice(0, MAX_BLOCKS) : [];
  if (Array.isArray(spec.blocks) && spec.blocks.length > MAX_BLOCKS) {
    warnings.push(i18n.t('Automation was truncated to {{max}} blocks.', { max: MAX_BLOCKS }));
  }

  // 1. Keep only supported, non-duplicate blocks. Re-mint ids that clash with the existing
  //    tree (or with each other) so ADD_BLOCK never overwrites a real block.
  const seenNewIds = new Set<string>();
  const idRemap: Record<string, string> = {};
  const kept: FlowBlock[] = [];
  for (const b of rawBlocks) {
    if (!b || typeof b !== 'object' || !b.blockType) {
      dropped.push({ blockType: String(b?.blockType ?? 'unknown'), reason: 'malformed block' });
      continue;
    }
    const def = BLOCK_CATALOG[b.blockType];
    if (!def) {
      dropped.push({ blockType: b.blockType, reason: 'unknown block type' });
      continue;
    }
    if (def.status === 'planned') {
      dropped.push({ blockType: b.blockType, reason: 'not available yet' });
      continue;
    }
    if (!def.platforms.includes(platform)) {
      requiresCloud = requiresCloud || def.platforms.includes('cloud');
      dropped.push({ blockType: b.blockType, reason: def.cloudOnlyReason || 'not available on this platform' });
      continue;
    }
    // Extending: a second root event block makes no sense — drop it.
    if (def.category === 'event' && !b.parentId) {
      dropped.push({ blockType: b.blockType, reason: 'cannot add a second trigger' });
      continue;
    }
    let id = typeof b.id === 'string' && b.id ? b.id : genBlockId();
    if (existingIds.has(id) || seenNewIds.has(id)) {
      const fresh = genBlockId();
      if (typeof b.id === 'string' && b.id) idRemap[b.id] = fresh;
      id = fresh;
    } else if (typeof b.id === 'string' && b.id !== id) {
      idRemap[b.id] = id;
    }
    seenNewIds.add(id);
    kept.push({
      id,
      type: def.category,
      blockType: b.blockType,
      config: b.config && typeof b.config === 'object' ? { ...b.config } : {},
      parentId: b.parentId,
      children: undefined,
    });
  }

  // Re-point parentIds that referenced a remapped new-block id.
  for (const b of kept) {
    if (b.parentId && idRemap[b.parentId]) b.parentId = idRemap[b.parentId];
  }

  // 2. Every new block must attach to a real anchor: an EXISTING block id or another KEPT
  //    new block. Anything else is an orphan we can't safely place — drop it.
  const keptIds = new Set(kept.map((b) => b.id));
  const anchored: FlowBlock[] = [];
  for (const b of kept) {
    const parent = b.parentId;
    if (parent && (existingIds.has(parent) || keptIds.has(parent))) {
      anchored.push(b);
    } else {
      dropped.push({ blockType: b.blockType, reason: 'new block did not attach to the existing automation' });
    }
  }

  // 3. Validate config: resource refs + regex safety (same rules as a fresh build).
  for (const b of anchored) {
    const def = BLOCK_CATALOG[b.blockType] as BlockDef;
    for (const field of def.config) {
      validateField(b, field, index, unresolved);
    }
  }

  return {
    name: (spec.name || '').trim(),
    description: (spec.description || '').trim(),
    blocks: anchored,
    blockNotes: spec.block_notes || {},
    unresolved: dedupeUnresolved([...(spec.unresolved || []), ...unresolved]),
    requiresCloud,
    dropped,
    warnings,
  };
}

function validateField(
  block: FlowBlock,
  field: BlockField,
  index: ResourceIndex,
  unresolved: UnresolvedItem[],
): void {
  const val = block.config?.[field.key];

  if (field.type === 'ref' && field.ref && val != null && val !== '') {
    const pool = refPool(index, field.ref);
    if (!pool.has(val)) {
      // The AI referenced an id we don't have — clear it and ask the user to pick.
      delete block.config[field.key];
      unresolved.push({
        blockId: block.id,
        field: field.key,
        kind: unresolvedKindFor(field.ref),
        question: i18n.t('Pick a {{kind}} for “{{block}}”.', {
          kind: i18n.t(pool.kindLabel),
          block: i18n.t(BLOCK_CATALOG[block.blockType].label),
        }),
      });
    }
  }

  if (block.blockType === 'condition' && field.key === 'value' && block.config?.operator === 'matches') {
    if (typeof val === 'string' && val && !isRegexSafe(val)) {
      delete block.config.value;
      unresolved.push({
        blockId: block.id,
        field: 'value',
        kind: 'value',
        question: i18n.t('The generated regex was rejected as unsafe — enter a pattern.'),
      });
    }
  }
}

function dedupeUnresolved(items: UnresolvedItem[]): UnresolvedItem[] {
  const seen = new Set<string>();
  const out: UnresolvedItem[] = [];
  for (const it of items) {
    if (!it || !it.blockId || !it.field) continue;
    const key = `${it.blockId}:${it.field}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(it);
  }
  return out;
}

// --- Edit operations (the embedded assist modifying the current automation) ------------------

export interface EditValidationResult {
  /** Sanitized, ordered ops safe to apply in sequence. */
  edits: BlockEdit[];
  /** blockId -> narration for `add`ed blocks. */
  addedNotes: Record<string, string>;
  unresolved: UnresolvedItem[];
  dropped: Array<{ blockType: string; reason: string }>;
  warnings: string[];
  requiresCloud: boolean;
  /** A conversational answer (when the user asked about the flow rather than editing it). */
  message: string;
}

function descendantIdSet(blocks: FlowBlock[], parentId: string): Set<string> {
  const ids = new Set<string>();
  const walk = (pid: string) => {
    for (const b of blocks) {
      if (b.parentId === pid && !ids.has(b.id)) {
        ids.add(b.id);
        walk(b.id);
      }
    }
  };
  walk(parentId);
  return ids;
}

/**
 * Validate the AI's edit operations against the CURRENT tree, catalog, and platform. Ops are
 * simulated against a working copy so later ops see the effect of earlier ones (an `add`ed id
 * becomes a valid anchor; a `remove`d subtree stops anchoring). Invalid ops are dropped with a
 * reason rather than applied, so a bad op can never corrupt the flow.
 */
export function validateEdits(
  spec: AutomationSpec,
  platform: Platform,
  index: ResourceIndex,
  currentBlocks: FlowBlock[],
): EditValidationResult {
  const warnings: string[] = [];
  const dropped: Array<{ blockType: string; reason: string }> = [];
  const unresolved: UnresolvedItem[] = [];
  const addedNotes: Record<string, string> = {};
  let requiresCloud = !!spec.requires_cloud;

  const rawEdits = Array.isArray(spec.edits) ? spec.edits.slice(0, MAX_BLOCKS) : [];
  if (Array.isArray(spec.edits) && spec.edits.length > MAX_BLOCKS) {
    warnings.push(i18n.t('Only the first {{max}} edits were applied.', { max: MAX_BLOCKS }));
  }

  // Working copy of the tree so anchor/cycle checks reflect prior ops in this batch.
  const work: FlowBlock[] = currentBlocks.map((b) => ({ ...b }));
  const byId = (id: string) => work.find((b) => b.id === id);
  const out: BlockEdit[] = [];

  for (const e of rawEdits) {
    if (!e || typeof e !== 'object') continue;
    switch (e.op) {
      case 'set_meta': {
        if (typeof e.name === 'string' || typeof e.description === 'string') {
          out.push({ op: 'set_meta', name: e.name, description: e.description, note: e.note });
        }
        break;
      }
      case 'remove': {
        const target = e.blockId && byId(e.blockId);
        if (!target) {
          dropped.push({ blockType: 'remove', reason: 'block to remove not found' });
          break;
        }
        if (!target.parentId) {
          dropped.push({ blockType: 'remove', reason: 'cannot remove the root trigger' });
          break;
        }
        const gone = descendantIdSet(work, target.id);
        gone.add(target.id);
        for (let i = work.length - 1; i >= 0; i -= 1) if (gone.has(work[i].id)) work.splice(i, 1);
        out.push({ op: 'remove', blockId: target.id, note: e.note });
        break;
      }
      case 'move': {
        const target = e.blockId && byId(e.blockId);
        const parent = e.parentId && byId(e.parentId);
        if (!target || !parent) {
          dropped.push({ blockType: 'move', reason: 'block or destination not found' });
          break;
        }
        if (!target.parentId) {
          dropped.push({ blockType: 'move', reason: 'cannot move the root trigger' });
          break;
        }
        if (target.id === parent.id || descendantIdSet(work, target.id).has(parent.id)) {
          dropped.push({ blockType: 'move', reason: 'would create a cycle' });
          break;
        }
        target.parentId = parent.id; // reflect in the working copy
        out.push({ op: 'move', blockId: target.id, parentId: parent.id, note: e.note });
        break;
      }
      case 'update': {
        const target = e.blockId && byId(e.blockId);
        if (!target) {
          dropped.push({ blockType: 'update', reason: 'block to update not found' });
          break;
        }
        const merged: FlowBlock = { ...target, config: { ...(target.config || {}), ...(e.config || {}) } };
        const def = BLOCK_CATALOG[merged.blockType];
        if (def) for (const field of def.config) validateField(merged, field, index, unresolved);
        target.config = merged.config; // reflect in the working copy
        out.push({ op: 'update', blockId: target.id, config: merged.config, note: e.note });
        break;
      }
      case 'add': {
        const b = e.block;
        if (!b || typeof b !== 'object' || !b.blockType) {
          dropped.push({ blockType: 'add', reason: 'malformed block' });
          break;
        }
        const def = BLOCK_CATALOG[b.blockType];
        if (!def) { dropped.push({ blockType: b.blockType, reason: 'unknown block type' }); break; }
        if (def.status === 'planned') { dropped.push({ blockType: b.blockType, reason: 'not available yet' }); break; }
        if (!def.platforms.includes(platform)) {
          requiresCloud = requiresCloud || def.platforms.includes('cloud');
          dropped.push({ blockType: b.blockType, reason: def.cloudOnlyReason || 'not available on this platform' });
          break;
        }
        if (def.category === 'event' && !b.parentId) {
          dropped.push({ blockType: b.blockType, reason: 'cannot add a second trigger' });
          break;
        }
        if (!b.parentId || !byId(b.parentId)) {
          dropped.push({ blockType: b.blockType, reason: 'new block did not attach to the automation' });
          break;
        }
        let id = typeof b.id === 'string' && b.id ? b.id : genBlockId();
        if (byId(id)) id = genBlockId(); // never overwrite a real block
        const block: FlowBlock = {
          id,
          type: def.category,
          blockType: b.blockType,
          config: b.config && typeof b.config === 'object' ? { ...b.config } : {},
          parentId: b.parentId,
        };
        for (const field of def.config) validateField(block, field, index, unresolved);
        work.push(block);
        if (e.note) addedNotes[id] = e.note;
        out.push({ op: 'add', block, note: e.note });
        break;
      }
      default:
        dropped.push({ blockType: String(e.op ?? 'unknown'), reason: 'unknown edit op' });
    }
  }

  return {
    edits: out,
    addedNotes,
    unresolved: dedupeUnresolved([...(spec.unresolved || []), ...unresolved]),
    dropped,
    warnings,
    requiresCloud,
    message: typeof spec.message === 'string' ? spec.message : '',
  };
}

/** Build a ResourceIndex from the FlowBuilder reference-data lists. */
export function buildResourceIndex(state: {
  workflows?: Array<{ id: number }>;
  targets?: Array<{ id: number }>;
  sessions?: Array<{ id: number }>;
  personas?: Array<{ id: number }>;
  recipients?: Array<{ id: number | string; provider?: string }>;
  selectors?: Array<{ id: number }>;
  files?: Array<{ id: string }>;
}): ResourceIndex {
  return {
    workflows: new Set((state.workflows || []).map((w) => Number(w.id))),
    targets: new Set((state.targets || []).map((t) => Number(t.id))),
    selectors: new Set((state.selectors || []).map((s) => Number(s.id))),
    aiSessions: new Set((state.sessions || []).map((s) => Number(s.id))),
    personas: new Set((state.personas || []).map((p) => Number(p.id))),
    files: new Set((state.files || []).map((f) => String(f.id))),
    recipients: new Set(
      (state.recipients || []).map((r) => (r.provider ? `${r.provider}:${r.id}` : String(r.id))),
    ),
  };
}
