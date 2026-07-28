import React, { useState } from 'react';
import {
  TrashIcon,
  Squares2X2Icon,
  CodeBracketIcon,
  DocumentTextIcon,
  PhotoIcon,
} from '@heroicons/react/24/outline';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { WizardSelector } from './WizardContext';

/** The full "how do I add a target" vocabulary. Browse = interact with the page
 *  (recorded as setup steps); Click/Area/Zone turn canvas gestures into picks; CSS
 *  types a selector by hand. Broader than the recorder's canvas SelectionMode
 *  ('click' | 'area' | 'zone'); the bridge maps browse/manual → null. The mode
 *  SWITCHER lives in the recorder's floating stage toolbar; this panel reads
 *  `selectionMode` to reveal the CSS input. AI-find runs through the recorder's own
 *  bottom "Ask AI" dock, not here. */
export type MonitorSelectionMode = 'browse' | 'click' | 'area' | 'zone' | 'manual';

const CHECK_TYPES: Array<{ id: string; label: string; icon: React.FC<any> }> = [
  { id: 'text', label: 'Text', icon: DocumentTextIcon },
  { id: 'html', label: 'HTML', icon: CodeBracketIcon },
  { id: 'visual', label: 'Visual', icon: PhotoIcon },
];

export interface MonitorTargetsPanelProps {
  /** The tracked selectors (check targets), owned by the wizard config. */
  selectors: WizardSelector[];
  /** Only used to reveal the manual CSS input when the switcher is in "CSS" mode. */
  selectionMode: MonitorSelectionMode;
  /** Append new selectors (from the manual CSS input). */
  onAddSelectors: (selectors: WizardSelector[]) => void;
  onRemoveSelector: (id: string) => void;
  onUpdateSelector: (id: string, updates: Partial<WizardSelector>) => void;
  /** Visual-zone hover sync with the live-preview overlay. */
  hoveredZoneId: string | null;
  onHoverZone: (id: string | null) => void;
}

const newSelectorId = () => `sel_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;

/**
 * MonitorTargetsPanel — the check-target list (selectors + per-target check-type +
 * ignore-regex + manual CSS add). Rendered inside the recorder's own "Targets" spine
 * tab so monitor creation shares the exact BrowserRecorder chrome as workflow
 * recording; also reused in the onboarding sandbox sidebar. AI selector-finding lives
 * in the recorder's bottom "Ask AI" dock.
 */
export const MonitorTargetsPanel: React.FC<MonitorTargetsPanelProps> = ({
  selectors,
  selectionMode,
  onAddSelectors,
  onRemoveSelector,
  onUpdateSelector,
  hoveredZoneId,
  onHoverZone,
}) => {
  const { t } = useTranslation();

  const [editingSelectorId, setEditingSelectorId] = useState<string | null>(null);
  const [manualSelectorInput, setManualSelectorInput] = useState('');

  const addManual = (selector: string) => {
    if (selectors.some(s => s.selector === selector)) { toast.error(t('Selector already added')); return; }
    onAddSelectors([{
      id: newSelectorId(),
      name: t('Selector {{n}}', { n: selectors.length + 1 }),
      selector, description: '', checkType: 'text', ignoreRegex: '', preview: '', enabled: true,
    }]);
    toast.success(t('Selector added'));
  };

  return (
    <div className="flex flex-col h-full min-h-0 bg-surface">
      {/* Header */}
      <div className="px-4 py-3">
        <div className="flex items-center gap-2">
          <p className="text-sm font-medium text-ink">{t('Selectors')}</p>
          <span className="text-[10px] px-2 py-0.5 rounded-full border border-border text-secondary font-medium">{selectors.length}</span>
        </div>
        <p className="text-[11px] text-tertiary mt-0.5">
          {selectionMode === 'manual'
            ? t('Type a CSS selector below to add')
            : t('Click elements in the preview, or Ask AI below')}
        </p>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-2 border-t border-border">
        {selectors.length === 0 ? (
          <div className="text-center py-10">
            <div className="w-10 h-10 rounded-lg bg-canvas border border-border flex items-center justify-center mx-auto mb-3">
              <Squares2X2Icon className="w-5 h-5 text-tertiary" />
            </div>
            <p className="text-xs text-secondary">{t('No selectors yet')}</p>
            <p className="text-[11px] text-tertiary mt-0.5">{t('Click on page elements or use CSS mode')}</p>
          </div>
        ) : (
          selectors.map(sel => sel.checkType === 'visual' && sel.region ? (
            /* Visual zone — show the watched region (mini map + coords),
               highlight it on the live preview while hovered. */
            <div
              key={sel.id}
              onMouseEnter={() => onHoverZone(sel.id)}
              onMouseLeave={() => onHoverZone(hoveredZoneId === sel.id ? null : hoveredZoneId)}
              className={clsx(
                'border rounded-xl p-3 bg-surface space-y-2 group transition-colors',
                hoveredZoneId === sel.id ? "border-ink" : "border-ink/20 hover:border-border-strong",
              )}
            >
              <div className="flex items-start justify-between gap-2">
                {editingSelectorId === sel.id ? (
                  <input
                    type="text" value={sel.name}
                    onChange={(e) => onUpdateSelector(sel.id, { name: e.target.value })}
                    onBlur={() => setEditingSelectorId(null)}
                    onKeyDown={(e) => e.key === 'Enter' && setEditingSelectorId(null)}
                    autoFocus
                    className="text-xs font-medium text-ink bg-canvas border border-border rounded-lg px-2 py-1 w-full outline-none focus:ring-1 focus:ring-ink/10"
                  />
                ) : (
                  <span className="text-xs font-medium text-ink cursor-pointer hover:text-secondary transition-colors flex items-center gap-1" onClick={() => setEditingSelectorId(sel.id)}>
                    <PhotoIcon className="w-3 h-3 text-tertiary" /> {sel.name}
                  </span>
                )}
                <button onClick={() => onRemoveSelector(sel.id)} className="p-0.5 text-tertiary hover:text-danger opacity-0 group-hover:opacity-100 transition-all flex-shrink-0">
                  <TrashIcon className="w-3.5 h-3.5" />
                </button>
              </div>

              {/* Mini map of the region within its OWN capture frame. The zone carries
                  the viewport it was drawn in (1920x1080 from the Rust recorder, 1280x800
                  from the older Python one); dividing by a fixed 1280x800 drew the marker
                  in the wrong place, and off the map entirely for a zone near the edge. */}
              <div className="flex items-center gap-2">
                <div className="relative w-16 h-10 bg-canvas border border-border rounded shrink-0 overflow-hidden">
                  <div
                    className="absolute bg-ink/15 border border-ink rounded-[2px]"
                    style={{
                      left: `${(sel.region.x / (sel.region.viewport?.width || 1280)) * 100}%`,
                      top: `${(sel.region.y / (sel.region.viewport?.height || 800)) * 100}%`,
                      width: `${Math.max(4, (sel.region.width / (sel.region.viewport?.width || 1280)) * 100)}%`,
                      height: `${Math.max(6, (sel.region.height / (sel.region.viewport?.height || 800)) * 100)}%`,
                    }}
                  />
                </div>
                <div className="text-[10px] text-secondary leading-tight">
                  <div className="font-mono">{sel.region.width}×{sel.region.height}px</div>
                  <div className="text-tertiary font-mono">@ {sel.region.x}, {sel.region.y}</div>
                  <div className="text-tertiary">{t('Visual diff')}</div>
                </div>
              </div>
            </div>
          ) : (
            <div key={sel.id} className="border border-ink/20 rounded-xl p-3 bg-surface space-y-2 group hover:border-border-strong transition-colors">
              <div className="flex items-start justify-between gap-2">
                {editingSelectorId === sel.id ? (
                  <input
                    type="text" value={sel.name}
                    onChange={(e) => onUpdateSelector(sel.id, { name: e.target.value })}
                    onBlur={() => setEditingSelectorId(null)}
                    onKeyDown={(e) => e.key === 'Enter' && setEditingSelectorId(null)}
                    autoFocus
                    className="text-xs font-medium text-ink bg-canvas border border-border rounded-lg px-2 py-1 w-full outline-none focus:ring-1 focus:ring-ink/10"
                  />
                ) : (
                  <span className="text-xs font-medium text-ink cursor-pointer hover:text-secondary transition-colors" onClick={() => setEditingSelectorId(sel.id)}>
                    {sel.name}
                  </span>
                )}
                <button onClick={() => onRemoveSelector(sel.id)} className="p-0.5 text-tertiary hover:text-danger opacity-0 group-hover:opacity-100 transition-all flex-shrink-0">
                  <TrashIcon className="w-3.5 h-3.5" />
                </button>
              </div>

              <code className="text-[10px] text-tertiary block truncate font-mono bg-canvas rounded px-1.5 py-1">{sel.selector}</code>

              {sel.preview && <p className="text-[11px] text-tertiary truncate">{sel.preview}</p>}

              <div className="flex gap-0.5 bg-hover/50 rounded-lg p-0.5">
                {CHECK_TYPES.map(ct => (
                  <button key={ct.id} onClick={() => onUpdateSelector(sel.id, { checkType: ct.id as any })} className={clsx(
                    'flex items-center gap-1 px-2 py-1 rounded-md text-[10px] font-medium transition-all flex-1 justify-center',
                    sel.checkType === ct.id ? 'bg-ink text-white shadow-sm' : 'text-tertiary hover:text-secondary',
                  )}>
                    <ct.icon className="w-3 h-3" /> {t(ct.label)}
                  </button>
                ))}
              </div>

              <input
                type="text" value={sel.ignoreRegex}
                onChange={(e) => onUpdateSelector(sel.id, { ignoreRegex: e.target.value })}
                placeholder={t('Ignore regex (optional)')}
                className="w-full text-[10px] px-2.5 py-1.5 border border-border rounded-lg bg-canvas text-secondary font-mono placeholder:text-tertiary outline-none focus:ring-1 focus:ring-ink/10 transition-all"
              />
            </div>
          ))
        )}
      </div>

      {/* Manual CSS selector input */}
      {selectionMode === 'manual' && (
        <div className="p-3 border-t border-border">
          <div className="flex gap-2">
            <input
              type="text" value={manualSelectorInput}
              onChange={(e) => setManualSelectorInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && manualSelectorInput.trim() && (addManual(manualSelectorInput.trim()), setManualSelectorInput(''))}
              placeholder="#id, .class, div > p"
              className="flex-1 text-xs px-3 py-2 border border-border rounded-xl bg-canvas font-mono placeholder:text-tertiary outline-none focus:ring-1 focus:ring-ink/10"
            />
            <button
              onClick={() => { if (manualSelectorInput.trim()) { addManual(manualSelectorInput.trim()); setManualSelectorInput(''); } }}
              disabled={!manualSelectorInput.trim()}
              className={clsx(
                'px-3 py-2 text-xs font-medium rounded-xl transition-colors',
                manualSelectorInput.trim() ? 'bg-accent-strong text-accent-on hover:bg-accent-strong/90' : 'bg-hover text-tertiary cursor-not-allowed',
              )}
            >{t('Add')}</button>
          </div>
        </div>
      )}
    </div>
  );
};
