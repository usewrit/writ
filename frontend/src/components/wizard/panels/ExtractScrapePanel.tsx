import React, { useState } from 'react';
import {
  GlobeAltIcon,
  TrashIcon,
  CodeBracketIcon,
  SparklesIcon,
  PlusIcon,
} from '@heroicons/react/24/outline';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { useWizard, WizardSelector, ExtractorConfig } from '../WizardContext';
import { Button } from '../../ui/Button';
import { Input } from '../../ui/Input';
import { Checkbox } from '../../ui';

const EXTRACT_TYPES = [
  { value: 'text', label: 'Text' },
  { value: 'attribute', label: 'Attr' },
  { value: 'regex', label: 'Regex' },
  { value: 'css', label: 'CSS' },
  { value: 'json_path', label: 'JSON' },
];

// NOTE: "Generate with AI" extraction-rule synthesis is
// not built yet (the handler only showed a "coming soon" toast). Hidden behind
// this flag until the backend AI flow exists; flip to true when implemented.
const AI_SCRIPT_GENERATION_ENABLED = false;

export const ExtractScrapePanel: React.FC = () => {
  const { t } = useTranslation();
  const { state, updateConfig } = useWizard();
  const { url, selectors, extractors } = state.config;

  const [newSelector, setNewSelector] = useState('');

  const addSelectorAndExtractor = (selector: string, preview: string) => {
    const trimmed = selector.trim();
    if (!trimmed) return;
    if (selectors.some(s => s.selector === trimmed)) { toast.error(t('Already added')); return; }
    const selectorId = `sel_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
    const newSel: WizardSelector = { id: selectorId, name: t('Element {{n}}', { n: selectors.length + 1 }), selector: trimmed, description: preview, checkType: 'text', ignoreRegex: '', preview, enabled: true };
    const newExt: ExtractorConfig = { id: `ext_${Date.now()}`, selectorId, name: `extract_${selectors.length + 1}`, extractType: 'text', expression: '', outputName: `field_${selectors.length + 1}`, isArray: false, defaultValue: '' };
    updateConfig({ selectors: [...selectors, newSel], extractors: [...extractors, newExt] });
    toast.success(t('Element added'));
  };

  const addManualSelector = () => {
    if (!newSelector.trim()) { toast.error(t('Enter a CSS selector')); return; }
    addSelectorAndExtractor(newSelector, '');
    setNewSelector('');
  };

  const removeExtractor = (id: string) => {
    const ext = extractors.find(e => e.id === id);
    updateConfig({ extractors: extractors.filter(e => e.id !== id), selectors: ext ? selectors.filter(s => s.id !== ext.selectorId) : selectors });
  };

  const updateExtractor = (id: string, updates: Partial<ExtractorConfig>) => {
    updateConfig({ extractors: extractors.map(e => e.id === id ? { ...e, ...updates } : e) });
  };

  return (
    <div className="flex flex-col h-full">
      <div className="px-6 py-3 border-b border-border flex items-center gap-3">
        <GlobeAltIcon className="w-5 h-5 text-tertiary flex-shrink-0" />
        <Input value={url} onChange={(e) => updateConfig({ url: e.target.value })} placeholder="https://example.com/page-to-scrape" className="flex-1" />
      </div>

      <div className="flex-1 flex overflow-hidden min-h-0">
        <div className="flex-1 bg-surface relative flex flex-col">
          <div className="px-6 py-4 border-b border-border">
            <p className="text-sm font-medium text-ink mb-1">{t('Add extraction targets')}</p>
            <p className="text-xs text-secondary mb-3">{t('Enter a CSS selector for each element you want to extract. The agent resolves it against the live page at run time.')}</p>
            <div className="flex items-center gap-2">
              <Input
                value={newSelector}
                onChange={(e) => setNewSelector(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addManualSelector(); } }}
                placeholder=".product-title, #price, h1"
                className="flex-1 font-mono text-sm"
              />
              <Button variant="primary" size="sm" onClick={addManualSelector} disabled={!newSelector.trim()}>
                <PlusIcon className="w-4 h-4" /> {t('Add')}
              </Button>
            </div>
          </div>
          <div className="flex-1 flex items-center justify-center p-6">
            <div className="text-center max-w-sm">
              <CodeBracketIcon className="w-12 h-12 text-tertiary mx-auto mb-3" />
              <p className="text-sm text-secondary">{t('Extraction runs against the live page through the agent. Add the selectors you want on the right and refine the rules per field.')}</p>
            </div>
          </div>
        </div>

        <div className="w-96 border-l border-border flex flex-col bg-surface">
          <div className="px-4 py-3 border-b border-border">
            <p className="text-sm font-medium text-ink">{t('Extraction Rules ({{n}})', { n: extractors.length })}</p>
          </div>
          <div className="flex-1 overflow-y-auto p-3 space-y-2">
            {extractors.length === 0 ? (
              <div className="text-center py-8">
                <CodeBracketIcon className="w-8 h-8 text-tertiary mx-auto mb-2" />
                <p className="text-xs text-tertiary">{t('Add a CSS selector to create an extraction rule')}</p>
              </div>
            ) : extractors.map(ext => {
              const sel = selectors.find(s => s.id === ext.selectorId);
              return (
                <div key={ext.id} className="border border-ink/20 rounded-lg p-3 space-y-2 bg-surface shadow-sm">
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <input type="text" value={ext.outputName} onChange={(e) => updateExtractor(ext.id, { outputName: e.target.value })} className="text-sm font-medium text-ink bg-transparent border-none p-0 focus:ring-0 w-full" placeholder={t('Output name')} />
                      {sel && <code className="text-[10px] text-tertiary block truncate font-mono mt-0.5">{sel.selector}</code>}
                    </div>
                    <div className="flex gap-1 flex-shrink-0">
                      <button onClick={() => removeExtractor(ext.id)} className="p-1 text-tertiary hover:text-danger rounded-lg"><TrashIcon className="w-3.5 h-3.5" /></button>
                    </div>
                  </div>
                  <div className="flex gap-1">
                    {EXTRACT_TYPES.map(et => (
                      <button key={et.value} onClick={() => updateExtractor(ext.id, { extractType: et.value as any })} className={clsx(
                        'px-1.5 py-0.5 rounded text-[10px] font-medium transition-colors',
                        ext.extractType === et.value ? 'bg-ink text-white' : 'bg-hover text-tertiary hover:text-secondary',
                      )}>{t(et.label)}</button>
                    ))}
                  </div>
                  {(ext.extractType === 'attribute' || ext.extractType === 'regex') && (
                    <input type="text" value={ext.expression} onChange={(e) => updateExtractor(ext.id, { expression: e.target.value })} placeholder={ext.extractType === 'attribute' ? t('Attribute (e.g., href)') : t('Regex pattern')} className="w-full text-[11px] px-2 py-1 border border-border rounded bg-hover font-mono text-secondary placeholder:text-tertiary" />
                  )}
                  <Checkbox
                    checked={ext.isArray}
                    onChange={(e) => updateExtractor(ext.id, { isArray: e.target.checked })}
                    size="sm"
                    label={t('Extract all matches (array)')}
                  />
                </div>
              );
            })}
          </div>
          <div className="p-3 border-t border-border space-y-2">
            <Input value={state.config.name} onChange={(e) => updateConfig({ name: e.target.value })} placeholder={t('Extraction name...')} />
            {/* Hidden until AI extraction-rule synthesis is built — see AI_SCRIPT_GENERATION_ENABLED above. */}
            {AI_SCRIPT_GENERATION_ENABLED && (
              <Button variant="secondary" size="sm" className="w-full" onClick={() => toast(t('AI script generation coming soon'))}>
                <SparklesIcon className="w-4 h-4" /> {t('Generate with AI')}
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
