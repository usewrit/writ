// The floating "describe it, AI builds it" input for the automation start step.
//
// Mounted by FlowBuilder (not SourceBlockPicker) so it survives the picker→canvas swap:
// when no blocks exist it renders the input on the start step; during/after generation it
// becomes a progress strip narrating each block as it drops in, then a review checklist.

import React, { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import clsx from 'clsx';
import {
  SparklesIcon,
  ArrowUpIcon,
  XMarkIcon,
  ExclamationTriangleIcon,
  Cog6ToothIcon,
  ForwardIcon,
  CheckCircleIcon,
} from '@heroicons/react/24/outline';
import { useFlowBuilder, FlowBuilderState } from './FlowBuilderContext';
import { useAutomationGenerator } from '../../hooks/useAutomationGenerator';
import { UnresolvedItem, UnresolvedOption } from './automationSpec';

/** Candidate items to pick from for an unresolved slot, sourced by kind from builder state. */
function candidatesForItem(item: UnresolvedItem, state: FlowBuilderState): UnresolvedOption[] {
  if (item.options && item.options.length) return item.options;
  const anyState = state as any;
  switch (item.kind) {
    case 'workflow':
      return (state.workflows || []).map((w) => ({ id: w.id, label: w.name }));
    case 'selector':
      return (state.selectors || []).map((s) => ({ id: s.id, label: s.name }));
    case 'persona':
      return (anyState.personas || []).map((p: any) => ({ id: p.id, label: p.name }));
    case 'file':
      return (anyState.files || []).map((f: any) => ({ id: f.id, label: f.filename || f.name }));
    case 'recipient':
      return (state.recipients || []).map((r) => ({
        id: r.id,
        label: r.name || r.identifier_preview,
      }));
    default:
      // 'value' / 'confirm' and anything else: no list unless the AI offered options.
      return [];
  }
}

/** One inline "AI asks you to pick" list for a single unresolved item. */
const UnresolvedPicker: React.FC<{
  item: UnresolvedItem;
  candidates: UnresolvedOption[];
  onResolve: (blockId: string, field: string, value: any) => void;
  onFocus: (blockId: string) => void;
}> = ({ item, candidates, onResolve, onFocus }) => {
  const { t } = useTranslation();
  const multi = item.multi === true || item.kind === 'recipient';
  const [checked, setChecked] = useState<Array<string | number>>([]);

  const toggle = (id: string | number) =>
    setChecked((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));

  // No candidates to show: fall back to the "click the question -> focus the block" affordance.
  if (!candidates.length) {
    return (
      <button
        type="button"
        onClick={() => onFocus(item.blockId)}
        className="text-left text-xs text-secondary hover:text-ink transition-colors"
      >
        • {item.question}
      </button>
    );
  }

  return (
    <div className="rounded-lg border border-border bg-canvas p-2">
      <button
        type="button"
        onClick={() => onFocus(item.blockId)}
        className="mb-1.5 block text-left text-xs font-medium text-ink hover:text-accent transition-colors"
      >
        {item.question}
      </button>
      <div className="max-h-40 space-y-0.5 overflow-y-auto">
        {candidates.map((c) => {
          const isChecked = checked.includes(c.id);
          return (
            <button
              key={String(c.id)}
              type="button"
              onClick={() => (multi ? toggle(c.id) : onResolve(item.blockId, item.field, c.id))}
              className={clsx(
                'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors',
                multi && isChecked ? 'bg-active text-ink' : 'text-secondary hover:bg-hover hover:text-ink',
              )}
            >
              {multi && (
                <span
                  className={clsx(
                    'flex h-4 w-4 shrink-0 items-center justify-center rounded border',
                    isChecked ? 'border-accent bg-accent text-surface' : 'border-border',
                  )}
                >
                  {isChecked && <CheckCircleIcon className="h-3 w-3" />}
                </span>
              )}
              <span className="min-w-0 flex-1 truncate">{c.label}</span>
            </button>
          );
        })}
      </div>
      {multi && (
        <div className="mt-1.5 flex justify-end">
          <button
            type="button"
            disabled={!checked.length}
            onClick={() => onResolve(item.blockId, item.field, checked)}
            className={clsx(
              'rounded-md px-2.5 py-1 text-[11px] font-medium transition-colors',
              checked.length
                ? 'bg-accent-strong text-accent-on hover:bg-accent-strong/90'
                : 'bg-hover text-tertiary cursor-not-allowed',
            )}
          >
            {t('Use selection')}
          </button>
        </div>
      )}
    </div>
  );
};

const EXAMPLE_GOALS = [
  'Alert me when this page changes',
  'When my workflow finishes, notify me only if it failed',
  'When a webhook arrives, run my workflow and return the result',
];

export const AiAutomationBar: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { state } = useFlowBuilder();
  const { gen, generate, augment, reset, skip, focusBlock, resolveItem } = useAutomationGenerator();
  const [goal, setGoal] = useState('');
  // Part B: the compact "Ask AI" pill on the canvas (blocks already exist) expands into an input.
  const [askOpen, setAskOpen] = useState(false);
  const [askGoal, setAskGoal] = useState('');
  // Idle start step: collapse to a pill by default so the input never covers the
  // create-start choices behind it; it expands to the full input on click.
  const [idleOpen, setIdleOpen] = useState(false);

  const pickerLists = useMemo(
    () => gen.unresolved.map((u) => ({ item: u, candidates: candidatesForItem(u, state) })),
    [gen.unresolved, state],
  );

  const hasBlocks = state.blocks.length > 0;
  const busy = gen.phase === 'generating';
  const providerMissing = gen.phase === 'error' && /provider/i.test(gen.error || '');

  const submit = () => {
    const g = goal.trim();
    if (g.length < 4 || busy) return;
    generate(g);
  };

  const submitAsk = () => {
    const g = askGoal.trim();
    if (g.length < 4 || busy) return;
    // Kick off the augment; the progress strip (phase !== idle/error) takes over the UI.
    // Keep the panel state so a returned error is shown here for a retry. Clear the text
    // so a follow-up ask starts fresh once the strip is dismissed.
    augment(g);
    setAskGoal('');
  };

  // --- Progress / review strip (during and after generation) ---
  if (gen.phase === 'generating' || gen.phase === 'playing' || gen.phase === 'review' || gen.phase === 'done') {
    return (
      <div className="pointer-events-auto absolute inset-x-0 bottom-20 z-20 flex justify-center px-4">
        <div className="w-full max-w-2xl rounded-2xl border border-border bg-surface/95 backdrop-blur shadow-lg p-3">
          <div className="flex items-start gap-3">
            <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-ink text-surface">
              {gen.phase === 'done' ? <CheckCircleIcon className="h-4 w-4" /> : <SparklesIcon className="h-4 w-4" />}
            </div>
            <div className="min-w-0 flex-1">
              {gen.phase === 'generating' && (
                <p className="text-sm text-ink">{t('Designing your automation…')}</p>
              )}
              {gen.phase === 'playing' && (
                <>
                  <p className="text-sm font-medium text-ink">{t('Building it, block by block…')}</p>
                  {gen.narration && <p className="mt-0.5 text-xs text-secondary">{gen.narration}</p>}
                </>
              )}
              {gen.phase === 'review' && (
                <>
                  <p className="text-sm font-medium text-ink">
                    {t('Almost there — {{n}} to finish', { n: gen.unresolved.length })}
                  </p>
                  <div className="mt-1.5 space-y-1.5">
                    {pickerLists.map(({ item, candidates }) => (
                      <UnresolvedPicker
                        key={`${item.blockId}:${item.field}`}
                        item={item}
                        candidates={candidates}
                        onResolve={resolveItem}
                        onFocus={focusBlock}
                      />
                    ))}
                  </div>
                </>
              )}
              {gen.phase === 'done' && (
                <p className="text-sm text-ink">{gen.message || t('Ready — review the blocks and Save.')}</p>
              )}
              {gen.message && gen.phase !== 'done' && (
                <p className="mt-1 text-xs text-secondary">{gen.message}</p>
              )}
              {gen.rationale && (gen.phase === 'playing' || gen.phase === 'review') && (
                <p className="mt-1 text-[11px] text-tertiary italic">{gen.rationale}</p>
              )}
              {gen.requiresCloud && (
                <p className="mt-1 text-[11px] text-tertiary">
                  {t('Some steps need the cloud and were left out on this device.')}
                </p>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-1">
              {gen.phase === 'playing' && (
                <button
                  type="button"
                  onClick={skip}
                  className="flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-secondary hover:bg-hover hover:text-ink transition-colors"
                >
                  <ForwardIcon className="h-3.5 w-3.5" />
                  {t('Skip')}
                </button>
              )}
              <button
                type="button"
                onClick={reset}
                aria-label={t('Dismiss')}
                className="rounded-lg p-1 text-tertiary hover:bg-hover hover:text-ink transition-colors"
              >
                <XMarkIcon className="h-4 w-4" />
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // --- Part B: blocks already exist → a compact "Ask AI" pill that expands to extend the flow ---
  if (hasBlocks) {
    return (
      <div className="pointer-events-auto absolute inset-x-0 bottom-6 z-20 flex justify-center px-4">
        {askOpen ? (
          <div className="w-full max-w-xl rounded-2xl border border-border bg-surface shadow-lg">
            {gen.phase === 'error' && (
              <div className="flex items-start gap-2 border-b border-border px-4 py-2.5">
                <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
                <p className="text-xs text-secondary">{gen.error}</p>
              </div>
            )}
            <div className="p-3">
              <div className="flex items-center justify-between px-1 pb-2">
                <div className="flex items-center gap-2">
                  <SparklesIcon className="h-4 w-4 text-ink" />
                  <span className="text-xs font-medium text-ink">{t('Ask AI to continue')}</span>
                </div>
                <button
                  type="button"
                  onClick={() => setAskOpen(false)}
                  aria-label={t('Dismiss')}
                  className="rounded-lg p-1 text-tertiary hover:bg-hover hover:text-ink transition-colors"
                >
                  <XMarkIcon className="h-4 w-4" />
                </button>
              </div>
              <div className="flex items-end gap-2">
                <textarea
                  value={askGoal}
                  onChange={(e) => setAskGoal(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                      e.preventDefault();
                      submitAsk();
                    }
                  }}
                  rows={2}
                  autoFocus
                  placeholder={t('Describe what to add — AI extends what you have.')}
                  className="flex-1 resize-none rounded-xl border border-border bg-canvas px-3 py-2 text-sm text-ink placeholder:text-tertiary outline-none focus:border-ink/40 transition-colors"
                />
                <button
                  type="button"
                  onClick={submitAsk}
                  disabled={askGoal.trim().length < 4 || busy}
                  aria-label={t('Add')}
                  className={clsx(
                    'flex h-9 w-9 shrink-0 items-center justify-center rounded-xl transition-colors',
                    askGoal.trim().length < 4 || busy
                      ? 'bg-hover text-tertiary cursor-not-allowed'
                      : 'bg-accent-strong text-accent-on hover:bg-accent-strong/90',
                  )}
                >
                  <ArrowUpIcon className="h-4 w-4" />
                </button>
              </div>
            </div>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setAskOpen(true)}
            className="flex items-center gap-1.5 rounded-full border border-border bg-surface px-3.5 py-2 text-xs font-medium text-secondary shadow-lg hover:border-ink hover:text-ink transition-colors"
          >
            <SparklesIcon className="h-4 w-4" />
            {t('Ask AI')}
          </button>
        )}
      </div>
    );
  }

  // --- Idle: a collapsible "Build with AI" dock on the start step. Collapsed to a
  //     pill by default (like the recorder's Ask-AI dock) so it never hides the
  //     create-start choices; expands to the full input on click. ---
  return (
    <div className="pointer-events-auto absolute inset-x-0 bottom-6 z-20 flex justify-center px-4">
      {idleOpen ? (
        <div className="w-full max-w-xl rounded-2xl border border-border bg-surface shadow-lg">
          {gen.phase === 'error' && (
            <div className="flex items-start gap-2 border-b border-border px-4 py-2.5">
              <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
              <p className="text-xs text-secondary">{gen.error}</p>
            </div>
          )}
          <div className="p-3">
            <div className="flex items-center justify-between px-1 pb-2">
              <div className="flex items-center gap-2">
                <SparklesIcon className="h-4 w-4 text-ink" />
                <span className="text-xs font-medium text-ink">{t('Build with AI')}</span>
              </div>
              <button
                type="button"
                onClick={() => setIdleOpen(false)}
                aria-label={t('Dismiss')}
                className="rounded-lg p-1 text-tertiary hover:bg-hover hover:text-ink transition-colors"
              >
                <XMarkIcon className="h-4 w-4" />
              </button>
            </div>
            <div className="flex items-end gap-2">
              <textarea
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    submit();
                  }
                }}
                rows={2}
                autoFocus
                placeholder={t('Describe what you want to automate — AI will build it.')}
                className="flex-1 resize-none rounded-xl border border-border bg-canvas px-3 py-2 text-sm text-ink placeholder:text-tertiary outline-none focus:border-ink/40 transition-colors"
              />
              <button
                type="button"
                onClick={submit}
                disabled={goal.trim().length < 4 || busy}
                aria-label={t('Build')}
                className={clsx(
                  'flex h-9 w-9 shrink-0 items-center justify-center rounded-xl transition-colors',
                  goal.trim().length < 4 || busy
                    ? 'bg-hover text-tertiary cursor-not-allowed'
                    : 'bg-accent-strong text-accent-on hover:bg-accent-strong/90',
                )}
              >
                <ArrowUpIcon className="h-4 w-4" />
              </button>
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {providerMissing ? (
                <button
                  type="button"
                  onClick={() => navigate('/settings?tab=ai')}
                  className="flex items-center gap-1 rounded-full border border-border px-2.5 py-1 text-[11px] text-secondary hover:border-ink hover:text-ink transition-colors"
                >
                  <Cog6ToothIcon className="h-3 w-3" />
                  {t('Set up an AI provider')}
                </button>
              ) : (
                EXAMPLE_GOALS.map((ex) => (
                  <button
                    key={ex}
                    type="button"
                    onClick={() => setGoal(t(ex))}
                    className="rounded-full border border-border px-2.5 py-1 text-[11px] text-secondary hover:border-ink hover:text-ink transition-colors"
                  >
                    {t(ex)}
                  </button>
                ))
              )}
            </div>
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setIdleOpen(true)}
          className="flex items-center gap-1.5 rounded-full border border-border bg-surface px-3.5 py-2 text-xs font-medium text-secondary shadow-lg hover:border-ink hover:text-ink transition-colors"
        >
          <SparklesIcon className="h-4 w-4" />
          {t('Build with AI')}
        </button>
      )}
    </div>
  );
};
