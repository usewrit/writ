import { useEffect, useRef, useState } from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { STEP_TYPES, GROUP_ORDER, GROUP_NODE_STYLE, type StepType } from './stepMeta';
import { MagnifyingGlassIcon, XMarkIcon } from '@heroicons/react/24/outline';

/**
 * Add-step palette: grouped, searchable type picker. Single source shared by the
 * workflow StepsEditor and the BrowserRecorder's manual step adder so both surfaces
 * offer the exact same set of step types with identical metadata (icon/label/desc).
 */
export function StepTypePalette({ onSelect, onCancel, embedded = false }: { onSelect: (type: StepType) => void; onCancel: () => void; embedded?: boolean }) {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => { inputRef.current?.focus(); }, []);

  const q = query.trim().toLowerCase();
  // Search matches the TRANSLATED label/description/group, so a French user can
  // find "Faire défiler" by typing it rather than the English key.
  const visible = STEP_TYPES.filter(st => !st.internal && (
    !q || t(st.label).toLowerCase().includes(q) || st.type.includes(q) ||
    t(st.description).toLowerCase().includes(q) || t(st.group).toLowerCase().includes(q)
  ));

  // `embedded`: drop the floating-card chrome (border/shadow/own-scroll/close-X) when
  // the palette is hosted inside a roomy modal that already provides those — the modal
  // frames it and scrolls, so the grid gets full width instead of being squeezed.
  return (
    <div className={clsx(!embedded && 'my-2 bg-surface border border-border rounded-xl shadow-[0_4px_16px_rgba(0,0,0,0.06)] overflow-hidden')}>
      <div className={clsx('flex items-center gap-2 px-3 py-2', embedded ? 'border border-border rounded-lg bg-canvas' : 'border-b border-border')}>
        <MagnifyingGlassIcon className="w-3.5 h-3.5 text-tertiary shrink-0" />
        <input
          ref={inputRef}
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && visible.length > 0) { e.preventDefault(); onSelect(visible[0].type); }
            if (e.key === 'Escape') { e.stopPropagation(); onCancel(); }
          }}
          placeholder={t('Search step types…')}
          className="flex-1 text-xs bg-transparent outline-none placeholder:text-tertiary"
        />
        {!embedded && <button onClick={onCancel} className="text-tertiary hover:text-ink"><XMarkIcon className="w-3.5 h-3.5" /></button>}
      </div>
      <div className={clsx('space-y-3', embedded ? 'pt-3' : 'max-h-72 overflow-y-auto p-3')}>
        {GROUP_ORDER.map(group => {
          const items = visible.filter(st => st.group === group);
          if (!items.length) return null;
          return (
            <div key={group}>
              <div className="text-[10px] font-medium text-tertiary uppercase tracking-wider mb-1.5">{t(group)}</div>
              <div className="grid grid-cols-2 @pair/stage:grid-cols-3 gap-1.5">
                {items.map(st => (
                  <button
                    key={st.type}
                    onClick={() => onSelect(st.type)}
                    className="flex items-center gap-2 px-2.5 py-2 border border-border/80 rounded-lg text-left hover:border-ink/30 hover:bg-hover transition-colors"
                  >
                    <span className={clsx('w-5 h-5 rounded-md flex items-center justify-center shrink-0', GROUP_NODE_STYLE[st.group])}>
                      <st.Icon className="w-2.5 h-2.5" />
                    </span>
                    <span className="min-w-0">
                      <span className="block text-xs font-medium text-ink leading-tight">{t(st.label)}</span>
                      <span className="block text-[10px] text-tertiary truncate leading-tight">{t(st.description)}</span>
                    </span>
                  </button>
                ))}
              </div>
            </div>
          );
        })}
        {visible.length === 0 && <p className="text-xs text-tertiary text-center py-4">{t('No step type matches "{{q}}"', { q: query })}</p>}
      </div>
    </div>
  );
}
