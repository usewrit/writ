import React, { createContext, useContext, useEffect, useCallback, useMemo, useRef } from 'react';
import { createStore, type StoreApi } from 'zustand/vanilla';
import { useStoreWithEqualityFn } from 'zustand/traditional';
import {
  FlowBlock,
  BlockType,
  BlockMeta,
  TriggerRule,
  AISession,
  TargetSelector,
  Workflow,
  Recipient,
  TargetInfo,
} from './types';
import {
  BellIcon,
  BoltIcon,
  CpuChipIcon,
  Cog6ToothIcon,
  CheckCircleIcon,
  FunnelIcon,
  TableCellsIcon,
  DocumentArrowUpIcon,
  SignalIcon,
  ArrowPathRoundedSquareIcon,
} from '@heroicons/react/24/outline';
import { ArrowUturnLeftIcon } from '@heroicons/react/24/outline';
import { aiSessionsApi, selectorsApi, automationApi, recipientsApi, targetsApi } from '../../api/endpoints';
import i18n from '../../i18n';
import { isBlockAvailable, blockOutputTokens, Platform } from './blockCatalog';

// --- State ---

export interface FlowBuilderState {
  flowId: number | null;
  name: string;
  description: string;
  enabled: boolean;
  blocks: FlowBlock[];
  targets: TargetInfo[];
  workflows: Workflow[];
  sessions: AISession[];
  recipients: Recipient[];
  selectors: TargetSelector[];
  selectedBlockId: string | null;
  isDirty: boolean;
  saving: boolean;
  saveError: string | null;
  loading: boolean;
  expandedAdvancedBlocks: Set<string>;
  /**
   * blockId → placeholder tokens (without braces) that block produces for
   * downstream blocks, derived from the catalog `produces` plus role. Recomputed
   * whenever the block list changes (add/update/remove/load).
   */
  blockOutputs: Record<string, string[]>;
}

const initialState: FlowBuilderState = {
  flowId: null,
  name: '',
  description: '',
  enabled: true,
  blocks: [],
  targets: [],
  workflows: [],
  sessions: [],
  recipients: [],
  selectors: [],
  selectedBlockId: null,
  isDirty: false,
  saving: false,
  saveError: null,
  loading: true,
  expandedAdvancedBlocks: new Set(),
  blockOutputs: {},
};

/**
 * Derive the produced-output map (blockId → placeholder tokens) from the current
 * block list. Tokens come from the catalog `produces` for each block's type
 * (change_detected → extracted.*, a workflow action → result.* / success / status,
 * etc.). Pure — safe to call on every block mutation.
 */
function computeBlockOutputs(blocks: FlowBlock[]): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const b of blocks) {
    const tokens = blockOutputTokens(b.blockType);
    if (tokens.length > 0) out[b.id] = tokens;
  }
  return out;
}

// --- Actions ---

type Action =
  | { type: 'LOAD_FLOW'; flow: TriggerRule; dirty?: boolean }
  | { type: 'SET_META'; name?: string; description?: string; enabled?: boolean }
  | { type: 'SET_BLOCKS'; blocks: FlowBlock[] }
  | { type: 'ADD_BLOCK'; block: FlowBlock }
  | { type: 'REMOVE_BLOCK'; blockId: string }
  | { type: 'MOVE_BLOCK'; blockId: string; parentId: string }
  | { type: 'UPDATE_BLOCK_CONFIG'; blockId: string; config: any }
  | { type: 'UPDATE_BLOCK_TYPE'; blockId: string; blockType: string }
  | { type: 'SET_REFERENCE_DATA'; targets?: TargetInfo[]; workflows?: Workflow[]; sessions?: AISession[]; recipients?: Recipient[]; selectors?: TargetSelector[] }
  | { type: 'SET_SELECTORS'; selectors: TargetSelector[] }
  | { type: 'SELECT_BLOCK'; blockId: string | null }
  | { type: 'SET_SAVING'; saving: boolean; error?: string | null }
  | { type: 'MARK_SAVED'; flowId: number }
  | { type: 'SET_LOADING'; loading: boolean }
  | { type: 'TOGGLE_ADVANCED'; blockId: string }
  | { type: 'RESET' };

function reducer(state: FlowBuilderState, action: Action): FlowBuilderState {
  switch (action.type) {
    case 'LOAD_FLOW': {
      const flow = action.flow;
      const parsedBlocks = coerceBlocks(flow.blocks);
      let blocks = (parsedBlocks && parsedBlocks.length > 0)
        ? parsedBlocks
        : reconstructBlocksFromLegacy(flow);
      // Hydrate firing guardrails (stored in `conditions`) back onto the root
      // event block config so the source panel's Guardrails section reflects them.
      const cond: any = flow.conditions || {};
      const cooldownMin = cond?.schedule?.cooldown_minutes;
      const maxFires = cond?.max_fires;
      if (cooldownMin || maxFires) {
        blocks = blocks.map(b =>
          b.type === 'event' && !b.parentId
            ? { ...b, config: {
                ...b.config,
                ...(cooldownMin ? { cooldown_minutes: cooldownMin } : {}),
                ...(maxFires ? { max_fires: maxFires } : {}),
              } }
            : b
        );
      }
      return {
        ...state,
        flowId: flow.id,
        name: flow.name,
        description: flow.description || '',
        enabled: flow.enabled,
        blocks,
        blockOutputs: computeBlockOutputs(blocks),
        // A restored hand-off draft carries edits that were never persisted, so it
        // loads dirty — otherwise Save stays disabled and the work is stranded.
        isDirty: !!action.dirty,
      };
    }
    case 'SET_META':
      return {
        ...state,
        ...(action.name !== undefined && { name: action.name }),
        ...(action.description !== undefined && { description: action.description }),
        ...(action.enabled !== undefined && { enabled: action.enabled }),
        isDirty: true,
      };
    case 'SET_BLOCKS':
      return { ...state, blocks: action.blocks, blockOutputs: computeBlockOutputs(action.blocks), isDirty: true };
    case 'ADD_BLOCK': {
      const blocks = [...state.blocks, action.block];
      return { ...state, blocks, blockOutputs: computeBlockOutputs(blocks), isDirty: true };
    }
    case 'REMOVE_BLOCK': {
      const idsToRemove = collectDescendantIds(state.blocks, action.blockId);
      idsToRemove.add(action.blockId);
      const blocks = state.blocks.filter(b => !idsToRemove.has(b.id));
      return { ...state, blocks, blockOutputs: computeBlockOutputs(blocks), isDirty: true };
    }
    case 'MOVE_BLOCK': {
      // Reparent a block, guarding: never the root trigger, the destination must exist, and
      // never under itself or a descendant (that would orphan/cycle the subtree).
      const target = state.blocks.find(b => b.id === action.blockId);
      if (!target || !target.parentId || action.blockId === action.parentId) return state;
      if (!state.blocks.some(b => b.id === action.parentId)) return state;
      const descendants = collectDescendantIds(state.blocks, action.blockId);
      if (descendants.has(action.parentId)) return state;
      const blocks = state.blocks.map(b => b.id === action.blockId ? { ...b, parentId: action.parentId } : b);
      return { ...state, blocks, blockOutputs: computeBlockOutputs(blocks), isDirty: true };
    }
    case 'UPDATE_BLOCK_CONFIG':
      return {
        ...state,
        blocks: state.blocks.map(b => b.id === action.blockId ? { ...b, config: action.config } : b),
        isDirty: true,
      };
    case 'UPDATE_BLOCK_TYPE': {
      const blocks = state.blocks.map(b => b.id === action.blockId ? { ...b, blockType: action.blockType } : b);
      return {
        ...state,
        blocks,
        blockOutputs: computeBlockOutputs(blocks),
        isDirty: true,
      };
    }
    case 'SET_REFERENCE_DATA':
      return {
        ...state,
        ...(action.targets && { targets: action.targets }),
        ...(action.workflows && { workflows: action.workflows }),
        ...(action.sessions && { sessions: action.sessions }),
        ...(action.recipients && { recipients: action.recipients }),
        ...(action.selectors && { selectors: action.selectors }),
      };
    case 'SET_SELECTORS':
      return { ...state, selectors: action.selectors };
    case 'SELECT_BLOCK':
      return { ...state, selectedBlockId: action.blockId };
    case 'SET_SAVING':
      return { ...state, saving: action.saving, saveError: action.error ?? state.saveError };
    case 'MARK_SAVED':
      return { ...state, flowId: action.flowId, isDirty: false, saving: false, saveError: null };
    case 'SET_LOADING':
      return { ...state, loading: action.loading };
    case 'TOGGLE_ADVANCED': {
      const next = new Set(state.expandedAdvancedBlocks);
      if (next.has(action.blockId)) next.delete(action.blockId);
      else next.add(action.blockId);
      return { ...state, expandedAdvancedBlocks: next };
    }
    case 'RESET':
      return { ...initialState, targets: state.targets, workflows: state.workflows, sessions: state.sessions, recipients: state.recipients };
    default:
      return state;
  }
}

// --- Helpers ---

function collectDescendantIds(blocks: FlowBlock[], parentId: string): Set<string> {
  const ids = new Set<string>();
  const children = blocks.filter(b => b.parentId === parentId);
  for (const child of children) {
    ids.add(child.id);
    const deeper = collectDescendantIds(blocks, child.id);
    deeper.forEach(id => ids.add(id));
  }
  return ids;
}

// Normalize `blocks` from either shape: the cloud returns a parsed array; the desktop daemon
// returns a JSON-TEXT string. Parse a string, keep an array, else null (→ legacy reconstruction).
// Guards against a string `blocks` becoming `state.blocks` (→ `blocks.filter is not a function`).
function coerceBlocks(raw: any): FlowBlock[] | null {
  if (Array.isArray(raw)) return raw as FlowBlock[];
  if (typeof raw === 'string' && raw.trim()) {
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) return parsed as FlowBlock[];
    } catch {
      /* not JSON — fall through to legacy reconstruction */
    }
  }
  return null;
}

function reconstructBlocksFromLegacy(flow: TriggerRule): FlowBlock[] {
  const blocks: FlowBlock[] = [];
  blocks.push({
    id: 'block_event',
    type: 'event',
    blockType: flow.event_type,
    config: {
      selector_id: flow.target_selector_id,
      ai_session_id: flow.ai_session_id,
      workflow_id: flow.workflow_id,
    },
  });
  if (flow.conditions) {
    const entries = Object.entries(flow.conditions);
    entries.forEach(([field, cond], i) => {
      blocks.push({
        id: `block_cond_${i}`,
        type: 'condition',
        blockType: 'condition',
        config: { field, operator: (cond as any).operator || 'contains', value: (cond as any).value || '' },
        parentId: i === 0 ? 'block_event' : `block_cond_${i - 1}`,
      });
    });
  }
  const lastNonAction = blocks[blocks.length - 1];
  (flow.actions || []).forEach((action, i) => {
    blocks.push({
      id: `block_action_${i}`,
      type: 'action',
      blockType: action.type,
      config: action.config,
      parentId: lastNonAction.id,
    });
  });
  return blocks;
}

// --- Block utilities ---

export function getChildBlocks(blocks: FlowBlock[], parentId: string): FlowBlock[] {
  return blocks.filter(b => b.parentId === parentId);
}

export function getAncestorChain(blocks: FlowBlock[], blockId: string): FlowBlock[] {
  const result: FlowBlock[] = [];
  let current = blocks.find(b => b.id === blockId);
  while (current) {
    result.push(current);
    if (current.parentId) {
      current = blocks.find(b => b.id === current!.parentId);
    } else {
      break;
    }
  }
  return result;
}

export function getAvailableBlocks(blocks: FlowBlock[], parentBlockId: string, platform: Platform = 'cloud'): BlockMeta[] {
  const result: BlockMeta[] = [];
  const parentBlock = blocks.find(b => b.id === parentBlockId);
  if (!parentBlock) return result;

  // Find the root source block to determine flow origin
  const rootSource = blocks.find(b => b.type === 'event' && !b.parentId);
  const isWebhookFlow = rootSource?.blockType === 'webhook_received';
  const isContentMonitorFlow = rootSource?.blockType === 'change_detected' && !rootSource.parentId;
  const isWorkflowSourceFlow = (rootSource?.blockType === 'workflow_completed' || rootSource?.blockType === 'workflow_started') && !rootSource.parentId;
  const isAiSessionSourceFlow = (rootSource?.blockType === 'ai_session_completed' || rootSource?.blockType === 'ai_session_started') && !rootSource.parentId;
  const isDirectlyAfterSource = parentBlock.id === rootSource?.id;

  const ancestors = getAncestorChain(blocks, parentBlockId);
  const hasAiSessionAction = ancestors.some(b => b.type === 'action' && b.blockType === 'ai_session');
  const hasWorkflowAction = ancestors.some(b => b.type === 'action' && b.blockType === 'workflow');
  const lastIsAiSession = parentBlock.type === 'action' && parentBlock.blockType === 'ai_session';
  const lastIsWorkflow = parentBlock.type === 'action' && parentBlock.blockType === 'workflow';

  // Smart recommendations based on what just happened
  if (lastIsAiSession) {
    result.push({ type: 'event', blockType: 'ai_session_completed', label: i18n.t('Wait for AI Session'), icon: CheckCircleIcon, color: 'purple', description: i18n.t('Continue when the AI session finishes'), priority: 100 });
  }
  if (lastIsWorkflow) {
    result.push({ type: 'event', blockType: 'workflow_completed', label: i18n.t('Wait for Workflow'), icon: CheckCircleIcon, color: 'green', description: i18n.t('Continue when the workflow finishes'), priority: 100 });
  }

  // Content monitor: first suggest a content changed block (selector-specific), then notification + condition
  if (isContentMonitorFlow && isDirectlyAfterSource) {
    result.push({ type: 'event', blockType: 'change_detected', label: i18n.t('On Content Changed'), icon: BoltIcon, color: 'blue', description: i18n.t('React to a specific selector change'), priority: 100 });
    result.push({ type: 'action', blockType: 'notification', label: i18n.t('Send Notification'), icon: BellIcon, color: 'blue', description: i18n.t('Alert when content changes'), priority: 95 });
    result.push({ type: 'condition', blockType: 'condition', label: i18n.t('Check What Changed'), icon: FunnelIcon, color: 'yellow', description: i18n.t('Filter by price, text, or extracted data'), priority: 90 });
  }

  // Workflow source: suggest notification + condition
  if (isWorkflowSourceFlow && isDirectlyAfterSource) {
    result.push({ type: 'action', blockType: 'notification', label: i18n.t('Send Notification'), icon: BellIcon, color: 'blue', description: i18n.t('Notify when workflow completes'), priority: 95 });
    result.push({ type: 'condition', blockType: 'condition', label: i18n.t('Check Result'), icon: FunnelIcon, color: 'yellow', description: i18n.t('Branch on success/failure'), priority: 90 });
  }

  // AI session source: suggest notification + condition
  if (isAiSessionSourceFlow && isDirectlyAfterSource) {
    result.push({ type: 'action', blockType: 'notification', label: i18n.t('Send Notification'), icon: BellIcon, color: 'blue', description: i18n.t('Notify when AI session completes'), priority: 95 });
    result.push({ type: 'condition', blockType: 'condition', label: i18n.t('Check Result'), icon: FunnelIcon, color: 'yellow', description: i18n.t('Branch on success/failure'), priority: 90 });
  }

  if (hasAiSessionAction && !lastIsAiSession) {
    result.push({ type: 'event', blockType: 'ai_session_completed', label: i18n.t('AI Session Completed'), icon: CheckCircleIcon, color: 'purple', description: i18n.t('When an AI session above finishes'), priority: 50 });
  }
  if (hasWorkflowAction && !lastIsWorkflow) {
    result.push({ type: 'event', blockType: 'workflow_completed', label: i18n.t('Workflow Completed'), icon: CheckCircleIcon, color: 'green', description: i18n.t('When a workflow above finishes'), priority: 50 });
  }

  // Return Data is ONLY available in webhook-triggered flows
  if (isWebhookFlow) {
    result.push({ type: 'action', blockType: 'return_data', label: i18n.t('Return Data to Caller'), icon: ArrowUturnLeftIcon, color: 'emerald', description: i18n.t('Return extracted data to the webhook/API caller'), priority: 90 });
  }

  // Standard action blocks — skip duplicates already added as recommendations
  const alreadyHasNotification = result.some(r => r.blockType === 'notification');
  if (!alreadyHasNotification) {
    result.push({ type: 'action', blockType: 'notification', label: i18n.t('Send Notification'), icon: BellIcon, color: 'blue', description: i18n.t('Send email, push, or webhook'), priority: 30 });
  }
  result.push(
    { type: 'action', blockType: 'ai_session', label: i18n.t('Run AI Agent'), icon: CpuChipIcon, color: 'purple', description: i18n.t('Execute an AI browser session'), priority: 30 },
    { type: 'action', blockType: 'workflow', label: i18n.t('Run Workflow'), icon: Cog6ToothIcon, color: 'green', description: i18n.t('Execute a browser automation'), priority: 30 },
  );

  // Data / file surface actions. Offer the data-export/append actions whenever the
  // flow has (or can produce) extracted data upstream — a data_extracted source, a
  // workflow that emits rows, or another data action. send_file appears once a file
  // has been produced (save/query export). Streaming controls are broadly useful.
  const hasDataUpstream =
    ancestors.some((b) =>
      b.blockType === 'data_extracted' ||
      b.blockType === 'workflow' ||
      b.blockType === 'workflow_completed' ||
      b.blockType === 'save_data_to_file' ||
      b.blockType === 'query_and_export',
    ) || isWorkflowSourceFlow;
  if (hasDataUpstream) {
    result.push(
      { type: 'action', blockType: 'save_data_to_file', label: i18n.t('Save Data to File'), icon: TableCellsIcon, color: 'emerald', description: i18n.t('Export extracted rows to a CSV/JSON file'), priority: 28 },
      { type: 'action', blockType: 'query_and_export', label: i18n.t('Query & Export Data'), icon: TableCellsIcon, color: 'emerald', description: i18n.t('Filter a data table and export the matches'), priority: 27 },
      { type: 'action', blockType: 'append_to_data', label: i18n.t('Append to Data'), icon: TableCellsIcon, color: 'emerald', description: i18n.t('Copy matching rows into another data table'), priority: 26 },
    );
  }
  const hasFileUpstream = ancestors.some((b) =>
    b.blockType === 'save_data_to_file' ||
    b.blockType === 'query_and_export' ||
    b.blockType === 'file_uploaded',
  ) || rootSource?.blockType === 'file_uploaded';
  if (hasFileUpstream) {
    result.push({ type: 'action', blockType: 'send_file', label: i18n.t('Send File'), icon: DocumentArrowUpIcon, color: 'orange', description: i18n.t('Deliver a stored file to a webhook'), priority: 28 });
  }
  result.push(
    { type: 'action', blockType: 'start_streaming_session', label: i18n.t('Start Streaming Session'), icon: SignalIcon, color: 'purple', description: i18n.t('Launch a live streaming session for a workflow'), priority: 22 },
    { type: 'action', blockType: 'stop_streaming_session', label: i18n.t('Stop Streaming Session'), icon: SignalIcon, color: 'purple', description: i18n.t('End a live streaming session'), priority: 21 },
  );

  // Create Persona — only when the flow contains a workflow that produces a login
  // (credentials / auth session) a persona can capture.
  const hasLoginWorkflow = blocks.some((b) =>
    (b.blockType === 'workflow' || b.blockType === 'workflow_completed' || b.blockType === 'workflow_started')
    && b.config?.workflow_has_login === true);
  if (hasLoginWorkflow) {
    result.push({ type: 'action', blockType: 'create_persona', label: i18n.t('Create Persona'), icon: Cog6ToothIcon, color: 'green', description: i18n.t('Save the workflow login as a reusable persona'), priority: 25 });
  }

  // Condition block — skip if already added as recommendation
  const alreadyHasCondition = result.some(r => r.blockType === 'condition');
  if (!alreadyHasCondition && (parentBlock.type === 'event' || parentBlock.type === 'action')) {
    result.push({ type: 'condition', blockType: 'condition', label: i18n.t('Add Condition'), icon: FunnelIcon, color: 'yellow', description: i18n.t('Filter based on data values'), priority: 20 });
  }

  // Loop block — repeat the blocks below once per item of an upstream list.
  const alreadyHasForEach = result.some(r => r.blockType === 'for_each');
  if (!alreadyHasForEach && (parentBlock.type === 'event' || parentBlock.type === 'action')) {
    result.push({ type: 'action', blockType: 'for_each', label: i18n.t('For Each'), icon: ArrowPathRoundedSquareIcon, color: 'yellow', description: i18n.t('Repeat the blocks below for each item in a list'), priority: 19 });
  }

  // Capability gate: never offer a block the current platform can't run (cloud-only
  // blocks on desktop, or roadmap 'planned' blocks) — the catalog is the source of truth.
  return result
    .filter((r) => isBlockAvailable(r.blockType, platform))
    .sort((a, b) => b.priority - a.priority);
}

export function buildNewBlock(
  meta: { type: BlockType; blockType: string },
  parentBlockId: string,
  blocks: FlowBlock[],
): FlowBlock {
  let config: any = {};

  if (meta.type === 'condition') {
    config = { field: '', operator: 'contains', value: '' };
  }

  // Child change_detected block inherits target_id from the source
  if (meta.blockType === 'change_detected' && meta.type === 'event') {
    const rootSource = blocks.find(b => b.type === 'event' && !b.parentId);
    if (rootSource?.blockType === 'change_detected' && rootSource.config?.target_id) {
      config.target_id = rootSource.config.target_id;
      config.linked_to_block = rootSource.id;
    }
  }

  if (meta.blockType === 'ai_session_completed') {
    const ancestors = getAncestorChain(blocks, parentBlockId);
    for (const b of ancestors) {
      if (b.type === 'action' && b.blockType === 'ai_session' && b.config.session_ids?.length > 0) {
        config.ai_session_id = b.config.session_ids[0];
        config.linked_to_block = b.id;
        break;
      }
    }
  }

  if (meta.blockType === 'workflow_completed') {
    const ancestors = getAncestorChain(blocks, parentBlockId);
    for (const b of ancestors) {
      if (b.type === 'action' && b.blockType === 'workflow' && b.config.workflow_id) {
        config.workflow_id = b.config.workflow_id;
        config.linked_to_block = b.id;
        break;
      }
    }
  }

  return {
    id: `block_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
    type: meta.type,
    blockType: meta.blockType,
    config,
    parentId: parentBlockId,
  };
}

// --- Store ---
//
// State lives in a component-scoped Zustand store (one per FlowBuilderProvider), not
// a plain context value. This is what makes per-block editing cheap: a config
// keystroke changes `state` identity, but consumers can subscribe to a SLICE via
// `useFlowState(selector)` and re-render only when THAT slice changes — so typing in
// one block no longer re-renders every other block's config panel. The reducer is
// left untouched; the store just wraps it (`dispatch = set(reducer(state, action))`).

interface FlowStore {
  state: FlowBuilderState;
  dispatch: (action: Action) => void;
  addBlock: (meta: { type: BlockType; blockType: string }, parentBlockId: string) => void;
  removeBlock: (blockId: string) => void;
  updateBlockConfig: (blockId: string, config: any) => void;
  updateBlockType: (blockId: string, blockType: string) => void;
}

type FlowStoreApi = StoreApi<FlowStore>;

function createFlowStore(): FlowStoreApi {
  return createStore<FlowStore>((set, get) => ({
    state: initialState,
    dispatch: (action) => set((s) => ({ state: reducer(s.state, action) })),
    addBlock: (meta, parentBlockId) => {
      const newBlock = buildNewBlock(meta, parentBlockId, get().state.blocks);
      get().dispatch({ type: 'ADD_BLOCK', block: newBlock });
    },
    removeBlock: (blockId) => {
      const blocks = get().state.blocks;
      const block = blocks.find((b) => b.id === blockId);
      if (!block) return;
      if (!block.parentId && blocks.filter((b) => !b.parentId).length === 1) return;
      get().dispatch({ type: 'REMOVE_BLOCK', blockId });
    },
    updateBlockConfig: (blockId, config) => get().dispatch({ type: 'UPDATE_BLOCK_CONFIG', blockId, config }),
    updateBlockType: (blockId, blockType) => get().dispatch({ type: 'UPDATE_BLOCK_TYPE', blockId, blockType }),
  }));
}

// --- Context (holds the store handle + the async loaders that need React hooks) ---

interface FlowBuilderContextValue {
  store: FlowStoreApi;
  loadSelectors: (targetId: number) => Promise<void>;
  loadReferenceData: () => Promise<void>;
}

const FlowBuilderContext = createContext<FlowBuilderContextValue | null>(null);

function useFlowBuilderContext(): FlowBuilderContextValue {
  const ctx = useContext(FlowBuilderContext);
  if (!ctx) throw new Error('useFlowBuilder must be used within FlowBuilderProvider');
  return ctx;
}

/**
 * Subscribe to a SLICE of the flow-builder state. Re-renders only when the selected
 * value changes (Object.is by default; pass `equalityFn` — e.g. zustand `shallow` —
 * for array/object selections like a block's children). This is the selective read
 * that keeps an edit to one block from re-rendering the whole tree.
 */
export function useFlowState<T>(selector: (s: FlowBuilderState) => T, equalityFn?: (a: T, b: T) => boolean): T {
  const { store } = useFlowBuilderContext();
  return useStoreWithEqualityFn(store, (s) => selector(s.state), equalityFn);
}

/**
 * The store handle — for reading a NON-reactive snapshot at event time via
 * `store.getState().state` (e.g. a save handler that needs the full current state
 * without subscribing the component to every change).
 */
export function useFlowStore(): FlowStoreApi {
  return useFlowBuilderContext().store;
}

/**
 * The flow-builder action handles — stable for the life of the provider. Reading
 * them NEVER triggers a re-render, so a component that only mutates (never reads
 * state) can subscribe to nothing.
 */
export function useFlowActions() {
  const { store, loadSelectors, loadReferenceData } = useFlowBuilderContext();
  return useMemo(() => {
    const { dispatch, addBlock, removeBlock, updateBlockConfig, updateBlockType } = store.getState();
    return { dispatch, addBlock, removeBlock, updateBlockConfig, updateBlockType, loadSelectors, loadReferenceData };
  }, [store, loadSelectors, loadReferenceData]);
}

/**
 * Back-compat hook — same shape and re-render behavior as the original context value
 * (subscribes to the WHOLE state, so it re-renders on any change). Existing
 * low-frequency consumers keep using this unchanged; hot-path components prefer
 * `useFlowState` + `useFlowActions`.
 */
export function useFlowBuilder() {
  const state = useFlowState((s) => s);
  const actions = useFlowActions();
  return { state, ...actions };
}

// --- Provider ---

interface FlowBuilderProviderProps {
  children: React.ReactNode;
  initialFlow?: TriggerRule;
  /** Load `initialFlow` as unsaved work (a restored hand-off draft, not a fetched flow). */
  initialFlowDirty?: boolean;
}

export const FlowBuilderProvider: React.FC<FlowBuilderProviderProps> = ({ children, initialFlow, initialFlowDirty }) => {
  // One store per provider instance (mirrors the old per-mount useReducer state).
  const storeRef = useRef<FlowStoreApi | undefined>(undefined);
  if (!storeRef.current) storeRef.current = createFlowStore();
  const store = storeRef.current;

  const loadReferenceData = useCallback(async () => {
    const { dispatch } = store.getState();
    dispatch({ type: 'SET_LOADING', loading: true });
    try {
      const [sessionsData, workflowsData, recipientsData, targetsData] = await Promise.all([
        aiSessionsApi.listAll().catch(() => []),
        automationApi.listWorkflows().catch(() => []),
        recipientsApi.getAll(true).catch(() => []),
        targetsApi.getAll().catch(() => []),
      ]);
      dispatch({
        type: 'SET_REFERENCE_DATA',
        sessions: sessionsData,
        workflows: workflowsData,
        recipients: recipientsData,
        targets: (targetsData || []).map((t: any) => ({ id: t.id, url: t.url, check_type: t.check_type })),
      });
    } catch (e) {
      console.error('Failed to load reference data:', e);
    } finally {
      dispatch({ type: 'SET_LOADING', loading: false });
    }
  }, [store]);

  useEffect(() => {
    loadReferenceData();
  }, [loadReferenceData]);

  useEffect(() => {
    if (initialFlow) {
      store.getState().dispatch({ type: 'LOAD_FLOW', flow: initialFlow, dirty: initialFlowDirty });
    }
  }, [store, initialFlow, initialFlowDirty]);

  const loadSelectors = useCallback(async (targetId: number) => {
    const { dispatch } = store.getState();
    try {
      const sels = await selectorsApi.listForTarget(targetId);
      dispatch({ type: 'SET_SELECTORS', selectors: sels });
    } catch {
      dispatch({ type: 'SET_SELECTORS', selectors: [] });
    }
  }, [store]);

  const value: FlowBuilderContextValue = useMemo(
    () => ({ store, loadSelectors, loadReferenceData }),
    [store, loadSelectors, loadReferenceData],
  );

  return (
    <FlowBuilderContext.Provider value={value}>
      {children}
    </FlowBuilderContext.Provider>
  );
};
