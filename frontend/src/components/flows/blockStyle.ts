import type { CSSProperties } from 'react';
import { FlowBlock, blockColorToken } from './types';
import type { Tint } from '../../utils/tint';

/**
 * Token-driven styling for automation blocks.
 *
 * The canvas node and the add-block menu paint themselves entirely from three
 * CSS custom properties set per block family — `--bk-a` (soft card wash),
 * `--bk-b` (deeper icon-chip fill) and `--bk-fg` (icon/accent color). Those map
 * onto the app's category-tint tokens (`--tint-<family>-a/-b/-fg`, defined for
 * BOTH themes in styles/theme.css), so a block is correct in light and dark mode
 * without a single hard-coded hue. The visual rules that consume these vars live
 * in index.css (`.block-card`, `.block-icon`, `.block-badge`, `.block-connector`);
 * this module only chooses the family and hands back the variables.
 */

export function blockTint(block: FlowBlock): Tint {
  return blockColorToken(block.blockType, block.type) as Tint;
}

/**
 * The three family variables to spread onto a block element's `style`. Neutral
 * blocks fall back to surface/hover/secondary so they still read as a real card.
 */
export function blockVars(tint: Tint): CSSProperties {
  if (tint === 'neutral') {
    return {
      ['--bk-a' as string]: 'var(--surface)',
      ['--bk-b' as string]: 'var(--hover)',
      ['--bk-fg' as string]: 'var(--secondary)',
    } as CSSProperties;
  }
  return {
    ['--bk-a' as string]: `var(--tint-${tint}-a)`,
    ['--bk-b' as string]: `var(--tint-${tint}-b)`,
    ['--bk-fg' as string]: `var(--tint-${tint}-fg)`,
  } as CSSProperties;
}

/** Convenience: family variables straight from a block. */
export function blockStyleVars(block: FlowBlock): CSSProperties {
  return blockVars(blockTint(block));
}

/**
 * Whether a block has its underlying resource actually selected/configured.
 * Drives the "Needs setup" affordance so an unfinished automation is obvious at
 * a glance instead of silently invalid at save time.
 */
export function isBlockReady(block: FlowBlock): boolean {
  const c = block.config || {};
  switch (block.blockType) {
    case 'change_detected':
      if (c.target_id) return true;
      return !!(c.url && c.wizardSelectors?.length > 0);
    case 'webhook_received':
      return true;
    case 'ai_session':
      return !!(c.session_ids?.length > 0);
    case 'ai_session_completed':
    case 'ai_session_started':
      return !!c.ai_session_id;
    case 'workflow':
    case 'workflow_completed':
    case 'workflow_started':
      return !!c.workflow_id;
    case 'notification':
      return !!(c.recipients?.length > 0 || c.channels?.length > 0);
    default:
      return true;
  }
}
