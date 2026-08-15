import React, { useEffect, useMemo, useState } from 'react';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import {
  CheckIcon, ArrowPathIcon, ChevronDownIcon, ChevronRightIcon,
  ShieldCheckIcon, EnvelopeIcon, QrCodeIcon,
  NoSymbolIcon, PlusIcon, TrashIcon, CheckBadgeIcon, FingerPrintIcon,
} from '@heroicons/react/24/outline';
import { useTranslation, Trans } from 'react-i18next';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { Checkbox, NumberInput, Select } from '../ui';
import { ConfirmDialog } from '../ConfirmDialog';
import { personasApi, agentsApi, vaultApi, automationApi } from '../../api/endpoints';
import type { Persona, PersonaCreate, PersonaUpdate, TwoFactorMethod, ImapConfig, Agent } from '../../types/api';
import { parseOtpauthUri, decodeQrFromImageFile, canDecodeQr, isMigrationUri, parseMigrationUri, type ParsedOtpauth } from '../../utils/otpauth';

// ---------------------------------------------------------------------------
// Steps
// ---------------------------------------------------------------------------
type StepId = 'identity' | 'login' | 'twofa' | 'execution' | 'review';
const STEPS: StepId[] = ['identity', 'login', 'twofa', 'execution', 'review'];
const STEP_LABELS: Record<StepId, string> = {
  identity: 'Identity',
  login: 'Login',
  twofa: '2FA',
  execution: 'Execution',
  review: 'Review',
};

interface ExtraField { key: string; value: string; secretKey?: string }

interface WizardForm {
  name: string;
  description: string;
  target_domain: string;
  login_username: string;
  password: string;
  /** When set, username + password are both linked to this vault credential secret. */
  loginSecretKey: string;
  extraFields: ExtraField[];
  twofa_method: TwoFactorMethod;
  totp_seed: string;
  totp_digits: number;
  totp_period_seconds: number;
  totp_algorithm: string;
  // Email OTP reads codes from a connected IMAP mailbox (BYO app password).
  email_otp_mode: 'oauth_mailbox';
  imap: ImapConfig;
  otpFrom: string;
  otpSubjectRegex: string;
  otpCodeRegex: string;
  otpMaxAge: string;
  preferred_agent_id: string;
  pinFingerprint: boolean;
  fingerprint: { user_agent: string; locale: string; timezone: string } | null;
  fingerprintTouched: boolean;
  is_active: boolean;
  /** Workflow that SIGNS THIS PERSONA IN. Null = it can't log itself in, so its
   * session can only ever be captured from elsewhere and an expiry is terminal. */
  login_workflow_id: number | null;
  // ── BYO/residential proxy ──
  proxy_server: string;
  proxy_username: string;
  proxy_password: string;
  /** The owner acknowledges lawful use of the configured proxy. */
  proxy_lawful_use_ack: boolean;
  /** Edit-mode: only re-send proxy config if the user actually touched it. */
  proxyTouched: boolean;
}

// Same pools the agents draw from — pinning one of these keeps the persona's
// "device" consistent across runs instead of being re-randomized.
const FP_USER_AGENTS = [
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
];
const FP_LOCALES = ['en-US', 'en-GB', 'en'];
const FP_TIMEZONES = ['America/New_York', 'America/Los_Angeles', 'America/Chicago', 'America/Toronto'];

function randomFingerprint() {
  const pick = <T,>(a: T[]) => a[Math.floor(Math.random() * a.length)];
  return { user_agent: pick(FP_USER_AGENTS), locale: pick(FP_LOCALES), timezone: pick(FP_TIMEZONES) };
}

function describeUserAgent(ua: string): string {
  const chrome = ua.match(/Chrome\/(\d+)/)?.[1];
  const os = ua.includes('Macintosh') ? 'macOS' : ua.includes('Windows') ? 'Windows' : 'Linux';
  return chrome ? `Chrome ${chrome} · ${os}` : os;
}

function normalizeDomain(raw: string): string {
  let d = raw.trim().toLowerCase();
  d = d.replace(/^[a-z]+:\/\//, '');
  d = d.split('/')[0].split('?')[0].split(':')[0];
  d = d.replace(/^www\./, '');
  return d;
}

function isBase32(s: string): boolean {
  return /^[A-Z2-7]+=*$/.test(s.replace(/\s/g, '').toUpperCase());
}

/** Create-mode prefill captured from a recording (domain/username/2FA channel). */
export interface PersonaPrefill {
  target_domain?: string;
  login_username?: string;
  twofa_method?: TwoFactorMethod;
}

function buildInitialForm(persona?: Persona | null, defaultDomain?: string, prefill?: PersonaPrefill): WizardForm {
  return {
    name: persona?.name || '',
    description: persona?.description || '',
    target_domain: persona?.target_domain || prefill?.target_domain || defaultDomain || '',
    login_username: persona?.login_username || prefill?.login_username || '',
    password: '',
    // A linked login reports the same base secret name for username + password.
    loginSecretKey: persona?.linked_secrets?.password || persona?.linked_secrets?.username || '',
    // On edit, surface any vault-linked extra fields so the link is preserved.
    extraFields: Object.entries(persona?.linked_secrets || {})
      .filter(([field]) => field !== 'password' && field !== 'username')
      .map(([field, secretKey]) => ({ key: field, value: '', secretKey })),
    // Self-host supports none/totp/email_otp only — clamp an unsupported hint
    // (the recorder prefills 'sms' when the site texts codes) so the 2FA step
    // never opens on a method with no card that the coordinator would 422.
    twofa_method: (() => {
      const m = persona?.twofa_method || prefill?.twofa_method || 'none';
      return m === 'sms' ? 'none' : m;
    })(),
    totp_seed: '',
    totp_digits: 6,
    totp_period_seconds: 30,
    totp_algorithm: 'SHA1',
    // Email OTP reads codes from a connected IMAP mailbox (BYO app password).
    email_otp_mode: 'oauth_mailbox',
    imap: { host: '', port: 993, username: '', password: '', use_ssl: true, mailbox: '' },
    otpFrom: '',
    otpSubjectRegex: '',
    otpCodeRegex: '',
    otpMaxAge: '',
    preferred_agent_id: persona?.preferred_agent_id || '',
    pinFingerprint: persona ? !!persona.has_fingerprint : true,
    fingerprint: persona ? null : randomFingerprint(),
    fingerprintTouched: false,
    is_active: persona ? !!persona.is_active : true,
    login_workflow_id: persona?.login_workflow_id ?? null,
    // Proxy secrets are write-only; on edit we only know whether one is set.
    proxy_server: '',
    proxy_username: '',
    proxy_password: '',
    // A stored proxy was already acknowledged, so the gate is satisfied on edit
    // until the user types a new proxy server (which clears proxyTouched logic).
    proxy_lawful_use_ack: persona ? !!persona.has_proxy : false,
    proxyTouched: false,
  };
}

// ---------------------------------------------------------------------------
// Small UI primitives (monochrome, matches the app design system)
// ---------------------------------------------------------------------------
const inputCls =
  'w-full px-3 py-2 text-sm border border-border rounded-lg bg-surface text-ink placeholder:text-tertiary focus:outline-none focus:ring-1 focus:ring-ink/10';

const Field: React.FC<{ label: string; hint?: React.ReactNode; required?: boolean; children: React.ReactNode }> = ({
  label, hint, required, children,
}) => (
  <div>
    <label className="text-xs font-medium text-secondary">
      {label}{required && <span className="text-tertiary"> *</span>}
    </label>
    <div className="mt-1">{children}</div>
    {hint && <p className="text-[11px] text-tertiary mt-1">{hint}</p>}
  </div>
);

const OptionCard: React.FC<{
  selected: boolean;
  onClick: () => void;
  icon?: React.ReactNode;
  title: string;
  description?: string;
  compact?: boolean;
}> = ({ selected, onClick, icon, title, description, compact }) => (
  <button
    type="button"
    onClick={onClick}
    className={clsx(
      'text-left border rounded-xl transition-all w-full',
      compact ? 'px-3 py-2' : 'px-3.5 py-3',
      selected ? 'border-ink bg-ink/[0.03] ring-1 ring-ink/20' : 'border-border bg-surface hover:bg-hover/50',
    )}
  >
    <div className="flex items-center gap-2">
      {icon && <span className={clsx('shrink-0', selected ? 'text-ink' : 'text-tertiary')}>{icon}</span>}
      <span className={clsx('text-[13px] font-medium', selected ? 'text-ink' : 'text-secondary')}>{title}</span>
      {selected && <CheckIcon className="w-3.5 h-3.5 text-ink ml-auto shrink-0" />}
    </div>
    {description && <p className="text-[11px] text-tertiary mt-1 leading-snug">{description}</p>}
  </button>
);

const Disclosure: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => {
  const [open, setOpen] = useState(false);
  return (
    <div className="border border-border rounded-lg overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-1.5 px-3 py-2 text-xs font-medium text-secondary hover:text-ink hover:bg-hover/50 transition-colors"
      >
        {open ? <ChevronDownIcon className="w-3.5 h-3.5" /> : <ChevronRightIcon className="w-3.5 h-3.5" />}
        {label}
      </button>
      {open && <div className="px-3 pb-3 pt-1 space-y-3 border-t border-border bg-canvas/50">{children}</div>}
    </div>
  );
};

const StepIndicator: React.FC<{
  current: StepId;
  completed: Set<StepId>;
  onJump: (s: StepId) => void;
}> = ({ current, completed, onJump }) => {
  const currentIndex = STEPS.indexOf(current);
  return (
    <nav className="flex items-center gap-1">
      {STEPS.map((step, index) => {
        const isCurrent = step === current;
        const isPast = index < currentIndex;
        const isClickable = isPast || completed.has(step);
        return (
          <React.Fragment key={step}>
            {index > 0 && <div className={clsx('h-px w-4 sm:w-6', isPast ? 'bg-ink' : 'bg-border')} />}
            <button
              type="button"
              onClick={() => isClickable && onJump(step)}
              disabled={!isClickable}
              className={clsx(
                'flex items-center gap-1.5 px-2 py-1 rounded-lg text-[11px] transition-all',
                isCurrent && 'bg-ink text-white font-medium',
                !isCurrent && isClickable && 'text-ink hover:bg-hover cursor-pointer',
                !isCurrent && !isClickable && 'text-tertiary cursor-default',
              )}
            >
              {isPast && !isCurrent ? (
                <CheckIcon className="w-3 h-3" />
              ) : (
                <span className={clsx(
                  'w-3.5 h-3.5 rounded-full text-[9px] flex items-center justify-center font-medium',
                  isCurrent ? 'bg-surface text-ink' : 'bg-hover text-tertiary',
                )}>
                  {index + 1}
                </span>
              )}
              <span className="hidden sm:inline">{STEP_LABELS[step]}</span>
            </button>
          </React.Fragment>
        );
      })}
    </nav>
  );
};

// ---------------------------------------------------------------------------
// Wizard
// ---------------------------------------------------------------------------
interface PersonaWizardProps {
  isOpen: boolean;
  onClose: () => void;
  /** Called with the created/updated persona. */
  onSaved: (persona: Persona) => void;
  /** Prefill the site domain (e.g. from a workflow's entry_url host). */
  defaultDomain?: string;
  /** Create-mode prefill from a recording (domain/username/detected 2FA channel). */
  prefill?: PersonaPrefill;
  /** Pass an existing persona to edit it; omit to create. */
  persona?: Persona | null;
}

export const PersonaWizard: React.FC<PersonaWizardProps> = ({ isOpen, onClose, onSaved, defaultDomain, prefill, persona }) => {
  const { t } = useTranslation();
  const isEdit = !!persona;
  const [step, setStep] = useState<StepId>('identity');
  const [completed, setCompleted] = useState<Set<StepId>>(new Set());
  const [form, setForm] = useState<WizardForm>(() => buildInitialForm(persona, defaultDomain, prefill));
  // Snapshot of the pristine form — the discard-guard compares against it. Seeded
  // from the same initial form so a wizard mounted already-open is never "dirty".
  const [initialJson, setInitialJson] = useState(() => JSON.stringify(form));
  const [saving, setSaving] = useState(false);
  const [phase, setPhase] = useState<'form' | 'success'>('form');
  const [created, setCreated] = useState<Persona | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [imapBusy, setImapBusy] = useState(false);
  // Edit-mode: reveal the IMAP form to replace an already-connected mailbox.
  const [imapReplace, setImapReplace] = useState(false);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [secrets, setSecrets] = useState<{ id: number; name: string; is_credential: boolean }[]>([]);
  const [loginWorkflowOptions, setLoginWorkflowOptions] = useState<any[]>([]);
  // On edit, only overwrite stored credentials if the user actually touched them.
  const [credsTouched, setCredsTouched] = useState(false);
  // Optional live check: confirm a pasted TOTP seed reproduces a code the user
  // reads from their own authenticator app (proves it's the RIGHT secret).
  const [totpVerifyCode, setTotpVerifyCode] = useState('');
  const [totpVerifyState, setTotpVerifyState] = useState<'idle' | 'checking' | 'match' | 'nomatch'>('idle');
  // When a Google Authenticator export holds >1 account, the user picks which one
  // to apply to THIS persona.
  const [migrationChoices, setMigrationChoices] = useState<ParsedOtpauth[] | null>(null);

  // Re-seed the whole wizard whenever it (re)opens or is pointed at a different
  // persona. Done DURING RENDER — React's derive-state-from-props escape hatch —
  // instead of from an effect: an effect would paint one frame of the PREVIOUS
  // persona's answers before the reset landed. The key goes null while closed so
  // reopening on the same persona re-seeds too.
  const seedKey = isOpen ? `${persona?.id ?? 'new'}|${defaultDomain ?? ''}` : null;
  const [seededFor, setSeededFor] = useState<string | null>(seedKey);
  if (seededFor !== seedKey) {
    setSeededFor(seedKey);
    if (isOpen) {
      const initial = buildInitialForm(persona, defaultDomain, prefill);
      setForm(initial);
      setInitialJson(JSON.stringify(initial));
      setStep('identity');
      setCompleted(new Set());
      setPhase('form');
      setCreated(null);
      setTestResult(null);
      setCredsTouched(false);
      setImapReplace(false);
      setMigrationChoices(null);
    }
  }

  // Reference data the wizard picks from. Fetched on open (writes land in the
  // promise continuations, so no render pass is wasted on them).
  useEffect(() => {
    if (!isOpen) return;
    let alive = true;
    agentsApi.getAll()
      .then((rows) => { if (alive) setAgents(rows); })
      .catch(() => { if (alive) setAgents([]); });
    // Candidates for "this persona's login workflow". Crawl workflows are already
    // excluded server-side, so this is the full list of things a login could be.
    automationApi.listWorkflows()
      .then((rows: any[]) => { if (alive) setLoginWorkflowOptions(rows || []); })
      .catch(() => { if (alive) setLoginWorkflowOptions([]); });
    vaultApi.listSecrets()
      .then((rows: any[]) => {
        if (alive) setSecrets((rows || []).map((s) => ({ id: s.id, name: s.name, is_credential: !!s.is_credential })));
      })
      .catch(() => { if (alive) setSecrets([]); });
    return () => { alive = false; };
  }, [isOpen, persona, defaultDomain]);

  const update = (patch: Partial<WizardForm>) => setForm((f) => ({ ...f, ...patch }));
  // Wrap credential edits so edit-mode knows the stored set should be replaced.
  const updateCreds = (patch: Partial<WizardForm>) => { setCredsTouched(true); update(patch); };

  // Reset the verify check whenever the seed itself changes (a stale green check
  // must not carry over to a different secret). Re-keyed during render so the
  // check can never be shown against a seed it wasn't run for.
  const [verifiedSeed, setVerifiedSeed] = useState(form.totp_seed);
  if (verifiedSeed !== form.totp_seed) {
    setVerifiedSeed(form.totp_seed);
    setTotpVerifyState('idle');
    setTotpVerifyCode('');
  }

  const verifyTotpCode = async (code: string) => {
    setTotpVerifyCode(code);
    const digits = code.replace(/\D/g, '');
    if (!form.totp_seed.trim() || !isBase32(form.totp_seed) || digits.length < (form.totp_digits || 6)) {
      setTotpVerifyState('idle');
      return;
    }
    setTotpVerifyState('checking');
    try {
      const res = await personasApi.validateTotpSeed({
        totp_seed: form.totp_seed.trim(), code: digits,
        algorithm: form.totp_algorithm, digits: form.totp_digits, period: form.totp_period_seconds,
      });
      setTotpVerifyState(res.matches_code ? 'match' : 'nomatch');
    } catch {
      setTotpVerifyState('idle');
    }
  };
  // Apply one parsed TOTP account to the form: fill the seed + parameters, and
  // pre-fill name/domain from the issuer/label when those fields are still empty.
  const applyTotpEntry = (e: ParsedOtpauth) => {
    const patch: Partial<WizardForm> = {
      totp_seed: e.secret,
      totp_digits: e.digits,
      totp_period_seconds: e.period,
      totp_algorithm: e.algorithm,
    };
    if (!form.name.trim()) {
      patch.name = e.issuer && e.label ? `${e.issuer} — ${e.label}` : (e.issuer || e.label || '');
    }
    if (!form.target_domain.trim()) {
      const host = (e.issuer || '').toLowerCase().includes('.')
        ? (e.issuer as string).toLowerCase()
        : ((e.label || '').includes('@') ? (e.label as string).split('@')[1] : '');
      if (host) patch.target_domain = host;
    }
    update(patch);
    setMigrationChoices(null);
  };

  // Handle a scanned/pasted otpauth string. Single otpauth:// fills directly; a
  // Google export (otpauth-migration://) fills directly when it has one account,
  // otherwise opens a picker. Returns true if it was recognized as an OTP payload.
  const ingestOtpInput = (raw: string): boolean => {
    const s = (raw || '').trim();
    if (isMigrationUri(s) || /otpauth-migration/i.test(s)) {
      const list = parseMigrationUri(s);
      if (!list.length) {
        toast.error(t('Couldn’t read any TOTP accounts from that Google Authenticator export.'));
        return true;
      }
      if (list.length === 1) {
        applyTotpEntry(list[0]);
        toast.success(t('Filled the TOTP secret from the export.'));
      } else {
        setMigrationChoices(list);
      }
      return true;
    }
    const parsed = parseOtpauthUri(s);
    if (parsed) {
      applyTotpEntry(parsed);
      toast.success(t('Read the TOTP secret from the pasted link.'));
      return true;
    }
    return false;
  };

  // Credential (login pair) secrets fill username+password; single-value secrets back extra fields.
  const credentialSecrets = secrets.filter((s) => s.is_credential);
  const valueSecrets = secrets.filter((s) => !s.is_credential);

  const handleClose = () => {
    if (phase === 'form' && !saving && JSON.stringify(form) !== initialJson) {
      setConfirmDiscard(true);
      return;
    }
    onClose();
  };

  // --- Validation -----------------------------------------------------------
  const stepError = (s: StepId): string | null => {
    if (s === 'identity') {
      if (!form.name.trim()) return t('Give this persona a name.');
    }
    if (s === 'login') {
      // A linked login carries its own username; only a literal password needs one.
      if (form.password && !form.loginSecretKey && !form.login_username.trim() && !(isEdit && persona?.login_username)) {
        return t('Add the login username/email that goes with this password.');
      }
      // A field needs a value OR a linked secret.
      if (form.extraFields.some((f) => f.key.trim() && !f.value.trim() && !f.secretKey)) {
        return t('Fill in a value (or link a secret) for every extra login field, or remove it.');
      }
    }
    if (s === 'twofa') {
      if (form.twofa_method === 'totp') {
        if (!form.totp_seed.trim() && !(isEdit && persona?.has_totp_seed)) return t('Paste the TOTP secret from the site’s authenticator setup.');
        if (form.totp_seed.trim() && !isBase32(form.totp_seed)) return t('That doesn’t look like a base32 TOTP secret (A–Z and 2–7 only).');
      }
      // Email-OTP reads codes from a connected IMAP mailbox. On create the IMAP
      // details are required; on edit an already-connected mailbox needs no re-entry.
      if (!isEdit && form.twofa_method === 'email_otp') {
        if (!form.imap.host.trim() || !form.imap.username.trim() || !form.imap.password.trim()) {
          return t('IMAP needs host, username and an app password.');
        }
      }
    }
    if (s === 'execution') {
      // A configured BYO/residential proxy requires the lawful-use acknowledgment.
      if (form.proxy_server.trim() && !form.proxy_lawful_use_ack) {
        return t('Acknowledge lawful use of this proxy before continuing.');
      }
    }
    return null;
  };

  const currentError = stepError(step);

  const goNext = () => {
    if (currentError) { toast.error(currentError); return; }
    const i = STEPS.indexOf(step);
    setCompleted((c) => new Set(c).add(step));
    if (i < STEPS.length - 1) setStep(STEPS[i + 1]);
  };
  const goBack = () => {
    const i = STEPS.indexOf(step);
    if (i > 0) setStep(STEPS[i - 1]);
  };
  const jumpTo = (s: StepId) => setStep(s);

  // --- Payload builders -----------------------------------------------------
  // A linked login stores {{vault:key.password}} / {{vault:key.username}} refs;
  // a literal stores the typed value.
  const passwordPayload = (): string | undefined =>
    form.loginSecretKey ? `{{vault:${form.loginSecretKey}.password}}` : (form.password || undefined);
  const usernamePayload = (): string | undefined =>
    form.loginSecretKey ? `{{vault:${form.loginSecretKey}.username}}` : (form.login_username.trim() || undefined);

  const extraFieldsObject = (): Record<string, string> | undefined => {
    const entries = form.extraFields.filter((f) => f.key.trim() && (f.value.trim() || f.secretKey));
    if (!entries.length) return undefined;
    return Object.fromEntries(entries.map((f) => [
      f.key.trim(),
      f.secretKey ? `{{vault:${f.secretKey}}}` : f.value,
    ]));
  };

  const otpExtractConfig = (): Record<string, any> | undefined => {
    if (form.twofa_method !== 'email_otp') return undefined;
    const cfg: Record<string, any> = {};
    if (form.otpFrom.trim()) cfg.from_filter = form.otpFrom.trim();
    if (form.otpSubjectRegex.trim()) cfg.subject_regex = form.otpSubjectRegex.trim();
    if (form.otpCodeRegex.trim()) cfg.code_regex = form.otpCodeRegex.trim();
    if (form.otpMaxAge.trim() && !isNaN(Number(form.otpMaxAge))) cfg.max_age_seconds = Number(form.otpMaxAge);
    return Object.keys(cfg).length ? cfg : undefined;
  };

  const cleanSeed = () => form.totp_seed.replace(/\s/g, '').toUpperCase();

  // --- Save -----------------------------------------------------------------
  const save = async () => {
    for (const s of STEPS) {
      const err = stepError(s);
      if (err) { setStep(s); toast.error(err); return; }
    }
    setSaving(true);
    try {
      if (!isEdit) {
        const payload: PersonaCreate = {
          name: form.name.trim(),
          description: form.description.trim() || undefined,
          target_domain: normalizeDomain(form.target_domain) || undefined,
          login_username: usernamePayload(),
          password: passwordPayload(),
          extra_login_fields: extraFieldsObject(),
          twofa_method: form.twofa_method,
          preferred_agent_id: form.preferred_agent_id || undefined,
          fingerprint: form.pinFingerprint && form.fingerprint ? form.fingerprint : undefined,
          login_workflow_id: form.login_workflow_id ?? undefined,
        };
        if (form.proxy_server.trim()) {
          payload.proxy_server = form.proxy_server.trim();
          payload.proxy_username = form.proxy_username.trim() || undefined;
          payload.proxy_password = form.proxy_password || undefined;
          payload.proxy_lawful_use_ack = form.proxy_lawful_use_ack;
        }
        if (form.twofa_method === 'totp') {
          payload.totp_seed = cleanSeed();
          payload.totp_digits = form.totp_digits;
          payload.totp_period_seconds = form.totp_period_seconds;
          payload.totp_algorithm = form.totp_algorithm;
        }
        if (form.twofa_method === 'email_otp') {
          payload.email_otp_mode = 'oauth_mailbox';
          payload.otp_extract_config = otpExtractConfig();
        }

        const p = await personasApi.create(payload);

        // IMAP mailbox hookup for email-OTP (the persona must exist first).
        if (form.twofa_method === 'email_otp' && form.imap.host.trim() && form.imap.username.trim() && form.imap.password.trim()) {
          try {
            await personasApi.setImap(p.id, {
              ...form.imap,
              port: form.imap.port || undefined,
              mailbox: form.imap.mailbox || undefined,
            } as ImapConfig);
            toast.success(t('IMAP mailbox connected'));
          } catch (err: any) {
            const d = err?.response?.data?.detail;
            toast.error(typeof d === 'string' ? d : t('Persona created, but the IMAP connection failed — retry from the Personas page.'));
          }
        }

        toast.success(t('Persona created'));
        onSaved(p);
        setCreated(p);
        setPhase('success');
      } else {
        const data: PersonaUpdate = {
          name: form.name.trim(),
          description: form.description.trim() || (null as any),
          target_domain: normalizeDomain(form.target_domain) || (null as any),
          // Linked → username ref; literal → typed; cleared → null.
          login_username: usernamePayload() ?? (null as any),
          twofa_method: form.twofa_method,
          preferred_agent_id: form.preferred_agent_id || (null as any),
          is_active: form.is_active,
          // Explicit null DETACHES; the coordinator distinguishes absent from null.
          login_workflow_id: form.login_workflow_id ?? (null as any),
        };
        // Secrets: only overwrite when the user actually changed a credential
        // (typed a value, or linked/unlinked a secret). Saving replaces the
        // whole stored credential set.
        if (credsTouched) {
          const pw = passwordPayload();
          const extras = extraFieldsObject();
          const wasLoginLinked = !!(persona?.linked_secrets?.password || persona?.linked_secrets?.username);
          if (pw) data.password = pw;
          // Unlinked a previously-linked login → overwrite the stored vault ref
          // (otherwise the old {{vault:..password}} would persist).
          else if (wasLoginLinked && !form.loginSecretKey) data.password = form.password || '';
          if (extras) data.extra_login_fields = extras;
        }
        if (form.twofa_method === 'totp') {
          if (form.totp_seed.trim()) data.totp_seed = cleanSeed();
          data.totp_digits = form.totp_digits;
          data.totp_period_seconds = form.totp_period_seconds;
          data.totp_algorithm = form.totp_algorithm;
        }
        if (form.twofa_method === 'email_otp') {
          data.email_otp_mode = 'oauth_mailbox';
          const cfg = otpExtractConfig();
          if (cfg) data.otp_extract_config = cfg;
          // IMAP credentials are connected via the "Connect IMAP now" button (setImap),
          // not sent in this PATCH.
        }
        // Proxy: only re-send when the user actually edited it. Sending an empty
        // proxy_server explicitly clears the stored proxy on the backend; an
        // untouched edit leaves the stored proxy intact.
        if (form.proxyTouched) {
          data.proxy_server = form.proxy_server.trim();
          if (form.proxy_server.trim()) {
            data.proxy_username = form.proxy_username.trim() || undefined;
            data.proxy_password = form.proxy_password || undefined;
            data.proxy_lawful_use_ack = form.proxy_lawful_use_ack;
          }
        }
        // Fingerprint: keep silently unless changed.
        if (form.pinFingerprint && form.fingerprint && form.fingerprintTouched) data.fingerprint = form.fingerprint;
        if (!form.pinFingerprint && persona?.has_fingerprint) data.fingerprint = null as any;
        if (form.pinFingerprint && !persona?.has_fingerprint && form.fingerprint) data.fingerprint = form.fingerprint;

        const p = await personasApi.update(persona!.id, data);
        toast.success(t('Persona updated'));
        onSaved(p);
        onClose();
      }
    } catch (err: any) {
      const d = err?.response?.data?.detail;
      toast.error(typeof d === 'string' ? d : (isEdit ? t('Failed to update persona (Personas require a Growth or Scale plan)') : t('Failed to create persona (Personas require a Growth or Scale plan)')));
    } finally {
      setSaving(false);
    }
  };

  // --- Edit-mode IMAP connect (persona already exists) ----------------------
  const connectImapNow = async () => {
    if (!persona) return;
    if (!form.imap.host.trim() || !form.imap.username.trim() || !form.imap.password.trim()) {
      toast.error(t('IMAP needs host, username and an app password')); return;
    }
    setImapBusy(true);
    try {
      const p = await personasApi.setImap(persona.id, {
        ...form.imap,
        port: form.imap.port || undefined,
        mailbox: form.imap.mailbox || undefined,
      } as ImapConfig);
      toast.success(t('IMAP mailbox connected'));
      onSaved(p);
    } catch (err: any) {
      const d = err?.response?.data?.detail;
      toast.error(typeof d === 'string' ? d : t('IMAP connection failed'));
    } finally { setImapBusy(false); }
  };

  const runTest2fa = async () => {
    const target = created || persona;
    if (!target) return;
    setTesting(true);
    setTestResult(null);
    try {
      const r = await personasApi.test2fa(target.id);
      setTestResult(r.message || t('2FA OK ({{detail}})', { detail: `${r.method}${r.kind ? `, ${r.kind}` : ''}` }));
    } catch (err: any) {
      const d = err?.response?.data?.detail;
      setTestResult(typeof d === 'string' ? d : t('2FA test failed'));
    } finally { setTesting(false); }
  };

  // --- Step renderers ---------------------------------------------------------
  const renderIdentity = () => (
    <div className="space-y-4">
      <p className="text-[12px] text-secondary leading-relaxed">
        {t('A persona is a reusable identity for one site: a login, its 2FA, a consistent device fingerprint and a warm session. Workflows that use it log in automatically on every run.')}
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <Field label={t('Name')} required hint={t('A label you’ll recognize, e.g. “Acme prod login”.')}>
          <input className={inputCls} value={form.name} onChange={(e) => update({ name: e.target.value })} placeholder={t('Acme prod login')} autoFocus />
        </Field>
        <Field
          label={t('Site domain')}
          hint={form.target_domain ? t('Suggested for workflows on {{domain}} and its subdomains.', { domain: normalizeDomain(form.target_domain) || '…' }) : t('Used to suggest this persona for matching workflows.')}
        >
          <input
            className={inputCls}
            value={form.target_domain}
            onChange={(e) => update({ target_domain: e.target.value })}
            onBlur={() => update({ target_domain: normalizeDomain(form.target_domain) })}
            placeholder="example.com"
          />
        </Field>
      </div>
      <Field label={t('Description')} hint={t('Optional — what account is this, who owns it, anything future-you should know.')}>
        <textarea
          className={`${inputCls} resize-none`}
          rows={2}
          value={form.description}
          onChange={(e) => update({ description: e.target.value })}
          placeholder={t('Shared staging account for the pricing checks')}
        />
      </Field>
    </div>
  );

  const renderLogin = () => (
    <div className="space-y-4">
      {form.loginSecretKey ? (
        // Login linked to a saved credential — fills both username + password.
        <div>
          <label className="text-xs font-medium text-secondary block mb-1">{t('Login')}</label>
          <div className="flex items-center gap-2 px-3 py-2.5 border border-border rounded-lg bg-canvas/60">
            <ShieldCheckIcon className="w-4 h-4 text-secondary shrink-0" />
            <div className="min-w-0 flex-1">
              <div className="text-sm text-ink font-mono truncate">{form.loginSecretKey}</div>
              <div className="text-[11px] text-tertiary">{t('Email/username + password from this saved login, resolved at run time.')}</div>
            </div>
            <button type="button" onClick={() => updateCreds({ loginSecretKey: '' })} className="text-[11px] text-secondary hover:text-ink shrink-0">{t('Unlink')}</button>
          </div>
        </div>
      ) : (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <Field label={t('Login username / email')}>
              <input className={inputCls} value={form.login_username} onChange={(e) => updateCreds({ login_username: e.target.value })} placeholder="you@example.com" />
            </Field>
            <Field
              label={t('Password')}
              hint={isEdit && persona?.has_password ? t('A password is stored (encrypted). Leave blank to keep it.') : t('Stored encrypted, never shown again.')}
            >
              <input className={inputCls} type="password" value={form.password} onChange={(e) => updateCreds({ password: e.target.value })} placeholder={isEdit && persona?.has_password ? t('•••••••• (unchanged)') : '••••••••'} autoComplete="new-password" />
            </Field>
          </div>
          {credentialSecrets.length > 0 && (
            <div className="flex items-center gap-2">
              <span className="text-[11px] text-tertiary shrink-0">{t('…or use a saved login:')}</span>
              <Select
                size="sm"
                value=""
                onChange={(v) => v && updateCreds({ loginSecretKey: v, login_username: '', password: '' })}
                placeholder={t('Pick a saved login credential')}
                options={credentialSecrets.map((s) => ({ value: s.name, label: s.name }))}
              />
            </div>
          )}
        </>
      )}

      <div>
        <div className="flex items-center justify-between">
          <label className="text-xs font-medium text-secondary">{t('Extra login fields')}</label>
          <button
            type="button"
            onClick={() => updateCreds({ extraFields: [...form.extraFields, { key: '', value: '' }] })}
            className="flex items-center gap-1 text-[11px] text-secondary hover:text-ink px-2 py-1 rounded-lg hover:bg-hover"
          >
            <PlusIcon className="w-3 h-3" /> {t('Add field')}
          </button>
        </div>
        <p className="text-[11px] text-tertiary mt-0.5">
          <Trans i18nKey="For sites that ask for more than username + password (account ID, company code, security answer…). Enter a value or link a saved secret — available in login steps as <1>{{secretRef}}</1>." values={{ secretRef: '{{secret:key}}' }}>
            For sites that ask for more than username + password (account ID, company code, security answer…).
            Enter a value or link a saved secret — available in login steps as <span className="font-mono">{'{{secret:key}}'}</span>.
          </Trans>
        </p>
        {form.extraFields.length > 0 && (
          <div className="mt-2 space-y-2">
            {form.extraFields.map((f, i) => (
              <div key={i} className="flex items-center gap-2">
                <input
                  className={`${inputCls} w-40 font-mono text-xs`}
                  value={f.key}
                  onChange={(e) => updateCreds({ extraFields: form.extraFields.map((x, j) => j === i ? { ...x, key: e.target.value } : x) })}
                  placeholder="account_id"
                />
                {f.secretKey ? (
                  <div className="flex-1 flex items-center gap-2 px-3 py-2 border border-border rounded-lg bg-canvas/60 min-w-0">
                    <ShieldCheckIcon className="w-4 h-4 text-secondary shrink-0" />
                    <span className="text-sm text-ink font-mono truncate flex-1">{f.secretKey}</span>
                    <button type="button" onClick={() => updateCreds({ extraFields: form.extraFields.map((x, j) => j === i ? { ...x, secretKey: undefined } : x) })} className="text-[11px] text-secondary hover:text-ink shrink-0">{t('Unlink')}</button>
                  </div>
                ) : (
                  <div className="flex-1 flex items-center gap-2 min-w-0">
                    <input
                      className={inputCls}
                      type="password"
                      value={f.value}
                      onChange={(e) => updateCreds({ extraFields: form.extraFields.map((x, j) => j === i ? { ...x, value: e.target.value } : x) })}
                      placeholder="value"
                      autoComplete="new-password"
                    />
                    {valueSecrets.length > 0 && (
                      <Select
                        size="sm"
                        className="w-28 shrink-0"
                        value=""
                        onChange={(v) => v && updateCreds({ extraFields: form.extraFields.map((x, j) => j === i ? { ...x, secretKey: v, value: '' } : x) })}
                        placeholder={t('Link…')}
                        aria-label={t('Link a saved secret')}
                        options={valueSecrets.map((s) => ({ value: s.name, label: s.name }))}
                      />
                    )}
                  </div>
                )}
                <button
                  type="button"
                  onClick={() => updateCreds({ extraFields: form.extraFields.filter((_, j) => j !== i) })}
                  className="p-1.5 text-tertiary hover:text-ink hover:bg-hover rounded-lg shrink-0"
                >
                  <TrashIcon className="w-4 h-4" />
                </button>
              </div>
            ))}
          </div>
        )}
        {isEdit && persona?.has_password && credsTouched && (
          <p className="text-[11px] text-tertiary mt-2 border border-border rounded-lg px-3 py-2 bg-canvas/60">
            {t('Heads-up: saving new credentials replaces the whole stored credential set — re-enter every field this persona needs (including linked secrets), not just the changed one.')}
          </p>
        )}
      </div>

      {/* HOW THIS PERSONA SIGNS IN. Credentials alone never produced a session —
          they are only values a login flow types in. Without a workflow that
          actually performs the login, the persona can never authenticate a crawl,
          so this belongs on the Login step next to the credentials themselves. */}
      <div className="border-t border-border pt-4">
        <label className="text-xs font-medium text-secondary block mb-1">{t('Login workflow')}</label>
        <Select<string>
          value={form.login_workflow_id == null ? '' : String(form.login_workflow_id)}
          onChange={(v) => update({ login_workflow_id: v ? Number(v) : null })}
          options={[
            { value: '', label: t('None — this persona can\'t sign itself in') },
            ...loginWorkflowOptions.map((w: any) => ({ value: String(w.id), label: w.name })),
          ]}
        />
        <p className="text-[11px] text-tertiary mt-1.5">
          {form.login_workflow_id
            ? t('Runs whenever this persona needs a fresh session — including automatically mid-crawl.')
            : isEdit
              ? t('Pick the workflow that logs this account in, or record one from the persona once saved.')
              : t('Pick the workflow that logs this account in. You can also record one after saving.')}
        </p>
      </div>
    </div>
  );

  const mailboxConnected = isEdit && !!persona?.connected_mailbox;

  const renderTwofa = () => (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2">
        <OptionCard
          selected={form.twofa_method === 'none'}
          onClick={() => update({ twofa_method: 'none' })}
          icon={<NoSymbolIcon className="w-4 h-4" />}
          title={t('No 2FA')}
          description={t('The account signs in with just a password.')}
        />
        <OptionCard
          selected={form.twofa_method === 'totp'}
          onClick={() => update({ twofa_method: 'totp' })}
          icon={<QrCodeIcon className="w-4 h-4" />}
          title={t('Authenticator app')}
          description={t('Rotating 6-digit codes (TOTP). We generate them server-side.')}
        />
        <OptionCard
          selected={form.twofa_method === 'email_otp'}
          onClick={() => update({ twofa_method: 'email_otp' })}
          icon={<EnvelopeIcon className="w-4 h-4" />}
          title={t('Email code')}
          description={t('The site emails a code or magic link — we read it from a connected mailbox.')}
        />
      </div>

      {form.twofa_method === 'totp' && (
        <div className="space-y-3">
          {/* Reuse an EXISTING secret — scan the authenticator QR or paste the
              otpauth:// setup link and we fill the seed + parameters for you.
              The secret is in the URI, so this stays entirely in the browser. */}
          <label className="flex items-center gap-1.5 px-3 py-1.5 w-max text-[12px] font-medium rounded-lg bg-surface text-ink border border-border hover:bg-hover transition-colors cursor-pointer">
            <QrCodeIcon className="w-4 h-4" /> {t('Scan QR / paste setup link')}
            <input
              type="file"
              accept="image/*"
              className="hidden"
              onChange={async (e) => {
                const file = e.target.files?.[0];
                e.target.value = '';
                if (!file) return;
                if (!canDecodeQr()) {
                  toast.error(t('This browser can’t scan QR images — paste the otpauth:// link below instead.'));
                  return;
                }
                const decoded = await decodeQrFromImageFile(file);
                if (!decoded || !ingestOtpInput(decoded)) {
                  toast.error(t('Couldn’t read a TOTP secret from that QR code.'));
                }
              }}
            />
          </label>
          <p className="text-[11px] text-tertiary">
            {t('Works with a single site’s QR/link or a Google Authenticator “Export accounts” QR (pick which account to use).')}
          </p>
          <Field
            label={t('TOTP secret (base32)')}
            required={!(isEdit && persona?.has_totp_seed)}
            hint={isEdit && persona?.has_totp_seed
              ? t('A seed is stored. Leave blank to keep it, or paste a new one to replace it.')
              : t('Paste the base32 secret OR the whole otpauth:// link. On the site’s 2FA setup page, choose “can’t scan the QR code” to reveal the secret.')}
          >
            <input
              className={`${inputCls} font-mono`}
              value={form.totp_seed}
              onChange={(e) => {
                const v = e.target.value;
                // Pasting a full otpauth:// link or a Google Authenticator export
                // (otpauth-migration://) auto-expands into seed + params.
                if (/^otpauth(-migration)?:\/\//i.test(v.trim())) {
                  if (ingestOtpInput(v.trim())) return;
                }
                update({ totp_seed: v });
              }}
              placeholder={isEdit && persona?.has_totp_seed ? t('(unchanged)') : 'JBSWY3DPEHPK3PXP'}
            />
          </Field>

          {migrationChoices && migrationChoices.length > 1 && (
            <div className="rounded-lg border border-border bg-hover/40 p-3 space-y-2">
              <div className="flex items-center justify-between">
                <p className="text-[12px] font-medium text-ink">
                  {t('{{n}} accounts in that export — pick the one for this persona:', { n: migrationChoices.length })}
                </p>
                <button
                  type="button"
                  onClick={() => setMigrationChoices(null)}
                  className="text-[11px] text-tertiary hover:text-ink"
                >
                  {t('Cancel')}
                </button>
              </div>
              <div className="space-y-1 max-h-52 overflow-y-auto">
                {migrationChoices.map((acc, i) => (
                  <button
                    key={i}
                    type="button"
                    onClick={() => applyTotpEntry(acc)}
                    className="w-full flex items-center gap-2 px-3 py-2 text-left rounded-lg border border-border bg-surface hover:border-ink/30 transition-colors"
                  >
                    <ShieldCheckIcon className="w-4 h-4 text-tertiary shrink-0" />
                    <span className="min-w-0">
                      <span className="block text-[13px] text-ink truncate">{acc.issuer || acc.label || t('Account {{i}}', { i: i + 1 })}</span>
                      {acc.issuer && acc.label && <span className="block text-[11px] text-tertiary truncate">{acc.label}</span>}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
          {form.totp_seed.trim() && !isBase32(form.totp_seed) && (
            <p className="text-[11px] text-ink">{t('That doesn’t look like base32 — expected only A–Z and 2–7.')}</p>
          )}
          {form.totp_seed.trim() && isBase32(form.totp_seed) && (
            <div className="flex items-center gap-2">
              <input
                className={`${inputCls} font-mono w-32`}
                value={totpVerifyCode}
                inputMode="numeric"
                maxLength={8}
                onChange={(e) => verifyTotpCode(e.target.value)}
                placeholder={t('Code from app')}
              />
              {totpVerifyState === 'idle' && (
                <span className="text-[11px] text-tertiary">
                  {t('Optional: type the current code from your authenticator to confirm the secret.')}
                </span>
              )}
              {totpVerifyState === 'checking' && <span className="text-[11px] text-tertiary">{t('Checking…')}</span>}
              {totpVerifyState === 'match' && (
                <span className="inline-flex items-center gap-1 text-[11px] text-ink">
                  <CheckBadgeIcon className="w-4 h-4" /> {t('Verified — codes match')}
                </span>
              )}
              {totpVerifyState === 'nomatch' && (
                <span className="text-[11px] text-ink">{t('Doesn’t match — double-check the secret.')}</span>
              )}
            </div>
          )}
          <Disclosure label={t('Advanced TOTP settings (rarely needed)')}>
            <div className="grid grid-cols-3 gap-3">
              <Field label={t('Digits')}>
                <Select<number>
                  value={form.totp_digits}
                  onChange={(v) => update({ totp_digits: v })}
                  options={[{ value: 6, label: '6' }, { value: 8, label: '8' }]}
                />
              </Field>
              <Field label={t('Period (s)')}>
                <Select<number>
                  value={form.totp_period_seconds}
                  onChange={(v) => update({ totp_period_seconds: v })}
                  options={[
                    { value: 15, label: '15' },
                    { value: 30, label: '30' },
                    { value: 60, label: '60' },
                  ]}
                />
              </Field>
              <Field label={t('Algorithm')}>
                <Select
                  value={form.totp_algorithm}
                  onChange={(v) => update({ totp_algorithm: v })}
                  options={[
                    { value: 'SHA1', label: 'SHA1' },
                    { value: 'SHA256', label: 'SHA256' },
                    { value: 'SHA512', label: 'SHA512' },
                  ]}
                />
              </Field>
            </div>
          </Disclosure>
        </div>
      )}

      {form.twofa_method === 'email_otp' && (
        <div className="space-y-3">
          {/* Email OTP reads codes from a connected IMAP mailbox (BYO app password). */}
          {mailboxConnected && !imapReplace ? (
            <div className="flex items-center justify-between border border-border rounded-lg px-3 py-2.5 bg-canvas/50">
              <span className="flex items-center gap-1.5 text-xs text-secondary">
                <CheckBadgeIcon className="w-4 h-4 text-ink" />
                {t('Connected: {{mailbox}}', { mailbox: persona!.connected_mailbox })}
              </span>
              <button
                type="button"
                onClick={() => setImapReplace(true)}
                className="text-[11px] text-secondary hover:text-ink shrink-0"
              >
                {t('Replace mailbox')}
              </button>
            </div>
          ) : (
            <div className="border border-border rounded-lg p-3 space-y-3 bg-canvas/50">
              {mailboxConnected && (
                <div className="flex items-center justify-between">
                  <p className="text-[12px] font-medium text-ink">{t('Replace the connected mailbox')}</p>
                  <button
                    type="button"
                    onClick={() => setImapReplace(false)}
                    className="text-[11px] text-tertiary hover:text-ink"
                  >
                    {t('Cancel')}
                  </button>
                </div>
              )}
              <div className="grid grid-cols-3 gap-3">
                <div className="col-span-2">
                  <Field label={t('IMAP host')} required>
                    <input className={inputCls} value={form.imap.host} onChange={(e) => update({ imap: { ...form.imap, host: e.target.value } })} placeholder="imap.example.com" />
                  </Field>
                </div>
                <Field label={t('Port')}>
                  <NumberInput
                    value={form.imap.port ?? null}
                    onChange={(v) => update({ imap: { ...form.imap, port: v } })}
                    placeholder="993"
                  />
                </Field>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <Field label={t('Username')} required>
                  <input className={inputCls} value={form.imap.username} onChange={(e) => update({ imap: { ...form.imap, username: e.target.value } })} placeholder="you@example.com" />
                </Field>
                <Field label={t('App password')} required>
                  <input className={inputCls} type="password" value={form.imap.password} onChange={(e) => update({ imap: { ...form.imap, password: e.target.value } })} placeholder="••••••••" autoComplete="new-password" />
                </Field>
              </div>
              <div className="flex items-center justify-between gap-3">
                <Checkbox
                  checked={form.imap.use_ssl ?? true}
                  onChange={(e) => update({ imap: { ...form.imap, use_ssl: e.target.checked } })}
                  label={t('Use SSL/TLS')}
                  size="sm"
                />
                <input className={`${inputCls} w-36`} value={form.imap.mailbox ?? ''} onChange={(e) => update({ imap: { ...form.imap, mailbox: e.target.value } })} placeholder="INBOX" />
              </div>
              <p className="text-[11px] text-tertiary">{t('The connection is verified before saving. Credentials are stored encrypted, never returned.')}</p>
              {isEdit && (
                <div className="flex justify-end">
                  <Button size="sm" onClick={connectImapNow} loading={imapBusy}>{t('Connect IMAP now')}</Button>
                </div>
              )}
            </div>
          )}

          <Disclosure label={t('OTP parsing (optional — defaults work for most sites)')}>
            <div className="grid grid-cols-2 gap-3">
              <Field label={t('From filter')} hint={t('Only read emails from this sender.')}>
                <input className={inputCls} value={form.otpFrom} onChange={(e) => update({ otpFrom: e.target.value })} placeholder="no-reply@example.com" />
              </Field>
              <Field label={t('Subject regex')}>
                <input className={`${inputCls} font-mono text-xs`} value={form.otpSubjectRegex} onChange={(e) => update({ otpSubjectRegex: e.target.value })} placeholder={t('verification code')} />
              </Field>
              <Field label={t('Code regex')} hint={t('First capture group is the code.')}>
                <input className={`${inputCls} font-mono text-xs`} value={form.otpCodeRegex} onChange={(e) => update({ otpCodeRegex: e.target.value })} placeholder="(\d{6})" />
              </Field>
              <Field label={t('Max age (seconds)')} hint={t('Ignore older OTP emails.')}>
                <NumberInput
                  value={form.otpMaxAge === '' ? null : Number(form.otpMaxAge)}
                  onChange={(v) => update({ otpMaxAge: v === null ? '' : String(v) })}
                  placeholder="300"
                />
              </Field>
            </div>
          </Disclosure>
        </div>
      )}
    </div>
  );

  const renderExecution = () => (
    <div className="space-y-4">
      <Field
        label={t('Preferred agent')}
        hint={t('Pin runs to one trusted/residential agent so the site always sees the same network. “Auto” lets the dispatcher pick.')}
      >
        <Select
          value={form.preferred_agent_id}
          onChange={(v) => update({ preferred_agent_id: v })}
          options={[
            { value: '', label: t('Auto — any trusted agent') },
            ...agents.map((a) => ({
              value: a.agentId,
              label: `${a.agentId}${a.platform ? ` · ${a.platform}` : ''}${a.connected ? ` · ${t('online')}` : ` · ${t('offline')}`}`,
            })),
          ]}
        />
      </Field>

      <div className={clsx('border rounded-xl p-3.5 transition-colors', form.pinFingerprint ? 'border-ink/40 bg-ink/[0.02]' : 'border-border')}>
        <Checkbox
          checked={form.pinFingerprint}
          onChange={(e) => update({
            pinFingerprint: e.target.checked,
            fingerprint: e.target.checked ? (form.fingerprint || randomFingerprint()) : form.fingerprint,
            fingerprintTouched: form.fingerprintTouched || (e.target.checked && !persona?.has_fingerprint),
          })}
          label={
            <span className="flex items-center gap-1.5 text-[13px] font-medium text-ink">
              <FingerPrintIcon className="w-4 h-4" /> {t('Pin a consistent device fingerprint')}
            </span>
          }
          description={t('Recommended. The site sees the same browser, locale and timezone on every run — re-randomizing these is a common reason sites log identities out.')}
        />

        {form.pinFingerprint && (
          <div className="mt-3 flex items-center justify-between border border-border rounded-lg px-3 py-2 bg-surface">
            {isEdit && persona?.has_fingerprint && !form.fingerprintTouched ? (
              <span className="text-xs text-secondary flex items-center gap-1.5">
                <CheckBadgeIcon className="w-4 h-4 text-ink" /> {t('A fingerprint is pinned — kept as-is.')}
              </span>
            ) : form.fingerprint ? (
              <span className="text-xs text-secondary font-mono">
                {describeUserAgent(form.fingerprint.user_agent)} · {form.fingerprint.locale} · {form.fingerprint.timezone}
              </span>
            ) : null}
            <button
              type="button"
              onClick={() => update({ fingerprint: randomFingerprint(), fingerprintTouched: true })}
              className="flex items-center gap-1 text-[11px] text-secondary hover:text-ink px-2 py-1 rounded-lg hover:bg-hover shrink-0"
            >
              <ArrowPathIcon className="w-3.5 h-3.5" /> {isEdit && persona?.has_fingerprint && !form.fingerprintTouched ? t('Re-roll') : t('Shuffle')}
            </button>
          </div>
        )}
        {!form.pinFingerprint && isEdit && persona?.has_fingerprint && (
          <p className="text-[11px] text-tertiary mt-2">{t('The stored fingerprint will be removed on save — runs will randomize again.')}</p>
        )}
      </div>

      {/* BYO / residential proxy */}
      <div className="border border-border rounded-xl p-3.5 space-y-3">
        <div>
          <span className="text-[13px] font-medium text-ink">{t('Residential / BYO proxy')}</span>
          <p className="text-[11px] text-tertiary mt-0.5">
            {isEdit && persona?.has_proxy
              ? t('A proxy is configured (stored encrypted). Leave blank to keep it, or enter a new server to replace it.')
              : t('Optional. Route this persona’s traffic through your own proxy. Credentials are stored encrypted, never shown again.')}
          </p>
        </div>
        <Field label={t('Proxy server')} hint={t('URL or host:port, e.g. http://host:8080')}>
          <input
            className={inputCls}
            value={form.proxy_server}
            onChange={(e) => update({ proxy_server: e.target.value, proxyTouched: true })}
            placeholder={isEdit && persona?.has_proxy ? t('(configured — leave blank to keep)') : 'http://host:8080'}
            autoComplete="off"
          />
        </Field>
        {(form.proxy_server.trim() || form.proxyTouched) && (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <Field label={t('Proxy username')}>
              <input
                className={inputCls}
                value={form.proxy_username}
                onChange={(e) => update({ proxy_username: e.target.value, proxyTouched: true })}
                placeholder={t('Optional')}
                autoComplete="off"
              />
            </Field>
            <Field label={t('Proxy password')}>
              <input
                className={inputCls}
                type="password"
                value={form.proxy_password}
                onChange={(e) => update({ proxy_password: e.target.value, proxyTouched: true })}
                placeholder={t('Optional')}
                autoComplete="new-password"
              />
            </Field>
          </div>
        )}
        {form.proxy_server.trim() && (
          <Checkbox
            wrapperClassName={clsx(
              'border rounded-lg px-3 py-2.5 transition-colors',
              form.proxy_lawful_use_ack ? 'border-ink/40 bg-ink/[0.02]' : 'border-border',
            )}
            checked={form.proxy_lawful_use_ack}
            onChange={(e) => update({ proxy_lawful_use_ack: e.target.checked, proxyTouched: true })}
            label={t('I confirm I am authorized to use this proxy and will use it only for lawful purposes, in compliance with the proxy provider’s terms and all applicable laws.')}
            size="sm"
          />
        )}
      </div>

      {isEdit && (
        <Checkbox
          wrapperClassName="border border-border rounded-xl px-3.5 py-3"
          checked={form.is_active}
          onChange={(e) => update({ is_active: e.target.checked })}
          label={t('Active')}
          description={t('Inactive personas stay configured but aren’t used by runs.')}
        />
      )}

      <p className="text-[11px] text-tertiary">
        {t('Personas always execute on Writ Cloud trusted agents — local agents never receive persona secrets.')}
      </p>
    </div>
  );

  const reviewRows: { label: string; value: React.ReactNode; step: StepId }[] = useMemo(() => {
    const twofaSummary = (() => {
      if (form.twofa_method === 'none') return t('None');
      if (form.twofa_method === 'totp') {
        return form.totp_seed.trim() ? t('Authenticator (TOTP) — new seed, {{digits}} digits', { digits: form.totp_digits }) :
          (isEdit && persona?.has_totp_seed ? t('Authenticator (TOTP) — stored seed kept') : t('Authenticator (TOTP)'));
      }
      if (mailboxConnected && !imapReplace) return t('Email code — mailbox {{mailbox}}', { mailbox: persona?.connected_mailbox });
      return t('Email code — IMAP {{username}}', { username: form.imap.username.trim() || t('(missing)') });
    })();
    const linkedFieldsSuffix = form.extraFields.some((f) => f.secretKey)
      ? ` · ${t('{{count}} linked field(s)', { count: form.extraFields.filter((f) => f.secretKey).length })}`
      : '';
    return [
      { label: t('Name'), value: form.name || '—', step: 'identity' as StepId },
      { label: t('Site'), value: normalizeDomain(form.target_domain) || t('Any site'), step: 'identity' as StepId },
      {
        label: t('Login'),
        value: form.loginSecretKey
          ? `${t('Saved login')}: ${form.loginSecretKey}${linkedFieldsSuffix}`
          : form.login_username
            ? `${form.login_username} · ${
                form.password ? t('new password')
                : (isEdit && persona?.has_password ? t('stored password') : t('no password'))
              }${linkedFieldsSuffix}`
            : (isEdit && persona?.has_password ? t('Stored credentials kept') : t('No credentials — session-only persona')),
        step: 'login' as StepId,
      },
      { label: t('2FA'), value: twofaSummary, step: 'twofa' as StepId },
      {
        label: t('Execution'),
        value: `${form.preferred_agent_id || t('Auto agent')} · ${form.pinFingerprint ? t('pinned fingerprint') : t('random fingerprint')}${
          form.proxy_server.trim() ? ` · ${t('custom proxy')}` : (isEdit && persona?.has_proxy && !form.proxyTouched ? ` · ${t('stored proxy')}` : '')
        }${isEdit && !form.is_active ? ` · ${t('INACTIVE')}` : ''}`,
        step: 'execution' as StepId,
      },
    ];
  }, [form, isEdit, persona, mailboxConnected, imapReplace, t]);

  const renderReview = () => (
    <div className="space-y-3">
      <p className="text-[12px] text-secondary">
        {isEdit ? t('Review the changes, then save.') : t('Almost done — check everything, then create the persona.')}
      </p>
      <div className="border border-border rounded-xl divide-y divide-zinc-100 overflow-hidden">
        {reviewRows.map((r) => (
          <div key={r.label} className="flex items-center justify-between gap-3 px-3.5 py-2.5">
            <div className="min-w-0">
              <div className="text-[10px] font-semibold text-tertiary uppercase tracking-wider">{t(r.label)}</div>
              <div className="text-[13px] text-ink truncate">{r.value}</div>
            </div>
            <button type="button" onClick={() => jumpTo(r.step)} className="text-[11px] text-secondary hover:text-ink px-2 py-1 rounded-lg hover:bg-hover shrink-0">
              {t('Edit')}
            </button>
          </div>
        ))}
      </div>
    </div>
  );

  const renderSuccess = () => (
    <div className="py-4 text-center space-y-4">
      <div className="mx-auto w-12 h-12 rounded-full bg-ink text-white flex items-center justify-center">
        <CheckIcon className="w-6 h-6" />
      </div>
      <div>
        <p className="text-sm font-semibold text-ink">{t('Persona “{{name}}” is ready', { name: created?.name })}</p>
        <p className="text-xs text-tertiary mt-1">
          {t('Attach it to a workflow (or set it as a workflow’s default persona) and runs will log in automatically.')}
        </p>
      </div>
      {created && created.twofa_method !== 'none' && (
        <div className="max-w-sm mx-auto space-y-2">
          <Button variant="secondary" size="sm" onClick={runTest2fa} loading={testing}>
            <ShieldCheckIcon className="w-4 h-4" /> {t('Test 2FA now')}
          </Button>
          {testResult && <p className="text-[11px] text-secondary">{testResult}</p>}
        </div>
      )}
      <div className="pt-2">
        <Button onClick={onClose}>{t('Done')}</Button>
      </div>
    </div>
  );

  const stepBody: Record<StepId, () => React.ReactNode> = {
    identity: renderIdentity,
    login: renderLogin,
    twofa: renderTwofa,
    execution: renderExecution,
    review: renderReview,
  };

  const isLast = step === 'review';

  return (
    <>
    <Modal isOpen={isOpen} onClose={handleClose} title={isEdit ? t('Edit persona — {{name}}', { name: persona?.name }) : t('New persona')} size="lg">
      {phase === 'success' ? renderSuccess() : (
        <div className="space-y-5">
          <StepIndicator current={step} completed={completed} onJump={jumpTo} />
          <div className="min-h-[300px]">{stepBody[step]()}</div>
          <div className="flex items-center justify-between gap-2 pt-3 border-t border-border">
            <button type="button" onClick={handleClose} className="px-3 py-2 text-sm text-secondary hover:text-ink rounded-lg hover:bg-hover">
              {t('Cancel')}
            </button>
            <div className="flex items-center gap-2">
              {step !== 'identity' && (
                <Button variant="secondary" onClick={goBack}>{t('Back')}</Button>
              )}
              {isLast ? (
                <Button onClick={save} loading={saving}>{isEdit ? t('Save changes') : t('Create persona')}</Button>
              ) : (
                <Button onClick={goNext} disabled={!!currentError}>{t('Next')}</Button>
              )}
            </div>
          </div>
          {currentError && <p className="text-[11px] text-tertiary -mt-2">{currentError}</p>}
        </div>
      )}
    </Modal>

    <ConfirmDialog
      isOpen={confirmDiscard}
      onClose={() => setConfirmDiscard(false)}
      onConfirm={() => { setConfirmDiscard(false); onClose(); }}
      title={t('Discard changes')}
      message={t("Discard this persona setup? Any details you've entered will be lost.")}
      confirmText={t('Discard')}
      variant="danger"
    />
    </>
  );
};

export default PersonaWizard;
