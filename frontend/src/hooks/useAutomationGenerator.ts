// The interactive AI automation build: goal in -> AutomationSpec -> validate/repair ->
// reveal the canvas -> drop blocks in one-by-one with narration -> pause for anything the
// user must resolve -> leave the flow dirty for review + Save.
//
// It drives the existing FlowBuilder reducer (SET_BLOCKS / ADD_BLOCK / SET_META /
// SELECT_BLOCK), so nothing new is needed on the persistence side — FlowBuilder.handleSave
// already stores whatever tree ends up in state.

import { useCallback, useRef, useState } from 'react';
import { useFlowBuilder } from '../components/flows/FlowBuilderContext';
import { FlowBlock } from '../components/flows/types';
import { catalogPromptDigest } from '../components/flows/blockCatalog';
import {
  validateAndRepairSpec,
  validateAndRepairAugment,
  validateEdits,
  buildResourceIndex,
} from '../components/flows/specValidator';
import { UnresolvedItem, BlockEdit } from '../components/flows/automationSpec';
import { aiAssistApi, APP_PLATFORM, ResourceContext } from '../api/aiAssist';
import i18n from '../i18n';

export type GenPhase = 'idle' | 'generating' | 'error' | 'playing' | 'review' | 'done';

export interface GeneratorState {
  phase: GenPhase;
  error: string | null;
  rationale: string;
  /** Live narration line for the block currently being dropped in. */
  narration: string | null;
  unresolved: UnresolvedItem[];
  requiresCloud: boolean;
  dropped: Array<{ blockType: string; reason: string }>;
  source: 'local' | 'cloud' | null;
  /** Conversational answer when the user ASKED the assist about the automation. */
  message: string | null;
}

const PLAYBACK_BEAT_MS = 380;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function buildResourceContext(state: any): ResourceContext {
  return {
    workflows: (state.workflows || []).map((w: any) => ({
      id: w.id,
      name: w.name,
      type: w.workflow_type,
      has_login: w.config?.workflow_has_login || undefined,
      outputs: Array.isArray(w.outputs) ? w.outputs.map((o: any) => o.key || o) : undefined,
    })),
    monitors: (state.targets || []).map((t: any) => ({ id: t.id, url: t.url })),
    ai_sessions: (state.sessions || []).map((s: any) => ({ id: s.id, name: s.name, goal: s.goal })),
    personas: (state.personas || []).map((p: any) => ({ id: p.id, name: p.name, domain: p.target_domain })),
    files: (state.files || []).map((f: any) => ({ id: f.id, filename: f.filename })),
  };
}

/** Root first, then each level of children — a stable, readable drop order. */
function orderBlocks(blocks: FlowBlock[], rootId: string): FlowBlock[] {
  const byParent = new Map<string, FlowBlock[]>();
  for (const b of blocks) {
    const key = b.parentId || '__root__';
    if (!byParent.has(key)) byParent.set(key, []);
    byParent.get(key)!.push(b);
  }
  const out: FlowBlock[] = [];
  const root = blocks.find((b) => b.id === rootId);
  if (!root) return blocks;
  const queue: FlowBlock[] = [root];
  const seen = new Set<string>();
  while (queue.length) {
    const b = queue.shift()!;
    if (seen.has(b.id)) continue;
    seen.add(b.id);
    out.push(b);
    for (const child of byParent.get(b.id) || []) queue.push(child);
  }
  // Include any strays not reached (shouldn't happen after validation).
  for (const b of blocks) if (!seen.has(b.id)) out.push(b);
  return out;
}

/**
 * Order NEW blocks so a parent is always added before its child when BOTH are new.
 * Anchors already in `existingIds` are considered available from the start.
 */
function orderNewBlocks(blocks: FlowBlock[], existingIds: Set<string>): FlowBlock[] {
  const available = new Set(existingIds);
  const remaining = [...blocks];
  const out: FlowBlock[] = [];
  // Repeatedly take blocks whose parent is already available; guard against loops.
  let guard = remaining.length + 1;
  while (remaining.length && guard > 0) {
    guard -= 1;
    for (let i = 0; i < remaining.length; i += 1) {
      const b = remaining[i];
      if (!b.parentId || available.has(b.parentId)) {
        out.push(b);
        available.add(b.id);
        remaining.splice(i, 1);
        i -= 1;
      }
    }
  }
  // Anything left (unexpected after validation) is appended in its given order.
  return [...out, ...remaining];
}

export function useAutomationGenerator() {
  const { state, dispatch } = useFlowBuilder();
  const [gen, setGen] = useState<GeneratorState>({
    phase: 'idle',
    error: null,
    rationale: '',
    narration: null,
    unresolved: [],
    requiresCloud: false,
    dropped: [],
    source: null,
    message: null,
  });
  const cancelRef = useRef(false);
  const skipRef = useRef(false);
  const orderedRef = useRef<FlowBlock[] | null>(null);

  const reset = useCallback(() => {
    cancelRef.current = true;
    setGen((g) => ({ ...g, phase: 'idle', error: null, narration: null, message: null }));
  }, []);

  /**
   * Apply the AI's edit operations to the current tree, one at a time with narration —
   * add / remove / move (reparent) / update (config) / set_meta (rename). The reducer guards
   * cycles + root moves; the validator has already dropped invalid ops.
   */
  const applyEdits = useCallback(
    async (edits: BlockEdit[], addedNotes: Record<string, string>, instant: boolean) => {
      orderedRef.current = null;
      for (let i = 0; i < edits.length; i += 1) {
        if (cancelRef.current) return;
        if (!instant && i > 0) await sleep(PLAYBACK_BEAT_MS);
        if (cancelRef.current) return;
        const e = edits[i];
        switch (e.op) {
          case 'add':
            if (e.block) {
              dispatch({ type: 'ADD_BLOCK', block: e.block });
              dispatch({ type: 'SELECT_BLOCK', blockId: e.block.id });
            }
            break;
          case 'remove':
            if (e.blockId) dispatch({ type: 'REMOVE_BLOCK', blockId: e.blockId });
            break;
          case 'move':
            if (e.blockId && e.parentId) {
              dispatch({ type: 'MOVE_BLOCK', blockId: e.blockId, parentId: e.parentId });
              dispatch({ type: 'SELECT_BLOCK', blockId: e.blockId });
            }
            break;
          case 'update':
            if (e.blockId && e.config) {
              dispatch({ type: 'UPDATE_BLOCK_CONFIG', blockId: e.blockId, config: e.config });
              dispatch({ type: 'SELECT_BLOCK', blockId: e.blockId });
            }
            break;
          case 'set_meta':
            dispatch({ type: 'SET_META', name: e.name, description: e.description });
            break;
        }
        const note = e.note || (e.op === 'add' && e.block ? addedNotes[e.block.id] : null) || null;
        setGen((g) => ({ ...g, narration: note }));
      }
    },
    [dispatch],
  );

  /** Drop the rest of the tree at once (the "Skip animation" affordance). */
  const skip = useCallback(() => {
    skipRef.current = true;
    if (orderedRef.current) dispatch({ type: 'SET_BLOCKS', blocks: orderedRef.current });
  }, [dispatch]);

  const playback = useCallback(
    async (
      blocks: FlowBlock[],
      rootId: string,
      notes: Record<string, string>,
      instant: boolean,
    ) => {
      const ordered = orderBlocks(blocks, rootId);
      orderedRef.current = ordered;
      // Reveal the canvas with just the root (SourceBlockPicker unmounts on first block).
      dispatch({ type: 'SET_BLOCKS', blocks: [ordered[0]] });
      dispatch({ type: 'SELECT_BLOCK', blockId: ordered[0].id });
      setGen((g) => ({ ...g, narration: notes[ordered[0].id] || null }));
      if (instant) {
        dispatch({ type: 'SET_BLOCKS', blocks: ordered });
        return;
      }
      for (let i = 1; i < ordered.length; i += 1) {
        if (cancelRef.current) return;
        if (skipRef.current) {
          dispatch({ type: 'SET_BLOCKS', blocks: ordered });
          return;
        }
        await sleep(PLAYBACK_BEAT_MS);
        if (cancelRef.current) return;
        dispatch({ type: 'ADD_BLOCK', block: ordered[i] });
        dispatch({ type: 'SELECT_BLOCK', blockId: ordered[i].id });
        setGen((g) => ({ ...g, narration: notes[ordered[i].id] || null }));
      }
    },
    [dispatch],
  );

  const generate = useCallback(
    async (goal: string, opts?: { url?: string; instant?: boolean }) => {
      cancelRef.current = false;
      skipRef.current = false;
      orderedRef.current = null;
      setGen((g) => ({ ...g, phase: 'generating', error: null, narration: null, dropped: [], message: null }));
      try {
        const res = await aiAssistApi.generateAutomation({
          goal,
          url: opts?.url,
          platform: APP_PLATFORM,
          catalog_digest: catalogPromptDigest(APP_PLATFORM),
          resource_context: buildResourceContext(state),
        });
        if (cancelRef.current) return;

        const index = buildResourceIndex(state as any);
        const v = validateAndRepairSpec(res.automation, APP_PLATFORM, index);
        if (!v.blocks.length) {
          setGen((g) => ({
            ...g,
            phase: 'error',
            error: v.warnings.join(' ') || i18n.t('The AI could not build a usable automation. Try rephrasing.'),
          }));
          return;
        }

        dispatch({ type: 'SET_META', name: v.name, description: v.description });
        setGen((g) => ({
          ...g,
          phase: 'playing',
          rationale: res.automation.rationale || '',
          requiresCloud: v.requiresCloud,
          dropped: v.dropped,
          source: res.source || null,
          unresolved: v.unresolved,
        }));

        const rootId = v.blocks.find((b) => b.type === 'event' && !b.parentId)!.id;
        await playback(v.blocks, rootId, v.blockNotes, !!opts?.instant);
        if (cancelRef.current) return;

        setGen((g) => ({
          ...g,
          phase: v.unresolved.length ? 'review' : 'done',
          narration: null,
        }));
      } catch (e: any) {
        if (cancelRef.current) return;
        const status = e?.response?.status;
        const detail =
          e?.response?.data?.error || e?.response?.data?.detail || e?.message || i18n.t('Generation failed');
        setGen((g) => ({
          ...g,
          phase: 'error',
          // 400 from the daemon == no provider configured; surface the actionable hint verbatim.
          error: status === 400 ? String(detail) : i18n.t("Couldn't generate the automation: {{detail}}", { detail }),
        }));
      }
    },
    [state, dispatch, playback],
  );

  /**
   * Append newly-generated blocks one-by-one WITHOUT resetting the existing tree.
   * `anchored` blocks are already ordered so a parent is added before its children
   * when both are new; existing parents are already in state.
   */
  const appendPlayback = useCallback(
    async (blocks: FlowBlock[], notes: Record<string, string>, instant: boolean) => {
      orderedRef.current = null; // skip() targets from-scratch playback only; disable it here.
      for (let i = 0; i < blocks.length; i += 1) {
        if (cancelRef.current) return;
        if (!instant && i > 0) await sleep(PLAYBACK_BEAT_MS);
        if (cancelRef.current) return;
        dispatch({ type: 'ADD_BLOCK', block: blocks[i] });
        dispatch({ type: 'SELECT_BLOCK', blockId: blocks[i].id });
        setGen((g) => ({ ...g, narration: notes[blocks[i].id] || null }));
      }
    },
    [dispatch],
  );

  /**
   * "Ask AI in the block editor": EXTEND the current automation instead of building from
   * scratch. Sends the current tree as context; the AI returns ONLY new blocks parented onto
   * existing (or other new) blocks; we validate + append them without disturbing what's there.
   */
  const augment = useCallback(
    async (goal: string, opts?: { instant?: boolean }) => {
      cancelRef.current = false;
      skipRef.current = false;
      orderedRef.current = null;
      setGen((g) => ({ ...g, phase: 'generating', error: null, narration: null, dropped: [], message: null }));
      try {
        const res = await aiAssistApi.generateAutomation({
          goal,
          platform: APP_PLATFORM,
          catalog_digest: catalogPromptDigest(APP_PLATFORM),
          resource_context: buildResourceContext(state),
          current_automation: {
            name: state.name,
            description: state.description,
            blocks: state.blocks,
          },
        });
        if (cancelRef.current) return;

        const index = buildResourceIndex(state as any);
        const spec = res.automation;

        // EDIT MODE: the AI returned operations to modify the current flow
        // (add / remove / move / update / rename).
        if (Array.isArray(spec.edits) && spec.edits.length) {
          const ev = validateEdits(spec, APP_PLATFORM, index, state.blocks);
          if (!ev.edits.length) {
            // Nothing applicable — treat as an answer if we got one, else surface why.
            setGen((g) => ({
              ...g,
              phase: ev.message ? 'done' : 'error',
              message: ev.message || null,
              error: ev.message ? null : ev.warnings.join(' ') || i18n.t('No applicable changes were found.'),
              narration: null,
            }));
            return;
          }
          setGen((g) => ({
            ...g,
            phase: 'playing',
            rationale: spec.rationale || '',
            requiresCloud: ev.requiresCloud,
            dropped: ev.dropped,
            source: res.source || null,
            unresolved: ev.unresolved,
            message: ev.message || null,
          }));
          await applyEdits(ev.edits, ev.addedNotes, !!opts?.instant);
          if (cancelRef.current) return;
          setGen((g) => ({
            ...g,
            phase: ev.unresolved.length ? 'review' : 'done',
            narration: null,
          }));
          return;
        }

        // ANSWER MODE: the user asked ABOUT the automation (no structural change).
        if (spec.message && (!Array.isArray(spec.blocks) || !spec.blocks.length)) {
          setGen((g) => ({ ...g, phase: 'done', message: spec.message || null, narration: null }));
          return;
        }

        // APPEND MODE (fallback): the AI returned new blocks to graft onto the tree.
        const existingIds = new Set(state.blocks.map((b) => b.id));
        const v = validateAndRepairAugment(spec, APP_PLATFORM, index, existingIds);
        if (!v.blocks.length) {
          setGen((g) => ({
            ...g,
            phase: 'error',
            error:
              v.warnings.join(' ') ||
              i18n.t('The AI did not return any changes to make. Try rephrasing.'),
          }));
          return;
        }

        setGen((g) => ({
          ...g,
          phase: 'playing',
          rationale: spec.rationale || '',
          requiresCloud: v.requiresCloud,
          dropped: v.dropped,
          source: res.source || null,
          unresolved: v.unresolved,
        }));

        const ordered = orderNewBlocks(v.blocks, existingIds);
        await appendPlayback(ordered, v.blockNotes, !!opts?.instant);
        if (cancelRef.current) return;

        setGen((g) => ({
          ...g,
          phase: v.unresolved.length ? 'review' : 'done',
          narration: null,
        }));
      } catch (e: any) {
        if (cancelRef.current) return;
        const status = e?.response?.status;
        const detail =
          e?.response?.data?.error || e?.response?.data?.detail || e?.message || i18n.t('Generation failed');
        setGen((g) => ({
          ...g,
          phase: 'error',
          error: status === 400 ? String(detail) : i18n.t("Couldn't extend the automation: {{detail}}", { detail }),
        }));
      }
    },
    [state, appendPlayback, applyEdits],
  );

  /** Jump to a block that still needs input (used by the review checklist). */
  const focusBlock = useCallback(
    (blockId: string) => dispatch({ type: 'SELECT_BLOCK', blockId }),
    [dispatch],
  );

  /**
   * Apply an inline "AI asks you to pick" choice: merge { [field]: value } into the
   * block's config, drop the matching unresolved item, and finish when nothing is left.
   */
  const resolveItem = useCallback(
    (blockId: string, field: string, value: any) => {
      const block = state.blocks.find((b) => b.id === blockId);
      if (block) {
        dispatch({
          type: 'UPDATE_BLOCK_CONFIG',
          blockId,
          config: { ...(block.config || {}), [field]: value },
        });
      }
      setGen((g) => {
        const unresolved = g.unresolved.filter(
          (u) => !(u.blockId === blockId && u.field === field),
        );
        return {
          ...g,
          unresolved,
          phase: unresolved.length ? g.phase : 'done',
        };
      });
    },
    [state.blocks, dispatch],
  );

  return { gen, generate, augment, reset, skip, focusBlock, resolveItem };
}
