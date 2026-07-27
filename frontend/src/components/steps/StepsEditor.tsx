import React, { useEffect, useMemo, useRef, useState } from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import type { WorkflowStep } from '../../types/api';
import { stepMeta, getStepInfo, createStep, stepSearchText, GROUP_NODE_STYLE, type StepType } from './stepMeta';
import { StepConfigForm } from './StepConfigForm';
import { StepTypePalette } from './StepTypePalette';
import { segmentsToFunctions } from '../../utils/workflowFunctions';
import { Expand, Checkbox } from '../ui';
import {
  ChevronUpIcon,
  ChevronDownIcon,
  TrashIcon,
  EyeIcon,
  EyeSlashIcon,
  DocumentDuplicateIcon,
  PlusIcon,
  MagnifyingGlassIcon,
  XMarkIcon,
  Bars3Icon,
  ExclamationTriangleIcon,
  PuzzlePieceIcon,
  CheckIcon,
} from '@heroicons/react/24/outline';

/**
 * StepsEditor — THE single surface for viewing and editing workflow steps.
 *
 * Scannable timeline (tab contexts nested), click a row to expand its
 * type-specific edit form inline, drag to reorder, insert anywhere, filter.
 * Edits accumulate in a local draft; the sticky bar saves everything at once.
 */

interface StepsEditorProps {
  steps: WorkflowStep[];
  onSave: (steps: WorkflowStep[]) => Promise<void>;
  /** Names of functions already defined (dup-guard for the inline builder). */
  existingFunctionNames?: string[];
  /** When provided, enables the "Group into function" builder; persists the new function. */
  onCreateFunction?: (fn: ReturnType<typeof segmentsToFunctions>[number]) => Promise<void>;
}

/** Ensure every step has a stable id for keys, expansion and drag-reorder. */
function withIds(steps: WorkflowStep[]): WorkflowStep[] {
  return (steps || []).map(s => (s.id ? s : { ...s, id: crypto.randomUUID() }));
}

export const StepsEditor: React.FC<StepsEditorProps> = ({ steps, onSave, existingFunctionNames, onCreateFunction }) => {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<WorkflowStep[]>(() => withIds(steps));
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const [addingAt, setAddingAt] = useState<number | null>(null);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [dropIndex, setDropIndex] = useState<number | null>(null);

  // ── Inline function builder: select steps → group into a callable function ──
  const [selecting, setSelecting] = useState(false);
  const [selectedIdx, setSelectedIdx] = useState<Set<number>>(new Set());
  const [fnName, setFnName] = useState('');
  const [creatingFn, setCreatingFn] = useState(false);

  const toggleSelect = (idx: number) =>
    setSelectedIdx(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });

  const cancelSelecting = () => {
    setSelecting(false);
    setSelectedIdx(new Set());
    setFnName('');
  };

  const createFunction = async () => {
    const slug = fnName.trim().replace(/\s+/g, '_').toLowerCase();
    if (!slug || selectedIdx.size === 0 || creatingFn || !onCreateFunction) return;
    if ((existingFunctionNames || []).some(n => n.toLowerCase() === slug)) {
      toast.error(t('A function named "{{name}}" already exists', { name: slug }));
      return;
    }
    setCreatingFn(true);
    try {
      // Persist any pending step edits first so the saved indices match the server.
      if (dirty) { await onSave(draft); setDirty(false); }
      const indices = Array.from(selectedIdx).sort((a, b) => a - b);
      const isExtract = indices.some(i => draft[i]?.type === 'extract');
      const [fn] = segmentsToFunctions([{
        name: slug,
        segment_type: isExtract ? 'extract' : 'action',
        step_indices: indices,
        depends_on: [],
        extract_outputs: indices
          .filter(i => draft[i]?.type === 'extract')
          .map(i => (draft[i]?.config?.variable as string) || `output_${i + 1}`),
      }]);
      await onCreateFunction(fn);
      toast.success(t('Function "{{name}}" created', { name: slug }));
      cancelSelecting();
    } catch {
      toast.error(t('Failed to create function'));
    } finally {
      setCreatingFn(false);
    }
  };

  // Re-sync from server data unless the user has unsaved edits. `dirty` is
  // mirrored into a ref AFTER commit rather than during render (a render-path
  // ref write is a render-time mutation React may tear on); this effect is
  // declared FIRST so the mirror is already current when the `steps` effect
  // below reads it in the same commit.
  const dirtyRef = useRef(dirty);
  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);
  useEffect(() => {
    if (!dirtyRef.current) setDraft(withIds(steps));
  }, [steps]);

  const mutate = (fn: (prev: WorkflowStep[]) => WorkflowStep[]) => {
    setDraft(prev => fn(prev));
    setDirty(true);
  };

  const updateStep = (id: string, updates: Partial<WorkflowStep>) =>
    mutate(prev => prev.map(s => (s.id === id ? { ...s, ...updates } : s)));

  const deleteStep = (id: string) => {
    mutate(prev => prev.filter(s => s.id !== id));
    if (expandedId === id) setExpandedId(null);
  };

  const toggleStep = (id: string) =>
    mutate(prev => prev.map(s => (s.id === id ? { ...s, enabled: s.enabled === false } : s)));

  const duplicateStep = (id: string) =>
    mutate(prev => {
      const idx = prev.findIndex(s => s.id === id);
      if (idx === -1) return prev;
      const copy = { ...prev[idx], id: crypto.randomUUID() };
      return [...prev.slice(0, idx + 1), copy, ...prev.slice(idx + 1)];
    });

  const moveStep = (idx: number, dir: 'up' | 'down') => {
    const target = dir === 'up' ? idx - 1 : idx + 1;
    if (target < 0 || target >= draft.length) return;
    mutate(prev => {
      const next = [...prev];
      [next[idx], next[target]] = [next[target], next[idx]];
      return next;
    });
  };

  const insertStep = (idx: number, type: StepType) => {
    const step = createStep(type);
    mutate(prev => [...prev.slice(0, idx), step, ...prev.slice(idx)]);
    setAddingAt(null);
    setExpandedId(step.id!);
  };

  const reorderStep = (from: number, to: number) => {
    if (from === to || from + 1 === to) return;
    mutate(prev => {
      const next = [...prev];
      const [moved] = next.splice(from, 1);
      next.splice(from < to ? to - 1 : to, 0, moved);
      return next;
    });
  };

  const handleSave = async () => {
    if (saving || !dirty) return;
    setSaving(true);
    try {
      await onSave(draft);
      setDirty(false);
    } finally {
      setSaving(false);
    }
  };

  const handleDiscard = () => {
    setDraft(withIds(steps));
    setDirty(false);
    setExpandedId(null);
    setAddingAt(null);
  };

  // ⌘S saves, Esc collapses the open editor / palette.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 's') {
        if (dirtyRef.current) { e.preventDefault(); handleSave(); }
      } else if (e.key === 'Escape') {
        setExpandedId(null);
        setAddingAt(null);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft]);

  const filtering = filter.trim().length > 0;
  const filteredIds = useMemo(() => {
    if (!filtering) return null;
    const q = filter.trim().toLowerCase();
    return new Set(draft.filter(s => stepSearchText(s).includes(q)).map(s => s.id));
  }, [draft, filter, filtering]);

  const rowProps = {
    expandedId,
    onExpand: (id: string) => setExpandedId(prev => (prev === id ? null : id)),
    onUpdate: updateStep,
    onDelete: deleteStep,
    onToggle: toggleStep,
    onDuplicate: duplicateStep,
    onMove: moveStep,
    total: draft.length,
    dragIndex,
    dropIndex,
    setDragIndex,
    setDropIndex,
    onReorder: reorderStep,
    interactive: !filtering,
  };

  return (
    <div className="bg-white border border-zinc-200/80 rounded-xl overflow-hidden">
      {/* Header: count + filter */}
      <div className="flex items-center gap-3 px-4 py-2.5 border-b border-border">
        <span className="text-xs font-semibold text-secondary uppercase tracking-wider shrink-0">
          {draft.length === 1 ? t('{{n}} Step', { n: draft.length }) : t('{{n}} Steps', { n: draft.length })}
        </span>
        {filtering && filteredIds && (
          <span className="text-[11px] text-tertiary">{filteredIds.size === 1 ? t('{{n}} match', { n: filteredIds.size }) : t('{{n}} matches', { n: filteredIds.size })}</span>
        )}
        <div className="flex-1" />
        {onCreateFunction && !selecting && !filtering && draft.length >= 2 && (
          <button
            onClick={() => setSelecting(true)}
            className="flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-medium text-secondary border border-border rounded-lg hover:text-ink hover:border-ink/40 transition-colors"
            title={t('Select steps and group them into a callable function')}
          >
            <PuzzlePieceIcon className="w-3.5 h-3.5" />
            {t('Group into function')}
          </button>
        )}
        {draft.length > 5 && !selecting && (
          <div className="relative">
            <MagnifyingGlassIcon className="w-3.5 h-3.5 text-tertiary absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              value={filter}
              onChange={e => setFilter(e.target.value)}
              placeholder={t('Filter steps…')}
              className="w-44 pl-8 pr-7 py-1 text-xs bg-canvas border border-border rounded-lg outline-none focus:border-ink/30 placeholder:text-tertiary"
            />
            {filtering && (
              <button onClick={() => setFilter('')} className="absolute right-2 top-1/2 -translate-y-1/2 text-tertiary hover:text-ink">
                <XMarkIcon className="w-3 h-3" />
              </button>
            )}
          </div>
        )}
      </div>

      {/* Function builder — select steps, name them, group into a callable function */}
      {selecting && (
        <div className="px-4 py-3">
          <div className="mb-3 rounded-xl border border-ink/15 bg-ink/[0.03] p-3 space-y-2.5">
            <div className="flex items-center gap-2">
              <PuzzlePieceIcon className="w-4 h-4 text-ink shrink-0" />
              <span className="text-xs font-semibold text-ink">{t('New function')}</span>
              <span className="text-[11px] text-tertiary">{t('Select steps below, then name them')}</span>
            </div>
            <div className="flex items-center gap-2">
              <input
                value={fnName}
                autoFocus
                onChange={e => setFnName(e.target.value.replace(/[^a-zA-Z0-9_ ]/g, ''))}
                onKeyDown={e => { if (e.key === 'Enter') createFunction(); }}
                placeholder={t('function_name')}
                className="w-48 px-2.5 py-1.5 text-xs font-mono bg-white border border-border rounded-lg outline-none focus:border-ink/30"
              />
              <span className="text-[11px] text-tertiary tabular-nums">
                {t('{{n}} selected', { n: selectedIdx.size })}
              </span>
              <div className="flex-1" />
              <button
                onClick={cancelSelecting}
                disabled={creatingFn}
                className="px-3 py-1.5 text-xs font-medium text-secondary rounded-lg hover:bg-hover hover:text-ink transition-colors disabled:opacity-50"
              >
                {t('Cancel')}
              </button>
              <button
                onClick={createFunction}
                disabled={!fnName.trim() || selectedIdx.size === 0 || creatingFn}
                className="px-4 py-1.5 text-xs font-medium bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 transition-colors disabled:opacity-50"
              >
                {creatingFn ? t('Creating…') : t('Create function')}
              </button>
            </div>
          </div>

          {/* Selectable step list (flat, indexed) */}
          <div className="space-y-0.5">
            {draft.map((step, idx) => {
              const sel = selectedIdx.has(idx);
              const meta = stepMeta(step.type);
              const { summary } = getStepInfo(step);
              const Icon = meta.Icon;
              return (
                <button
                  key={step.id}
                  type="button"
                  onClick={() => toggleSelect(idx)}
                  className={clsx(
                    'w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg text-left transition-colors',
                    sel ? 'bg-ink/[0.05] ring-1 ring-ink/20' : 'hover:bg-hover',
                  )}
                >
                  <span className={clsx(
                    'w-4 h-4 rounded border flex items-center justify-center shrink-0 transition-colors',
                    sel ? 'bg-ink border-ink text-white' : 'border-zinc-300',
                  )}>
                    {sel && <CheckIcon className="w-3 h-3" />}
                  </span>
                  <span className="text-[10px] font-mono text-tertiary w-4 text-right shrink-0 tabular-nums">{idx + 1}</span>
                  <span className={clsx('w-5 h-5 rounded-md flex items-center justify-center shrink-0', GROUP_NODE_STYLE[meta.group])}>
                    <Icon className="h-2.5 w-2.5" />
                  </span>
                  <span className="text-[13px] font-semibold text-ink shrink-0">{t(meta.label)}</span>
                  {summary && <span className="text-[11px] text-secondary truncate font-mono">{summary}</span>}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* Step list */}
      {!selecting && (
      <div className="px-4 py-3">
        {draft.length === 0 && addingAt === null ? (
          <div className="flex flex-col items-center justify-center py-10 text-center">
            <ExclamationTriangleIcon className="h-5 w-5 text-tertiary mb-2" />
            <p className="text-xs text-secondary mb-3">{t('No steps yet')}</p>
            <button
              onClick={() => setAddingAt(0)}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium border border-dashed border-zinc-300 rounded-full text-secondary hover:text-ink hover:border-ink/40 transition-colors"
            >
              <PlusIcon className="w-3.5 h-3.5" />
              {t('Add first step')}
            </button>
          </div>
        ) : filtering ? (
          // Flat filtered view — quick scanning over long recordings
          <div>
            {draft.map((step, idx) =>
              filteredIds?.has(step.id) ? (
                <StepRow key={step.id} step={step} idx={idx} showLine={false} {...rowProps} />
              ) : null,
            )}
            {filteredIds?.size === 0 && (
              <p className="text-xs text-tertiary text-center py-6">{t('No steps match "{{q}}"', { q: filter.trim() })}</p>
            )}
          </div>
        ) : (
          <GroupedSteps steps={draft} rowProps={rowProps} addingAt={addingAt} setAddingAt={setAddingAt} onInsert={insertStep} />
        )}

        {/* Add at end */}
        {!filtering && draft.length > 0 && (
          addingAt === draft.length ? (
            <StepTypePalette onSelect={type => insertStep(draft.length, type)} onCancel={() => setAddingAt(null)} />
          ) : (
            <div className="flex justify-center pt-3">
              <button
                onClick={() => setAddingAt(draft.length)}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium border border-dashed border-zinc-300 rounded-full text-secondary hover:text-ink hover:border-ink/40 transition-colors"
              >
                <PlusIcon className="w-3.5 h-3.5" />
                {t('Add step')}
              </button>
            </div>
          )
        )}
        {draft.length === 0 && addingAt !== null && (
          <StepTypePalette onSelect={type => insertStep(0, type)} onCancel={() => setAddingAt(null)} />
        )}
      </div>
      )}

      {/* Sticky save bar */}
      {dirty && !selecting && (
        <div className="sticky bottom-0 flex items-center gap-3 px-4 py-2.5 bg-white/95 backdrop-blur border-t border-zinc-200">
          <span className="text-xs text-secondary">{t('Unsaved changes')}</span>
          <span className="hidden @pair/stage:inline text-[10px] text-tertiary">{t('⌘S to save')}</span>
          <div className="flex-1" />
          <button
            onClick={handleDiscard}
            disabled={saving}
            className="px-3 py-1.5 text-xs font-medium text-secondary rounded-lg hover:bg-hover hover:text-ink transition-colors disabled:opacity-50"
          >
            {t('Discard')}
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-4 py-1.5 text-xs font-medium bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 transition-colors disabled:opacity-50"
          >
            {saving ? t('Saving…') : t('Save steps')}
          </button>
        </div>
      )}
    </div>
  );
};

/* ── Grouped timeline (tab contexts nested) ── */

interface RowHandlers {
  expandedId: string | null;
  onExpand: (id: string) => void;
  onUpdate: (id: string, updates: Partial<WorkflowStep>) => void;
  onDelete: (id: string) => void;
  onToggle: (id: string) => void;
  onDuplicate: (id: string) => void;
  onMove: (idx: number, dir: 'up' | 'down') => void;
  total: number;
  dragIndex: number | null;
  dropIndex: number | null;
  setDragIndex: (i: number | null) => void;
  setDropIndex: (i: number | null) => void;
  onReorder: (from: number, to: number) => void;
  interactive: boolean;
}

function GroupedSteps({ steps, rowProps, addingAt, setAddingAt, onInsert }: {
  steps: WorkflowStep[];
  rowProps: RowHandlers;
  addingAt: number | null;
  setAddingAt: (i: number | null) => void;
  onInsert: (idx: number, type: StepType) => void;
}) {
  const { t } = useTranslation();
  type Item = { step: WorkflowStep; idx: number };
  type Group = { kind: 'main'; items: Item[] } | { kind: 'tab'; trigger: Item; items: Item[]; closer?: Item };

  // Consecutive steps between a tab-opening step (wait_for_tab / open_tab) and
  // tab_closed form a nested "tab context".
  const groups: Group[] = [];
  let main: Item[] = [];
  let i = 0;
  while (i < steps.length) {
    if (steps[i].type === 'wait_for_tab' || steps[i].type === 'open_tab') {
      if (main.length) { groups.push({ kind: 'main', items: main }); main = []; }
      const trigger: Item = { step: steps[i], idx: i };
      const items: Item[] = [];
      let closer: Item | undefined;
      i++;
      while (i < steps.length) {
        if (steps[i].type === 'tab_closed') { closer = { step: steps[i], idx: i }; i++; break; }
        items.push({ step: steps[i], idx: i });
        i++;
      }
      groups.push({ kind: 'tab', trigger, items, closer });
    } else {
      main.push({ step: steps[i], idx: i });
      i++;
    }
  }
  if (main.length) groups.push({ kind: 'main', items: main });

  const renderRow = (item: Item, showLine: boolean, nested?: boolean) => (
    <React.Fragment key={item.step.id}>
      <InsertPoint idx={item.idx} addingAt={addingAt} setAddingAt={setAddingAt} onInsert={onInsert} />
      <StepRow step={item.step} idx={item.idx} showLine={showLine} nested={nested} {...rowProps} />
    </React.Fragment>
  );

  return (
    <div>
      {groups.map((group, gi) => {
        const lastGroup = gi === groups.length - 1;
        if (group.kind === 'main') {
          return (
            <div key={gi}>
              {group.items.map((item, ii) => renderRow(item, !(lastGroup && ii === group.items.length - 1)))}
            </div>
          );
        }
        return (
          <div key={gi}>
            {renderRow(group.trigger, true)}
            {group.items.length > 0 && (
              <div className="ml-5 pl-5 border-l-2 border-border/80 relative">
                <div className="absolute -left-px top-0 w-4 h-px bg-border/80" />
                <div className="text-[9px] uppercase tracking-widest text-tertiary mb-1 -mt-0.5">{t('New tab context')}</div>
                {group.items.map((item, ii) => renderRow(item, ii < group.items.length - 1, true))}
              </div>
            )}
            {group.closer && renderRow(group.closer, !lastGroup)}
          </div>
        );
      })}
    </div>
  );
}

/** Hover-revealed "+" between rows; turns into the type palette when active. */
function InsertPoint({ idx, addingAt, setAddingAt, onInsert }: {
  idx: number;
  addingAt: number | null;
  setAddingAt: (i: number | null) => void;
  onInsert: (idx: number, type: StepType) => void;
}) {
  const { t } = useTranslation();
  if (addingAt === idx) {
    return <StepTypePalette onSelect={type => onInsert(idx, type)} onCancel={() => setAddingAt(null)} />;
  }
  return (
    <div className="group/insert relative h-1.5 -my-0.5">
      <button
        onClick={() => setAddingAt(idx)}
        className="absolute inset-x-0 -top-1.5 -bottom-1.5 z-20 flex items-center opacity-0 group-hover/insert:opacity-100 transition-opacity"
        title={t('Insert step here')}
      >
        <span className="flex-1 h-px bg-zinc-300" />
        <span className="mx-2 w-4 h-4 rounded-full bg-ink text-white flex items-center justify-center">
          <PlusIcon className="w-2.5 h-2.5" />
        </span>
        <span className="flex-1 h-px bg-zinc-300" />
      </button>
    </div>
  );
}

/* ── Single step row: scannable line + inline editor ── */

function StepRow({
  step, idx, showLine, nested,
  expandedId, onExpand, onUpdate, onDelete, onToggle, onDuplicate, onMove, total,
  dragIndex, dropIndex, setDragIndex, setDropIndex, onReorder, interactive,
}: RowHandlers & { step: WorkflowStep; idx: number; showLine: boolean; nested?: boolean }) {
  const { t } = useTranslation();
  const meta = stepMeta(step.type);
  const { summary, details, badges } = getStepInfo(step);
  const StepIcon = meta.Icon;
  const isDisabled = step.enabled === false;
  const isExpanded = expandedId === step.id;
  const isDropTarget = dropIndex === idx && dragIndex !== null && dragIndex !== idx;
  const nodeSize = nested ? 'w-5 h-5' : 'w-6 h-6';
  const iconSize = nested ? 'h-2.5 w-2.5' : 'h-3 w-3';

  return (
    <div
      className={clsx('relative group', isDisabled && !isExpanded && 'opacity-40')}
      onDragOver={interactive ? (e) => { e.preventDefault(); setDropIndex(idx); } : undefined}
      onDrop={interactive ? (e) => { e.preventDefault(); if (dragIndex !== null) onReorder(dragIndex, idx); setDragIndex(null); setDropIndex(null); } : undefined}
    >
      {/* Drop indicator */}
      {isDropTarget && <div className="absolute -top-px inset-x-0 h-0.5 bg-ink rounded-full z-20" />}

      {/* Connector line */}
      {showLine && !isExpanded && (
        <div className={clsx('absolute w-px bg-border', nested ? 'left-[0.625rem] top-6 bottom-0' : 'left-[0.75rem] top-7 bottom-0')} />
      )}

      <div
        className={clsx(
          'rounded-lg -mx-2 transition-colors duration-100',
          isExpanded ? 'bg-canvas/80 ring-1 ring-zinc-200 my-1' : 'hover:bg-hover/60',
        )}
      >
        {/* Scannable row */}
        <div
          className={clsx('flex gap-2.5 px-2 cursor-pointer', nested && !isExpanded ? 'py-1.5' : 'py-2')}
          onClick={() => onExpand(step.id!)}
        >
          {/* Drag handle (replaces the number on hover) */}
          <div className="flex items-start shrink-0 pt-0.5">
            <div
              draggable={interactive}
              onDragStart={interactive ? (e) => { e.stopPropagation(); setDragIndex(idx); e.dataTransfer.effectAllowed = 'move'; } : undefined}
              onDragEnd={() => { setDragIndex(null); setDropIndex(null); }}
              onClick={e => e.stopPropagation()}
              className={clsx('relative', interactive && 'cursor-grab active:cursor-grabbing')}
              title={interactive ? t('Drag to reorder') : undefined}
            >
              <div className={clsx(nodeSize, 'rounded-md flex items-center justify-center relative z-10', GROUP_NODE_STYLE[meta.group])}>
                <StepIcon className={clsx(iconSize, 'group-hover:opacity-0 transition-opacity')} />
                {interactive && <Bars3Icon className={clsx(iconSize, 'absolute opacity-0 group-hover:opacity-100 transition-opacity')} />}
              </div>
            </div>
          </div>

          {/* Content */}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-1.5">
              <span className="text-[10px] font-mono text-tertiary w-4 text-right shrink-0 tabular-nums">{idx + 1}</span>
              <span className={clsx('font-semibold text-ink shrink-0', nested ? 'text-xs' : 'text-[13px]')}>{t(meta.label)}</span>

              {!isExpanded && summary && (
                <span className="text-[11px] text-secondary truncate font-mono max-w-[320px]">{summary}</span>
              )}

              {badges.map((badge, i) => (
                <span key={i} className="text-[9px] px-1.5 py-0.5 rounded-full bg-canvas border border-border text-tertiary font-medium shrink-0">{badge}</span>
              ))}

              <div className="flex-1" />

              {/* Hover actions */}
              <div
                className={clsx(
                  'flex items-center gap-px shrink-0 transition-opacity',
                  isExpanded ? 'opacity-100' : 'opacity-0 group-hover:opacity-100',
                )}
                onClick={e => e.stopPropagation()}
              >
                <button onClick={() => onMove(idx, 'up')} disabled={idx === 0} className="p-1 text-tertiary hover:text-ink disabled:opacity-30" title={t('Move up')}><ChevronUpIcon className="h-3 w-3" /></button>
                <button onClick={() => onMove(idx, 'down')} disabled={idx === total - 1} className="p-1 text-tertiary hover:text-ink disabled:opacity-30" title={t('Move down')}><ChevronDownIcon className="h-3 w-3" /></button>
                <button onClick={() => onDuplicate(step.id!)} className="p-1 text-tertiary hover:text-ink" title={t('Duplicate')}><DocumentDuplicateIcon className="h-3 w-3" /></button>
                <button onClick={() => onToggle(step.id!)} className="p-1 text-tertiary hover:text-ink" title={isDisabled ? t('Enable') : t('Disable (skipped at run time)')}>
                  {isDisabled ? <EyeIcon className="h-3 w-3" /> : <EyeSlashIcon className="h-3 w-3" />}
                </button>
                <button onClick={() => onDelete(step.id!)} className="p-1 text-tertiary hover:text-red-500" title={t('Delete')}><TrashIcon className="h-3 w-3" /></button>
              </div>
            </div>

            {/* Secondary details, collapsed view only */}
            {!isExpanded && details.length > 1 && (
              <div className="mt-0.5 ml-[1.35rem] space-y-px">
                {details.slice(1).map((d, i) => (
                  <div key={i} className="text-[11px] text-secondary truncate">
                    <span className="text-tertiary">{t(d.label)} </span>
                    <span className={d.mono ? 'font-mono' : ''}>{d.value}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Inline editor */}
        <Expand open={isExpanded} mountOnEnter>
          <div className="px-4 pb-3 pt-1 space-y-3 cursor-default" onClick={e => e.stopPropagation()}>
            <div className="border-t border-zinc-200/70 pt-3 space-y-3">
              <StepConfigForm step={step} onUpdate={updates => onUpdate(step.id!, updates)} />

              {/* Common fields */}
              <div className="flex items-start gap-3">
                <label className="text-xs text-secondary shrink-0 w-[88px] pt-2">{t('Note')}</label>
                <input
                  value={step.description || ''}
                  onChange={e => onUpdate(step.id!, { description: e.target.value })}
                  placeholder={t('Optional description')}
                  className="flex-1 px-3 py-1.5 text-sm bg-canvas border border-border rounded-lg outline-none focus:border-ink/30 focus:ring-2 focus:ring-ink/5"
                />
              </div>
              <div className="flex items-center gap-5 pl-[100px]">
                <Checkbox
                  checked={step.optional || false}
                  onChange={e => onUpdate(step.id!, { optional: e.target.checked })}
                  label={<span className="text-secondary text-xs">{t("Optional — failures don't stop the run")}</span>}
                />
                <Checkbox
                  checked={isDisabled}
                  onChange={() => onToggle(step.id!)}
                  label={<span className="text-secondary text-xs">{t('Disabled')}</span>}
                />
              </div>
            </div>
          </div>
        </Expand>
      </div>
    </div>
  );
}

