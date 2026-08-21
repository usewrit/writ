import React, { useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import { WizardProvider, useWizard } from './WizardContext';
import { StudioAppBar } from './shared/StudioAppBar';
import { ConfirmDialog } from '../ConfirmDialog';
import { ModeSelectionStep } from './steps/ModeSelectionStep';
import { ConfigureStep } from './steps/ConfigureStep';
import { FinalizeStep } from './steps/FinalizeStep';
import { automationApi, targetsApi, selectorsApi, extractorsApi } from '../../api/endpoints';
import { crawlApi } from '../../api/crawl';
import { segmentsToFunctions } from '../../utils/workflowFunctions';
import { scheduleToPayload, type ScheduleValue } from '../../utils/schedule';

/** Ids of whatever the wizard actually created, handed to `onComplete` so the caller
 *  can route on them (e.g. bind the new workflow to the automation block that asked). */
export interface WizardCreatedIds {
  workflowId: number | null;
  targetId: number | null;
  aiSessionId: number | null;
}

interface UnifiedCreationWizardProps {
  onComplete?: (ids?: WizardCreatedIds) => void;
  onCancel?: () => void;
  initialMode?: 'content_monitor' | 'manual_workflow' | 'ai_workflow' | 'api_workflow' | 'streaming_workflow' | 'site_crawl';
  allowedModes?: string[];
  /** When true, fills parent container with no close button */
  embedded?: boolean;
  /** Seed the entry URL (used by the onboarding simulation). */
  initialUrl?: string;
  /** Seed the workflow name (used by the onboarding simulation). */
  initialName?: string;
  /** When true, on completion redirect straight into the automation builder seeded with the
   *  created entity — even if the user didn't wire an automation. Set when the wizard was
   *  launched from the automation source picker's "Create new monitor". */
  forceAutomateReturn?: boolean;
}

const WizardInner: React.FC<UnifiedCreationWizardProps> = ({ onComplete, onCancel, initialMode, allowedModes, embedded, initialUrl, initialName, forceAutomateReturn }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const {
    state,
    dispatch,
    steps,
    currentStepIndex,
    canGoBack,
    goBack,
    goNext,
  } = useWizard();

  const isLastStep = currentStepIndex === steps.length - 1;

  // Discard-guard: leaving the recorder (Configure step) tears down the live browser
  // session and drops everything captured so far, so a Back/Close there must confirm.
  // Holds the deferred navigation to run if the user confirms the discard.
  const [pendingLeave, setPendingLeave] = React.useState<null | (() => void)>(null);

  // The "Please enter a name" tooltip anchored to the top-bar name input. Typing
  // clears it immediately (see onNameChange below); otherwise it dismisses itself.
  const [nameError, setNameError] = React.useState<string | null>(null);
  React.useEffect(() => {
    if (!nameError) return;
    const id = setTimeout(() => setNameError(null), 4000);
    return () => clearTimeout(id);
  }, [nameError]);

  // Pre-select mode but stay on step 1 so user can set name + URL first.
  // Exception: site_crawl is a single-screen flow (URL lives on the crawl
  // panel), so a preselected crawl skips Setup and lands on Configure directly.
  React.useEffect(() => {
    if (initialMode && !state.mode) {
      dispatch({ type: 'SET_MODE', mode: initialMode });
      if (initialMode === 'site_crawl') {
        dispatch({ type: 'COMPLETE_STEP', step: 'mode' });
        dispatch({ type: 'GO_TO_STEP', step: 'configure' });
      }
    }
  }, [initialMode, state.mode, dispatch]);

  // Seed name/URL once (e.g. the onboarding simulation prefills the demo site).
  React.useEffect(() => {
    const updates: Partial<typeof state.config> = {};
    if (initialUrl && !state.config.url) updates.url = initialUrl;
    if (initialName && !state.config.name) updates.name = initialName;
    if (Object.keys(updates).length) dispatch({ type: 'UPDATE_CONFIG', updates });
    // Run only on mount / when the seeds change — not on every field edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialUrl, initialName]);

  // ── Site crawl (Dragnet) ─────────────────────────────────────────────────
  // Fire-and-forget: start the distributed crawl and go straight to its own live
  // detail page. Crawl is a separate flow (its own /crawls page, no Finalize
  // connectors), so Configure is its last step and this IS its submit. The
  // self-hosted coordinator does deterministic crawls only (no AI executor).
  const handleCrawlStart = useCallback(async () => {
    dispatch({ type: 'SET_SUBMITTING', submitting: true });
    dispatch({ type: 'SET_SUBMIT_ERROR', error: null });
    try {
      const { config } = state;
      const parsePaths = (text: string): string[] =>
        text.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
      let seed = (config.url || '').trim();
      if (seed && !/^https?:\/\//i.test(seed)) seed = `https://${seed}`;

      // Content-selection spec — omit when everything is default (backend null ⇒ main isolation,
      // comments kept), so we only override when the user actually changed a knob.
      const buildContentSpec = () => {
        const preset = config.crawlContentPreset || 'main';
        const includeComments = config.crawlIncludeComments !== false;
        const keepImages = config.crawlKeepImages !== false;
        const keepTables = config.crawlKeepTables !== false;
        const keepLinks = config.crawlKeepLinks !== false;
        const excludeSel = parsePaths(config.crawlExcludeSelectors);
        const includeSel = parsePaths(config.crawlIncludeSelectors);
        const isDefault =
          preset === 'main' && includeComments && keepImages && keepTables && keepLinks &&
          excludeSel.length === 0 && includeSel.length === 0;
        if (isDefault) return undefined;
        const spec: import('../../api/crawl').ContentSpec = {
          preset,
          include_comments: includeComments,
          keep: { images: keepImages, tables: keepTables, links: keepLinks },
        };
        if (excludeSel.length) spec.exclude_selectors = excludeSel;
        if (includeSel.length) spec.include_selectors = includeSel;
        return spec;
      };
      const contentSpec = buildContentSpec();

      // The URL is collected on the crawl panel itself (single-screen flow),
      // so validate it here rather than on a prior step.
      if (!seed) {
        dispatch({ type: 'SET_SUBMITTING', submitting: false });
        toast.error(t('Enter the site to crawl first.'));
        return;
      }

      // SCRAPE = fetch just the entry URL — a depth-0 crawl with no scope filters
      // (the coordinator has no multi-URL seed list, so scrape is single-page here).
      const isScrape = config.crawlVerb === 'scrape';
      // A plain-English GOAL derives the scope AND ranks the frontier. When the
      // operator gave one but pinned no paths by hand, send no include/exclude/depth
      // at all so the coordinator's own derivation wins (an explicit [] would read
      // as "the operator chose an empty scope" and suppress it).
      const intent = config.crawlIntent.trim();
      const inc = parsePaths(config.crawlIncludePaths);
      const exc = parsePaths(config.crawlExcludePaths);
      const deriveScope = !!intent && inc.length === 0 && exc.length === 0;
      const crawl = await crawlApi.start({
        url: seed,
        name: config.name?.trim() || undefined,
        executor: config.crawlExecutor,
        extract_mode: config.crawlOutput,
        // Only meaningful for the ai executor; omitted otherwise so a stale prompt
        // left in the form can never ride along on a deterministic crawl.
        extract_prompt:
          config.crawlExecutor === 'ai' && config.crawlPrompt.trim()
            ? config.crawlPrompt.trim()
            : undefined,
        render_mode: config.crawlRenderMode,
        ocr_mode: config.crawlOcrMode,
        intent: intent || undefined,
        // Hand-picked entry set from the Map step (crawl only — a scrape is one page).
        seed_urls: !isScrape && config.crawlSeedUrls.length ? config.crawlSeedUrls : undefined,
        relevance_threshold: intent ? config.crawlRelevanceThreshold || undefined : undefined,
        // Floor only — the coordinator caps nothing about a crawl's shape (no plan
        // ladder self-hosted), so don't silently clamp what the operator asked for.
        max_depth: isScrape ? 0 : deriveScope ? undefined : Math.max(0, config.crawlMaxDepth),
        page_budget: isScrape ? 1 : Math.max(1, config.crawlPageBudget),
        max_concurrent_shards: Math.max(1, config.crawlConcurrency),
        shard_size: Math.max(1, config.crawlShardSize),
        delay_ms: Math.max(0, config.crawlDelayMs),
        respect_robots: config.crawlRespectRobots,
        same_domain: config.crawlSameDomain,
        allow_subdomains: config.crawlAllowSubdomains,
        include_paths: isScrape ? [] : deriveScope ? undefined : inc,
        exclude_paths: isScrape ? [] : deriveScope ? undefined : exc,
        persona_id: config.crawlPersonaId ?? undefined,
        content_spec: contentSpec,
      });
      dispatch({ type: 'SET_SUBMITTING', submitting: false });
      toast.success(isScrape ? t('Scrape started') : t('Crawl started'));
      navigate(`/crawls/${crawl.id}`);
    } catch (err: any) {
      const msg = err.response?.data?.detail || err.message || t('Creation failed');
      dispatch({ type: 'SET_SUBMIT_ERROR', error: msg });
      dispatch({ type: 'SET_SUBMITTING', submitting: false });
      toast.error(msg);
    }
  }, [state, dispatch, t, navigate]);

  // ── Create-on-advance ─────────────────────────────────────────────────────
  // Runs when the user leaves Configure for Finalize. It CREATES the entity (or
  // UPDATES it if they went Back and edited, so no duplicates), then stores the
  // ids on state.createdIds. That's what lets the Finalize step act on a real
  // entity — copy the endpoint, mint a key, expose MCP, wire an automation, run a
  // test — with no "create it first" gate. Returns false to keep the user on
  // Configure when creation fails.
  //
  const ensureCreated = useCallback(async (): Promise<boolean> => {
    dispatch({ type: 'SET_SUBMITTING', submitting: true });
    dispatch({ type: 'SET_SUBMIT_ERROR', error: null });
    try {
      const { mode, config } = state;

      if (mode === 'content_monitor') {
        // Recorded setup steps (login / navigation) the checker replays before each
        // check, shipped as an inline manifest. JS-rendered setup implies a browser.
        const setupSteps = config.monitorSetupSteps || [];
        const setupStepsJson = setupSteps.length > 0
          ? JSON.stringify({ steps: setupSteps, credentials: config.credentials || {} })
          : undefined;
        const monitorSchedule: ScheduleValue = {
          kind: config.scheduleKind,
          intervalMs: config.checkPeriodMs ?? 60000,
          time: config.scheduleTime,
          days: config.scheduleDays,
          tz: config.scheduleTz,
        };

        // Re-advance after edits: patch the existing target's cadence/JS/region.
        // Selectors are left as first created (editing them post-create is an edge
        // case; the monitor keeps working) so we don't duplicate rows.
        if (state.createdIds.targetId) {
          await targetsApi.update(String(state.createdIds.targetId), {
            check_period_ms: config.checkPeriodMs,
            requires_playwright: config.requiresPlaywright || setupSteps.length > 0,
            preferred_region: config.preferredRegion || null,
            ...scheduleToPayload(monitorSchedule),
            ...(setupStepsJson ? { setup_steps: setupStepsJson } : {}),
          } as any);
          dispatch({ type: 'SET_SUBMITTING', submitting: false });
          return true;
        }

        const targetResp = await targetsApi.create({
          url: config.url,
          selector: config.selectors[0]?.selector || 'body',
          check_type: 'content',
          enabled: true,
          check_period_ms: config.checkPeriodMs,
          ...scheduleToPayload(monitorSchedule),
          requires_playwright: config.requiresPlaywright || setupSteps.length > 0,
          preferred_region: config.preferredRegion || null,
          ...(setupStepsJson ? { setup_steps: setupStepsJson } : {}),
        } as any);
        const targetId = (targetResp as any).id || (targetResp as any).data?.id;
        dispatch({ type: 'UPDATE_CREATED_IDS', updates: { targetId: parseInt(targetId) } });

        for (const sel of config.selectors) {
          const createdSel = await selectorsApi.create(parseInt(targetId), {
            name: sel.name,
            selector: sel.selector,
            content_type: sel.checkType,
            enabled: true,
            ignore_regex: sel.ignoreRegex || undefined,
            visual_region: sel.checkType === 'visual' ? sel.region : undefined,
          });
          const selectorId = createdSel?.id ?? createdSel?.data?.id;
          if (selectorId && sel.extractors?.length) {
            for (const ex of sel.extractors) {
              try {
                await extractorsApi.create({
                  target_selector_id: parseInt(selectorId),
                  name: ex.name,
                  output_name: ex.outputName,
                  extract_type: ex.extractType,
                  config: ex.config || {},
                  is_array: !!ex.isArray,
                  default_value: ex.defaultValue,
                  enabled: true,
                });
              } catch { /* non-critical: the monitor still works without the extractor */ }
            }
          }
        }
        try { await selectorsApi.setAllBaselines(parseInt(targetId)); } catch { /* non-critical */ }
        dispatch({ type: 'SET_SUBMITTING', submitting: false });
        return true;
      }

      if (mode === 'manual_workflow' || mode === 'extract_scrape') {
        // Normalize recorder step-group "segments" into canonical workflow functions.
        const functions = segmentsToFunctions(config.segments);
        const payload = {
          name: config.name || t('Untitled Workflow'),
          description: config.description,
          workflow_type: mode === 'extract_scrape' ? 'recorded' : (config.workflowType || 'recorded'),
          steps: config.recordedSteps,
          form_data: config.formData || {},
          ...(functions.length > 0 ? { functions } : {}),
          entry_url: config.url,
          timeout_ms: config.timeoutMs,
          retry_count: config.retryCount,
          headless: config.headless,
          fast_mode: config.fastMode,
          execution_target: config.executionTarget as 'auto' | 'local' | 'cloud',
          default_persona_id: config.defaultPersonaId ?? undefined,
        };
        const workflow = state.createdIds.workflowId
          ? await automationApi.updateWorkflow(state.createdIds.workflowId, payload as any)
          : await automationApi.createWorkflow(payload);
        dispatch({ type: 'UPDATE_CREATED_IDS', updates: { workflowId: workflow.id } });
        dispatch({ type: 'SET_SUBMITTING', submitting: false });
        return true;
      }

      if (mode === 'ai_workflow') {
        // AI sessions are fire-and-forget in the self-host build: createAISession
        // FIRES an autonomous session at a fleet agent (no schedulable/updatable row),
        // so fire it only once — a re-advance keeps the already-launched session.
        if (state.createdIds.aiSessionId) {
          dispatch({ type: 'SET_SUBMITTING', submitting: false });
          return true;
        }
        // The coordinator's AI-session start wants NON-secret hints and secret fill
        // values SPLIT: available_data (plaintext) vs credentials (re-sealed under the
        // agent channel key, never stored). Split config.availableData by isSecret.
        const availableData: Record<string, string> = {};
        const credentials: Record<string, string> = {};
        for (const f of config.availableData) {
          if (!f.key) continue;
          (f.isSecret ? credentials : availableData)[f.key] = f.value;
        }
        const session = await automationApi.createAISession({
          name: config.name || t('AI Workflow'),
          goal: config.goal,
          entry_url: config.url || undefined,
          available_data: Object.keys(availableData).length > 0 ? availableData : undefined,
          credentials: Object.keys(credentials).length > 0 ? credentials : undefined,
          max_steps: config.maxSteps,
          // Default identity the AI signs in as (login creds + server-side 2FA).
          persona_id: config.defaultPersonaId ?? undefined,
          generate_workflow: config.generateWorkflow,
        });
        const sessionId = session?.id ?? session?.ai_session_id;
        if (sessionId != null) {
          dispatch({ type: 'UPDATE_CREATED_IDS', updates: { aiSessionId: sessionId } });
        }
        toast.success(
          config.generateWorkflow
            ? t('AI session started — it will save a workflow when it finishes')
            : t('AI session started'),
        );
        dispatch({ type: 'SET_SUBMITTING', submitting: false });
        return true;
      }

      if (mode === 'streaming_workflow') {
        const streamingConfig: any = {
          setup_steps_count: config.setupStepsCount || config.recordedSteps.length,
          handlers: config.streamingHandlers,
          keepalive_interval_ms: 30000,
          multi_conversation: config.multiConversation,
          ...(config.multiConversation ? { max_concurrent_threads: config.maxConcurrentConversations } : {}),
        };
        let streamingSteps = config.recordedSteps;
        if (config.advancedScriptEnabled && config.advancedScript) {
          streamingConfig.advanced_script = { enabled: true, code: config.advancedScript, persistent: true, functions: [] };
          streamingSteps = [
            ...config.recordedSteps,
            {
              id: `advscript-${(globalThis.crypto?.randomUUID?.() || `${Date.now()}`)}`.slice(0, 36),
              type: 'advanced_script' as const,
              enabled: true,
              config: { code: config.advancedScript, persistent: true, functions: [] },
            },
          ];
        }
        if (config.openaiCompatEnabled) {
          streamingConfig.openai_compat = {
            enabled: true,
            default_handler: config.openaiDefaultHandler || 'chat',
            model_name: config.openaiModelName || 'streaming',
            response_field: 'content',
          };
        }
        const payload = {
          name: config.name || t('Streaming Workflow'),
          description: config.description,
          workflow_type: 'streaming',
          steps: streamingSteps,
          form_data: config.formData,
          credentials: Object.keys(config.credentials).length > 0 ? config.credentials : undefined,
          entry_url: config.url,
          timeout_ms: config.timeoutMs,
          retry_count: 0,
          headless: config.headless,
          fast_mode: config.fastMode,
          streaming_config: streamingConfig,
        } as any;
        const workflow = state.createdIds.workflowId
          ? await automationApi.updateWorkflow(state.createdIds.workflowId, payload)
          : await automationApi.createWorkflow(payload);
        dispatch({ type: 'UPDATE_CREATED_IDS', updates: { workflowId: workflow.id } });
        dispatch({ type: 'SET_SUBMITTING', submitting: false });
        return true;
      }

      dispatch({ type: 'SET_SUBMITTING', submitting: false });
      return true;
    } catch (err: any) {
      // Launching an AI session with no connected fleet agent replies 409 — show a
      // clear, actionable message instead of the raw coordinator detail.
      const isNoAgent = state.mode === 'ai_workflow' && err.response?.status === 409;
      const msg = isNoAgent
        ? t('No online agent to run the AI session')
        : (err.response?.data?.detail || err.message || t('Creation failed'));
      dispatch({ type: 'SET_SUBMIT_ERROR', error: msg });
      dispatch({ type: 'SET_SUBMITTING', submitting: false });
      toast.error(msg);
      return false;
    }
  }, [state, dispatch, t]);

  // ── Finish ────────────────────────────────────────────────────────────────
  // The entity already exists, and the Finalize step applied its exposures (key,
  // MCP, schedule, automations) inline. Edits made ON the Finalize sheet live in
  // config, so we re-run ensureCreated (which UPDATES when the id exists) to flush
  // them, then route onward.
  const handleFinish = useCallback(async () => {
    const ok = await ensureCreated();
    if (!ok) return;
    const { mode } = state;
    const wfId = state.createdIds.workflowId;
    const tId = state.createdIds.targetId;
    if (forceAutomateReturn && (wfId || tId)) {
      if (mode === 'content_monitor' && tId) {
        navigate(`/automations/new?source=check&checkId=${tId}`);
      } else if (wfId) {
        navigate(`/automations/new?source=workflow&workflowId=${wfId}&event=completed`);
      }
      return;
    }
    onComplete?.({ workflowId: wfId, targetId: tId, aiSessionId: state.createdIds.aiSessionId });
  }, [ensureCreated, state, forceAutomateReturn, navigate, onComplete, t]);

  // The app-bar's single primary button: for a crawl (Configure is its last step)
  // it starts the crawl; otherwise (Finalize) it finishes and routes onward.
  const handlePrimary = useCallback(() => {
    if (state.mode === 'site_crawl') { void handleCrawlStart(); return; }
    void handleFinish();
  }, [state.mode, handleCrawlStart, handleFinish]);

  const renderStep = () => {
    switch (state.currentStep) {
      case 'mode': return <ModeSelectionStep allowedModes={allowedModes} preSelectedMode={initialMode} />;
      // Crawl submits from its own panel button; wire that to the crawl starter.
      case 'configure': return <ConfigureStep onSubmit={handleCrawlStart} />;
      case 'finalize': return <FinalizeStep />;
      default: return null;
    }
  };

  const handleNext = async () => {
    // Validation per step
    if (state.currentStep === 'mode') {
      if (!state.mode) {
        toast.error(t('Please select an automation type'));
        return;
      }
      if (!state.config.name.trim()) {
        setNameError(t('Please enter a name'));
        return;
      }
      if (!state.config.url.trim() && state.mode !== 'content_monitor') {
        toast.error(t('Please enter an entry URL'));
        return;
      }
      dispatch({ type: 'COMPLETE_STEP', step: 'mode' });
      goNext();
      return;
    }

    if (state.currentStep === 'configure') {
      if (state.mode === 'content_monitor' && state.config.selectors.length === 0) {
        toast.error(t('Add at least one selector'));
        return;
      }
      if (state.mode === 'ai_workflow' && !state.config.goal) {
        toast.error(t('Please describe a goal for the AI'));
        return;
      }
      // Recorder modes need at least one recorded step before advancing. Without
      // this toast the always-enabled "Done recording" button looks dead — goNext()
      // silently returns because canGoNext is false for empty recordings.
      if (state.mode === 'manual_workflow' && state.config.recordedSteps.length === 0) {
        toast.error(t('Record at least one step before continuing'));
        return;
      }
      // Save the workflow/monitor NOW, before Finalize, so its wiring works.
      const ok = await ensureCreated();
      if (!ok) return;
      // AI sessions are fire-and-forget in the self-host build — no schedulable
      // entity and no update API, so there is no Finalize step to expose them.
      // Route to the workflows list, where the running session and (on finish) its
      // recorded workflow surface. Mirrors the desktop wizard.
      if (state.mode === 'ai_workflow') {
        onComplete?.();
        navigate('/workflows');
        return;
      }
      goNext();
      return;
    }

    goNext();
  };

  // True while the recorder (Configure) step holds work that leaving would drop — the
  // recorded steps, captured segments, streaming handlers, or (monitor) the picked
  // check-target selectors + setup steps. Only guards BEFORE create.
  const hasUnsavedRecorderWork = state.currentStep === 'configure' && !state.createdIds.workflowId && !state.createdIds.targetId && (() => {
    const c = state.config;
    switch (state.mode) {
      case 'content_monitor':
        return c.selectors.length > 0 || c.monitorSetupSteps.length > 0;
      case 'manual_workflow':
      case 'extract_scrape':
        return c.recordedSteps.length > 0 || c.segments.length > 0;
      case 'streaming_workflow':
        return c.recordedSteps.length > 0 || c.streamingHandlers.length > 0 || c.advancedScriptEnabled;
      default:
        return false;
    }
  })();

  const rawBack = canGoBack ? goBack : (onCancel ?? (() => {}));
  const rawCancel = onCancel && !embedded ? onCancel : undefined;
  // Route leave affordances through the discard guard when there's recorder work.
  const guardedBack = () => { if (hasUnsavedRecorderWork) setPendingLeave(() => rawBack); else rawBack(); };
  const guardedCancel = rawCancel
    ? () => { if (hasUnsavedRecorderWork) setPendingLeave(() => rawCancel); else rawCancel(); }
    : undefined;

  return (
    <div className="overflow-hidden flex flex-col h-full">
      {/* Top app-bar — the standing chrome of the Studio shell. Carries Back, the
          step-crumb popover, the inline-editable name, and the single morphing
          primary button (Continue / Done recording / Done). */}
      <StudioAppBar
        steps={steps}
        currentStep={state.currentStep}
        completedSteps={state.completedSteps}
        onStepClick={(step) => dispatch({ type: 'GO_TO_STEP', step })}
        name={state.config.name}
        nameError={nameError}
        onNameChange={(name) => {
          if (nameError) setNameError(null);
          dispatch({ type: 'UPDATE_CONFIG', updates: { name } });
        }}
        canGoBack={canGoBack}
        onBack={guardedBack}
        isLastStep={isLastStep}
        isSubmitting={state.isSubmitting}
        mode={state.mode}
        onNext={handleNext}
        onSubmit={handlePrimary}
        onCancel={guardedCancel}
      />

      {/* Discard-changes guard — shown when leaving the recorder step with unsaved work. */}
      <ConfirmDialog
        isOpen={!!pendingLeave}
        onClose={() => setPendingLeave(null)}
        onConfirm={() => { const go = pendingLeave; setPendingLeave(null); go?.(); }}
        title={t('Discard changes?')}
        message={t('Leaving now discards everything captured here — all recorded steps and selectors will be lost. This cannot be undone.')}
        confirmText={t('Discard & leave')}
        cancelText={t('Keep editing')}
        variant="danger"
      />

      {/* Step content = the three STATES of the one Studio shell:
            Setup    → IntentPicker / mode form as floating cards on the dot-grid stage,
            Configure→ the recorder's full-bleed browser stage (or a focused form card),
            Finalize → a slide-up glass sheet over the dimmed-but-visible stage. */}
      <div className="flex-1 min-h-0 overflow-hidden flex flex-col">
        <div key={state.currentStep} className="h-full flex-1 min-h-0 flex flex-col animate-wizard-field-in">
          {renderStep()}
        </div>
      </div>
    </div>
  );
};

export const UnifiedCreationWizard: React.FC<UnifiedCreationWizardProps> = (props) => {
  return (
    <WizardProvider>
      <WizardInner {...props} />
    </WizardProvider>
  );
};
