import React, { useState } from 'react';
import {
  GlobeAltIcon,
  CursorArrowRaysIcon,
  Squares2X2Icon,
  CodeBracketIcon,
  PhotoIcon,
} from '@heroicons/react/24/outline';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import client from '../../../api/client';
import { useWizard, WizardSelector } from '../WizardContext';
import { BrowserRecorder, type RecorderApiHandle } from '../../BrowserRecorder';
import { MonitorTargetsPanel, type MonitorSelectionMode } from '../MonitorTargetsPanel';

// The action switcher — how a canvas gesture is interpreted while picking check targets.
const MODE_OPTIONS: Array<{ mode: MonitorSelectionMode; icon: React.FC<any>; label: string }> = [
  { mode: 'browse', icon: GlobeAltIcon, label: 'Browse' },
  { mode: 'click', icon: CursorArrowRaysIcon, label: 'Click' },
  { mode: 'area', icon: Squares2X2Icon, label: 'Area' },
  { mode: 'zone', icon: PhotoIcon, label: 'Zone' },
  { mode: 'manual', icon: CodeBracketIcon, label: 'CSS' },
];

// Only Click/Area/Zone drive canvas picking; Browse/CSS/AI use the page, a typed
// selector, or a text prompt instead, so the recorder's canvas selection stays off.
const canvasSelectionMode = (m: MonitorSelectionMode): 'click' | 'area' | 'zone' | null =>
  m === 'click' || m === 'area' || m === 'zone' ? m : null;

const newSelectorId = () => `sel_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;

/**
 * ContentMonitorPanel — a thin bridge that hosts the SAME full-bleed BrowserRecorder as
 * the workflow recorder, with the check-target picker (MonitorTargetsPanel) folded into
 * the recorder's own "Targets" spine tab. All monitor state (selectors, selection mode)
 * lives here and syncs to the wizard config; interval / JS / region moved to Finalize.
 * The onboarding sandbox keeps a fake-storefront layout (no live browser) so the tour runs.
 */
export const ContentMonitorPanel: React.FC = () => {
  const { t } = useTranslation();
  const { state, updateConfig } = useWizard();
  const { url, selectors, requiresPlaywright } = state.config;

  // Start in Browse so the user can navigate / log in to reach the page they want to watch (those
  // clicks interact with the page and are recorded as setup steps); switching to Click/Area/Zone
  // turns canvas clicks into selector/region picks instead.
  const [selectionMode, setSelectionMode] = useState<MonitorSelectionMode>('browse');
  // The zone whose region is highlighted on the live preview (sidebar hover).
  const [hoveredZoneId, setHoveredZoneId] = useState<string | null>(null);

  const previewRef = React.useRef<RecorderApiHandle>(null);

  // A visual zone is a screenshot diff, so it needs the JS/browser path; if JS is turned OFF while
  // in zone mode, fall back to element picking. (We no longer FORCE zone-only when JS is on — all
  // selection modes stay freely available, and picking Zone enables JS itself.)
  // Reconciled DURING RENDER off the JS toggle's own transition, so the canvas is
  // never armed for zone picking on a frame where JS is already off.
  const [jsWas, setJsWas] = React.useState(requiresPlaywright);
  if (jsWas !== requiresPlaywright) {
    setJsWas(requiresPlaywright);
    if (!requiresPlaywright && selectionMode === 'zone') setSelectionMode('click');
  }

  const handleSelectionModeChange = (m: MonitorSelectionMode) => {
    setSelectionMode(m);
    // A visual zone is a screenshot diff — it requires the JS/browser check path.
    if (m === 'zone' && !requiresPlaywright) updateConfig({ requiresPlaywright: true });
  };

  const addSelectors = (sels: WizardSelector[]) => updateConfig({ selectors: [...selectors, ...sels] });
  const removeSelector = (id: string) => updateConfig({ selectors: selectors.filter(s => s.id !== id) });
  const updateSelector = (id: string, updates: Partial<WizardSelector>) => {
    updateConfig({ selectors: selectors.map(s => s.id === id ? { ...s, ...updates } : s) });
  };

  // Live preview: a picked element → its CSS selector becomes a tracked selector.
  const handleElementClick = (info: { selector?: string; text?: string; tag?: string; matchCount?: number }) => {
    if (!info.selector) return;
    if (selectors.some(s => s.selector === info.selector)) {
      toast.error(t('Selector already added'));
      return;
    }
    addSelectors([{
      id: newSelectorId(),
      name: info.text?.slice(0, 40) || info.tag || t('Selector {{n}}', { n: selectors.length + 1 }),
      selector: info.selector,
      description: info.text?.slice(0, 100) || '',
      checkType: 'text',
      ignoreRegex: '',
      preview: info.text?.slice(0, 100) || '',
      enabled: true,
    }]);
    if (info.matchCount && info.matchCount > 1) {
      toast(t('Selector added — matches {{n}} elements, all are checked together', { n: info.matchCount }), { icon: '⚠️' });
    } else {
      toast.success(t('Selector added'));
    }
  };

  // Area mode: the live page returned every element inside the dragged rect —
  // add each one that isn't already tracked.
  const handleElementsFound = (infos: Array<{ selector?: string; text?: string; tag?: string }>) => {
    const fresh = infos.filter(i => i.selector && !selectors.some(s => s.selector === i.selector));
    if (fresh.length === 0) {
      toast.error(t('No new elements in that area'));
      return;
    }
    const newSels: WizardSelector[] = fresh.map((info, i) => ({
      id: `sel_${Date.now()}_${i}_${Math.random().toString(36).slice(2, 6)}`,
      name: info.text?.slice(0, 40) || info.tag || t('Selector {{n}}', { n: selectors.length + i + 1 }),
      selector: info.selector!,
      description: info.text?.slice(0, 100) || '',
      checkType: 'text',
      ignoreRegex: '',
      preview: info.text?.slice(0, 100) || '',
      enabled: true,
    }));
    addSelectors(newSels);
    toast.success(newSels.length === 1 ? t('{{n}} selector added from area', { n: newSels.length }) : t('{{n}} selectors added from area', { n: newSels.length }));
  };

  // Visual zone — screenshot comparison. Each zone needs a UNIQUE sentinel selector
  // ("viewport-zone-XXXX") so multiple zones on the same page don't collide on the
  // per-target duplicate-selector check.
  const handleZoneDrawn = (region: { x: number; y: number; width: number; height: number; scroll_x?: number; scroll_y?: number }) => {
    const zoneSel: WizardSelector = {
      id: newSelectorId(),
      name: t('Zone {{n}}', { n: selectors.filter(s => s.checkType === 'visual').length + 1 }),
      selector: `viewport-zone-${Math.random().toString(36).slice(2, 8)}`,
      description: t('Visual zone {{w}}x{{h}} at ({{x}}, {{y}})', { w: region.width, h: region.height, x: region.x, y: region.y }),
      checkType: 'visual',
      ignoreRegex: '',
      preview: '',
      enabled: true,
      region,
    };
    updateConfig({ selectors: [...selectors, zoneSel], requiresPlaywright: true });
    toast.success(t('Visual zone added'));
  };

  // AI selector-find — invoked from the recorder's own bottom "Ask AI" dock (not a
  // separate panel). Takes the user's natural-language description, finds matching
  // selectors on the live page, adds the valid ones, and returns a summary for the
  // dock transcript. Always resolves to a string (never throws) so the dock can show it.
  const handleMonitorAiFind = async (prompt: string): Promise<string> => {
    const q = prompt.trim();
    if (!q) return t('Tell me what to watch.');
    if (!url) return t('Enter a URL to watch first.');
    try {
      // Capture the current frame + DOM from the live recorder for grounding.
      const screenshot_b64 = previewRef.current?.getScreenshot() || '';
      const dom = (await previewRef.current?.getDOM()) || '';
      const resp = await client.post('/ai-assist/find-selectors', {
        url,
        prompt: q,
        screenshot_b64,
        page_dom: dom ? dom.slice(0, 50000) : '',
        existing_selectors: selectors.map(s => s.selector),
      });
      let found: Array<{ selector: string; name: string; description: string }> = resp.data?.selectors || [];

      // Live validation: drop selectors that match zero elements on the real page.
      let droppedInvalid = 0;
      try {
        const validated: typeof found = [];
        for (const f of found) {
          let count: any = null;
          try { count = await previewRef.current?.evaluate(`document.querySelectorAll(${JSON.stringify(f.selector)}).length`); } catch { count = null; }
          if (count === 0) droppedInvalid++; else validated.push(f);
        }
        found = validated;
      } catch { /* validation unavailable — keep AI results as-is */ }

      if (found.length === 0) {
        const extra = droppedInvalid > 0
          ? (droppedInvalid === 1 ? t(' ({{n}} suggestion didn\'t match the page)', { n: droppedInvalid }) : t(' ({{n}} suggestions didn\'t match the page)', { n: droppedInvalid }))
          : '';
        return t('No matching elements found{{extra}}. Try describing what you want to watch differently.', { extra });
      }

      const newSels: WizardSelector[] = found
        .filter(f => !selectors.some(s => s.selector === f.selector))
        .map(f => ({
          id: newSelectorId(),
          name: f.name,
          selector: f.selector,
          description: f.description,
          checkType: 'text' as const,
          ignoreRegex: '',
          preview: f.description,
          enabled: true,
        }));
      if (newSels.length === 0) return t('All suggested selectors already exist.');
      addSelectors(newSels);
      const names = newSels.map(s => s.name).join(', ');
      toast.success(t('{{n}} selectors added by AI', { n: newSels.length }));
      return newSels.length === 1
        ? t('Added {{n}} selector: {{names}}', { n: newSels.length, names })
        : t('Added {{n}} selectors: {{names}}', { n: newSels.length, names });
    } catch (err: any) {
      return err?.response?.data?.detail || t('AI request failed. Make sure the page is accessible.');
    }
  };

  const targetsPanel = (
    <MonitorTargetsPanel
      selectors={selectors}
      selectionMode={selectionMode}
      onAddSelectors={addSelectors}
      onRemoveSelector={removeSelector}
      onUpdateSelector={updateSelector}
      hoveredZoneId={hoveredZoneId}
      onHoverZone={setHoveredZoneId}
    />
  );

  // The floating action switcher (Browse/Click/Area/Zone/CSS), rendered by the recorder
  // as a glass toolbar just under the URL bar at the top of the stage.
  const actionToolbar = (
    <div className="flex items-center gap-1 rounded-xl border border-border bg-surface/85 backdrop-blur-xl shadow-lg px-1.5 py-1">
      {MODE_OPTIONS.map(m => (
        <button
          key={m.mode}
          onClick={() => handleSelectionModeChange(m.mode)}
          className={clsx(
            'flex items-center gap-1 px-2 py-1.5 rounded-lg text-[11px] font-medium transition-all',
            selectionMode === m.mode ? 'bg-ink text-white shadow-sm' : 'text-secondary hover:text-ink hover:bg-chrome',
          )}
        >
          <m.icon className="w-3.5 h-3.5" /> {t(m.label)}
        </button>
      ))}
    </div>
  );

  // ── Real path — the SAME full-bleed BrowserRecorder as the workflow recorder, with
  //    the check-target picker folded into its Targets spine tab. ──────────────────
  return (
    <div className="h-full flex flex-col">
      <div data-tour="monitor-preview" className="flex-1 min-h-0">
        <BrowserRecorder
          isOpen
          embedded
          initialUrl={url}
          autoConnect={!!url}
          onClose={() => {}}
          onSave={() => {}}
          apiRef={previewRef}
          monitorMode
          monitorTargetCount={selectors.length}
          monitorPanel={targetsPanel}
          monitorToolbar={actionToolbar}
          onMonitorAiFind={handleMonitorAiFind}
          // Capture any setup steps (login / navigation) the user records on the live
          // preview so the checker replays them before each check. Stored separately from
          // `recordedSteps` (workflow modes) to avoid cross-talk.
          onStepsChange={(steps) => updateConfig({ monitorSetupSteps: steps as any[] })}
          onCredentialsChange={(creds) => updateConfig({ credentials: creds })}
          onUrlChange={(u) => updateConfig({ url: u })}
          selectionMode={canvasSelectionMode(selectionMode)}
          selectionZones={selectors.filter(s => s.checkType === 'visual' && s.region).map(s => ({ id: s.id, region: s.region! }))}
          highlightZoneId={hoveredZoneId}
          onElementClick={handleElementClick}
          onElementsFound={handleElementsFound}
          onZoneDrawn={handleZoneDrawn}
        />
      </div>
    </div>
  );
};
