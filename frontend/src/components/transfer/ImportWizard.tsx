import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import {
  ArrowLeftIcon,
  ArrowRightIcon,
  ArrowUpTrayIcon,
  BellAlertIcon,
  CheckCircleIcon,
  ClockIcon,
  ExclamationTriangleIcon,
  KeyIcon,
  LockClosedIcon,
  ShieldExclamationIcon,
  UserCircleIcon,
} from '@heroicons/react/24/outline';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { Input } from '../ui/Input';
import { Select } from '../ui/Select';
import { Checkbox } from '../ui/Checkbox';
import { Switch } from '../ui/Switch';
import { Badge } from '../ui/Badge';
import { apiErrorDetail, apiErrorMessage } from '../../api/client';
import { formatNumber, formatDate } from '../../utils/format';
import { personasApi, vaultApi } from '../../api/endpoints';
import {
  transferApi,
  type ImportState,
  type PackageHeaderSummary,
  type Resolution,
  type SummaryItem,
} from '../../api/transfer';
import type { Persona } from '../../types/api';

/**
 * ImportWizard — bringing a `.writ` package into this account.
 *
 * Seven steps (DATA_PORTABILITY_SPEC §12), deliberately in this order:
 *
 *   1 File      what is in it, from the CLEARTEXT header — before any passphrase
 *   2 Unlock    the passphrase, and one honest sentence about where it is used
 *   3 Review    every asset by name; collisions; what cannot import, and why
 *   3b Sealed   only when the package carries credential values, per item
 *   4 Logins    persona slots → your OWN personas
 *   5 Keys      the carried key NAMES → your vault (or a value typed now)
 *   6 Wiring    alerts, inputs, and schedules — arming is OFF by default
 *   7 Confirm   exactly what will happen, then results with Undo
 *
 * The order is the security story: the user learns what a package claims to be,
 * and whether it carries credentials, BEFORE they commit a passphrase to it; and
 * they see every asset by name before anything is created. Nothing in the account
 * changes until step 7 — steps 1-6 are pure reads on the server.
 *
 * Slot groups reuse the framing of `AutomationAttachWizard` so an import feels
 * like the attach flow users already know: it is always YOUR data being attached,
 * never the sender's.
 */

type StepId = 'file' | 'unlock' | 'review' | 'sealed' | 'logins' | 'keys' | 'wiring' | 'confirm';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  /** Called after a successful commit so the caller can refresh its lists. */
  onImported?: () => void;
}

const KIND_LABEL: Record<string, string> = {
  workflows: 'Workflow',
  automations: 'Automation',
  monitors: 'Monitor',
  crawls: 'Crawl',
  personas: 'Login',
  ai_sessions: 'AI session',
  endpoints: 'Endpoint',
  webhooks: 'Webhook',
};

const ImportWizardBody: React.FC<Props> = ({ isOpen, onClose, onImported }) => {
  const { t } = useTranslation();

  const [step, setStep] = useState<StepId>('file');
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [header, setHeader] = useState<PackageHeaderSummary | null>(null);
  const [headerError, setHeaderError] = useState<{ message: string; producer?: any } | null>(null);
  const [passphrase, setPassphrase] = useState('');
  const [unlockError, setUnlockError] = useState<string | null>(null);
  const [attemptsLeft, setAttemptsLeft] = useState(5);
  const [state, setState] = useState<ImportState | null>(null);

  // Local plan mirrors, flushed to the server per step.
  const [resolutions, setResolutions] = useState<Record<string, { action: Resolution; name?: string }>>({});
  const [personaBindings, setPersonaBindings] = useState<Record<string, number | null>>({});
  const [secretBindings, setSecretBindings] = useState<Record<string, string | null>>({});
  const [newSecretValues, setNewSecretValues] = useState<Record<string, string>>({});
  const [notifyBindings, setNotifyBindings] = useState<Record<string, string[]>>({});
  const [credentialChoices, setCredentialChoices] = useState<Record<string, boolean>>({});
  const [includeData, setIncludeData] = useState(true);
  const [armSchedules, setArmSchedules] = useState(false);

  const [personas, setPersonas] = useState<Persona[]>([]);
  const [vaultKeys, setVaultKeys] = useState<{ id: number; name: string }[]>([]);

  // One key per staged import, so a double-click cannot import twice.
  const idempotencyKey = useRef<string>('');

  // No reset pass: `ImportWizard` below remounts this body on every open, so all
  // of the above is already at its initial value — including `idempotencyKey`,
  // which a fresh mount gives a fresh ref. Clearing seventeen fields from an
  // effect is what the cascading-render rule objects to, and it also kept the
  // passphrase, the decrypted package and every entered secret alive in memory
  // for as long as the parent page stayed mounted.

  // Personas + vault keys are the user's OWN data, loaded once we know the
  // package needs them. Silent: a failure here must not block the wizard.
  useEffect(() => {
    if (!state) return;
    const needsPersona = (state.requirements.persona_slots || []).length > 0;
    const needsSecret = (state.requirements.secret_slots || []).length > 0;
    if (needsPersona) personasApi.list().then(setPersonas).catch(() => undefined);
    if (needsSecret) {
      vaultApi
        .listSecrets()
        .then((res: any) => {
          const rows = Array.isArray(res) ? res : res?.secrets || [];
          setVaultKeys(rows.map((r: any) => ({ id: r.id, name: r.name || r.key })));
        })
        .catch(() => undefined);
    }
  }, [state]);

  const steps = useMemo<StepId[]>(() => {
    const base: StepId[] = ['file', 'unlock', 'review'];
    if (header?.has_sealed_credentials) base.push('sealed');
    return [...base, 'logins', 'keys', 'wiring', 'confirm'];
  }, [header?.has_sealed_credentials]);

  const stepIndex = steps.indexOf(step);
  const summary = state?.summary;
  const requirements = state?.requirements;
  const readiness = state?.readiness;

  // ── step 1: inspect ──────────────────────────────────────────────────────

  const pickFile = async (picked: File) => {
    setFile(picked);
    setHeader(null);
    setHeaderError(null);
    setBusy(true);
    try {
      const res = await transferApi.inspect(picked);
      setHeader(res.header);
    } catch (err) {
      const detail = apiErrorDetail<any>(err);
      setHeaderError({
        message: detail?.message || apiErrorMessage(err, 'That file is not a Writ package.'),
        producer: detail?.producer,
      });
    } finally {
      setBusy(false);
    }
  };

  // ── step 2: stage ────────────────────────────────────────────────────────

  const unlock = async () => {
    if (!file || !passphrase) return;
    setBusy(true);
    setUnlockError(null);
    try {
      const staged = await transferApi.stage(file, passphrase);
      setState(staged);
      idempotencyKey.current =
        (globalThis.crypto?.randomUUID?.() as string) || `${staged.import.id}-${Date.now()}`;
      // Seed the local plan from the server's defaults so a user who clicks
      // straight through gets the safe choice (rename on collision, never replace).
      const seeded: Record<string, { action: Resolution }> = {};
      (staged.summary.items || []).forEach((item) => {
        if (!item.block) seeded[item.ref] = { action: item.default_resolution };
      });
      setResolutions(seeded);
      setCredentialChoices({});
      setStep('review');
    } catch (err) {
      const detail = apiErrorDetail<any>(err);
      setUnlockError(detail?.message || apiErrorMessage(err, 'Could not open that package.'));
      if (detail?.code === 'BAD_PASSPHRASE') setAttemptsLeft((n) => Math.max(0, n - 1));
    } finally {
      setBusy(false);
    }
  };

  // ── plan flushing ────────────────────────────────────────────────────────

  const flush = async (patch: Parameters<typeof transferApi.savePlan>[1]): Promise<boolean> => {
    if (!state) return false;
    setBusy(true);
    try {
      const res = await transferApi.savePlan(state.import.id, patch);
      setState((prev) => (prev ? { ...prev, import: res.import, readiness: res.readiness } : prev));
      return true;
    } catch (err) {
      toast.error(apiErrorMessage(err, 'Could not save that choice.'));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const goNext = async () => {
    if (step === 'review' && !(await flush({ resolutions }))) return;
    if (step === 'sealed' && !(await flush({ credentials: credentialChoices }))) return;
    if (step === 'logins' && !(await flush({ personas: personaBindings }))) return;
    if (step === 'keys') {
      // A value typed here goes straight to the user's OWN vault and is bound by
      // NAME — it never travels back into the package.
      const created: Record<string, string | null> = { ...secretBindings };
      for (const [key, value] of Object.entries(newSecretValues)) {
        if (!value) continue;
        try {
          await vaultApi.createSecret({ name: key, value, category: 'credentials' });
          created[key] = key;
        } catch (err) {
          toast.error(`${key}: ${apiErrorMessage(err, 'could not be saved to your vault')}`);
          return;
        }
      }
      setSecretBindings(created);
      if (!(await flush({ secrets: created }))) return;
    }
    if (step === 'wiring' && !(await flush({ notify: notifyBindings, include_data: includeData, arm_schedules: armSchedules }))) {
      return;
    }
    setStep(steps[Math.min(steps.length - 1, stepIndex + 1)]);
  };

  const goBack = () => setStep(steps[Math.max(0, stepIndex - 1)]);

  // ── step 7: commit ───────────────────────────────────────────────────────

  const commit = async () => {
    if (!state) return;
    setBusy(true);
    try {
      const res = await transferApi.commit(state.import.id, idempotencyKey.current);
      setState((prev) => (prev ? { ...prev, import: res.import, result: res.result } : prev));
      onImported?.();
      const created = (res.result.assets || []).filter((a) => a.outcome === 'created' || a.outcome === 'replaced').length;
      toast.success(t('Imported {{count}} items', { count: created }));
    } catch (err) {
      toast.error(apiErrorMessage(err, 'The import failed.'));
    } finally {
      setBusy(false);
    }
  };

  const undo = async () => {
    if (!state) return;
    setBusy(true);
    try {
      const res = await transferApi.undo(state.import.id);
      const kept = res.undo?.kept?.length || 0;
      if (kept) {
        toast.success(t('Undone, except {{count}} item(s) already in use', { count: kept }));
      } else {
        toast.success(t('Import undone'));
      }
      setState((prev) => (prev ? { ...prev, import: res.import } : prev));
      onImported?.();
    } catch (err) {
      toast.error(apiErrorMessage(err, 'Could not undo this import.'));
    } finally {
      setBusy(false);
    }
  };

  const discardAndClose = async () => {
    if (state && !state.import.committed_at && state.import.status !== 'committed') {
      // Destroy the staged plaintext now rather than leaving it until its TTL.
      transferApi.discard(state.import.id).catch(() => undefined);
    }
    onClose();
  };

  // ── render ───────────────────────────────────────────────────────────────

  const committed = state?.import.status === 'committed' || state?.import.status === 'undone';

  return (
    <Modal
      isOpen={isOpen}
      onClose={discardAndClose}
      size="2xl"
      title={t('Import a Writ package')}
      subtitle={t(STEP_SUBTITLE_KEY[step])}
      footer={
        <div className="flex items-center justify-between gap-3">
          <StepDots steps={steps} current={stepIndex} />
          <div className="flex items-center gap-2">
            {stepIndex > 0 && !committed && (
              <Button variant="ghost" onClick={goBack} disabled={busy}>
                <ArrowLeftIcon className="w-4 h-4 mr-1.5" />
                {t('Back')}
              </Button>
            )}
            {step === 'file' && (
              <Button onClick={() => setStep('unlock')} disabled={!header || busy}>
                {t('Continue')}
                <ArrowRightIcon className="w-4 h-4 ml-1.5" />
              </Button>
            )}
            {step === 'unlock' && (
              <Button onClick={unlock} disabled={!passphrase || busy || attemptsLeft === 0} loading={busy}>
                <LockClosedIcon className="w-4 h-4 mr-1.5" />
                {t('Unlock')}
              </Button>
            )}
            {step !== 'file' && step !== 'unlock' && step !== 'confirm' && (
              <Button onClick={goNext} disabled={busy} loading={busy}>
                {t('Continue')}
                <ArrowRightIcon className="w-4 h-4 ml-1.5" />
              </Button>
            )}
            {step === 'confirm' && !committed && (
              <Button onClick={commit} disabled={busy || !readiness?.ready} loading={busy}>
                {t('Import')}
              </Button>
            )}
            {committed && (
              <Button variant="ghost" onClick={onClose}>
                {t('Done')}
              </Button>
            )}
          </div>
        </div>
      }
    >
      {step === 'file' && (
        <FileStep file={file} header={header} error={headerError} busy={busy} onPick={pickFile} />
      )}

      {step === 'unlock' && (
        <UnlockStep
          header={header}
          passphrase={passphrase}
          onChange={setPassphrase}
          onSubmit={unlock}
          error={unlockError}
          attemptsLeft={attemptsLeft}
          busy={busy}
        />
      )}

      {step === 'review' && summary && (
        <ReviewStep
          summary={summary}
          resolutions={resolutions}
          onChange={(ref, next) => setResolutions((prev) => ({ ...prev, [ref]: next }))}
        />
      )}

      {step === 'sealed' && (
        <SealedStep
          header={header}
          summary={summary}
          choices={credentialChoices}
          onToggle={(key, on) => setCredentialChoices((prev) => ({ ...prev, [key]: on }))}
        />
      )}

      {step === 'logins' && (
        <LoginsStep
          slots={requirements?.persona_slots || []}
          personas={personas}
          bindings={personaBindings}
          onBind={(slot, id) => setPersonaBindings((prev) => ({ ...prev, [slot]: id }))}
        />
      )}

      {step === 'keys' && (
        <KeysStep
          slots={requirements?.secret_slots || []}
          vaultKeys={vaultKeys}
          bindings={secretBindings}
          values={newSecretValues}
          onBind={(key, existing) => setSecretBindings((prev) => ({ ...prev, [key]: existing }))}
          onValue={(key, value) => setNewSecretValues((prev) => ({ ...prev, [key]: value }))}
        />
      )}

      {step === 'wiring' && (
        <WiringStep
          requirements={requirements}
          summary={summary}
          notify={notifyBindings}
          onNotify={(slot, channels) => setNotifyBindings((prev) => ({ ...prev, [slot]: channels }))}
          includeData={includeData}
          onIncludeData={setIncludeData}
          armSchedules={armSchedules}
          onArmSchedules={setArmSchedules}
        />
      )}

      {step === 'confirm' && (
        <ConfirmStep
          readiness={readiness}
          result={state?.result || null}
          status={state?.import.status}
          progress={state?.import.progress || null}
          onUndo={undo}
          busy={busy}
        />
      )}
    </Modal>
  );
};

/**
 * A closed-then-reopened wizard must start blank. React's answer to that is a
 * fresh instance — a `key` — not an effect that clears every field with its own
 * setState the moment `isOpen` flips.
 *
 * The counter advances on the CLOSED→OPEN edge only. Bumping it on close would
 * tear down the subtree the modal's leave transition is still animating, and the
 * dialog would vanish instead of fading. Comparing against the previous prop
 * during render is React's documented way to adjust state from a prop without an
 * effect; it settles in the same commit, so nothing renders with a stale key.
 */
export const ImportWizard: React.FC<Props> = (props) => {
  const [session, setSession] = useState(0);
  const [wasOpen, setWasOpen] = useState(props.isOpen);
  if (props.isOpen !== wasOpen) {
    setWasOpen(props.isOpen);
    if (props.isOpen) setSession((n) => n + 1);
  }
  return <ImportWizardBody key={session} {...props} />;
};

/** Step subtitles as [key, English]. Resolved at RENDER time — a module-level
 *  `t()` call would freeze at import, before the language is known. */
const STEP_SUBTITLE_KEY: Record<StepId, string> = {
  file: 'Choose the package file',
  unlock: 'Enter its passphrase',
  review: 'Choose what to bring in',
  sealed: 'This package carries credentials',
  logins: 'Attach your own logins',
  keys: 'Attach your own keys',
  wiring: 'Alerts, data and schedules',
  confirm: 'Review and import',
};

// ── step components ────────────────────────────────────────────────────────

const StepDots: React.FC<{ steps: StepId[]; current: number }> = ({ steps, current }) => (
  <div className="flex items-center gap-1.5" aria-hidden>
    {steps.map((s, i) => (
      <span
        key={s}
        className={clsx(
          'h-1.5 rounded-full transition-all',
          i === current ? 'w-6 bg-accent' : i < current ? 'w-1.5 bg-accent/50' : 'w-1.5 bg-active',
        )}
      />
    ))}
  </div>
);

const FileStep: React.FC<{
  file: File | null;
  header: PackageHeaderSummary | null;
  error: { message: string; producer?: any } | null;
  busy: boolean;
  onPick: (f: File) => void;
}> = ({ file, header, error, busy, onPick }) => {
  const { t } = useTranslation();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  return (
    <div className="space-y-4">
      <div
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          const dropped = e.dataTransfer.files?.[0];
          if (dropped) onPick(dropped);
        }}
        onClick={() => inputRef.current?.click()}
        className={clsx(
          'rounded-xl border-2 border-dashed p-8 text-center cursor-pointer transition-colors',
          dragging ? 'border-accent bg-accent/10' : 'border-border hover:border-accent/60',
        )}
      >
        <input
          ref={inputRef}
          type="file"
          accept=".writ"
          className="hidden"
          onChange={(e) => { const f = e.target.files?.[0]; if (f) onPick(f); }}
        />
        <ArrowUpTrayIcon className="w-8 h-8 mx-auto text-tertiary" />
        <p className="mt-2 text-sm font-medium text-ink">
          {file ? file.name : 'Drop a .writ package here, or click to choose one'}
        </p>
        {file && (
          <p className="text-xs text-secondary mt-0.5">{(file.size / 1024 / 1024).toFixed(1)} MB</p>
        )}
      </div>

      {busy && (
        <p className="text-sm text-secondary">
          {t('Reading the package…')}
        </p>
      )}

      {error && (
        <div className="rounded-lg bg-red-50 border border-red-200 p-3">
          <div className="flex gap-2">
            <ExclamationTriangleIcon className="w-5 h-5 text-red-600 shrink-0" />
            <div className="text-sm text-red-800">
              <p>{error.message}</p>
              {error.producer?.version && (
                <p className="mt-1 text-xs">
                  {t('It was made by {{app}} {{version}}. Update this install to open it.', { app: error.producer.app || 'Writ',
                    version: error.producer.version })}
                </p>
              )}
            </div>
          </div>
        </div>
      )}

      {header && (
        <div className="rounded-lg border border-border p-4 space-y-3">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="text-sm font-medium text-ink">
                {header.label || 'Writ package'}
              </p>
              <p className="text-xs text-secondary mt-0.5">
                {t('From {{app}} {{version}}', { app: header.producer_app || 'Writ',
                  version: header.producer_version || '' })}
                {header.created_at ? ` · ${formatDate(header.created_at)}` : ''}
              </p>
            </div>
            {header.has_sealed_credentials && (
              <Badge variant="error">
                <ShieldExclamationIcon className="w-3.5 h-3.5 mr-1" />
                {t('Carries {{count}} credential(s)', { count: header.sealed_credential_count })}
              </Badge>
            )}
          </div>

          <div className="flex flex-wrap gap-1.5">
            {Object.entries(header.contents)
              .filter(([, n]) => n > 0)
              .map(([kind, n]) => (
                <Badge key={kind} variant="default">
                  {n} {KIND_LABEL[kind] ? `${KIND_LABEL[kind].toLowerCase()}${n === 1 ? '' : 's'}` : kind.replace('_', ' ')}
                </Badge>
              ))}
          </div>

          {(header.requires.logins || header.requires.keys || header.requires.inputs) && (
            <p className="text-xs text-secondary">
              {t("You will be asked to attach {{what}} — your own, not the sender's.", {
                what: [
                  header.requires.logins
                    ? t('{{count}} login(s)', { count: header.requires.logins })
                    : null,
                  header.requires.keys
                    ? t('{{count}} key(s)', { count: header.requires.keys })
                    : null,
                  header.requires.inputs
                    ? t('{{count}} input(s)', { count: header.requires.inputs })
                    : null,
                ].filter(Boolean).join(', '),
              })}
            </p>
          )}

          {/* Counts come from the package's UNENCRYPTED header, so we say so
              rather than implying we have read the contents. */}
          <p className="text-[11px] text-tertiary">
            {t("Read from the package's unencrypted summary. Names appear after you unlock it.")}
          </p>
        </div>
      )}
    </div>
  );
};

const UnlockStep: React.FC<{
  header: PackageHeaderSummary | null;
  passphrase: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  error: string | null;
  attemptsLeft: number;
  busy: boolean;
}> = ({ header, passphrase, onChange, onSubmit, error, attemptsLeft, busy }) => {
  const { t } = useTranslation();
  return (
  <div className="space-y-4">
    <Input
      type="password"
      label={t('Package passphrase')}
      value={passphrase}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => { if (e.key === 'Enter' && passphrase && !busy) onSubmit(); }}
      placeholder={t('The passphrase whoever exported it chose')}
      autoFocus
      disabled={busy || attemptsLeft === 0}
    />

    {/* Say plainly where decryption happens. Implying a zero-knowledge property
        the product does not have would be worse than the truth. */}
    <p className="text-xs text-secondary">
      {t("The package is decrypted on the server, which already holds this account's vault. Writ cannot recover a package's passphrase — if it is lost, the file cannot be opened.")}
    </p>

    {header?.has_sealed_credentials && (
      <div className="rounded-lg bg-amber-50 border border-amber-200 p-3 text-sm text-amber-900">
        <p className="font-medium">{t('This package contains credential values.')}</p>
        <p className="mt-0.5 text-xs">
          {t('Whoever created it chose to include {{count}} of their own logins or keys. You will choose which to accept, one by one — only open this if you trust the sender.', { count: header.sealed_credential_count })}
        </p>
      </div>
    )}

    {error && (
      <div className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-800">
        <p>{error}</p>
        {attemptsLeft > 0 && attemptsLeft < 5 && (
          <p className="mt-0.5 text-xs">
            {t('{{count}} attempt(s) left before you need to wait.', { count: attemptsLeft })}
          </p>
        )}
      </div>
    )}

    {busy && (
      <p className="text-xs text-secondary">
        {t('Deriving the key — this takes a moment by design, so a stolen package cannot be brute-forced quickly.')}
      </p>
    )}
  </div>
  );
};

const ReviewStep: React.FC<{
  summary: NonNullable<ImportState['summary']>;
  resolutions: Record<string, { action: Resolution; name?: string }>;
  onChange: (ref: string, next: { action: Resolution; name?: string }) => void;
}> = ({ summary, resolutions, onChange }) => {
  const { t } = useTranslation();
  const grouped = useMemo(() => {
    const map: Record<string, SummaryItem[]> = {};
    (summary.items || []).forEach((item) => {
      map[item.kind] = map[item.kind] || [];
      map[item.kind].push(item);
    });
    return map;
  }, [summary.items]);

  const collisions = (summary.items || []).filter((i) => i.collides && !i.block).length;
  const blocked = (summary.items || []).filter((i) => i.block).length;

  return (
    <div className="space-y-4">
      {(collisions > 0 || blocked > 0) && (
        <div className="flex flex-wrap gap-2 text-xs">
          {collisions > 0 && (
            <Badge variant="warning">
              {t('{{count}} already exist here', { count: collisions })}
            </Badge>
          )}
          {blocked > 0 && (
            <Badge variant="error">
              {t('{{count}} cannot be imported', { count: blocked })}
            </Badge>
          )}
        </div>
      )}

      {Object.entries(grouped).map(([kind, items]) => (
        <div key={kind}>
          <p className="text-xs font-semibold uppercase tracking-wide text-secondary mb-1.5">
            {KIND_LABEL[kind] || kind} · {items.length}
          </p>
          <div className="rounded-lg border border-border divide-y divide-border">
            {items.map((item) => {
              const choice = resolutions[item.ref]?.action || item.default_resolution;
              return (
                <div key={item.ref} className="p-3 flex items-start gap-3">
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-ink truncate">
                      {item.name}
                    </p>
                    {item.detail && (
                      <p className="text-xs text-secondary truncate">{item.detail}</p>
                    )}
                    {item.block && (
                      // Never a silent drop: the reason is always on screen.
                      <p className="text-xs text-red-600 mt-1">
                        {t('Will not import — {{reason}}', { reason: item.block.message })}
                      </p>
                    )}
                    {!item.block && item.collides && (
                      <p className="text-xs text-amber-600 mt-1">
                        {item.kind === 'monitors'
                          ? t('You already have one with this URL.')
                          : t('You already have one with this name.')}
                      </p>
                    )}
                    {item.dropped.length > 0 && (
                      <p className="text-[11px] text-tertiary mt-1">
                        {t('Settings that could not travel: {{list}}', { list: item.dropped.join(', ') })}
                      </p>
                    )}
                    {choice === 'rename' && !item.block && (
                      <Input
                        className="mt-2"
                        value={resolutions[item.ref]?.name ?? `${item.name} (imported)`}
                        onChange={(e) => onChange(item.ref, { action: 'rename', name: e.target.value })}
                        placeholder={t('New name')}
                      />
                    )}
                  </div>
                  {!item.block && (
                    <Select
                      value={choice}
                      onChange={(value) =>
                        onChange(item.ref, {
                          action: value as Resolution,
                          name: value === 'rename' ? `${item.name} (imported)` : undefined,
                        })
                      }
                      options={[
                        { value: 'import', label: t('Import') },
                        { value: 'rename', label: t('Import as a copy') },
                        { value: 'replace', label: t('Replace mine') },
                        { value: 'skip', label: t('Skip') },
                      ]}
                      className="w-40 shrink-0"
                    />
                  )}
                </div>
              );
            })}
          </div>
        </div>
      ))}

      {summary.marketplace_refs.length > 0 && (
        <div className="rounded-lg border border-border p-3">
          <p className="text-sm font-medium text-ink">
            {t('{{count}} item(s) came from the marketplace', { count: summary.marketplace_refs.length })}
          </p>
          <p className="text-xs text-secondary mt-0.5">
            {t("Those are not in the package — a creator's recipe does not travel in a file. Reinstall them from the marketplace: {{list}}", { list: summary.marketplace_refs.map((m) => m.name || m.slug).join(', ') })}
          </p>
        </div>
      )}

      {summary.unknown_kinds.length > 0 && (
        <p className="text-xs text-secondary">
          {t('Made by a newer version of Writ: {{list}} — skipped, because this install does not know how to import them.', { list: summary.unknown_kinds.join(', ') })}
        </p>
      )}

      {/* Cross-edition asymmetry, stated plainly. A cloud package can carry kinds a
          self-hosted install has no table for; the alternative to saying so is an
          import that looks complete while quietly leaving things behind. */}
      {summary.unsupported_kinds && Object.keys(summary.unsupported_kinds).length > 0 && (
        <div className="rounded-lg border border-border p-3">
          <p className="text-sm font-medium text-ink">
            {t('Some items have no equivalent on this install')}
          </p>
          <ul className="mt-1 text-xs text-secondary space-y-0.5">
            {Object.entries(summary.unsupported_kinds).map(([kind, n]) => (
              <li key={kind}>
                {t('{{count}} × {{kind}} — this install has no equivalent, so they are not imported.', {
                  count: n as number,
                  kind: KIND_LABEL[kind] || kind.replace('_', ' '),
                })}
              </li>
            ))}
          </ul>
        </div>
      )}

      {summary.skipped_by_producer.length > 0 && (
        <details className="text-xs text-secondary">
          <summary className="cursor-pointer">
            {t("{{count}} item(s) the sender's install left out", { count: summary.skipped_by_producer.length })}
          </summary>
          <ul className="mt-1 space-y-0.5 pl-4 list-disc">
            {summary.skipped_by_producer.map((s, i) => (
              <li key={i}>{s.name} — {s.detail || s.reason}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
};

const SealedStep: React.FC<{
  header: PackageHeaderSummary | null;
  summary: ImportState['summary'] | undefined;
  choices: Record<string, boolean>;
  onToggle: (key: string, on: boolean) => void;
}> = ({ header, summary, choices, onToggle }) => {
  const { t } = useTranslation();
  // The sealed lane's item KEYS are not in the summary (they are inside the
  // encrypted section, applied server-side), so this step works off the persona
  // and key slots the package declares — the same names the later steps use.
  const personaItems = (summary?.items || []).filter((i) => i.kind === 'personas');
  return (
    <div className="space-y-4">
      <div className="rounded-lg bg-amber-50 border border-amber-200 p-3 text-sm text-amber-900">
        <p className="font-medium">{t('The sender included their own credentials.')}</p>
        <p className="mt-0.5 text-xs">
          {t("Anything you accept is written into this account's vault and can be used by imported assets. Everything is off by default — accept only what you actually want, and remember the sender knows these values.")}
        </p>
      </div>

      {personaItems.map((item) => (
        <label
          key={item.ref}
          className="flex items-start gap-3 rounded-lg border border-border p-3 cursor-pointer"
        >
          <Checkbox
            checked={!!choices[`persona:${item.ref}`]}
            onChange={(e) => onToggle(`persona:${item.ref}`, e.target.checked)}
          />
          <span className="min-w-0">
            <span className="block text-sm font-medium text-ink">{item.name}</span>
            <span className="block text-xs text-secondary">
              {item.detail || 'Login'} ·{' '}
              {[
                item.needs.username ? 'username' : null,
                item.needs.password ? 'password' : null,
                item.needs.totp_seed ? 'two-factor seed' : null,
              ].filter(Boolean).join(', ') || 'credentials'}
            </span>
          </span>
        </label>
      ))}

      <p className="text-xs text-secondary">
        {t('Session cookies and proxy credentials are never included in a package, even here — they are tied to the device that made them.')}
      </p>
      {header && (
        <p className="text-[11px] text-tertiary">
          {t('Vault keys in this package are accepted on the next step, alongside your own.')}
        </p>
      )}
    </div>
  );
};

const LoginsStep: React.FC<{
  slots: NonNullable<ImportState['requirements']['persona_slots']>;
  personas: Persona[];
  bindings: Record<string, number | null>;
  onBind: (slot: string, id: number | null) => void;
}> = ({ slots, personas, bindings, onBind }) => {
  const { t } = useTranslation();
  if (!slots.length) {
    return <EmptyGroup icon={UserCircleIcon} text="Nothing in this package needs a login." />;
  }
  return (
    <div className="space-y-3">
      <p className="text-sm text-secondary">
        {t("These assets sign in somewhere. Attach one of YOUR logins — the sender's credentials are not in this package.")}
      </p>
      {slots.map((slot) => {
        const candidates = personas.filter(
          (p) => !slot.domain || !p.target_domain || p.target_domain.includes(slot.domain),
        );
        return (
          <div key={slot.slot} className="rounded-lg border border-border p-3">
            <p className="text-sm font-medium text-ink">
              {slot.domain || 'Login'}
            </p>
            {slot.covers_fields?.length ? (
              <p className="text-xs text-secondary mt-0.5">Covers: {slot.covers_fields.join(', ')}</p>
            ) : null}
            <Select
              className="mt-2"
              value={bindings[slot.slot] != null ? String(bindings[slot.slot]) : ''}
              onChange={(v) => onBind(slot.slot, v ? Number(v) : null)}
              options={[
                { value: '', label: 'Leave unattached (imports paused)' },
                ...(candidates.length ? candidates : personas).map((p) => ({
                  value: String(p.id),
                  label: p.target_domain ? `${p.name} — ${p.target_domain}` : p.name,
                })),
              ]}
            />
            {!personas.length && (
              <p className="text-xs text-amber-600 mt-1.5">
                {t('You have no logins yet. Import anyway and the assets arrive paused — attach a login later and they are ready.')}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
};

const KeysStep: React.FC<{
  slots: NonNullable<ImportState['requirements']['secret_slots']>;
  vaultKeys: { id: number; name: string }[];
  bindings: Record<string, string | null>;
  values: Record<string, string>;
  onBind: (key: string, existing: string | null) => void;
  onValue: (key: string, value: string) => void;
}> = ({ slots, vaultKeys, bindings, values, onBind, onValue }) => {
  const { t } = useTranslation();
  if (!slots.length) {
    return <EmptyGroup icon={KeyIcon} text="Nothing in this package needs a key." />;
  }
  const have = new Set(vaultKeys.map((k) => k.name));
  return (
    <div className="space-y-3">
      <p className="text-sm text-secondary">
        {t('The package carries the NAMES of the keys it needs, never their values. Point each one at your own vault entry, or paste a value now — it is saved to your vault, never back into the package.')}
      </p>
      {slots.map((slot) => {
        const matched = have.has(slot.key);
        const bound = bindings[slot.key] ?? (matched ? slot.key : '');
        return (
          <div key={slot.key} className="rounded-lg border border-border p-3">
            <div className="flex items-center justify-between gap-2">
              <p className="text-sm font-mono font-medium text-ink truncate">
                {slot.key}
              </p>
              {matched && (
                <Badge variant="success">
                  {t('Already in your vault')}
                </Badge>
              )}
            </div>
            {slot.used_by?.length ? (
              <p className="text-xs text-secondary mt-0.5">
                {t('Needed by {{count}} imported item(s)', { count: slot.used_by.length })}
              </p>
            ) : null}
            <Select
              className="mt-2"
              value={bound || ''}
              onChange={(v) => onBind(slot.key, v || null)}
              options={[
                { value: '', label: 'Leave unattached (imports paused)' },
                ...vaultKeys.map((k) => ({ value: k.name, label: k.name })),
              ]}
            />
            {!bound && (
              <Input
                className="mt-2"
                type="password"
                placeholder={`Or paste a value for ${slot.key}`}
                value={values[slot.key] || ''}
                onChange={(e) => onValue(slot.key, e.target.value)}
              />
            )}
          </div>
        );
      })}
    </div>
  );
};

const WiringStep: React.FC<{
  requirements: ImportState['requirements'] | undefined;
  summary: ImportState['summary'] | undefined;
  notify: Record<string, string[]>;
  onNotify: (slot: string, channels: string[]) => void;
  includeData: boolean;
  onIncludeData: (v: boolean) => void;
  armSchedules: boolean;
  onArmSchedules: (v: boolean) => void;
}> = ({ requirements, summary, notify, onNotify, includeData, onIncludeData, armSchedules, onArmSchedules }) => {
  const { t } = useTranslation();
  const notifySlots = requirements?.notify_slots || [];
  const dataRows = (summary?.data || []).reduce((n, d) => n + d.row_count, 0);
  const truncated = (summary?.data || []).filter((d) => d.truncated);

  return (
    <div className="space-y-4">
      {notifySlots.length > 0 && (
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-secondary mb-1.5">
            <BellAlertIcon className="w-3.5 h-3.5 inline mr-1" />
            {t('Where alerts go')}
          </p>
          <div className="space-y-2">
            {notifySlots.map((slot) => (
              <div key={slot.slot} className="rounded-lg border border-border p-3">
                <p className="text-sm text-ink">{slot.slot}</p>
                <p className="text-xs text-secondary mt-0.5">
                  {t('The sender used {{channels}}. Recipients are always yours — theirs are not in the package.', {
                    channels: slot.channels.join(', ') || t('no channel'),
                  })}
                </p>
                <div className="flex flex-wrap gap-2 mt-2">
                  {(slot.channels.length ? slot.channels : ['email']).map((channel) => {
                    const on = (notify[slot.slot] || []).includes(channel);
                    return (
                      <label key={channel} className="flex items-center gap-1.5 text-sm">
                        <Checkbox
                          checked={on}
                          onChange={(e) => {
                            const current = new Set(notify[slot.slot] || []);
                            if (e.target.checked) current.add(channel);
                            else current.delete(channel);
                            onNotify(slot.slot, Array.from(current));
                          }}
                        />
                        {channel}
                      </label>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {dataRows > 0 && (
        <div className="rounded-lg border border-border p-3">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-sm font-medium text-ink">
                {t('Bring the collected data too')}
              </p>
              <p className="text-xs text-secondary mt-0.5">
                {t('{{rows}} row(s) of results. Added alongside anything you already have — nothing is replaced.', { rows: formatNumber(dataRows),
                  count: dataRows })}
              </p>
              {truncated.length > 0 && (
                <p className="text-xs text-amber-600 mt-0.5">
                  {t("The sender's export hit its row cap, so some older rows are not in this package.")}
                </p>
              )}
            </div>
            <Switch checked={includeData} onChange={onIncludeData} />
          </div>
        </div>
      )}

      <div className="rounded-lg border border-border p-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-sm font-medium text-ink">
              <ClockIcon className="w-4 h-4 inline mr-1" />
              {t('Start schedules right away')}
            </p>
            {/* Off by default, and the reason is on screen: this is the setting
                most likely to surprise someone importing 30 assets. */}
            <p className="text-xs text-secondary mt-0.5">
              {t('Off by default. Imported monitors have no baseline yet, so running them immediately reports a change that did not happen. Turn schedules on per asset afterwards.')}
            </p>
          </div>
          <Switch checked={armSchedules} onChange={onArmSchedules} />
        </div>
      </div>

      <p className="text-xs text-secondary">
        {t('Webhook URLs are minted fresh on import — a package never carries a working inbound URL. You will find the new URLs on each imported automation.')}
      </p>
    </div>
  );
};

const ConfirmStep: React.FC<{
  readiness: ImportState['readiness'] | undefined;
  result: ImportState['result'];
  status: string | undefined;
  progress: { done: number; total: number | null; phase: string } | null;
  onUndo: () => void;
  busy: boolean;
}> = ({ readiness, result, status, progress, onUndo, busy }) => {
  const { t } = useTranslation();
  if (result) {
    const byOutcome = (result.assets || []).reduce<Record<string, typeof result.assets>>((acc, a) => {
      acc[a.outcome] = acc[a.outcome] || [];
      acc[a.outcome].push(a);
      return acc;
    }, {});
    const undone = status === 'undone';
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-2">
          <CheckCircleIcon className="w-5 h-5 text-emerald-500" />
          <p className="text-sm font-medium text-ink">
            {undone ? 'This import was undone.' : 'Imported.'}
          </p>
        </div>

        {Object.entries(byOutcome).map(([outcome, assets]) => (
          <div key={outcome}>
            <p className="text-xs font-semibold uppercase tracking-wide text-secondary mb-1">
              {OUTCOME_LABEL[outcome] || outcome} · {assets.length}
            </p>
            <ul className="text-sm space-y-0.5">
              {assets.slice(0, 12).map((a) => (
                <li key={`${a.kind}:${a.ref}`} className="flex items-start gap-2">
                  <span className="text-ink">{a.name}</span>
                  {a.reason && <span className="text-xs text-secondary">— {a.reason}</span>}
                </li>
              ))}
              {assets.length > 12 && (
                <li className="text-xs text-secondary">and {assets.length - 12} more</li>
              )}
            </ul>
          </div>
        ))}

        {byOutcome.paused?.length ? (
          <p className="text-xs text-amber-600">
            {t('Paused items are imported but inactive until you attach the login or key they need. They appear in Needs attention.')}
          </p>
        ) : null}

        {!undone && (
          <div className="pt-2 border-t border-border">
            <Button variant="ghost" onClick={onUndo} disabled={busy} loading={busy}>
              {t('Undo this import')}
            </Button>
            <p className="text-xs text-secondary mt-1">
              {t('Available for 24 hours. Anything that has since run or collected data is kept.')}
            </p>
          </div>
        )}
      </div>
    );
  }

  if (status === 'committing') {
    return (
      <div className="space-y-2">
        <p className="text-sm text-ink">Importing…</p>
        {progress && (
          <p className="text-xs text-secondary">
            {progress.done} done{progress.phase ? ` · ${progress.phase}` : ''}
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <Summary label={t('Will be created')} items={readiness?.will_create || []} tone="ok" />
      <Summary label={t('Will replace what you have')} items={readiness?.will_replace || []} tone="warn" />
      <Summary label={t('Skipped')} items={readiness?.will_skip || []} tone="muted" />
      <Summary label={t('Cannot be imported')} items={readiness?.blocked || []} tone="bad" />

      {readiness?.paused_refs.length ? (
        <div className="rounded-lg bg-amber-50 border border-amber-200 p-3 text-sm text-amber-900">
          {t('{{count}} item(s) will import PAUSED — a login or key was not attached. Nothing is lost; attach it later and they run.', { count: readiness.paused_refs.length })}
        </div>
      ) : null}

      {readiness?.data_rows ? (
        <p className="text-sm text-secondary">
          {t('Plus {{rows}} rows of collected data.', { rows: formatNumber(readiness.data_rows) })}
        </p>
      ) : null}

      <p className="text-xs text-secondary">
        {readiness?.arm_schedules
          ? t('Schedules will start immediately.')
          : t('Schedules stay off until you turn them on.')}
      </p>
    </div>
  );
};

const OUTCOME_LABEL: Record<string, string> = {
  created: 'Created',
  replaced: 'Replaced',
  paused: 'Imported, paused',
  skipped: 'Skipped',
  blocked: 'Not imported',
  failed: 'Failed',
};

const Summary: React.FC<{ label: string; items: { name: string }[]; tone: 'ok' | 'warn' | 'bad' | 'muted' }> = ({
  label,
  items,
  tone,
}) => {
  if (!items.length) return null;
  return (
    <div>
      <p
        className={clsx(
          'text-xs font-semibold uppercase tracking-wide mb-1',
          tone === 'ok' && 'text-emerald-600',
          tone === 'warn' && 'text-amber-600',
          tone === 'bad' && 'text-red-600',
          tone === 'muted' && 'text-secondary',
        )}
      >
        {label} · {items.length}
      </p>
      <p className="text-sm text-ink">
        {items.slice(0, 8).map((i) => i.name).join(', ')}
        {items.length > 8 ? `, and ${items.length - 8} more` : ''}
      </p>
    </div>
  );
};

const EmptyGroup: React.FC<{ icon: React.ComponentType<any>; text: string }> = ({ icon: Icon, text }) => (
  <div className="text-center py-8">
    <Icon className="w-8 h-8 mx-auto text-tertiary" />
    <p className="mt-2 text-sm text-secondary">{text}</p>
  </div>
);

export default ImportWizard;
