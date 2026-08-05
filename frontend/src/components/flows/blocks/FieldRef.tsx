import React from 'react';
import { Menu } from '@headlessui/react';
import { VariableIcon } from '@heroicons/react/24/outline';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { useFlowState, getAncestorChain } from '../FlowBuilderContext';
import { shallow } from 'zustand/shallow';
import { getBlockDef } from '../blockCatalog';

interface FieldRefProps {
  /** The block whose input is being edited — its ancestors are the data sources. */
  blockId: string;
  /** Called with a ready-to-paste placeholder token, e.g. `{{result.price}}`. */
  onInsert: (token: string) => void;
  /** Optional extra classes on the trigger button. */
  className?: string;
}

interface OutputEntry {
  /** Bare token (without braces), e.g. `result.*` or `success`. */
  token: string;
  /** Insertable token (without braces); `.*` is trimmed so the user completes it. */
  insert: string;
  /** True when the user still needs to type a key after the dot. */
  editable: boolean;
}

interface SourceGroup {
  blockId: string;
  label: string;
  outputs: OutputEntry[];
}

/**
 * Small "insert field" dropdown of the placeholder tokens produced by upstream
 * (ancestor) blocks. Choosing one inserts its `{{token}}` into the target input
 * via `onInsert`. Manual typing into the input still works — this is additive.
 *
 * The available outputs come from `state.blockOutputs` (derived from the catalog
 * `produces`), walked over the ancestor chain so only genuinely-upstream data is
 * offered.
 */
export const FieldRef: React.FC<FieldRefProps> = ({ blockId, onInsert, className }) => {
  const { t } = useTranslation();
  // Slice subscriptions — this menu is mounted per editable field, so a full-state
  // subscription multiplied every edit by the number of fields on the canvas.
  const blocks = useFlowState((s) => s.blocks, shallow);
  const blockOutputs = useFlowState((s) => s.blockOutputs, shallow);
  const workflows = useFlowState((s) => s.workflows, shallow);
  const sessions = useFlowState((s) => s.sessions, shallow);

  const groups = React.useMemo<SourceGroup[]>(() => {
    const result: SourceGroup[] = [];
    const ancestors = getAncestorChain(blocks, blockId);
    // getAncestorChain returns [self, parent, …root]; self can't reference its own
    // output, so skip it and present nearest-ancestor first.
    for (const b of ancestors) {
      if (b.id === blockId) continue;
      const tokens = blockOutputs[b.id];
      if (!tokens || tokens.length === 0) continue;
      const def = getBlockDef(b.blockType);

      // For a workflow / ai_session ancestor, resolve the concrete output keys of
      // the chosen workflow/session so the generic `result.*` / `ai_result.*`
      // expands into its REAL fields (direct-insertable, no key to type).
      const concreteKeys: Record<string, string[]> = {};
      if (['workflow', 'workflow_completed', 'workflow_started'].includes(b.blockType)) {
        const wf = b.config?.workflow_id ? workflows.find((w) => w.id === b.config.workflow_id) : undefined;
        if (wf?.outputs?.length) concreteKeys.result = wf.outputs.map((o) => o.key);
      } else if (['ai_session', 'ai_session_completed', 'ai_session_started'].includes(b.blockType)) {
        const sid = b.config?.ai_session_id ?? b.config?.session_ids?.[0];
        const sess = sid ? sessions.find((s) => s.id === sid) : undefined;
        if (sess?.outputs?.length) concreteKeys.ai_result = sess.outputs.map((o) => o.key);
      }

      const outputs: OutputEntry[] = [];
      for (const tok of tokens) {
        if (tok.endsWith('.*')) {
          const prefix = tok.slice(0, -2); // 'result.*' -> 'result'
          const keys = concreteKeys[prefix];
          if (keys && keys.length > 0) {
            // Concrete keys replace the generic placeholder entry.
            for (const k of keys) outputs.push({ token: `${prefix}.${k}`, insert: `${prefix}.${k}`, editable: false });
            continue;
          }
          outputs.push({ token: tok, insert: tok.slice(0, -1), editable: true });
        } else {
          outputs.push({ token: tok, insert: tok, editable: false });
        }
      }
      result.push({ blockId: b.id, label: def ? t(def.label) : b.blockType, outputs });
    }
    return result;
  }, [blocks, blockOutputs, workflows, sessions, blockId, t]);

  if (groups.length === 0) return null;

  return (
    <Menu as="div" className="relative inline-block text-left shrink-0">
      <Menu.Button
        type="button"
        title={t('Insert a field from an upstream block')}
        className={clsx(
          'flex items-center gap-1 px-1.5 py-1 rounded border border-border bg-surface text-tertiary hover:text-ink hover:border-secondary transition text-[11px]',
          className,
        )}
      >
        <VariableIcon className="h-3.5 w-3.5" />
        <span className="hidden @pair/stage:inline">{t('Insert field')}</span>
      </Menu.Button>
      <Menu.Items className="absolute right-0 z-20 mt-1 w-64 max-h-72 overflow-auto rounded-lg border border-border bg-surface shadow-lg focus:outline-none py-1">
        {groups.map((group) => (
          <div key={group.blockId} className="py-1">
            <div className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wider text-tertiary">
              {group.label}
            </div>
            {group.outputs.map((out) => (
              <Menu.Item key={out.token}>
                {({ active }) => (
                  <button
                    type="button"
                    onClick={() => onInsert(`{{${out.insert}}}`)}
                    className={clsx(
                      'w-full flex items-center justify-between gap-2 px-3 py-1.5 text-left text-xs',
                      active ? 'bg-hover text-ink' : 'text-secondary',
                    )}
                  >
                    <code className="font-mono text-ink">{`{{${out.token}}}`}</code>
                    {out.editable && (
                      <span className="text-[10px] text-tertiary">{t('+ key')}</span>
                    )}
                  </button>
                )}
              </Menu.Item>
            ))}
          </div>
        ))}
      </Menu.Items>
    </Menu>
  );
};
