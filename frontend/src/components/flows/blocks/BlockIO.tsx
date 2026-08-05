import React from 'react';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { FlowBlock } from '../types';
import { blockOutputTokens, blockInputTokens } from '../blockCatalog';
import { useFlowState, getAncestorChain } from '../FlowBuilderContext';
import { shallow } from 'zustand/shallow';

interface BlockIOProps {
  block: FlowBlock;
  /** Extra classes on the strip container (padding + divider per card variant). */
  className?: string;
}

// Object-output prefixes that MUST come from an upstream block — if a detected
// input uses one of these but no ancestor produces it, the wiring is broken.
// Mirrors blockCatalog OBJECT_OUTPUT_KEYS. Bare/system tokens (now_time,
// target_name, …) are never flagged — too risky to second-guess.
const PRODUCER_PREFIXES = new Set(['extracted', 'result', 'ai_result', 'payload', 'item']);

/** The prefix of a token: `result.price` → `result`, `success` → `success`. */
const prefixOf = (tok: string): string => (tok.includes('.') ? tok.slice(0, tok.indexOf('.')) : tok);

/**
 * A block's "function signature" footer: the inputs it CONSUMES (auto-detected
 * from `{{...}}` placeholders in its config) and the outputs it PRODUCES (catalog
 * `produces`). Output chips copy their `{{token}}`; input chips are flagged when
 * no upstream block/trigger provides them (broken wiring). Renders nothing when a
 * block neither consumes nor produces anything, keeping the canvas calm.
 */
export const BlockIO: React.FC<BlockIOProps> = ({ block, className }) => {
  const { t } = useTranslation();
  // Slice subscriptions, not the whole state: this strip renders once per block,
  // so a full-state subscription made every keystroke re-render every block's IO.
  const blocks = useFlowState((s) => s.blocks, shallow);
  const blockOutputs = useFlowState((s) => s.blockOutputs, shallow);

  const outputs = blockOutputTokens(block.blockType);
  const inputs = blockInputTokens(block.config);

  // Tokens + prefixes reachable from this block's ancestors, so a detected input
  // can be flagged satisfied vs unresolved.
  const upstream = React.useMemo(() => {
    const set = new Set<string>();
    for (const b of getAncestorChain(blocks, block.id)) {
      if (b.id === block.id) continue;
      for (const tok of blockOutputs[b.id] || []) {
        set.add(tok);
        set.add(prefixOf(tok));
      }
    }
    return set;
  }, [blocks, blockOutputs, block.id]);

  if (outputs.length === 0 && inputs.length === 0) return null;

  const isResolved = (tok: string): boolean => {
    const p = prefixOf(tok);
    if (upstream.has(tok) || upstream.has(p)) return true;
    // Only the known data-object prefixes are provably missing a producer.
    return !PRODUCER_PREFIXES.has(p);
  };

  const copyOutput = (token: string) => {
    const editable = token.endsWith('.*');
    const value = editable ? `{{${token.slice(0, -1)}` : `{{${token}}}`;
    try {
      navigator.clipboard?.writeText(value);
      toast.success(t('Copied — paste into any input below'));
    } catch {
      /* clipboard unavailable — the chip is still a useful hint */
    }
  };

  return (
    <div className={clsx('space-y-1.5', className)}>
      {inputs.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] font-medium uppercase tracking-wider text-tertiary">{t('Inputs')}</span>
          {inputs.map((tok) => {
            const resolved = isResolved(tok);
            return (
              <span
                key={tok}
                title={resolved
                  ? t('Provided by an upstream block or the trigger')
                  : t('No upstream block provides this — check the wiring')}
                className={clsx(
                  'inline-flex items-center gap-1 font-mono text-[10px] px-1.5 py-0.5 rounded border',
                  resolved && 'border-ink/15 bg-canvas text-secondary',
                )}
                style={resolved ? undefined : {
                  backgroundColor: 'rgb(var(--tint-amber-a))',
                  color: 'rgb(var(--tint-amber-fg))',
                  borderColor: 'rgb(var(--tint-amber-fg) / 0.3)',
                }}
              >
                {!resolved && <ExclamationTriangleIcon className="h-2.5 w-2.5" />}
                {`{{${tok}}}`}
              </span>
            );
          })}
        </div>
      )}
      {outputs.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] font-medium uppercase tracking-wider text-tertiary">{t('Outputs')}</span>
          {outputs.map((token) => (
            <button
              key={token}
              type="button"
              onClick={() => copyOutput(token)}
              title={t('Copy this output to chain into a downstream input')}
              className="font-mono text-[10px] px-1.5 py-0.5 rounded border border-ink/20 bg-surface text-secondary hover:text-ink hover:border-ink/40 transition-colors"
            >
              {`{{${token}}}`}
            </button>
          ))}
        </div>
      )}
    </div>
  );
};
