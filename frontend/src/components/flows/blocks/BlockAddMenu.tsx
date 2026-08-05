import React, { useRef, useState, useLayoutEffect, useEffect, CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { SparklesIcon } from '@heroicons/react/24/outline';
import { BlockMeta, blockColorToken } from '../types';
import { blockVars } from '../blockStyle';
import type { Tint } from '../../../utils/tint';
import { getAvailableBlocks } from '../FlowBuilderContext';
import { useFlowActions, useFlowState } from '../FlowBuilderContext';
import { shallow } from 'zustand/shallow';
import { APP_PLATFORM } from '../../../api/aiAssist';

// Family variables for a menu row, mirroring the canvas node tints so the color
// association is identical in both surfaces (and dark-mode correct via tokens).
const metaVars = (b: BlockMeta): CSSProperties => blockVars(blockColorToken(b.blockType, b.type) as Tint);

interface BlockAddMenuProps {
  parentBlockId: string;
  anchorRef: React.RefObject<HTMLButtonElement | null>;
  onClose: () => void;
}

export const BlockAddMenu: React.FC<BlockAddMenuProps> = ({ parentBlockId, anchorRef, onClose }) => {
  const { t } = useTranslation();
  // `addBlock` comes from the stable action handles (never a re-render source);
  // only `blocks` is subscribed, and only while the menu is open.
  const { addBlock } = useFlowActions();
  const blocks = useFlowState((s) => s.blocks, shallow);
  const availableBlocks = getAvailableBlocks(blocks, parentBlockId, APP_PLATFORM);
  const recommended = availableBlocks.filter(b => b.priority >= 80);
  const actions = availableBlocks.filter(b => b.type === 'action' && b.priority < 80);
  const other = availableBlocks.filter(b => b.priority < 80 && b.type !== 'action');

  const menuRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  useLayoutEffect(() => {
    const anchor = anchorRef.current;
    const menu = menuRef.current;
    if (!anchor || !menu) return;

    const anchorRect = anchor.getBoundingClientRect();
    const menuH = menu.offsetHeight;
    const menuW = menu.offsetWidth;
    const viewportH = window.innerHeight;
    const viewportW = window.innerWidth;

    let top = anchorRect.top + anchorRect.height / 2 - 8;
    let left = anchorRect.right + 12;

    // Flip up if overflowing bottom
    if (top + menuH > viewportH - 16) {
      top = Math.max(16, anchorRect.bottom - menuH + 8);
    }
    // Flip to the left of the anchor if overflowing right
    if (left + menuW > viewportW - 16) {
      left = Math.max(16, anchorRect.left - menuW - 12);
    }

    setPos({ top, left });
  }, [anchorRef]);

  // Close on Escape; focus the first item once positioned for keyboard use.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  useEffect(() => {
    if (pos) menuRef.current?.querySelector<HTMLButtonElement>('button')?.focus();
  }, [pos]);

  const handleAdd = (meta: BlockMeta) => {
    const existingIds = new Set(
      Array.from(document.querySelectorAll('[data-block-node]')).map(el => el.getAttribute('data-block-node'))
    );
    addBlock({ type: meta.type, blockType: meta.blockType }, parentBlockId);
    onClose();
    requestAnimationFrame(() => {
      setTimeout(() => {
        const allBlocks = document.querySelectorAll('[data-block-node]');
        for (const el of Array.from(allBlocks)) {
          if (!existingIds.has(el.getAttribute('data-block-node'))) {
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
            break;
          }
        }
      }, 60);
    });
  };

  const menuContent = (
    <>
      <div className="fixed inset-0 z-[999]" onClick={onClose} />
      <div
        ref={menuRef}
        role="menu"
        className="fixed z-[1000] w-72 bg-surface rounded-xl border border-border shadow-xl overflow-hidden block-menu-in"
        style={pos ? { top: pos.top, left: pos.left } : { opacity: 0, pointerEvents: 'none', top: 0, left: 0 }}
      >
        {recommended.length > 0 && (
          <div className="p-2 bg-chrome/60 border-b border-border">
            <div className="text-[10px] uppercase tracking-wider text-secondary px-2 py-1 font-semibold flex items-center gap-1">
              <SparklesIcon className="h-3 w-3" /> {t('Suggested')}
            </div>
            {recommended.map((b, i) => {
              const Icon = b.icon;
              return (
                <button key={i} type="button" role="menuitem" onClick={() => handleAdd(b)}
                  className="w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-left transition-colors hover:bg-hover mt-0.5 focus:bg-hover focus:outline-none"
                >
                  <div className="block-icon w-8 h-8 rounded-lg flex items-center justify-center shrink-0" style={metaVars(b)}>
                    <Icon className="h-4 w-4" />
                  </div>
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-ink">{t(b.label)}</div>
                    <div className="text-xs text-tertiary truncate">{t(b.description)}</div>
                  </div>
                </button>
              );
            })}
          </div>
        )}

        {actions.length > 0 && (
          <div className="p-2">
            <div className="text-[10px] uppercase tracking-wider text-tertiary px-2 py-1 font-semibold">{t('Actions')}</div>
            {actions.map((b, i) => {
              const Icon = b.icon;
              return (
                <button key={i} type="button" role="menuitem" onClick={() => handleAdd(b)}
                  className="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-left transition-colors hover:bg-hover focus:bg-hover focus:outline-none"
                >
                  <Icon className="h-4 w-4 shrink-0" style={{ ...metaVars(b), color: 'rgb(var(--bk-fg))' }} />
                  <div className="min-w-0">
                    <div className="text-sm text-ink">{t(b.label)}</div>
                    <div className="text-xs text-tertiary truncate">{t(b.description)}</div>
                  </div>
                </button>
              );
            })}
          </div>
        )}

        {other.length > 0 && (
          <div className="p-2 border-t border-border">
            <div className="text-[10px] uppercase tracking-wider text-tertiary px-2 py-1 font-semibold">{t('Logic')}</div>
            {other.map((b, i) => {
              const Icon = b.icon;
              return (
                <button key={i} type="button" role="menuitem" onClick={() => handleAdd(b)}
                  className="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-left transition-colors hover:bg-hover focus:bg-hover focus:outline-none"
                >
                  <Icon className="h-4 w-4 shrink-0" style={{ ...metaVars(b), color: 'rgb(var(--bk-fg))' }} />
                  <div className="min-w-0">
                    <div className="text-sm text-ink">{t(b.label)}</div>
                    <div className="text-xs text-tertiary truncate">{t(b.description)}</div>
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </div>
    </>
  );

  return createPortal(menuContent, document.body);
};
