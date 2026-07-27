import React, { useRef, useState, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  EyeIcon,
  CursorArrowRaysIcon,
  SparklesIcon,
  PhotoIcon,
  SignalIcon,
  ComputerDesktopIcon,
  CloudIcon,
  GlobeAltIcon,
  CheckIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { useWizard, WizardMode } from '../WizardContext';
import { IntentPicker } from './IntentPicker';
import { StageBackdrop } from '../shared/StageBackdrop';
import { triggerMini } from '../../../onboarding/miniTrigger';
import { useQuery } from '../../../hooks/useQuery';
import { Q } from '../../../stores/queryKeys';
import { userRecorderApi } from '../../../api/endpoints';
import { useRecorderCapability } from '../../../hooks/useRecorderCapability';
import { Select } from '../../ui';
import i18n from '../../../i18n';

const hasProtocol = (url: string) => /^https?:\/\//i.test(url);
const looksLikeDomain = (url: string) => /^[a-zA-Z0-9]([a-zA-Z0-9-]*\.)+[a-zA-Z]{2,}/.test(url);

export function UrlInput({ value, onChange }: { value: string; onChange: (url: string) => void }) {
  const [focused, setFocused] = useState(false);
  const [hovered, setHovered] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const showPrefix = hasProtocol(value);
  const showGhostPrefix = !showPrefix && !focused && !value && hovered;
  const rawValue = showPrefix ? value.replace(/^https?:\/\//i, '') : value;

  const handleChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const v = e.target.value;
    if (showPrefix) {
      onChange(value.match(/^https?:\/\//i)![0] + v);
    } else if (hasProtocol(v)) {
      onChange(v);
    } else {
      onChange(v);
    }
  }, [showPrefix, value, onChange]);

  const handleBlur = useCallback(() => {
    setFocused(false);
    const trimmed = value.trim();
    if (trimmed && !hasProtocol(trimmed) && looksLikeDomain(trimmed)) {
      onChange('https://' + trimmed);
    }
  }, [value, onChange]);

  const handleFocus = useCallback(() => {
    setFocused(true);
  }, []);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Backspace' && showPrefix && rawValue === '') {
      e.preventDefault();
      onChange('');
    }
  }, [showPrefix, rawValue, onChange]);

  return (
    <div
      data-tour="wizard-url"
      className={clsx(
        'flex items-center w-full border rounded-lg bg-surface transition-colors',
        focused ? 'border-ink/30' : 'border-border hover:border-ink/20'
      )}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {showPrefix && (
        <span className="pl-3 text-sm text-tertiary select-none shrink-0">
          {value.match(/^https?:\/\//i)![0]}
        </span>
      )}
      {showGhostPrefix && (
        <span className="pl-3 text-sm text-tertiary/40 select-none shrink-0 animate-fade-in">
          https://
        </span>
      )}
      <input
        ref={inputRef}
        type="text"
        value={rawValue}
        onChange={handleChange}
        onBlur={handleBlur}
        onFocus={handleFocus}
        onKeyDown={handleKeyDown}
        placeholder={showPrefix ? i18n.t('example.com/page') : (showGhostPrefix ? i18n.t('example.com/page') : i18n.t('example.com/page'))}
        className={clsx(
          'flex-1 py-2.5 text-sm bg-transparent text-ink placeholder:text-tertiary focus:outline-none',
          showPrefix || showGhostPrefix ? 'pl-0' : 'pl-4',
          'pr-4'
        )}
      />
    </div>
  );
}

const MODES: Array<{
  id: WizardMode;
  title: string;
  subtitle: string;
  detail: string;
  icon: React.FC<any>;
  badge?: string;
}> = [
  {
    id: 'content_monitor',
    title: 'Content Monitor',
    subtitle: 'Monitor a page for changes',
    detail: 'Pick any URL and CSS selectors to watch. Get notified via push, email, or webhook when content changes — checks run as fast as every 10 seconds.',
    icon: EyeIcon,
  },
  {
    id: 'manual_workflow',
    title: 'Manual Recording',
    subtitle: 'Record browser actions step by step',
    detail: 'We open a live browser — you click, type, and navigate. Every action is recorded as a replayable workflow you can run on demand, on a schedule, or via API.',
    icon: CursorArrowRaysIcon,
  },
  {
    id: 'streaming_workflow',
    title: 'Streaming Session',
    subtitle: 'Persistent browser with callable handlers',
    detail: 'Record login steps, then define callable handlers (step groups or JS scripts). The browser stays alive and your handlers become API endpoints — SSE, WebSocket, or OpenAI-compatible.',
    icon: SignalIcon,
  },
  {
    id: 'site_crawl',
    title: 'Site Crawl',
    subtitle: 'Crawl a whole site into one dataset',
    detail: 'Point Dragnet at a site and it maps every page, then fans out across the agent fleet — many agents each crawl a slice. Collect clean markdown or structured records into one deduped, queryable dataset.',
    icon: GlobeAltIcon,
  },
];

// Sub-modes for content_monitor
const CHECK_SUB_MODES = [
  { id: 'elements', title: 'Selectors', desc: 'Click elements or CSS', icon: CursorArrowRaysIcon, badge: '1x', hint: 'HTTP check' },
  { id: 'visual', title: 'Visual Zone', desc: 'Screenshot region', icon: PhotoIcon, badge: '5x', hint: 'JS browser' },
];

interface ModeSelectionStepProps {
  allowedModes?: string[];
  preSelectedMode?: string;
}

export const ModeSelectionStep: React.FC<ModeSelectionStepProps> = ({ allowedModes, preSelectedMode }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { state, setMode, updateConfig, goToStep, dispatch } = useWizard();
  const filteredModes = allowedModes ? MODES.filter(m => allowedModes.includes(m.id)) : MODES;
  // Launched from a dedicated single-purpose surface (e.g. /crawls/new): the mode
  // is fixed, so there's nothing to "Change" back to — hide that affordance.
  const lockedMode = !!allowedModes && allowedModes.length === 1;

  // Picking a mode is the user's first real use of that feature — fire its
  // contextual mini-tutorial (suppressed if the user already did the full "?"
  // walkthrough).
  const handlePick = (id: WizardMode) => {
    setMode(id);
    triggerMini(id);
    // Site crawl is a single-screen flow: the URL lives on the crawl panel
    // itself, so there is no Setup form to stop at — jump straight to Configure.
    if (id === 'site_crawl') {
      dispatch({ type: 'COMPLETE_STEP', step: 'mode' });
      goToStep('configure');
    }
  };
  const hideGenericCards = !!preSelectedMode;
  // A recorded workflow — one-shot (manual) or live session (streaming). These two
  // are the SAME build method ("Record the steps"); the run-style switch below flips
  // between them, so we skip the generic mode banner and show the switch instead.
  const isRecordWorkflow = state.mode === 'manual_workflow' || state.mode === 'streaming_workflow';

  // Fetch connected agents for execution target picker
  const { data: agentData } = useQuery(Q.userAgents(), () => userRecorderApi.getAgents());
  const agents = agentData?.agents || [];
  const onlineAgents = agents.filter((a: any) => a.status === 'online');
  // Always offer the execution-target picker for recorder-backed modes, even with
  // no agent connected — the user can pick Auto/cloud or go connect a local agent.
  // A site crawl runs distributed across the whole agent fleet (never a single
  // pinned agent), so it has no execution-target picker.
  const showAgentPicker = !!state.mode && state.mode !== 'content_monitor' && state.mode !== 'site_crawl';
  // The currently chosen agent + whether it brings its OWN AI keys (BYO) → AI is
  // free on that agent (it runs on the user's provider, not the platform's).
  const selectedAgent = onlineAgents.find((a: any) => a.agent_id === state.config.executionTarget);
  const selectedAgentByoAi = !!selectedAgent?.ai_provider;
  // A specific-agent pin is active only when executionTarget names an ONLINE
  // agent; anything else ('auto'/'cloud'/'local'/empty) reads as "Auto". The
  // dropdown value is empty in the Auto case so it shows its placeholder.
  const pinnedAgentId = selectedAgent ? (state.config.executionTarget as string) : '';
  const isAutoTarget = !pinnedAgentId;

  // Plan-aware "where will this run" hint, shown for recorder-backed modes so the
  // user knows up front whether it runs in the cloud or needs a local agent.
  const { capability } = useRecorderCapability(!!state.mode && state.mode !== 'content_monitor' && state.mode !== 'site_crawl');
  const runHint = (() => {
    if (!capability || !state.mode || state.mode === 'content_monitor' || state.mode === 'site_crawl') return null;
    if (onlineAgents.length > 0) return null; // picker already shows the choice
    if (capability.cloud_recording_unlimited) {
      return t('Runs automatically on a cloud agent — nothing to install.');
    }
    const remaining = capability.cloud_quota_remaining ?? 0;
    if (capability.cloud_recording_allowed && remaining > 0) {
      return t('Runs in the cloud — {{n}} free recordings left this month. After that, connect a local agent to keep recording for free.', { n: remaining });
    }
    return t('Recording runs on a connected agent. Connect a local agent to start — we’ll guide you when you continue.');
  })();

  // Selected mode info (rendered when mode is picked). Skipped for record
  // workflows — the run-style switch in the form carries the explanation instead.
  const selectedModeInfo = !hideGenericCards && state.mode && !isRecordWorkflow ? (() => {
    const selected = filteredModes.find(m => m.id === state.mode);
    if (!selected) return null;
    const Icon = selected.icon;
    return (
      <div className="max-w-xl mx-auto px-6 w-full pt-6 pb-2 shrink-0">
        <div className="border border-border rounded-xl px-5 py-4 bg-canvas">
          <div className="flex items-start gap-4">
            <div className="w-9 h-9 rounded-lg bg-surface border border-border flex items-center justify-center shrink-0 mt-0.5">
              <Icon className="w-[18px] h-[18px] text-ink" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center justify-between mb-1">
                <div className="flex items-center gap-2 min-w-0">
                  <span className="text-[15px] font-semibold text-ink">{t(selected.title)}</span>
                  {selected.badge && (
                    <span className="text-[10px] font-medium text-secondary border border-border px-1.5 py-0.5 rounded-full shrink-0">{t(selected.badge)}</span>
                  )}
                </div>
                {!lockedMode && (
                  <button
                    onClick={() => setMode(null as any)}
                    className="text-[12px] text-tertiary hover:text-ink shrink-0"
                  >
                    {t('Change')}
                  </button>
                )}
              </div>
              <p className="text-[13px] text-secondary leading-relaxed">{t(selected.detail)}</p>
            </div>
          </div>
        </div>
      </div>
    );
  })() : null;

  return (
    <StageBackdrop className="flex flex-col">
      {/* No mode: intent-first picker ("what do you want to do?") rendered as a
          floating card on the dot-grid stage. */}
      {!state.mode && !hideGenericCards && (
        <IntentPicker allowedModes={allowedModes} onPickMode={handlePick} />
      )}

      {/* Mode selected: floating mode banner + a floating glass form card. Name
          lives in the app-bar now, so the form carries only the mode-specific
          inputs (check method, URL, run-on). */}
      {state.mode && (
        <>
          {selectedModeInfo}
          <div className="flex-1 flex items-start sm:items-center justify-center px-4 sm:px-6 pb-8">
          <div className={clsx(hideGenericCards ? '' : 'animate-wizard-slide-up', 'w-full max-w-md rounded-2xl border border-ink/20 bg-surface/90 backdrop-blur-sm shadow-sm px-6 py-6')}>
            <div className="w-full space-y-6">
              {/* Title when pre-selected */}
              {hideGenericCards && (
                <div className="text-center mb-2">
                  <h2 className="text-lg font-semibold text-ink">{t('New Monitor')}</h2>
                  <p className="text-sm text-secondary mt-0.5">{t('Set up what to monitor')}</p>
                </div>
              )}
              {/* Content check sub-mode picker */}
              {state.mode === 'content_monitor' && (
                <div className="animate-wizard-field-in" style={{ animationDelay: '0ms', animationFillMode: 'both' }}>
                  <label className="block text-xs font-medium text-secondary mb-2">{t('Check method')}</label>
                  <div data-tour="wizard-check-method" className="grid grid-cols-2 gap-2">
                    {CHECK_SUB_MODES.map(sub => {
                      const Icon = sub.icon;
                      const isActive = sub.id === 'visual'
                        ? state.config.requiresPlaywright
                        : !state.config.requiresPlaywright;
                      return (
                        <button key={sub.id} type="button"
                          onClick={() => updateConfig({ requiresPlaywright: sub.id === 'visual' })}
                          className={clsx(
                            'flex items-center gap-3 p-3 rounded-lg border text-left transition-all',
                            isActive
                              ? 'border-ink bg-ink/[0.03] ring-1 ring-ink/10'
                              : 'border-border hover:border-ink/20',
                          )}
                        >
                          <div className={clsx(
                            'w-8 h-8 rounded-lg flex items-center justify-center shrink-0',
                            isActive ? 'bg-ink text-white' : 'bg-hover text-secondary',
                          )}>
                            <Icon className="w-4 h-4" />
                          </div>
                          <div className="flex-1 flex items-center justify-between">
                            <div>
                              <div className="text-sm font-medium text-ink">{t(sub.title)}</div>
                              <div className="text-[10px] text-tertiary">{t(sub.desc)} · {t(sub.hint)}</div>
                            </div>
                            <span className="text-[10px] font-medium text-secondary border border-border px-1.5 py-0.5 rounded-full shrink-0">{t(sub.badge)}</span>
                          </div>
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}

              {/* Run style — the short-vs-long-running switch. One-shot = a
                  recorded workflow (run → result → stop); Live session = a
                  streaming workflow (browser stays open, callable over time).
                  Flipping it swaps the underlying mode; the recorder adapts. */}
              {isRecordWorkflow && (
                <div className="animate-wizard-field-in" style={{ animationDelay: '40ms', animationFillMode: 'both' }}>
                  <div className="flex items-center justify-between mb-2">
                    <label className="block text-xs font-medium text-secondary">{t('Run style')}</label>
                    {!lockedMode && (
                      <button
                        type="button"
                        onClick={() => setMode(null as any)}
                        className="text-[12px] text-tertiary hover:text-ink"
                      >
                        {t('Change')}
                      </button>
                    )}
                  </div>
                  <div className="flex gap-1 bg-hover/50 rounded-xl p-0.5">
                    {([
                      ['manual_workflow', t('One-shot')],
                      ['streaming_workflow', t('Live session')],
                    ] as const).map(([m, label]) => {
                      const active = state.mode === m;
                      return (
                        <button
                          key={m}
                          type="button"
                          onClick={() => setMode(m as WizardMode)}
                          className={clsx(
                            'flex-1 px-3 py-1.5 rounded-[22px] text-xs font-medium transition-all text-center',
                            active ? 'bg-ink text-white' : 'text-secondary hover:text-ink',
                          )}
                        >
                          {label}
                        </button>
                      );
                    })}
                  </div>
                  <p className="text-[11px] text-tertiary mt-2 leading-relaxed">
                    {state.mode === 'streaming_workflow'
                      ? t('Keep the browser open and call actions on it over time — a logged-in session you drive repeatedly (SSE, WebSocket, or OpenAI-compatible). Pick this for a chat-like or multi-step live integration.')
                      : t('Run it, get a result, then stop. Best for a scrape, a form submit, or a data pull — this is what most workflows need.')}
                  </p>
                </div>
              )}

              {/* Name lives in the StudioAppBar now (single inline-editable name);
                  the Setup form carries only the mode-specific inputs below. */}
              <div className="animate-wizard-field-in" style={{ animationDelay: '120ms', animationFillMode: 'both' }}>
                <label className="block text-sm font-medium text-ink mb-1.5">
                  {state.mode === 'content_monitor'
                    ? t('URL to monitor')
                    : state.mode === 'site_crawl'
                    ? t('Site to crawl')
                    : t('Entry URL')}
                </label>
                <UrlInput
                  value={state.config.url}
                  onChange={(url) => updateConfig({ url })}
                />
                <p className="text-xs text-tertiary mt-1.5">
                  {state.mode === 'content_monitor'
                    ? t('The page we will check for changes.')
                    : state.mode === 'ai_workflow'
                    ? t('The starting page for the AI to navigate from.')
                    : state.mode === 'site_crawl'
                    ? t('Dragnet starts here, maps the site, and follows links from this page.')
                    : t('The page where the recording will start.')}
                </p>
              </div>

              {/* Plan-aware run-location hint (shown when no local agent picker) */}
              {runHint && (
                <div className="animate-wizard-field-in flex items-start gap-2.5 px-3.5 py-2.5 rounded-lg border border-border bg-hover/40" style={{ animationDelay: '160ms', animationFillMode: 'both' }}>
                  {capability?.cloud_recording_unlimited
                    ? <CloudIcon className="w-4 h-4 text-secondary shrink-0 mt-0.5" />
                    : <ComputerDesktopIcon className="w-4 h-4 text-secondary shrink-0 mt-0.5" />}
                  <p className="text-xs text-secondary leading-relaxed">{runHint}</p>
                </div>
              )}

              {/* Agent / Execution target picker */}
              {showAgentPicker && (
                <div className="animate-wizard-field-in" style={{ animationDelay: '180ms', animationFillMode: 'both' }}>
                  <label className="block text-sm font-medium text-ink mb-1.5">{t('Run on')}</label>
                  {/* Auto tile + a dropdown to pin a specific agent. A dropdown
                      (not a tile per agent) keeps the card compact no matter how
                      large the fleet is. */}
                  <div className="space-y-2">
                    <button
                      type="button"
                      onClick={() => updateConfig({ executionTarget: 'auto' })}
                      className={clsx(
                        'w-full flex items-center gap-3 p-3 rounded-lg border text-left transition-all',
                        isAutoTarget
                          ? 'border-ink bg-ink/[0.03] ring-1 ring-ink/10'
                          : 'border-border hover:border-ink/20',
                      )}
                    >
                      <CloudIcon className={clsx('w-5 h-5 shrink-0', isAutoTarget ? 'text-ink' : 'text-tertiary')} />
                      <div className="flex-1 min-w-0">
                        <div className="text-sm font-medium text-ink">{t('Auto')}</div>
                        <div className="text-[10px] text-tertiary">{t('Best available agent')}</div>
                      </div>
                      {isAutoTarget && <CheckIcon className="w-4 h-4 text-ink shrink-0" />}
                    </button>

                    {onlineAgents.length > 0 && (
                      <Select<string>
                        value={pinnedAgentId}
                        onChange={(v) => updateConfig({ executionTarget: v || 'auto' })}
                        placeholder={t('Or pin to a specific agent…')}
                        options={onlineAgents.map((agent: any) => ({
                          value: agent.agent_id,
                          label: agent.name || agent.agent_id,
                          description: agent.ai_provider ? t('AI: your keys · free') : (agent.platform || t('Local')),
                          icon: <ComputerDesktopIcon className="w-4 h-4" />,
                        }))}
                      />
                    )}
                  </div>

                  {/* No local agent connected — let the user go set one up. */}
                  {onlineAgents.length === 0 && (
                    <button
                      type="button"
                      onClick={() => navigate('/fleet')}
                      className="mt-2 inline-flex items-center gap-1.5 text-xs font-medium text-ink hover:underline"
                    >
                      <ComputerDesktopIcon className="w-3.5 h-3.5" />
                      {t('Connect a local agent to run on your own machine')}
                    </button>
                  )}

                  {/* BYO AI: the chosen agent runs AI on its OWN keys → free. */}
                  {selectedAgentByoAi && (state.mode === 'ai_workflow' || state.mode === 'api_workflow') && (
                    <p className="text-xs text-ink mt-2 inline-flex items-center gap-1.5">
                      <SparklesIcon className="w-3.5 h-3.5" />
                      {t("AI runs on this agent's own API keys — free, no credits charged.")}
                    </p>
                  )}

                  {/* Encourage BYO: no local agent, or one without AI keys. */}
                  {(state.mode === 'ai_workflow' || state.mode === 'api_workflow')
                    && !selectedAgentByoAi
                    && !onlineAgents.some((a: any) => a.ai_provider) && (
                    <p className="text-xs text-tertiary mt-2 inline-flex items-start gap-1.5">
                      <SparklesIcon className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                      <span>
                        {t('Tip: install a local agent and configure your own AI keys to run AI for free.')}{' '}
                        <button type="button" onClick={() => navigate('/fleet')} className="text-ink hover:underline">
                          {t('Set up an agent')}
                        </button>
                      </span>
                    </p>
                  )}

                  <p className="text-xs text-tertiary mt-1.5">
                    {t("Choose where the browser runs. Local agents use your machine's browser and network.")}
                  </p>
                </div>
              )}
            </div>
          </div>
          </div>
        </>
      )}
    </StageBackdrop>
  );
};
