import React, { useState, useRef } from 'react';
import { shallow } from 'zustand/shallow';
import { PlusIcon, TrashIcon, ArrowsRightLeftIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { FlowBlock, getBlockIcon, getBlockLabel } from '../types';
import { blockStyleVars, isBlockReady } from '../blockStyle';
import { getChildBlocks, useFlowState, useFlowActions } from '../FlowBuilderContext';
import { BlockContent } from './BlockContent';
import { BlockIO } from './BlockIO';
import { BlockAddMenu } from './BlockAddMenu';

interface BlockNodeProps {
  block: FlowBlock;
  depth?: number;
  actionsOnly?: boolean;
}

/** The round "add next block" affordance + its popover, shared by every node. */
const AddButton: React.FC<{ parentBlockId: string; size?: 'sm' | 'lg' }> = ({ parentBlockId, size = 'sm' }) => {
  const { t } = useTranslation();
  const [menuOpen, setMenuOpen] = useState(false);
  const addBtnRef = useRef<HTMLButtonElement>(null);
  const dim = size === 'lg' ? 'w-9 h-9' : 'w-7 h-7';
  const icon = size === 'lg' ? 'h-5 w-5' : 'h-4 w-4';
  return (
    <>
      <button
        ref={addBtnRef}
        type="button"
        aria-expanded={menuOpen}
        aria-label={t('Add block')}
        title={t('Add block')}
        onClick={() => setMenuOpen(!menuOpen)}
        className={clsx(
          dim,
          'rounded-full flex items-center justify-center border transition-all duration-200 active:scale-95',
          menuOpen
            ? 'bg-ink text-surface border-ink shadow-md'
            : 'bg-surface hover:bg-chrome text-tertiary hover:text-ink border-border hover:border-ink/30 shadow-sm hover:shadow'
        )}
      >
        <PlusIcon className={clsx(icon, 'transition-transform duration-200', menuOpen && 'rotate-45')} />
      </button>
      {menuOpen && <BlockAddMenu parentBlockId={parentBlockId} anchorRef={addBtnRef} onClose={() => setMenuOpen(false)} />}
    </>
  );
};

/**
 * Renders a block's heroicon.
 *
 * `getBlockIcon` only ever hands back one of a fixed set of module-level icons,
 * but that is invisible across the module boundary: a component *type* produced
 * by a call inside a render body reads as "created during render", so React
 * would be told to remount the icon on every render
 * (`react-hooks/static-components`). Taking the resolved type as a prop is data,
 * not a locally-declared component, and keeps the identity stable.
 */
const BlockGlyph: React.FC<{ icon: React.ElementType; className: string }> = ({ icon: Icon, className }) => (
  <Icon className={className} />
);

/** Small "Needs setup" chip shown when a block's underlying resource is unset. */
const NeedsSetup: React.FC = () => {
  const { t } = useTranslation();
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-medium"
      style={{
        backgroundColor: 'rgb(var(--tint-amber-a))',
        color: 'rgb(var(--tint-amber-fg))',
        borderColor: 'rgb(var(--tint-amber-fg) / 0.3)',
      }}
      title={t('Pick or configure this block to finish it')}
    >
      <ExclamationTriangleIcon className="h-3 w-3" />
      {t('Needs setup')}
    </span>
  );
};

const ParallelBranches: React.FC<{ blocks: FlowBlock[]; depth: number }> = React.memo(({ blocks, depth }) => {
  const { t } = useTranslation();
  return (
    <div className="w-full mt-1">
      <div className="flex items-center justify-center gap-2 mb-4">
        <div className="h-px flex-1 bg-gradient-to-r from-transparent via-border to-transparent" />
        <div className="flex items-center gap-1.5 px-3 py-1 bg-surface rounded-full border border-border shadow-sm">
          <ArrowsRightLeftIcon className="h-3 w-3 text-secondary" />
          <span className="text-[11px] font-medium text-secondary">{t('Parallel')}</span>
          <span className="text-[11px] text-tertiary">({blocks.length})</span>
        </div>
        <div className="h-px flex-1 bg-gradient-to-r from-transparent via-border to-transparent" />
      </div>
      <div className={clsx(
        'grid gap-4',
        blocks.length === 2 && 'grid-cols-2',
        blocks.length === 3 && 'grid-cols-3',
        blocks.length >= 4 && 'grid-cols-2 @split/stage:grid-cols-4',
      )}>
        {blocks.map((child, idx) => (
          <div key={child.id} className="relative">
            <div className="flex items-center gap-1.5 mb-2">
              <div className="w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold bg-chrome text-secondary border border-border">
                {idx + 1}
              </div>
              <span className="text-[10px] text-tertiary">{t('Branch {{n}}', { n: idx + 1 })}</span>
            </div>
            <div className="p-2 rounded-lg border border-border bg-surface">
              <BlockNode block={child} depth={depth + 1} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
});
ParallelBranches.displayName = 'ParallelBranches';

export const BlockNode: React.FC<BlockNodeProps> = React.memo(({ block, depth = 0, actionsOnly = false }) => {
  const { t } = useTranslation();
  const { removeBlock } = useFlowActions();
  const vars = blockStyleVars(block);
  const Icon = getBlockIcon(block);
  const isFirst = !block.parentId;
  const label = getBlockLabel(block, isFirst);
  const ready = isBlockReady(block);

  // Subscribe to ONLY this node's children (shallow-compared) so an edit to a
  // sibling block doesn't re-render this subtree.
  const children = useFlowState((s) => getChildBlocks(s.blocks, block.id), shallow);

  // actionsOnly mode: skip rendering the block card, just show the add button + children
  if (actionsOnly) {
    return (
      <div className="flex flex-col items-center">
        <AddButton parentBlockId={block.id} size="lg" />
        {children.map(child => (
          <div key={child.id} className="w-full mt-3">
            <BlockNode block={child} depth={depth + 1} />
          </div>
        ))}
      </div>
    );
  }

  // Source blocks: the automation's starting point — a clean surface card whose
  // header icon is tinted by trigger family.
  if (isFirst) {
    return (
      <div className="flex flex-col items-center block-in" data-block-node={block.id}>
        <div
          style={vars}
          className="relative w-full bg-surface rounded-2xl border border-border shadow-[0_1px_3px_rgba(0,0,0,0.05),0_10px_28px_rgba(0,0,0,0.06)] overflow-hidden"
        >
          <div className="flex items-center gap-2.5 px-5 py-3 border-b border-border">
            <div className="block-icon w-6 h-6 rounded-md flex items-center justify-center">
              <BlockGlyph icon={Icon} className="h-3.5 w-3.5" />
            </div>
            <span className="text-sm font-semibold text-ink tracking-tight">{label}</span>
            <span className="text-[10px] px-2 py-0.5 rounded-full font-medium bg-chrome text-secondary">{t('Trigger')}</span>
            {!ready && <NeedsSetup />}
          </div>
          <BlockContent block={block} />
          <BlockIO block={block} className="px-5 py-2.5 border-t border-border" />
        </div>

        {/* Connector + add — only once the trigger is configured */}
        {ready && (
          <div className="py-3 flex flex-col items-center">
            <div className="w-0.5 h-6 bg-border" />
            <AddButton parentBlockId={block.id} />
            <div className={clsx('w-0.5 h-3', children.length > 0 ? 'bg-border' : 'bg-transparent')} />
          </div>
        )}

        {ready && children.length === 1 && (
          <div className="w-full"><BlockNode block={children[0]} depth={depth + 1} /></div>
        )}
        {ready && children.length > 1 && <ParallelBranches blocks={children} depth={depth} />}
      </div>
    );
  }

  // Action / condition blocks — tinted card floating on the canvas.
  return (
    <div className="flex flex-col items-center block-in" data-block-node={block.id}>
      <div style={vars} className="block-card group relative w-full max-w-lg p-4 rounded-xl border shadow-sm hover:shadow-md hover:-translate-y-0.5">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2 min-w-0">
            <div className="block-icon w-7 h-7 rounded-lg flex items-center justify-center shrink-0">
              <BlockGlyph icon={Icon} className="h-4 w-4" />
            </div>
            <span className="text-sm font-medium text-ink truncate">{label}</span>
            <span className="block-badge text-[10px] px-1.5 py-0.5 rounded-full font-medium shrink-0">
              {block.type}
            </span>
            {!ready && <NeedsSetup />}
          </div>
          <button
            type="button"
            onClick={() => removeBlock(block.id)}
            aria-label={t('Remove block')}
            title={t('Remove block')}
            className="p-1 rounded-md text-tertiary opacity-0 group-hover:opacity-100 focus:opacity-100 hover:bg-hover hover:text-red-500 transition-all shrink-0"
          >
            <TrashIcon className="h-4 w-4" />
          </button>
        </div>

        <BlockContent block={block} />
        <BlockIO block={block} className="mt-3 pt-3 border-t border-border" />
      </div>

      {/* Connector + add */}
      <div className="py-3 flex flex-col items-center">
        <div className="block-connector w-0.5 h-6" style={vars} />
        <AddButton parentBlockId={block.id} />
        <div className={clsx('w-0.5 h-3', children.length > 0 && 'block-connector')} style={children.length > 0 ? vars : undefined} />
      </div>

      {children.length === 1 && (
        <div className="w-full"><BlockNode block={children[0]} depth={depth + 1} /></div>
      )}
      {children.length > 1 && <ParallelBranches blocks={children} depth={depth} />}
    </div>
  );
});
BlockNode.displayName = 'BlockNode';
