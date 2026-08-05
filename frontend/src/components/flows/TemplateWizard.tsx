import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import {
  ArrowLeftIcon,
  CheckIcon,
  Cog6ToothIcon,
  EyeIcon,
  PlusIcon,
  TrashIcon,
  ShieldCheckIcon,
  BeakerIcon,
  LockClosedIcon,
} from '@heroicons/react/24/outline';
import {
  targetsApi,
  selectorsApi,
  extractorsApi,
  automationApi,
  triggersApi,
  vaultApi,
} from '../../api/endpoints';
import { BrowserRecorder } from '../BrowserRecorder';
import { Checkbox, Radio, NumberInput, Select } from '../ui';
import { segmentsToFunctions } from '../../utils/workflowFunctions';
import {
  TemplateSpec,
  PaymentMode,
  PAYMENT_LABELS,
  RISK_LABELS,
} from './templates';
import i18n from '../../i18n';

interface TemplateWizardProps {
  spec: TemplateSpec;
  onCancel?: () => void;
}

interface SelectorRow {
  name: string;
  selector: string;
  isPrice?: boolean;
}

type StepKey = 'watch' | 'selectors' | 'buy' | 'action' | 'review' | 'describe' | 'card' | 'review_ai';

type CardMode = 'vault' | 'new' | 'autofill';

function genId(tag: string): string {
  return `block_${tag}_${Date.now()}_${Math.floor(Math.random() * 10000)}`;
}

/** Coerce a user label into a valid Vault key: ^[a-zA-Z_][a-zA-Z0-9_.-]*$ */
function slugifySecret(s: string): string {
  const base = s
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_.-]+/g, '-')
    .replace(/^[^a-z_]+/, '');
  return (base || 'card').slice(0, 60);
}

function stepsFor(spec: TemplateSpec): { key: StepKey; label: string }[] {
  switch (spec.kind) {
    case 'monitor_buy':
      return [
        { key: 'watch', label: i18n.t('Watch') },
        { key: 'selectors', label: i18n.t('What to watch') },
        { key: 'buy', label: i18n.t('Buy & pay') },
        { key: 'review', label: i18n.t('Review') },
      ];
    case 'monitor_alert':
      return spec.actionWorkflow
        ? [
            { key: 'watch', label: i18n.t('Watch') },
            { key: 'selectors', label: i18n.t('What to watch') },
            { key: 'action', label: i18n.t('Grab action') },
            { key: 'review', label: i18n.t('Review') },
          ]
        : [
            { key: 'watch', label: i18n.t('Watch') },
            { key: 'selectors', label: i18n.t('What to watch') },
            { key: 'review', label: i18n.t('Review') },
          ];
    case 'monitor_extract':
      return [
        { key: 'watch', label: i18n.t('Watch') },
        { key: 'selectors', label: i18n.t('What to capture') },
        { key: 'review', label: i18n.t('Review') },
      ];
    // No 'ai_action' templates ship in the self-host build, but the branch is
    // kept so the exhaustive switch compiles.
    case 'ai_action': {
      const out: { key: StepKey; label: string }[] = [{ key: 'describe', label: i18n.t('Describe') }];
      if (spec.collectCard) out.push({ key: 'card', label: i18n.t('Card') });
      out.push({ key: 'review_ai', label: i18n.t('Review') });
      return out;
    }
  }
}

export const TemplateWizard: React.FC<TemplateWizardProps> = ({ spec, onCancel }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const steps = stepsFor(spec);
  const [step, setStep] = useState(0);
  const [creating, setCreating] = useState(false);
  const Icon = spec.icon;

  // Watch
  const [name, setName] = useState(spec.title);
  const [url, setUrl] = useState('');
  const [requiresPlaywright, setRequiresPlaywright] = useState(spec.requiresPlaywright ?? true);
  const [checkPeriodMs, setCheckPeriodMs] = useState(spec.checkPeriodMs ?? 120000);

  // Selectors
  const [selectors, setSelectors] = useState<SelectorRow[]>(
    (spec.selectorHints ?? [{ name: 'Content', selector: 'main' }]).map((h) => ({ ...h })),
  );
  const [pricePattern, setPricePattern] = useState(spec.pricePattern ?? '([0-9][0-9.,]*)');

  // Action workflow (buy / grab)
  const [workflows, setWorkflows] = useState<any[]>([]);
  const [workflowId, setWorkflowId] = useState<number | null>(null);
  const [workflowName, setWorkflowName] = useState<string>('');
  const [showRecorder, setShowRecorder] = useState(false);

  // Payment (monitor_buy) — never a card number, only how it pays
  const [paymentMode, setPaymentMode] = useState<PaymentMode>(spec.payment?.[0] ?? 'merchant_saved');

  // Commit step (monitor_buy) — which recorded step places the order. Dry run
  // executes everything up to, but not including, this step.
  const [wfSteps, setWfSteps] = useState<any[]>([]);
  const [commitIdx, setCommitIdx] = useState<number | null>(null);
  const [savingCommit, setSavingCommit] = useState(false);

  // Review / guardrails
  const [conditionText, setConditionText] = useState(spec.conditionText ?? '');
  const [priceCeiling, setPriceCeiling] = useState('');
  const [spendCap, setSpendCap] = useState('');
  const [dryRun, setDryRun] = useState(spec.defaultDryRun ?? false);
  const [requireConfirm, setRequireConfirm] = useState(false);
  const [confirmOver, setConfirmOver] = useState('');
  const [runOnce, setRunOnce] = useState(spec.kind === 'monitor_buy');
  const [cooldownMinutes, setCooldownMinutes] = useState(spec.kind === 'monitor_buy' ? 1440 : 60);
  const [notifyEmail, setNotifyEmail] = useState(true);

  // AI action
  const [aiGoal, setAiGoal] = useState(spec.aiGoal ?? '');

  // AI card input (collectCard templates) — card never lands on the workflow:
  // it's linked from / saved to the encrypted Vault, or autofilled locally.
  const [cardMode, setCardMode] = useState<CardMode>('vault');
  const [cardSecrets, setCardSecrets] = useState<any[]>([]);
  const [cardSecretName, setCardSecretName] = useState<string>('');
  const [cardLabel, setCardLabel] = useState('');
  const [card, setCard] = useState({ name: '', number: '', expiry: '', cvc: '', zip: '' });

  useEffect(() => {
    if (spec.kind === 'monitor_buy' || (spec.kind === 'monitor_alert' && spec.actionWorkflow)) {
      automationApi.listWorkflows().then(setWorkflows).catch(() => setWorkflows([]));
    }
  }, [spec]);

  // Load saved Vault cards so the user can link one instead of re-entering it.
  // (No 'ai_action' template ships in the self-host build, so this is inert — the
  // guard is always false — but it's kept so the card state stays consistent.)
  useEffect(() => {
    if (spec.kind !== 'ai_action' || !spec.collectCard) return;
    vaultApi
      .listSecrets({ category: 'card' })
      .then((secrets: any[]) => {
        const cards = secrets || [];
        setCardSecrets(cards);
        if (cards.length) {
          setCardSecretName(cards[0].name);
          setCardMode('vault');
        } else {
          setCardMode('new');
        }
      })
      .catch(() => {
        setCardSecrets([]);
        setCardMode('new');
      });
  }, [spec]);

  // Clear the step picker whenever the bound checkout workflow changes (including
  // to "none"). Adjusted DURING RENDER rather than from the loader effect below,
  // so the previous workflow's steps are never offered for a frame.
  const stepsWorkflowId = spec.kind === 'monitor_buy' ? workflowId || null : null;
  const [stepsLoadedFor, setStepsLoadedFor] = useState<number | null>(stepsWorkflowId);
  if (stepsLoadedFor !== stepsWorkflowId) {
    setStepsLoadedFor(stepsWorkflowId);
    setWfSteps([]);
    setCommitIdx(null);
  }

  // Load the chosen checkout workflow's steps so the user can mark the order step.
  useEffect(() => {
    if (spec.kind !== 'monitor_buy' || !workflowId) return;
    automationApi
      .getWorkflow(workflowId)
      .then((wf) => {
        const steps = ((wf as any).steps as any[]) || [];
        setWfSteps(steps);
        const existing = steps.findIndex((s) => s?.commit || s?.config?.commit);
        setCommitIdx(existing >= 0 ? existing : steps.length ? steps.length - 1 : null);
      })
      .catch(() => {
        setWfSteps([]);
        setCommitIdx(null);
      });
  }, [workflowId, spec.kind]);

  // Persist `commit: true` on the chosen step so both agents stop there in dry run.
  const setCommitStep = async (idx: number) => {
    setCommitIdx(idx);
    if (!workflowId || !wfSteps.length) return;
    setSavingCommit(true);
    try {
      const next = wfSteps.map((s, i) => ({ ...s, commit: i === idx }));
      await automationApi.updateWorkflow(workflowId, { steps: next });
      setWfSteps(next);
    } catch {
      toast.error(t('Could not save the place-order step'));
    } finally {
      setSavingCommit(false);
    }
  };

  const updateSelector = (i: number, patch: Partial<SelectorRow>) => {
    setSelectors((rows) => rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  };

  const byoOnly = !spec.editions.includes('cloud');

  const stepKey = steps[step].key;
  const canNext = (): boolean => {
    switch (stepKey) {
      case 'watch':
        return !!url.trim() && !!name.trim();
      case 'selectors':
        return selectors.length > 0 && selectors.every((s) => s.selector.trim());
      case 'buy':
        return !!workflowId;
      case 'describe':
        return !!url.trim() && !!name.trim() && !!aiGoal.trim();
      case 'card':
        if (cardMode === 'autofill') return true;
        if (cardMode === 'vault') return !!cardSecretName;
        return !!card.number.trim() && !!card.expiry.trim() && !!cardLabel.trim();
      default:
        return true;
    }
  };

  const handleRecorderSave = async (
    recSteps: any[],
    recName: string,
    credentials?: Record<string, string>,
    formData?: Record<string, string>,
    segments?: any[],
  ) => {
    try {
      // Persist any recorder step-group "functions" alongside the steps.
      const functions = segmentsToFunctions(segments);
      const wf = await automationApi.createWorkflow({
        name: recName || `${name} — ${spec.kind === 'monitor_buy' ? 'checkout' : 'grab'}`,
        workflow_type: 'recorded',
        steps: recSteps,
        form_data: formData || {},
        credentials,
        entry_url: url || undefined,
        ...(functions.length > 0 ? { functions } : {}),
        // BYO-only templates run the action on the user's own machine.
        execution_target: byoOnly || paymentMode === 'browser_autofill' ? 'local' : 'auto',
      });
      setWorkflowId(wf.id);
      setWorkflowName(wf.name);
      setWorkflows((prev) => [wf, ...prev.filter((w) => w.id !== wf.id)]);
      setShowRecorder(false);
      toast.success(t('Workflow recorded'));
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('Failed to save workflow'));
    }
  };

  const createAiAction = async () => {
    setCreating(true);
    try {
      let credentials: Record<string, string> | undefined;
      let userContext = spec.aiUserContext ?? '';

      if (spec.collectCard) {
        if (cardMode === 'autofill') {
          userContext +=
            '\n\nPayment entry: do NOT type a card number. Run on the linked local machine and let the browser autofill the saved card — focus the card-number field, let autofill populate it, then continue.';
        } else {
          // Resolve to a Vault card secret name; create one when entering a new card.
          let secretName = cardSecretName;
          if (cardMode === 'new') {
            secretName = slugifySecret(cardLabel);
            await vaultApi.createSecret({
              name: secretName,
              category: 'card',
              card: {
                name: card.name.trim(),
                number: card.number.replace(/\s+/g, ''),
                expiry: card.expiry.trim(),
                cvc: card.cvc.trim(),
                zip: card.zip.trim(),
              },
              description: `Payment card for ${name}`,
            });
          }
          // The AI only ever sees secure {{vault:…}} references, resolved to
          // plaintext at run time — the PAN never touches the workflow record.
          credentials = {
            card_name: `{{vault:${secretName}.name}}`,
            card_number: `{{vault:${secretName}.number}}`,
            card_expiry: `{{vault:${secretName}.expiry}}`,
            card_cvc: `{{vault:${secretName}.cvc}}`,
            card_zip: `{{vault:${secretName}.zip}}`,
          };
          userContext +=
            '\n\nThe new card is provided as secure inputs: card_number, card_expiry, card_cvc, card_name, card_zip. Fill the payment form from these — never log or echo their values.';
        }
      }

      // The coordinator's AI-session start (routers/ai_sessions.py) accepts a slim
      // payload (goal + entry_url + available_data / credentials + persona / budget);
      // richer cloud fields (mode / user_context / auto_validate / use_writ_ai) have no
      // coordinator equivalent and are ignored. Cast so the extra fields don't fail
      // the stricter AISessionStartInput type on this legacy template path.
      const session = await automationApi.createAISession({
        name,
        goal: aiGoal,
        entry_url: url,
        mode: spec.aiMode ?? 'intelligent',
        user_context: userContext,
        credentials,
        max_steps: 30,
        auto_validate: true,
        use_writ_ai: true,
      } as any);
      toast.success(t('AI workflow created'));
      navigate(`/workflows/ai/${session.id}`);
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || err?.message || t('Failed to create AI workflow'));
      setCreating(false);
    }
  };

  const createMonitor = async () => {
    setCreating(true);
    try {
      // 1. Target
      const targetResp: any = await targetsApi.create({
        url,
        check_type: 'content',
        selector: selectors[0]?.selector || 'body',
        enabled: true,
        check_period_ms: checkPeriodMs,
        requires_playwright: requiresPlaywright,
      } as any);
      const targetId = targetResp?.id ?? targetResp?.data?.id;
      if (!targetId) throw new Error(t('Target creation failed'));

      // 2. Selectors (+ optional price extractor on the watched one)
      let watchedSelectorId: number | undefined;
      for (const sel of selectors) {
        const created: any = await selectorsApi.create(parseInt(targetId), {
          name: sel.name,
          selector: sel.selector,
          content_type: 'text',
          enabled: true,
        });
        const selId = created?.id ?? created?.data?.id;
        if (!watchedSelectorId) watchedSelectorId = selId ? parseInt(selId) : undefined;
        if (selId && sel.isPrice) {
          try {
            await extractorsApi.create({
              target_selector_id: parseInt(selId),
              name: 'Price',
              output_name: 'price',
              extract_type: 'regex',
              config: { pattern: pricePattern, group: 1 },
              is_array: false,
              enabled: true,
            });
          } catch {
            /* non-critical */
          }
        }
      }
      try {
        await selectorsApi.setAllBaselines(parseInt(targetId));
      } catch {
        /* non-critical */
      }

      // 3. Blocks — shape depends on the template kind
      const evt = genId('event');
      const blocks: any[] = [
        {
          id: evt,
          type: 'event',
          blockType: 'change_detected',
          config: { target_id: parseInt(targetId), selector_id: watchedSelectorId ?? null },
        },
      ];
      let parent = evt;

      // Condition gate (buy + alert; extract fires on any change)
      if (spec.kind === 'monitor_buy' || spec.kind === 'monitor_alert') {
        if (conditionText.trim()) {
          const condStock = genId('cond');
          blocks.push({
            id: condStock,
            type: 'condition',
            blockType: 'condition',
            parentId: parent,
            config: { field: 'content', operator: 'contains', value: conditionText },
          });
          parent = condStock;
        }
      }
      // Price ceiling (buy only)
      if (spec.kind === 'monitor_buy' && (priceCeiling || '').trim()) {
        const condPrice = genId('cond');
        blocks.push({
          id: condPrice,
          type: 'condition',
          blockType: 'condition',
          parentId: parent,
          config: { field: 'extracted.price', operator: 'lte', value: priceCeiling, _isCustom: true },
        });
        parent = condPrice;
      }

      // Action workflow (buy = required, grab = optional)
      let notifyParent = parent;
      if ((spec.kind === 'monitor_buy' || spec.kind === 'monitor_alert') && workflowId) {
        const act = genId('action');
        const actionConfig: any = { workflow_id: workflowId, input_mapping: {} };
        if (spec.kind === 'monitor_buy') {
          // No-PAN payment + safety rails travel on the buy action so the executor
          // (and any reviewer) can see exactly how it pays and what limits apply.
          actionConfig.payment_mode = paymentMode;
          actionConfig.dry_run = dryRun;
          if (requireConfirm && (confirmOver || '').trim()) {
            actionConfig.require_confirmation = true;
            actionConfig.confirm_over = parseFloat(confirmOver);
          }
        }
        blocks.push({ id: act, type: 'action', blockType: 'workflow', parentId: parent, config: actionConfig });
        const done = genId('event');
        blocks.push({
          id: done,
          type: 'event',
          blockType: 'workflow_completed',
          parentId: act,
          config: { status_condition: 'success', linked_to_block: act },
        });
        notifyParent = done;
      }

      // Notify
      const notify = genId('action');
      blocks.push({
        id: notify,
        type: 'action',
        blockType: 'notification',
        parentId: notifyParent,
        config: {
          channels: notifyEmail ? ['email'] : [],
          title: spec.notifyTitle ?? 'Automation fired',
          template: spec.notifyTemplate ?? '{{target_name}} — {{now_time}}.',
        },
      });

      // 4. Trigger conditions (guardrails)
      const conditions: any = {};
      if (cooldownMinutes > 0) conditions.schedule = { cooldown_minutes: cooldownMinutes };
      if (runOnce) conditions.max_fires = 1;
      if (spec.kind === 'monitor_buy' && (spendCap || '').trim()) {
        conditions.spend_cap = { amount: parseFloat(spendCap), period: 'day' };
      }

      const actions: any[] = [];
      if (workflowId) actions.push({ type: 'workflow', config: { workflow_id: workflowId } });
      actions.push({ type: 'notification', config: blocks[blocks.length - 1].config });

      const rule = await triggersApi.create({
        name,
        description: spec.tagline,
        enabled: true,
        event_type: 'change_detected',
        target_id: parseInt(targetId),
        target_selector_id: watchedSelectorId,
        workflow_id: workflowId ?? undefined,
        actions,
        blocks,
        conditions,
        priority: 0,
      } as any);

      toast.success(t('{{title}} created', { title: spec.title }));
      navigate(`/automations/${rule.id}`);
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || err?.message || t('Failed to create automation'));
      setCreating(false);
    }
  };

  const handleCreate = () => {
    if (spec.kind === 'ai_action') return createAiAction();
    return createMonitor();
  };

  const risk = RISK_LABELS[spec.riskTier];

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-3 h-12 px-4 sm:px-6 bg-chrome chrome-topbar border-b border-border shrink-0">
        {onCancel && (
          <button type="button" onClick={onCancel} className="p-1 text-tertiary hover:text-ink transition-colors">
            <ArrowLeftIcon className="w-3.5 h-3.5" />
          </button>
        )}
        <Icon className="w-4 h-4 text-tertiary" />
        <span className="text-[13px] font-semibold text-ink">{t('{{title}} setup', { title: spec.title })}</span>
        <span
          className={clsx(
            'ml-1 text-[10px] px-1.5 py-0.5 rounded border',
            spec.riskTier === 'adversarial'
              ? 'bg-warning-bg text-warning-fg border-warning'
              : spec.riskTier === 'tos'
                ? 'bg-canvas text-secondary border-border'
                : 'bg-success-bg text-success-fg border-success',
          )}
          title={t(risk.hint)}
        >
          {t(risk.label)}
        </span>
      </div>

      {/* Stepper */}
      <div className="flex items-center gap-2 px-4 sm:px-6 py-3 border-b border-border shrink-0">
        {steps.map((s, i) => (
          <React.Fragment key={s.key}>
            <div className={clsx('flex items-center gap-1.5 text-xs font-medium', i === step ? 'text-ink' : i < step ? 'text-secondary' : 'text-tertiary')}>
              <div className={clsx('w-5 h-5 rounded-full flex items-center justify-center text-[10px] border', i === step ? 'bg-ink text-white border-ink' : i < step ? 'bg-hover border-border text-secondary' : 'border-border')}>
                {i < step ? <CheckIcon className="w-3 h-3" /> : i + 1}
              </div>
              <span className="hidden @rail/stage:inline">{s.label}</span>
            </div>
            {i < steps.length - 1 && <div className="flex-1 h-px bg-border" />}
          </React.Fragment>
        ))}
      </div>

      {/* Body */}
      <div className="flex-1 overflow-auto px-4 sm:px-6 py-6">
        <div className="max-w-xl mx-auto space-y-5">
          {step === 0 && (
            <div className="rounded-xl bg-canvas border border-border p-3 text-[12px] text-secondary leading-relaxed">
              {t(spec.description)}
              {byoOnly && (
                <div className="mt-2 flex items-start gap-1.5 text-[11px] text-warning-fg">
                  <ShieldCheckIcon className="w-3.5 h-3.5 mt-px shrink-0" /> {t(risk.hint)}
                </div>
              )}
            </div>
          )}

          {stepKey === 'watch' && (
            <>
              <Field label={t('Name')}>
                <input value={name} onChange={(e) => setName(e.target.value)} className={inputCls} placeholder={spec.title} />
              </Field>
              <Field label={t('Page URL')} hint={t('The page to watch.')}>
                <input value={url} onChange={(e) => setUrl(e.target.value)} className={inputCls} placeholder={spec.urlPlaceholder} />
              </Field>
              <Field label={t('Check frequency')}>
                <Select<number>
                  value={checkPeriodMs}
                  onChange={(v) => setCheckPeriodMs(v)}
                  options={[
                    { value: 30000, label: t('Every 30 seconds') },
                    { value: 60000, label: t('Every minute') },
                    { value: 120000, label: t('Every 2 minutes') },
                    { value: 300000, label: t('Every 5 minutes') },
                    { value: 900000, label: t('Every 15 minutes') },
                    { value: 3600000, label: t('Every hour') },
                    { value: 21600000, label: t('Every 6 hours') },
                  ]}
                />
              </Field>
              <Checkbox
                checked={requiresPlaywright}
                onChange={(e) => setRequiresPlaywright(e.target.checked)}
                label={t('Render JavaScript (recommended for most sites)')}
              />
            </>
          )}

          {stepKey === 'selectors' && (
            <>
              <p className="text-xs text-secondary flex items-center gap-1.5">
                <EyeIcon className="w-4 h-4" /> {spec.extractHint || t('What on the page to watch. Defaults usually work; adjust the CSS if needed.')}
              </p>
              {selectors.map((sel, i) => (
                <div key={i} className="rounded-xl border border-border p-3 space-y-2">
                  <div className="flex items-center gap-2">
                    <input value={sel.name} onChange={(e) => updateSelector(i, { name: e.target.value })} className="w-32 px-2 py-1.5 bg-surface border border-border rounded-lg text-sm text-ink" placeholder={t('Name')} />
                    {sel.isPrice && <span className="text-[10px] px-1.5 py-0.5 rounded bg-canvas text-secondary border border-border">→ extracted.price</span>}
                    {selectors.length > 1 && (
                      <button type="button" onClick={() => setSelectors((r) => r.filter((_, idx) => idx !== i))} className="ml-auto p-1 text-danger/70 hover:text-danger"><TrashIcon className="w-4 h-4" /></button>
                    )}
                  </div>
                  <input value={sel.selector} onChange={(e) => updateSelector(i, { selector: e.target.value })} className={clsx(inputCls, 'font-mono text-xs')} placeholder={t('CSS selector')} />
                  {sel.isPrice && (
                    <Field label={t('Price pattern (regex)')} hint={t('First capture group becomes the numeric price.')}>
                      <input value={pricePattern} onChange={(e) => setPricePattern(e.target.value)} className={clsx(inputCls, 'font-mono text-xs')} />
                    </Field>
                  )}
                </div>
              ))}
              <button type="button" onClick={() => setSelectors((r) => [...r, { name: 'Field', selector: '' }])} className="text-xs text-ink/70 hover:text-ink flex items-center gap-1"><PlusIcon className="w-3 h-3" /> {t('Add selector')}</button>
            </>
          )}

          {(stepKey === 'buy' || stepKey === 'action') && (
            <>
              <p className="text-xs text-secondary">
                {stepKey === 'buy'
                  ? t('Pick or record the workflow that buys the item (add-to-cart → checkout). It runs when the condition is met.')
                  : t('Optionally pick or record a workflow that claims the slot. Leave empty to just get an alert.')}
              </p>
              <Field label={stepKey === 'buy' ? t('Checkout workflow') : t('Grab workflow (optional)')}>
                <Select<string | number>
                  value={workflowId ?? ''}
                  onChange={(v) => {
                    const id = v === '' ? null : (typeof v === 'number' ? v : parseInt(String(v)));
                    setWorkflowId(id);
                    setWorkflowName(workflows.find((w) => w.id === id)?.name || '');
                  }}
                  placeholder={stepKey === 'buy' ? t('Select a workflow…') : t('None — alert only')}
                  options={[
                    { value: '', label: stepKey === 'buy' ? t('Select a workflow…') : t('None — alert only') },
                    ...workflows.map((w) => ({ value: w.id as number, label: w.name as string })),
                  ]}
                />
              </Field>
              <button type="button" onClick={() => setShowRecorder(true)} className="w-full px-4 py-3 border-2 border-dashed border-border rounded-xl hover:border-border-strong hover:bg-hover/50 transition-all group text-center">
                <Cog6ToothIcon className="h-5 w-5 text-tertiary group-hover:text-ink mx-auto mb-1" />
                <div className="text-sm font-medium text-secondary group-hover:text-ink">{stepKey === 'buy' ? t('Record checkout workflow') : t('Record grab workflow')}</div>
                <div className="text-[10px] text-tertiary">{t('Record clicks, fills, and navigation')}</div>
              </button>
              {workflowId && <div className="text-xs text-ink bg-canvas border border-border rounded-lg px-3 py-2">{t('Using')} <strong>{workflowName}</strong></div>}

              {/* No-PAN payment selector (buy only) */}
              {stepKey === 'buy' && spec.payment && (
                <div className="rounded-xl border border-border p-3 space-y-2.5">
                  <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-tertiary">
                    <LockClosedIcon className="w-3 h-3" /> {t('Payment')}
                  </div>
                  {spec.payment.map((mode) => {
                    const p = PAYMENT_LABELS[mode];
                    return (
                      <div key={mode} className={clsx('rounded-lg px-2 py-1.5 border', paymentMode === mode ? 'border-ink/30 bg-canvas' : 'border-transparent hover:bg-hover', p.comingSoon && 'opacity-50')}>
                        <Radio
                          name="payment"
                          disabled={p.comingSoon}
                          checked={paymentMode === mode}
                          onChange={() => setPaymentMode(mode)}
                          label={
                            <span>
                              <span className="text-ink font-medium">{t(p.label)}{p.comingSoon && <span className="ml-1.5 text-[10px] text-tertiary">{t('(coming soon)')}</span>}</span>
                              <span className="block text-[11px] text-tertiary leading-snug">{t(p.hint)}</span>
                            </span>
                          }
                        />
                      </div>
                    );
                  })}
                  <p className="text-[11px] text-secondary flex items-center gap-1.5 pt-0.5"><ShieldCheckIcon className="w-3.5 h-3.5" /> {t('We never store your card number.')}</p>
                </div>
              )}

              {/* Commit-step picker (buy only) — where dry run stops */}
              {stepKey === 'buy' && wfSteps.length > 0 && (
                <div className="rounded-xl border border-border p-3 space-y-2">
                  <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-tertiary">
                    <BeakerIcon className="w-3 h-3" /> {t('Dry-run stop point')}
                  </div>
                  <Field label={t('Which step places the order?')} hint={t('Dry run runs everything up to — but not including — this step.')}>
                    <Select<number>
                      value={commitIdx ?? undefined}
                      onChange={(v) => setCommitStep(v)}
                      disabled={savingCommit}
                      options={wfSteps.map((s, i) => ({
                        value: i,
                        label: `${i + 1}. ${s?.type || 'step'} ${(s?.config?.selector || s?.config?.url || s?.config?.value || '').toString().slice(0, 36)}`,
                      }))}
                    />
                  </Field>
                </div>
              )}
            </>
          )}

          {stepKey === 'review' && (
            <>
              {(spec.kind === 'monitor_buy' || spec.kind === 'monitor_alert') && spec.conditionLabel && (
                <Field label={spec.conditionLabel}>
                  <input value={conditionText} onChange={(e) => setConditionText(e.target.value)} className={inputCls} placeholder={spec.conditionText} />
                </Field>
              )}
              {spec.kind === 'monitor_buy' && (
                <>
                  <Field label={t('Max price (ceiling)')} hint={t('Only buy at or below this. Leave blank for no ceiling.')}>
                    <NumberInput
                      value={priceCeiling === '' ? null : Number(priceCeiling)}
                      onChange={(v) => setPriceCeiling(v == null ? '' : String(v))}
                      placeholder={t('e.g. 250')}
                    />
                  </Field>
                  <div className="rounded-xl border border-warning bg-warning-bg/40 p-3 space-y-3">
                    <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-warning-fg"><BeakerIcon className="w-3.5 h-3.5" /> {t('Safety')}</div>
                    <Checkbox
                      checked={dryRun}
                      onChange={(e) => setDryRun(e.target.checked)}
                      label={<span>{t('Dry run — go through checkout but')} <strong>{t("don't place the order")}</strong></span>}
                      description={t('Recommended for the first runs. Turn off when you trust it.')}
                    />
                    <div className="flex items-center gap-2">
                      <label className="text-sm text-secondary w-28">{t('Spend cap / day')}</label>
                      <NumberInput
                        value={spendCap === '' ? null : Number(spendCap)}
                        onChange={(v) => setSpendCap(v == null ? '' : String(v))}
                        placeholder={t('e.g. 500')}
                        className="w-28"
                      />
                    </div>
                    <div className="flex items-center gap-2">
                      <Checkbox
                        checked={requireConfirm}
                        onChange={(e) => setRequireConfirm(e.target.checked)}
                        label={t('Ask me to confirm before buying over')}
                      />
                      <NumberInput
                        value={confirmOver === '' ? null : Number(confirmOver)}
                        onChange={(v) => setConfirmOver(v == null ? '' : String(v))}
                        disabled={!requireConfirm}
                        placeholder="100"
                        className="w-20"
                      />
                    </div>
                  </div>
                </>
              )}
              <div className="rounded-xl border border-border p-3 space-y-3">
                <div className="text-[10px] font-medium uppercase tracking-wider text-tertiary">{t('Guardrails')}</div>
                <Checkbox
                  checked={runOnce}
                  onChange={(e) => setRunOnce(e.target.checked)}
                  label={spec.kind === 'monitor_buy' ? t('Buy once, then stop') : t('Fire once, then stop')}
                />
                <div className="flex items-center gap-2">
                  <label className="text-sm text-secondary">{t('Cooldown')}</label>
                  <NumberInput
                    min={0}
                    value={cooldownMinutes}
                    onChange={(v) => setCooldownMinutes(v ?? 0)}
                    className="w-24"
                  />
                  <span className="text-[11px] text-tertiary">{t('minutes between fires')}</span>
                </div>
              </div>
              <Checkbox
                checked={notifyEmail}
                onChange={(e) => setNotifyEmail(e.target.checked)}
                label={t('Email me when it fires')}
              />
            </>
          )}

          {stepKey === 'describe' && (
            <>
              <Field label={t('Name')}>
                <input value={name} onChange={(e) => setName(e.target.value)} className={inputCls} placeholder={spec.title} />
              </Field>
              <Field label={t('Account / page URL')} hint={t('Where the AI should start (your logged-in account page).')}>
                <input value={url} onChange={(e) => setUrl(e.target.value)} className={inputCls} placeholder={spec.urlPlaceholder} />
              </Field>
              <Field label={t('What the AI should do')} hint={t('Editable — tune it for this specific site.')}>
                <textarea value={aiGoal} onChange={(e) => setAiGoal(e.target.value)} rows={5} className={clsx(inputCls, 'resize-none leading-relaxed')} />
              </Field>
              {spec.suggestPersona && (
                <div className="rounded-xl bg-canvas border border-border p-3 text-[12px] text-secondary flex items-start gap-2">
                  <LockClosedIcon className="w-4 h-4 mt-px shrink-0 text-tertiary" />
                  <span>{t('This acts behind a login. Attach a')} <a href="/personas" className="text-ink underline">{t('persona')}</a> {t('when you run it so the AI can sign in and pass 2FA. Credentials are encrypted and never shown to the model.')}</span>
                </div>
              )}
            </>
          )}

          {stepKey === 'card' && (
            <>
              <p className="text-xs text-secondary flex items-center gap-1.5">
                <LockClosedIcon className="w-4 h-4" /> {t('How should the AI get the new card? It’s stored encrypted in your Vault (or stays on your machine) — never on this workflow.')}
              </p>

              <div className="rounded-xl border border-border p-3 space-y-2.5">
                <div className={clsx('rounded-lg px-2 py-1.5 border', cardMode === 'vault' ? 'border-ink/30 bg-canvas' : 'border-transparent hover:bg-hover', !cardSecrets.length && 'opacity-50')}>
                  <Radio
                    name="cardmode"
                    disabled={!cardSecrets.length}
                    checked={cardMode === 'vault'}
                    onChange={() => setCardMode('vault')}
                    label={
                      <span>
                        <span className="text-ink font-medium">{t('Use a saved card')}{!cardSecrets.length && <span className="ml-1.5 text-[10px] text-tertiary">{t('(none yet)')}</span>}</span>
                        <span className="block text-[11px] text-tertiary leading-snug">{t('Pick a card you’ve saved in your Vault. Only a reference is stored here.')}</span>
                      </span>
                    }
                  />
                </div>
                <div className={clsx('rounded-lg px-2 py-1.5 border', cardMode === 'new' ? 'border-ink/30 bg-canvas' : 'border-transparent hover:bg-hover')}>
                  <Radio
                    name="cardmode"
                    checked={cardMode === 'new'}
                    onChange={() => setCardMode('new')}
                    label={
                      <span>
                        <span className="text-ink font-medium">{t('Enter a new card')}</span>
                        <span className="block text-[11px] text-tertiary leading-snug">{t('Saved to your Vault (encrypted) and reusable across services.')}</span>
                      </span>
                    }
                  />
                </div>
                <div className={clsx('rounded-lg px-2 py-1.5 border', cardMode === 'autofill' ? 'border-ink/30 bg-canvas' : 'border-transparent hover:bg-hover')}>
                  <Radio
                    name="cardmode"
                    checked={cardMode === 'autofill'}
                    onChange={() => setCardMode('autofill')}
                    label={
                      <span>
                        <span className="text-ink font-medium">{t('Browser autofill (local agent)')}</span>
                        <span className="block text-[11px] text-tertiary leading-snug">{t('No card entered here. Your linked machine’s browser autofills it — the card never leaves your computer.')}</span>
                      </span>
                    }
                  />
                </div>
              </div>

              {cardMode === 'vault' && cardSecrets.length > 0 && (
                <Field label={t('Saved card')}>
                  <Select<string>
                    value={cardSecretName}
                    onChange={(v) => setCardSecretName(v)}
                    options={cardSecrets.map((s: any) => ({
                      value: s.name as string,
                      label: `${s.name}${s.card_last4 ? ` — •••• ${s.card_last4}` : ''}`,
                    }))}
                  />
                  <a href="/secrets" className="text-[11px] text-tertiary hover:text-ink underline">{t('Manage cards in Vault')}</a>
                </Field>
              )}

              {cardMode === 'new' && (
                <div className="space-y-3">
                  <Field label={t('Save as')} hint={t('A label for this card in your Vault — e.g. personal-visa.')}>
                    <input value={cardLabel} onChange={(e) => setCardLabel(e.target.value)} className={inputCls} placeholder={t('personal-visa')} autoComplete="off" />
                  </Field>
                  <Field label={t('Cardholder name')}>
                    <input value={card.name} onChange={(e) => setCard((c) => ({ ...c, name: e.target.value }))} className={inputCls} placeholder={t('Jane Doe')} autoComplete="off" />
                  </Field>
                  <Field label={t('Card number')}>
                    <input value={card.number} onChange={(e) => setCard((c) => ({ ...c, number: e.target.value }))} className={clsx(inputCls, 'font-mono')} inputMode="numeric" autoComplete="off" placeholder="•••• •••• •••• ••••" />
                  </Field>
                  <div className="grid grid-cols-3 gap-3">
                    <Field label={t('Expiry')}>
                      <input value={card.expiry} onChange={(e) => setCard((c) => ({ ...c, expiry: e.target.value }))} className={inputCls} autoComplete="off" placeholder="MM/YY" />
                    </Field>
                    <Field label={t('CVC')}>
                      <input value={card.cvc} onChange={(e) => setCard((c) => ({ ...c, cvc: e.target.value }))} className={inputCls} inputMode="numeric" autoComplete="off" placeholder="123" />
                    </Field>
                    <Field label={t('ZIP')}>
                      <input value={card.zip} onChange={(e) => setCard((c) => ({ ...c, zip: e.target.value }))} className={inputCls} autoComplete="off" placeholder="10001" />
                    </Field>
                  </div>
                  <p className="text-[11px] text-secondary flex items-center gap-1.5"><ShieldCheckIcon className="w-3.5 h-3.5 shrink-0" /> {t('Encrypted in your Vault. Never shown to the AI model or stored on this workflow.')}</p>
                </div>
              )}

              {cardMode === 'autofill' && (
                <div className="rounded-xl bg-canvas border border-border p-3 text-[12px] text-secondary flex items-start gap-2">
                  <ShieldCheckIcon className="w-4 h-4 mt-px shrink-0 text-tertiary" />
                  <span>{t('Run this on your linked machine. The AI opens the payment form and lets your browser autofill the card — nothing card-related is collected or stored here.')}</span>
                </div>
              )}
            </>
          )}

          {stepKey === 'review_ai' && (
            <div className="space-y-4">
              <div className="rounded-xl border border-border p-3 space-y-1.5 text-sm">
                <div className="text-[10px] font-medium uppercase tracking-wider text-tertiary">{t('Summary')}</div>
                <div className="text-secondary"><span className="text-tertiary">{t('Name:')}</span> {name}</div>
                <div className="text-secondary break-all"><span className="text-tertiary">{t('Start:')}</span> {url}</div>
                <div className="text-secondary"><span className="text-tertiary">{t('Mode:')}</span> {spec.aiMode ?? 'intelligent'}</div>
                {spec.collectCard && (
                  <div className="text-secondary"><span className="text-tertiary">{t('Card:')}</span>{' '}
                    {cardMode === 'autofill'
                      ? t('Browser autofill (local)')
                      : cardMode === 'new'
                        ? t('New card → saved to Vault')
                        : `${t('Saved card')} (${cardSecretName})`}
                  </div>
                )}
              </div>
              {byoOnly && (
                <div className="flex items-start gap-1.5 text-[11px] text-warning-fg"><ShieldCheckIcon className="w-3.5 h-3.5 mt-px shrink-0" /> {t(risk.hint)}</div>
              )}
              <p className="text-xs text-secondary">{t("We'll create the AI workflow and open it. From there, attach a persona and run it — review the result before relying on it.")}</p>
            </div>
          )}
        </div>
      </div>

      {/* Footer */}
      <div className="flex items-center justify-between px-4 sm:px-6 h-14 border-t border-border shrink-0">
        <button type="button" onClick={() => (step === 0 ? onCancel?.() : setStep((s) => s - 1))} className="px-3 py-1.5 text-[12px] font-medium text-secondary hover:text-ink transition-colors">
          {step === 0 ? t('Cancel') : t('Back')}
        </button>
        {step < steps.length - 1 ? (
          <button type="button" disabled={!canNext()} onClick={() => setStep((s) => s + 1)} className={clsx('px-4 py-1.5 text-[12px] font-medium rounded-lg transition-colors', canNext() ? 'bg-accent-strong text-accent-on hover:bg-accent-strong/90' : 'bg-hover text-tertiary cursor-not-allowed')}>
            {t('Next')}
          </button>
        ) : (
          <button type="button" disabled={creating} onClick={handleCreate} className={clsx('px-4 py-1.5 text-[12px] font-medium rounded-lg transition-colors', creating ? 'bg-hover text-tertiary cursor-not-allowed' : 'bg-accent-strong text-accent-on hover:bg-accent-strong/90')}>
            {creating ? t('Creating…') : spec.kind === 'ai_action' ? t('Create AI workflow') : t('Create automation')}
          </button>
        )}
      </div>

      {showRecorder && (
        <BrowserRecorder isOpen={showRecorder} onClose={() => setShowRecorder(false)} onSave={handleRecorderSave} />
      )}
    </div>
  );
};

const inputCls = 'w-full px-3 py-2 bg-surface border border-border rounded-lg text-sm text-ink focus:border-border-strong outline-none transition-colors';

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <label className="block text-xs font-medium text-tertiary">{label}</label>
      {children}
      {hint && <p className="text-[11px] text-tertiary">{hint}</p>}
    </div>
  );
}
