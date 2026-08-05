import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import {
  CheckCircleIcon, CheckIcon, ArrowLeftIcon, EnvelopeIcon, BellIcon,
  DevicePhoneMobileIcon, ClipboardDocumentIcon, ChevronRightIcon,
  SparklesIcon,
} from '@heroicons/react/24/outline';
import { setAuth } from '../utils/auth';
import client, { apiErrorMessage } from '../api/client';
import { WritWordmark } from '../components/brand/WritMark';
import { createAIProvider, updateNetworkSettings } from '../api/settings';
import { updateEmailConfig, updatePushoverConfig, updateTwilioConfig, addTwilioRecipient } from '../api/notifications';
import { fleetApi, type PairCode } from '../api/fleet';

/**
 * First-run onboarding — a fast, inline, self-contained setup flow.
 *
 * Purpose-built micro-forms (NOT the full Settings panels) get the coordinator
 * ready in five quick steps: create the admin, confirm the public address,
 * connect an AI provider, add a notification channel, connect the first agent.
 * Every step after the account is skippable; a subtle fade carries the user
 * between them. On finish it shows what got configured. Gated by
 * GET /api/auth/setup-status (bounces to /login once an owner exists); step 1
 * sets the session so the rest can call authed endpoints.
 *
 * The ADDRESS step is second on purpose. It is the setting everything else
 * depends on — the agent install command in step 5 embeds it, and an operator
 * who discovers it later in Settings has already copied a localhost one-liner
 * onto a remote machine and watched it fail to connect.
 */

type Step = 'account' | 'address' | 'ai' | 'notifications' | 'agent' | 'done';
const ORDER: Step[] = ['account', 'address', 'ai', 'notifications', 'agent', 'done'];
const LABELS: Record<Step, string> = {
  account: 'Account', address: 'Address', ai: 'AI', notifications: 'Alerts', agent: 'Agent', done: 'Done',
};

// ── Strong default password (>=8, upper+lower+digit), crypto RNG ─────────────
function generatePassword(): string {
  const lower = 'abcdefghijkmnpqrstuvwxyz', upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ', digits = '23456789';
  const all = lower + upper + digits;
  const rnd = (n: number) => { const b = new Uint32Array(1); (globalThis.crypto || (window as any).crypto).getRandomValues(b); return b[0] % n; };
  const pick = (s: string) => s[rnd(s.length)];
  const chars = [pick(upper), pick(lower), pick(digits)];
  while (chars.length < 16) chars.push(pick(all));
  for (let i = chars.length - 1; i > 0; i--) { const j = rnd(i + 1); [chars[i], chars[j]] = [chars[j], chars[i]]; }
  return chars.join('');
}

// ── Fade+rise transition, re-runs whenever `on` changes (the step key) ───────
const Fade: React.FC<{ on: string | number; children: React.ReactNode }> = ({ on, children }) => {
  // We remember WHICH key is faded in rather than a bare boolean: a new `on`
  // reads as "hidden" during render, so the reset needs no state write of its own
  // (and a late frame from the previous key can't reveal the new one early).
  const [shownFor, setShownFor] = useState<string | number | null>(null);
  const shown = shownFor === on;
  useEffect(() => {
    let inner = 0;
    const outer = requestAnimationFrame(() => {
      inner = requestAnimationFrame(() => setShownFor(on));
    });
    return () => { cancelAnimationFrame(outer); cancelAnimationFrame(inner); };
  }, [on]);
  return (
    <div className={clsx('transition-all duration-300 ease-out', shown ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-2')}>
      {children}
    </div>
  );
};

// ── Slim segmented progress ──────────────────────────────────────────────────
const Progress: React.FC<{ step: Step }> = ({ step }) => {
  const { t } = useTranslation();
  const idx = ORDER.indexOf(step);
  // Every step except the terminal 'done' card. Derived from ORDER rather than a
  // hardcoded slice length so adding a step cannot desync the bar from the count
  // in the caption below it.
  const steps = ORDER.filter((s) => s !== 'done');
  return (
    <div className="mb-6">
      <div className="flex items-center gap-1.5 mb-2">
        {steps.map((s, i) => (
          <div key={s} className={clsx('h-1 flex-1 rounded-full transition-colors duration-500', i <= idx || step === 'done' ? 'bg-ink' : 'bg-border')} />
        ))}
      </div>
      <p className="text-[11px] text-tertiary">
        {step === 'done'
          ? t('Setup complete')
          : t('Step {{n}} of {{total}} · {{label}}', { n: idx + 1, total: steps.length, label: t(LABELS[step]) })}
      </p>
    </div>
  );
};

// ── Reusable inputs ──────────────────────────────────────────────────────────
const Field: React.FC<{ label?: React.ReactNode; hint?: React.ReactNode; children: React.ReactNode; className?: string }> = ({ label, hint, children, className }) => (
  <div className={className}>
    {label && <label className="block text-[12px] font-medium text-ink mb-1">{label}</label>}
    {children}
    {hint && <p className="text-[11px] text-tertiary mt-1">{hint}</p>}
  </div>
);
const inputCls = 'w-full px-3 py-2 text-sm bg-canvas border border-border rounded-lg focus:outline-none focus:border-ink/40 transition-colors placeholder:text-tertiary';

// ── Chip selector (providers / channels) ─────────────────────────────────────
const Chip: React.FC<{ active: boolean; onClick: () => void; children: React.ReactNode }> = ({ active, onClick, children }) => (
  <button
    type="button"
    onClick={onClick}
    className={clsx(
      'flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-[12px] font-medium border transition-all',
      active ? 'border-ink bg-ink text-white shadow-sm' : 'border-border text-secondary hover:border-ink/30 hover:text-ink',
    )}
  >
    {children}
  </button>
);

// ── The reusable step frame: header, body, footer nav ────────────────────────
const StepFrame: React.FC<{
  title: string; subtitle?: string; onBack?: () => void; onSkipAll: () => void;
  onSkip?: () => void; primaryLabel: string; onPrimary: () => void; busy?: boolean; disabled?: boolean;
  children: React.ReactNode;
}> = ({ title, subtitle, onBack, onSkipAll, onSkip, primaryLabel, onPrimary, busy, disabled, children }) => {
  const { t } = useTranslation();
  return (
    <div className="bg-surface rounded-2xl shadow-sm border border-ink/20 p-6">
      <div className="flex items-start justify-between gap-3 mb-4">
        <div>
          <h1 className="text-base font-semibold text-ink">{title}</h1>
          {subtitle && <p className="text-[13px] text-secondary mt-0.5 leading-relaxed">{subtitle}</p>}
        </div>
        <button onClick={onSkipAll} className="shrink-0 text-[11px] text-tertiary hover:text-ink transition-colors">{t('Skip setup')}</button>
      </div>

      {children}

      <div className="flex items-center justify-between gap-3 mt-6 pt-4 border-t border-border">
        {onBack ? (
          <button onClick={onBack} className="inline-flex items-center gap-1 text-[13px] text-tertiary hover:text-ink transition-colors">
            <ArrowLeftIcon className="w-3.5 h-3.5" /> {t('Back')}
          </button>
        ) : <span />}
        <div className="flex items-center gap-2">
          {onSkip && <button onClick={onSkip} className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg transition-colors">{t('Skip for now')}</button>}
          <button onClick={onPrimary} disabled={busy || disabled} className="px-4 py-2 bg-accent-strong text-accent-on text-sm font-semibold rounded-lg shadow-sm hover:bg-accent-strong/90 transition-colors disabled:opacity-50">
            {busy ? t('Saving…') : primaryLabel}
          </button>
        </div>
      </div>
    </div>
  );
};

// ═══ Step: public address ════════════════════════════════════════════════════
/**
 * Classify the origin the browser is currently talking to. Everything this step
 * warns about is derivable from it, and the browser's own address bar is the
 * most reliable source there is — it is, by definition, an address that reaches
 * this coordinator.
 */
type AddressKind = 'secure' | 'insecure' | 'local';
function classifyOrigin(origin: string): AddressKind {
  let url: URL;
  try { url = new URL(origin); } catch { return 'local'; }
  const host = url.hostname.toLowerCase();
  const isLoopback =
    host === 'localhost' || host === '127.0.0.1' || host === '::1' || host.endsWith('.localhost');
  if (isLoopback) return 'local';
  return url.protocol === 'https:' ? 'secure' : 'insecure';
}

const AddressStep: React.FC<{ onNext: () => void; onBack: () => void; onSkipAll: () => void; onConfigured: (url: string) => void }> = ({ onNext, onBack, onSkipAll, onConfigured }) => {
  const { t } = useTranslation();
  // Prefill from the address bar. If the operator reached this page over
  // https://writ.example.com then that IS the public URL, and asking them to
  // retype it only invites a typo.
  const [url, setUrl] = useState(() => window.location.origin);
  const [busy, setBusy] = useState(false);
  const kind = classifyOrigin(url);

  const save = async () => {
    const trimmed = url.trim().replace(/\/+$/, '');
    if (!/^https?:\/\/.+/i.test(trimmed)) {
      toast.error(t('Enter a full URL including https://'));
      return;
    }
    setBusy(true);
    try {
      await updateNetworkSettings({ public_url: trimmed });
      onConfigured(trimmed);
      toast.success(t('Public address saved'));
      onNext();
    } catch (err) {
      toast.error(apiErrorMessage(err, t('Could not save the address.')));
    } finally {
      setBusy(false);
    }
  };

  return (
    <StepFrame
      title={t('Confirm your address')}
      subtitle={t('The URL people open, and the one agents dial back on. It is baked into the agent install command you get in a moment.')}
      onBack={onBack} onSkipAll={onSkipAll} onSkip={onNext}
      primaryLabel={t('Save & continue')} onPrimary={save} busy={busy}
    >
      <div className="space-y-3.5">
        <Field
          label={t('Public URL')}
          hint={t('Include the scheme. The hostname is trusted automatically — you do not need to configure anything else.')}
        >
          <input className={clsx(inputCls, 'font-mono')} value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://writ.example.com" />
        </Field>

        {kind === 'local' && (
          <div className="rounded-lg border border-border bg-canvas px-3 py-2.5 text-[12px] text-secondary leading-relaxed">
            <span className="font-medium text-ink">{t('This address only works on this machine.')}</span>{' '}
            {t('That is fine for trying things out. Agents on other machines cannot reach a localhost URL, so change this before you connect a fleet — or deploy on a domain with:')}
            <pre className="mt-1.5 text-[11px] font-mono bg-ink text-white/90 rounded p-2 overflow-x-auto">./scripts/deploy.sh writ.example.com you@example.com</pre>
          </div>
        )}

        {kind === 'insecure' && (
          <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2.5 text-[12px] text-amber-900 leading-relaxed">
            <span className="font-medium">{t('This address is not encrypted.')}</span>{' '}
            {t('Passwords and agent tokens would cross the network in the clear. Put TLS in front before you use this for real:')}
            <pre className="mt-1.5 text-[11px] font-mono bg-amber-900 text-amber-50 rounded p-2 overflow-x-auto">./scripts/deploy.sh writ.example.com you@example.com</pre>
          </div>
        )}

        {kind === 'secure' && (
          <div className="flex items-start gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2.5 text-[12px] text-emerald-800 leading-relaxed">
            <CheckCircleIcon className="w-4 h-4 shrink-0 mt-px" />
            <span>{t('Served over HTTPS. Agents anywhere can reach this, and credentials stay encrypted in transit.')}</span>
          </div>
        )}
      </div>
    </StepFrame>
  );
};

// ═══ Step: AI provider ═══════════════════════════════════════════════════════
const AI_PROVIDERS = [
  { id: 'anthropic', label: 'Anthropic', mono: 'A' },
  { id: 'openai', label: 'OpenAI', mono: 'O' },
  { id: 'openrouter', label: 'OpenRouter', mono: 'R' },
  { id: 'local', label: 'Local', mono: 'L' },
] as const;

const AiStep: React.FC<{ onNext: () => void; onBack: () => void; onSkipAll: () => void; onConfigured: () => void }> = ({ onNext, onBack, onSkipAll, onConfigured }) => {
  const { t } = useTranslation();
  const [provider, setProvider] = useState<string>('anthropic');
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('');
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const isLocal = provider === 'local';
  const baseUrlPlaceholder =
    provider === 'anthropic' ? 'https://api.anthropic.com'
    : provider === 'openrouter' ? 'https://openrouter.ai/api/v1'
    : 'https://api.openai.com/v1';

  const save = async () => {
    if (isLocal ? !baseUrl.trim() : !apiKey.trim()) {
      toast.error(isLocal ? t('Enter the local server URL.') : t('Paste your API key.'));
      return;
    }
    setBusy(true);
    try {
      await createAIProvider({
        provider,
        api_key: apiKey.trim() || undefined,
        base_url: baseUrl.trim() || undefined,
        model: model.trim() || undefined,
        is_active: true,
        priority: 0,
      });
      onConfigured();
      toast.success(t('AI provider connected'));
      onNext();
    } catch (err) {
      toast.error(apiErrorMessage(err, t('Could not save the provider.')));
    } finally {
      setBusy(false);
    }
  };

  return (
    <StepFrame
      title={t('Connect an AI provider')}
      subtitle={t('Powers AI-assist and AI browsing sessions. Optional — you can add or change this anytime in Settings.')}
      onBack={onBack} onSkipAll={onSkipAll} onSkip={onNext}
      primaryLabel={t('Add & continue')} onPrimary={save} busy={busy}
    >
      <div className="space-y-3.5">
        <div className="flex gap-1.5">
          {AI_PROVIDERS.map((p) => (
            <Chip key={p.id} active={provider === p.id} onClick={() => setProvider(p.id)}>
              <span className={clsx('w-4 h-4 rounded flex items-center justify-center text-[9px] font-bold', provider === p.id ? 'bg-surface/20' : 'bg-hover')}>{p.mono}</span>
              {p.label}
            </Chip>
          ))}
        </div>

        {isLocal ? (
          <Field label={t('Server URL')} hint={t('An Ollama or OpenAI-compatible endpoint on your network.')}>
            <input className={inputCls} value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="http://localhost:11434" />
          </Field>
        ) : (
          <Field label={t('API key')} hint={t('Stored encrypted; used only to call the provider you chose.')}>
            <input className={clsx(inputCls, 'font-mono')} type="password" autoComplete="off" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="sk-…" />
          </Field>
        )}

        <Field label={<>{t('Model')} <span className="text-tertiary font-normal">({t('optional')})</span></>}>
          <input className={inputCls} value={model} onChange={(e) => setModel(e.target.value)} placeholder={isLocal ? 'llama3.1' : 'claude-sonnet-4-20250514'} />
        </Field>

        {/* Advanced — keep the primary key/model inputs simple, tuck the custom
            endpoint away for users pointing a provider at a proxy/gateway. */}
        {!isLocal && (
          <div>
            <button
              type="button"
              onClick={() => setAdvanced((v) => !v)}
              className="inline-flex items-center gap-1 text-[12px] font-medium text-secondary hover:text-ink transition-colors"
            >
              <ChevronRightIcon className={clsx('w-3.5 h-3.5 transition-transform', advanced && 'rotate-90')} />
              {t('Advanced')}
              {!advanced && baseUrl.trim() && <span className="w-1.5 h-1.5 rounded-full bg-ink/60" />}
            </button>
            {advanced && (
              <div className="mt-2">
                <Field
                  label={t('Custom base URL')}
                  hint={t('Point this provider at a proxy or self-hosted gateway. Leave blank to use the official API.')}
                >
                  <input className={inputCls} value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder={baseUrlPlaceholder} />
                </Field>
              </div>
            )}
          </div>
        )}
      </div>
    </StepFrame>
  );
};

// ═══ Step: notifications ═════════════════════════════════════════════════════
type Channel = 'email' | 'pushover' | 'sms';
const CHANNELS: { id: Channel; label: string; Icon: React.FC<any> }[] = [
  { id: 'email', label: 'Email', Icon: EnvelopeIcon },
  { id: 'pushover', label: 'Pushover', Icon: BellIcon },
  { id: 'sms', label: 'SMS', Icon: DevicePhoneMobileIcon },
];

const NotifyStep: React.FC<{ onNext: () => void; onBack: () => void; onSkipAll: () => void; onConfigured: () => void }> = ({ onNext, onBack, onSkipAll, onConfigured }) => {
  const { t } = useTranslation();
  const [channel, setChannel] = useState<Channel>('email');
  const [busy, setBusy] = useState(false);
  // email
  const [host, setHost] = useState(''); const [port, setPort] = useState('587');
  const [user, setUser] = useState(''); const [pass, setPass] = useState(''); const [from, setFrom] = useState('');
  // pushover
  const [appToken, setAppToken] = useState(''); const [userKey, setUserKey] = useState('');
  // sms
  const [sid, setSid] = useState(''); const [authToken, setAuthToken] = useState(''); const [fromPhone, setFromPhone] = useState(''); const [toPhone, setToPhone] = useState('');

  const save = async () => {
    setBusy(true);
    try {
      if (channel === 'email') {
        if (!host.trim() || !from.trim()) { toast.error(t('Enter your SMTP host and from address.')); setBusy(false); return; }
        await updateEmailConfig({ smtp_host: host.trim(), smtp_port: Number(port) || 587, smtp_username: user.trim(), smtp_password: pass, from_email: from.trim(), from_name: 'writ', use_tls: true, enabled: true });
      } else if (channel === 'pushover') {
        if (!appToken.trim() || !userKey.trim()) { toast.error(t('Enter your Pushover app token and user key.')); setBusy(false); return; }
        await updatePushoverConfig({ app_token: appToken.trim(), user_key: userKey.trim() });
      } else {
        if (!sid.trim() || !authToken.trim() || !fromPhone.trim()) { toast.error(t('Enter your Twilio SID, token and from number.')); setBusy(false); return; }
        await updateTwilioConfig({ account_sid: sid.trim(), auth_token: authToken.trim(), from_phone: fromPhone.trim(), enabled: true });
        if (toPhone.trim()) { try { await addTwilioRecipient({ name: 'Me', phone_number: toPhone.trim() }); } catch { /* recipient optional */ } }
      }
      onConfigured();
      toast.success(t('Notifications set up'));
      onNext();
    } catch (err) {
      toast.error(apiErrorMessage(err, t('Could not save the channel.')));
    } finally {
      setBusy(false);
    }
  };

  return (
    <StepFrame
      title={t('Get notified')}
      subtitle={t('Choose one way for your monitors to reach you when something changes. Add more later in Settings.')}
      onBack={onBack} onSkipAll={onSkipAll} onSkip={onNext}
      primaryLabel={t('Save & continue')} onPrimary={save} busy={busy}
    >
      <div className="space-y-3.5">
        <div className="flex gap-1.5">
          {CHANNELS.map((c) => (
            <Chip key={c.id} active={channel === c.id} onClick={() => setChannel(c.id)}>
              <c.Icon className="w-3.5 h-3.5" /> {c.label}
            </Chip>
          ))}
        </div>

        {channel === 'email' && (
          <div className="grid grid-cols-2 gap-2.5">
            <Field label={t('SMTP host')} className="col-span-2"><input className={inputCls} value={host} onChange={(e) => setHost(e.target.value)} placeholder="smtp.gmail.com" /></Field>
            <Field label={t('Port')}><input className={inputCls} value={port} onChange={(e) => setPort(e.target.value)} placeholder="587" /></Field>
            <Field label={t('From address')}><input className={inputCls} value={from} onChange={(e) => setFrom(e.target.value)} placeholder="alerts@you.com" /></Field>
            <Field label={t('Username')}><input className={inputCls} value={user} onChange={(e) => setUser(e.target.value)} placeholder={t('SMTP user')} autoComplete="off" /></Field>
            <Field label={t('Password')}><input className={inputCls} type="password" value={pass} onChange={(e) => setPass(e.target.value)} placeholder="••••••••" autoComplete="off" /></Field>
          </div>
        )}
        {channel === 'pushover' && (
          <div className="space-y-2.5">
            <Field label={t('App token')}><input className={clsx(inputCls, 'font-mono')} value={appToken} onChange={(e) => setAppToken(e.target.value)} placeholder="a1b2c3…" autoComplete="off" /></Field>
            <Field label={t('User key')} hint={t('Both are on your Pushover dashboard.')}><input className={clsx(inputCls, 'font-mono')} value={userKey} onChange={(e) => setUserKey(e.target.value)} placeholder="u1x2y3…" autoComplete="off" /></Field>
          </div>
        )}
        {channel === 'sms' && (
          <div className="grid grid-cols-2 gap-2.5">
            <Field label={t('Account SID')} className="col-span-2"><input className={clsx(inputCls, 'font-mono')} value={sid} onChange={(e) => setSid(e.target.value)} placeholder="AC…" autoComplete="off" /></Field>
            <Field label={t('Auth token')} className="col-span-2"><input className={clsx(inputCls, 'font-mono')} type="password" value={authToken} onChange={(e) => setAuthToken(e.target.value)} placeholder="••••••••" autoComplete="off" /></Field>
            <Field label={t('From number')}><input className={inputCls} value={fromPhone} onChange={(e) => setFromPhone(e.target.value)} placeholder="+1555…" /></Field>
            <Field label={t('Your number')}><input className={inputCls} value={toPhone} onChange={(e) => setToPhone(e.target.value)} placeholder="+1555…" /></Field>
          </div>
        )}
      </div>
    </StepFrame>
  );
};

// ═══ Step: connect first agent ═══════════════════════════════════════════════
const AgentStep: React.FC<{ onNext: () => void; onBack: () => void; onSkipAll: () => void; onConnected: () => void }> = ({ onNext, onBack, onSkipAll, onConnected }) => {
  const { t } = useTranslation();
  const [pair, setPair] = useState<PairCode | null>(null);
  // The one-liner is the default. `manual` exposes the raw token forms for
  // air-gapped hosts and for anyone scripting enrolment, where a single-use
  // interactive code is the wrong shape.
  const [tab, setTab] = useState<'quick' | 'binary' | 'docker'>('quick');
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const notified = useRef(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        // ONE mint drives all three tabs. Requesting a pairing code and a raw
        // token separately minted two fleet tokens — two agent identities for a
        // single connection — and raced two writers into the token registry,
        // which on a fresh install failed outright with
        // "UNIQUE constraint failed: config.key".
        const code = await fleetApi.mintPairCode();
        if (alive) setPair(code);
      }
      catch (err) { if (alive) setError(apiErrorMessage(err, t('Could not prepare an agent token.'))); }
    })();
    return () => { alive = false; };
  }, [t]);

  useEffect(() => {
    if (connected) return;
    let alive = true;
    const tick = async () => {
      try {
        const { agents } = await fleetApi.listAgents();
        // First-run onboarding starts from zero agents, so ANY online agent means
        // the one the user just started has dialed in. (The minted token binds a
        // coordinator-side agent_id, but the stock agent connects under its own
        // self-chosen id and as a trusted role, so we can't match on id/trust.)
        if (alive && agents.some((a) => a.online)) {
          setConnected(true);
          if (!notified.current) { notified.current = true; onConnected(); }
        }
      } catch { /* ignore */ }
    };
    const h = setInterval(tick, 4000); tick();
    return () => { alive = false; clearInterval(h); };
  }, [connected, onConnected]);

  const command = !pair ? '' :
    tab === 'quick' ? pair.install_command :
    tab === 'binary' ? pair.connect_command : pair.docker_command;
  const copy = async () => { try { await navigator.clipboard.writeText(command); toast.success(t('Command copied')); } catch { /* http */ } };

  return (
    <StepFrame
      title={t('Connect your first agent')}
      subtitle={t('Agents run the browsers. Start one on any machine — it dials back in over WebSocket.')}
      onBack={onBack} onSkipAll={onSkipAll} primaryLabel={connected ? t('Finish') : t("I'll do this later")}
      onPrimary={onNext}
    >
      {error ? (
        <div className="rounded-lg border border-border bg-canvas px-3 py-2.5 text-[13px] text-secondary">{error}</div>
      ) : !pair ? (
        <div className="flex items-center gap-2 text-sm text-secondary py-2">
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-border border-t-zinc-800" /> {t('Preparing a connect token…')}
        </div>
      ) : (
        <div className="space-y-3">
          <div className="flex gap-1">
            {(['quick', 'binary', 'docker'] as const).map((k) => (
              <button key={k} type="button" onClick={() => setTab(k)}
                className={clsx('px-3 py-1 text-[12px] font-medium rounded-md transition-colors', tab === k ? 'bg-ink text-white' : 'bg-hover text-secondary hover:text-ink')}>
                {k === 'quick' ? t('One line') : k === 'binary' ? t('Binary') : t('Docker')}
              </button>
            ))}
          </div>
          {tab === 'quick' && (
            <p className="text-[12px] text-secondary">
              {t('Run this on any machine that should do the browsing — your laptop is fine. It installs the agent and connects it. The code works once and expires.')}
            </p>
          )}
          <div className="relative">
            <pre className="text-[11px] leading-relaxed font-mono bg-ink text-white/90 rounded-lg p-3 overflow-x-auto whitespace-pre-wrap break-all">{command}</pre>
            <button type="button" onClick={copy} className="absolute top-2 right-2 inline-flex items-center gap-1 px-2 py-0.5 text-[11px] font-medium bg-surface/15 text-white rounded hover:bg-surface/25 transition-colors">
              <ClipboardDocumentIcon className="w-3 h-3" /> {t('Copy')}
            </button>
          </div>
          <div className={clsx('flex items-center gap-2 text-sm rounded-lg px-3 py-2 transition-colors', connected ? 'bg-emerald-50 text-emerald-700' : 'bg-canvas text-secondary')}>
            {connected ? (
              <><CheckCircleIcon className="w-4 h-4" /> {t('Agent connected — you\'re ready to run work.')}</>
            ) : (
              <><div className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-border border-t-zinc-800" /> {t('Waiting for the agent to come online…')}</>
            )}
          </div>
        </div>
      )}
    </StepFrame>
  );
};

// ═══ Main ════════════════════════════════════════════════════════════════════
export const Setup: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [checking, setChecking] = useState(true);
  const [step, setStep] = useState<Step>('account');
  const [done, setDone] = useState({ address: false, ai: false, notify: false, agent: false });
  const [publicUrl, setPublicUrl] = useState('');

  const [email, setEmail] = useState('admin@local');
  const [name, setName] = useState('Owner');
  const [password, setPassword] = useState(useMemo(() => generatePassword(), []));
  const [revealed, setRevealed] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const { data } = await client.get('/auth/setup-status');
        if (!alive) return;
        if (!data?.needs_setup) { navigate('/login', { replace: true }); return; }
        if (typeof data.suggested_email === 'string' && data.suggested_email) setEmail(data.suggested_email);
      } catch { if (alive) navigate('/login', { replace: true }); return; }
      finally { if (alive) setChecking(false); }
    })();
    return () => { alive = false; };
  }, [navigate]);

  const copyPassword = async () => { try { await navigator.clipboard.writeText(password); toast.success(t('Password copied')); } catch { /* http */ } };
  const go = (s: Step) => setStep(s);
  const next = () => go(ORDER[Math.min(ORDER.indexOf(step) + 1, ORDER.length - 1)]);
  const back = () => { const i = ORDER.indexOf(step); if (i > 1) go(ORDER[i - 1]); };
  const finish = () => navigate('/', { replace: true });
  const skipAll = () => go('done');

  const createAccount = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    try {
      const { data } = await client.post('/auth/register', { email: email.trim(), password, name: name.trim() || null });
      setAuth(data.access_token, data.user, data.organization ?? null);
      go('address');
    } catch (err: any) {
      if (err?.response?.status === 403) { toast.error(t('This coordinator already has an owner — please sign in.')); navigate('/login', { replace: true }); return; }
      toast.error(apiErrorMessage(err, t('Could not create your account.')));
    } finally { setSubmitting(false); }
  };

  if (checking) {
    return <div className="min-h-screen flex items-center justify-center bg-[#EDEBE8]"><div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-zinc-800" /></div>;
  }

  return (
    <div className="min-h-screen bg-[#EDEBE8] px-4 py-10">
      <div className="mx-auto w-full max-w-md">
        <div className="flex items-center justify-center mb-7">
          <WritWordmark size={28} className="text-ink" />
        </div>

        <Progress step={step} />

        <Fade on={step}>
          {step === 'account' && (
            <div className="bg-surface rounded-2xl shadow-sm border border-ink/20 p-6">
              <h1 className="text-base font-semibold text-ink mb-1">{t('Welcome — create your account')}</h1>
              <p className="text-[13px] text-secondary mb-5 leading-relaxed">{t('This is the single admin for your self-hosted coordinator.')}</p>
              <form onSubmit={createAccount} className="space-y-3.5">
                <Field label={t('Admin email')}>
                  <input className={inputCls} type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="admin@local" autoFocus />
                </Field>
                <Field label={<>{t('Display name')} <span className="text-tertiary font-normal">({t('optional')})</span></>}>
                  <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder="Owner" />
                </Field>
                <Field label={t('Password')} hint={t('A strong password was generated — keep it or type your own, and save it somewhere safe.')}>
                  <div className="flex items-stretch gap-1.5">
                    <input className={clsx(inputCls, 'flex-1 min-w-0 font-mono')} type={revealed ? 'text' : 'password'} autoComplete="new-password" required minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} />
                    <button type="button" onClick={() => setRevealed((v) => !v)} className="px-2.5 text-[12px] font-medium text-secondary hover:text-ink border border-border rounded-lg transition-colors">{revealed ? t('Hide') : t('Show')}</button>
                    <button type="button" onClick={copyPassword} className="px-2.5 text-[12px] font-medium text-secondary hover:text-ink border border-border rounded-lg transition-colors">{t('Copy')}</button>
                    <button type="button" onClick={() => setPassword(generatePassword())} className="px-2.5 text-[12px] font-medium text-secondary hover:text-ink border border-border rounded-lg transition-colors">{t('New')}</button>
                  </div>
                </Field>
                <button type="submit" disabled={submitting} className="w-full px-4 py-2.5 bg-accent-strong text-accent-on text-sm font-semibold rounded-lg shadow-sm hover:bg-accent-strong/90 transition-colors disabled:opacity-50 !mt-5">
                  {submitting ? t('Creating…') : t('Create account & continue')}
                </button>
              </form>
            </div>
          )}

          {step === 'address' && (
            <AddressStep
              onNext={next} onBack={back} onSkipAll={skipAll}
              onConfigured={(url) => { setPublicUrl(url); setDone((d) => ({ ...d, address: true })); }}
            />
          )}
          {step === 'ai' && <AiStep onNext={next} onBack={back} onSkipAll={skipAll} onConfigured={() => setDone((d) => ({ ...d, ai: true }))} />}
          {step === 'notifications' && <NotifyStep onNext={next} onBack={back} onSkipAll={skipAll} onConfigured={() => setDone((d) => ({ ...d, notify: true }))} />}
          {step === 'agent' && <AgentStep onNext={next} onBack={back} onSkipAll={skipAll} onConnected={() => setDone((d) => ({ ...d, agent: true }))} />}

          {step === 'done' && (
            <div className="bg-surface rounded-2xl shadow-sm border border-ink/20 p-6">
              <div className="flex items-center gap-2 mb-1">
                <CheckCircleIcon className="w-5 h-5 text-emerald-500" />
                <h1 className="text-base font-semibold text-ink">{t("You're all set")}</h1>
              </div>
              <p className="text-[13px] text-secondary mb-4 leading-relaxed">{t('Your coordinator is ready. Save your login, then jump in.')}</p>

              <div className="space-y-1.5 mb-4">
                {[
                  { ok: true, label: t('Admin account created') },
                  { ok: done.address, label: done.address ? t('Public address · {{url}}', { url: publicUrl }) : t('Public address') },
                  { ok: done.ai, label: t('AI provider') },
                  { ok: done.notify, label: t('Notifications') },
                  { ok: done.agent, label: t('Agent connected') },
                ].map((r) => (
                  <div key={r.label} className="flex items-center gap-2 text-[13px]">
                    <span className={clsx('w-4 h-4 rounded-full flex items-center justify-center', r.ok ? 'bg-emerald-500 text-white' : 'bg-hover text-tertiary')}>
                      {r.ok ? <CheckIcon className="w-3 h-3" /> : <span className="text-[9px]">–</span>}
                    </span>
                    <span className={r.ok ? 'text-ink' : 'text-tertiary'}>{r.label}</span>
                    {!r.ok && <span className="text-[11px] text-tertiary">· {t('skipped — add in Settings')}</span>}
                  </div>
                ))}
              </div>

              <div className="rounded-lg border border-border bg-canvas px-3 py-2.5 mb-5 text-[13px]">
                <div className="flex justify-between gap-3"><span className="text-tertiary">{t('Email')}</span><span className="font-medium text-ink truncate">{email}</span></div>
                <div className="flex justify-between gap-3 mt-1.5">
                  <span className="text-tertiary">{t('Password')}</span>
                  <button type="button" onClick={copyPassword} className="font-mono text-ink hover:underline truncate" title={t('Copy')}>{password}</button>
                </div>
              </div>

              {/* Suggest connecting an AI assistant over MCP — the coordinator's
                  saved workflows become tools Claude & others can run. */}
              <button
                onClick={() => navigate('/connect', { replace: true })}
                className="w-full mb-2.5 flex items-center gap-3 px-3 py-2.5 rounded-lg border border-ink/20 bg-canvas text-left hover:bg-hover transition-colors"
              >
                <span className="w-8 h-8 rounded-lg bg-ink text-white flex items-center justify-center shrink-0">
                  <SparklesIcon className="w-4 h-4" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-[13px] font-medium text-ink">{t('Connect Claude to your coordinator')}</span>
                  <span className="block text-[11px] text-tertiary leading-snug">{t('Run your workflows from Claude Code, Claude Desktop, or any MCP client.')}</span>
                </span>
                <ChevronRightIcon className="w-4 h-4 text-tertiary shrink-0" />
              </button>

              <button onClick={finish} className="w-full px-4 py-2.5 bg-accent-strong text-accent-on text-sm font-semibold rounded-lg shadow-sm hover:bg-accent-strong/90 transition-colors">{t('Go to dashboard')}</button>
            </div>
          )}
        </Fade>

        <p className="text-center text-[11px] text-tertiary mt-6">{t('Self-hosted writ coordinator')}</p>
      </div>
    </div>
  );
};

export default Setup;
