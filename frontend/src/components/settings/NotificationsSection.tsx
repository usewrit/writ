import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import { PlusIcon, TrashIcon, PaperAirplaneIcon, ChevronRightIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { SectionHead } from '../common/SectionHead';
import { Button } from '../ui/Button';
import { Input } from '../ui/Input';
import { NumberInput, Switch } from '../ui';
import { Expand } from '../ui/Expand';
import { SettingGroup } from './SettingsLayout';
import { apiErrorMessage } from '../../api/client';
import {
  getAllProviders,
  getEmailConfig,
  updateEmailConfig,
  sendTestEmail,
  updatePushoverConfig,
  getTwilioConfig,
  updateTwilioConfig,
  sendTestTwilioSMS,
  getTwilioRecipients,
  addTwilioRecipient,
  deleteTwilioRecipient,
  toggleTwilioRecipient,
  getWhatsAppConfig,
  updateWhatsAppConfig,
  sendTestWhatsAppMessage,
  getWhatsAppRecipients,
  addWhatsAppRecipient,
  deleteWhatsAppRecipient,
  toggleWhatsAppRecipient,
  getSignalConfig,
  updateSignalConfig,
  sendTestSignalMessage,
  getSignalRecipients,
  addSignalRecipient,
  deleteSignalRecipient,
  toggleSignalRecipient,
  getNotificationSettings,
  updateNotificationSettings,
  getNotificationPreferences,
  updateNotificationPreferences,
  type NotificationSettings,
  type NotificationPreferences,
  type NotificationPreferencesUpdate,
  type TwilioRecipient,
  type WhatsAppRecipient,
  type SignalRecipient,
} from '../../api/notifications';

// Generic phone-recipient row (Twilio / WhatsApp / Signal share the shape).
type PhoneRecipient = TwilioRecipient | WhatsAppRecipient | SignalRecipient;

// ── Platform event preferences (what you're notified about) ─────────────────
const CHANNEL_LABELS: Record<string, string> = {
  in_app: 'In-app',
  email: 'Email',
  sms: 'SMS',
  whatsapp: 'WhatsApp',
  signal: 'Signal',
  pushover: 'Pushover',
};

const PreferenceMatrixCard: React.FC = () => {
  const { t } = useTranslation();
  const [prefs, setPrefs] = useState<NotificationPreferences | null>(null);
  const [loading, setLoading] = useState(true);
  const [phone, setPhone] = useState('');
  const [pushoverKey, setPushoverKey] = useState('');
  const [savingContact, setSavingContact] = useState(false);

  useEffect(() => {
    getNotificationPreferences()
      .then((p) => {
        setPrefs(p);
        setPhone(p.phone_number || '');
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  // Flip one cell: PUT the event's full non-locked channel map immediately.
  const toggle = async (eventKey: string, channel: string, next: boolean) => {
    if (!prefs) return;
    const event = prefs.catalog.events.find((e) => e.key === eventKey);
    if (!event) return;
    const current = prefs.effective[eventKey] || {};
    const values: Record<string, boolean> = {};
    for (const [ch, spec] of Object.entries(event.channels)) {
      if (spec.locked) continue;
      values[ch] = ch === channel ? next : !!current[ch];
    }
    // Optimistic flip; server response is authoritative.
    setPrefs((p) =>
      p ? { ...p, effective: { ...p.effective, [eventKey]: { ...current, [channel]: next } } } : p,
    );
    try {
      const updated = await updateNotificationPreferences({ preferences: { [eventKey]: values } });
      setPrefs(updated);
    } catch (e) {
      setPrefs((p) => (p ? { ...p, effective: { ...p.effective, [eventKey]: current } } : p));
      toast.error(apiErrorMessage(e, t('Failed to save')));
    }
  };

  const saveContact = async () => {
    setSavingContact(true);
    try {
      const update: NotificationPreferencesUpdate = { phone_number: phone.trim() };
      // "Leave blank to keep" convention: only send the key when the user typed one.
      if (pushoverKey.trim()) update.pushover_user_key = pushoverKey.trim();
      const updated = await updateNotificationPreferences(update);
      setPrefs(updated);
      setPhone(updated.phone_number || '');
      setPushoverKey('');
      toast.success(t('Contact details saved'));
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to save')));
    } finally {
      setSavingContact(false);
    }
  };

  if (loading) return <div className="text-sm text-secondary">{t('Loading...')}</div>;
  if (!prefs) return null;

  const availability = prefs.channel_availability;
  // Channels offered by at least one event but with no configured provider yet.
  const unavailableOffered = prefs.catalog.channels.filter(
    (ch) => !availability[ch] && prefs.catalog.events.some((e) => ch in e.channels),
  );

  return (
    <div className="pt-4 space-y-5">
      {prefs.catalog.events.map((event) => {
        const effective = prefs.effective[event.key] || {};
        return (
          <div key={event.key} className="space-y-2">
            <div>
              <p className="text-sm font-medium text-ink">{t(event.label)}</p>
              <p className="text-xs text-tertiary">{t(event.description)}</p>
            </div>
            <div className="flex flex-wrap gap-x-6 gap-y-2">
              {Object.entries(event.channels).map(([ch, spec]) => (
                <div
                  key={ch}
                  className={clsx(!availability[ch] && 'opacity-60')}
                  title={
                    spec.locked
                      ? t('Always on for this event.')
                      : !availability[ch]
                        ? t('Provider not configured yet — set it up below to enable delivery.')
                        : undefined
                  }
                >
                  <Switch
                    size="sm"
                    checked={spec.locked ? true : !!effective[ch]}
                    disabled={spec.locked}
                    onChange={(v) => toggle(event.key, ch, v)}
                    label={CHANNEL_LABELS[ch] || ch}
                  />
                </div>
              ))}
            </div>
          </div>
        );
      })}

      {unavailableOffered.length > 0 && (
        <p className="text-xs text-tertiary">
          {t('Not deliverable yet (configure the channel below):')}{' '}
          {unavailableOffered.map((ch) => CHANNEL_LABELS[ch] || ch).join(', ')}
        </p>
      )}

      <div className="border-t border-border pt-4 space-y-3">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-tertiary">
          {t('Your contact details')}
        </p>
        <div className="grid @pair/stage:grid-cols-2 gap-3">
          <div>
            <label className="block text-[13px] font-medium text-ink mb-1">{t('Phone (SMS / WhatsApp / Signal)')}</label>
            <Input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+15551234567" />
          </div>
          <div>
            <label className="block text-[13px] font-medium text-ink mb-1">{t('Pushover user key')}</label>
            <Input
              value={pushoverKey}
              onChange={(e) => setPushoverKey(e.target.value)}
              placeholder={prefs.pushover_user_key_set ? t('•••••••• (saved)') : t('Pushover user key')}
            />
          </div>
        </div>
        <div className="flex justify-end">
          <Button onClick={saveContact} loading={savingContact} size="sm">
            {t('Save')}
          </Button>
        </div>
      </div>
    </div>
  );
};

/**
 * One delivery channel, as a DISCLOSURE row.
 *
 * All five providers used to render their full form at once — an SMTP block, a
 * Pushover block, Twilio plus its recipient list, WhatsApp, Signal — stacked
 * under identical hairlines with no containment. The page was a wall you had to
 * scroll to find the one channel you came to set up, and every field was equally
 * loud whether or not you used that channel.
 *
 * Collapsed, the five rows are a menu: name, what state it is in, done. Open one
 * and only that form exists. `Expand` is the app's shared collapsible (so this
 * animates like every other disclosure rather than snapping), and `mountOnEnter`
 * keeps four unopened provider forms — each with its own effects and fetches —
 * from mounting at all.
 */
/** Every delivery-channel component takes the same shape. */
type ProviderProps = {
  configured: boolean;
  enabled: boolean;
  refresh: () => void;
  open: boolean;
  onToggle: () => void;
};

const ProviderRow: React.FC<{
  title: string;
  configured: boolean;
  enabled: boolean;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}> = ({ title, configured, enabled, open, onToggle, children }) => {
  const { t } = useTranslation();
  const tone = configured && enabled ? 'on' : configured ? 'idle' : 'off';
  return (
    <div className="border-b border-border">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full items-center gap-3 py-3.5 text-left transition-colors hover:bg-hover/40"
      >
        <ChevronRightIcon
          className={clsx('h-4 w-4 shrink-0 text-tertiary transition-transform', open && 'rotate-90')}
        />
        <span className="min-w-0 flex-1 text-[13px] font-medium leading-snug text-ink">{title}</span>
        <span
          className={clsx(
            'inline-flex shrink-0 items-center gap-1.5 text-[12px]',
            tone === 'on' ? 'text-emerald-600' : tone === 'idle' ? 'text-amber-600' : 'text-tertiary',
          )}
        >
          <span
            className={clsx(
              'h-1.5 w-1.5 rounded-full',
              tone === 'on' ? 'bg-emerald-500' : tone === 'idle' ? 'bg-amber-500' : 'bg-active',
            )}
          />
          {configured ? (enabled ? t('Enabled') : t('Configured, off')) : t('Not set up')}
        </span>
      </button>
      <Expand open={open} mountOnEnter className="pb-5 pl-7 pr-1 space-y-4">
        {children}
      </Expand>
    </div>
  );
};

// Reusable recipient list for the phone providers.
const RecipientList: React.FC<{
  recipients: PhoneRecipient[];
  onAdd: (r: { name: string; phone_number: string }) => Promise<void>;
  onDelete: (id: number) => Promise<void>;
  onToggle: (id: number) => Promise<void>;
}> = ({ recipients, onAdd, onDelete, onToggle }) => {
  const { t } = useTranslation();
  const [name, setName] = useState('');
  const [phone, setPhone] = useState('');
  const [adding, setAdding] = useState(false);

  const handleAdd = async () => {
    if (!phone.trim()) return;
    setAdding(true);
    try {
      await onAdd({ name: name.trim() || phone.trim(), phone_number: phone.trim() });
      setName('');
      setPhone('');
    } finally {
      setAdding(false);
    }
  };

  return (
    <div className="space-y-2">
      <p className="text-[11px] font-semibold uppercase tracking-wider text-tertiary">{t('Recipients')}</p>
      {recipients.length === 0 && <p className="text-xs text-tertiary">{t('No recipients yet.')}</p>}
      {recipients.map((r) => (
        <div key={r.id} className="flex items-center gap-3 text-sm">
          <Switch checked={r.enabled} onChange={() => onToggle(r.id)} size="sm" aria-label={t('Enabled')} />
          <span className="flex-1 min-w-0 truncate">
            <span className="text-ink">{r.name}</span> <span className="text-tertiary font-mono">{r.phone_number}</span>
          </span>
          <button onClick={() => onDelete(r.id)} className="text-red-500 hover:text-red-600" title={t('Remove')}>
            <TrashIcon className="w-4 h-4" />
          </button>
        </div>
      ))}
      <div className="flex items-end gap-2 pt-1">
        <div className="flex-1">
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={t('Name')} />
        </div>
        <div className="flex-1">
          <Input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+15551234567" />
        </div>
        <Button size="sm" variant="secondary" onClick={handleAdd} loading={adding} disabled={!phone.trim()}>
          <PlusIcon className="w-4 h-4" />
          {t('Add')}
        </Button>
      </div>
    </div>
  );
};

// ── Email / SMTP ────────────────────────────────────────────────────────────
const EmailProvider: React.FC<ProviderProps> = ({
  configured,
  enabled,
  refresh,
  open,
  onToggle,
}) => {
  const { t } = useTranslation();
  const [form, setForm] = useState({
    smtp_host: '',
    smtp_port: 587,
    smtp_username: '',
    smtp_password: '',
    from_email: '',
    from_name: '',
    use_tls: true,
    enabled: true,
  });
  const [testTo, setTestTo] = useState('');
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    getEmailConfig()
      .then((c) =>
        setForm((f) => ({
          ...f,
          smtp_host: c.smtp_host || '',
          smtp_port: c.smtp_port || 587,
          smtp_username: c.smtp_username || '',
          from_email: c.from_email || '',
          from_name: c.from_name || '',
          use_tls: c.use_tls ?? true,
          enabled: c.enabled ?? true,
        })),
      )
      .catch(() => {});
  }, []);

  const save = async () => {
    setSaving(true);
    try {
      await updateEmailConfig(form);
      toast.success(t('Email settings saved'));
      refresh();
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to save')));
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    if (!testTo.trim()) return;
    setTesting(true);
    try {
      await sendTestEmail(testTo.trim());
      toast.success(t('Test email sent'));
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to send test')));
    } finally {
      setTesting(false);
    }
  };

  return (
    <ProviderRow open={open} onToggle={onToggle} title={t('Email (SMTP)')} configured={configured} enabled={enabled}>
      <div className="grid @pair/stage:grid-cols-2 gap-3">
        <Input value={form.smtp_host} onChange={(e) => setForm((f) => ({ ...f, smtp_host: e.target.value }))} placeholder={t('SMTP host')} />
        <NumberInput value={form.smtp_port} onChange={(v) => setForm((f) => ({ ...f, smtp_port: v ?? 587 }))} min={1} max={65535} />
        <Input value={form.smtp_username} onChange={(e) => setForm((f) => ({ ...f, smtp_username: e.target.value }))} placeholder={t('SMTP username')} />
        <Input type="password" value={form.smtp_password} onChange={(e) => setForm((f) => ({ ...f, smtp_password: e.target.value }))} placeholder={configured ? t('•••••• (leave blank to keep)') : t('SMTP password')} />
        <Input value={form.from_email} onChange={(e) => setForm((f) => ({ ...f, from_email: e.target.value }))} placeholder={t('From email')} />
        <Input value={form.from_name} onChange={(e) => setForm((f) => ({ ...f, from_name: e.target.value }))} placeholder={t('From name')} />
      </div>
      <div className="flex items-center gap-6">
        <Switch checked={form.use_tls} onChange={(v) => setForm((f) => ({ ...f, use_tls: v }))} label={t('Use TLS')} size="sm" />
        <Switch checked={form.enabled} onChange={(v) => setForm((f) => ({ ...f, enabled: v }))} label={t('Enabled')} size="sm" />
        <div className="ml-auto">
          <Button onClick={save} loading={saving} size="sm">{t('Save')}</Button>
        </div>
      </div>
      <div className="flex items-end gap-2 border-t border-border pt-3">
        <div className="flex-1">
          <Input value={testTo} onChange={(e) => setTestTo(e.target.value)} placeholder={t('Send a test to…')} />
        </div>
        <Button variant="secondary" size="sm" onClick={test} loading={testing} disabled={!testTo.trim()}>
          <PaperAirplaneIcon className="w-4 h-4" />
          {t('Test')}
        </Button>
      </div>
    </ProviderRow>
  );
};

// ── Pushover ────────────────────────────────────────────────────────────────
const PushoverProvider: React.FC<ProviderProps> = ({
  configured,
  enabled,
  refresh,
  open,
  onToggle,
}) => {
  const { t } = useTranslation();
  const [appToken, setAppToken] = useState('');
  const [userKey, setUserKey] = useState('');
  const [saving, setSaving] = useState(false);

  const save = async () => {
    if (!appToken.trim() || !userKey.trim()) {
      toast.error(t('App token and user key are required.'));
      return;
    }
    setSaving(true);
    try {
      await updatePushoverConfig({ app_token: appToken.trim(), user_key: userKey.trim() });
      toast.success(t('Pushover settings saved'));
      setAppToken('');
      setUserKey('');
      refresh();
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to save')));
    } finally {
      setSaving(false);
    }
  };

  return (
    <ProviderRow open={open} onToggle={onToggle} title={t('Pushover')} configured={configured} enabled={enabled}>
      <div className="grid @pair/stage:grid-cols-2 gap-3">
        <Input value={appToken} onChange={(e) => setAppToken(e.target.value)} placeholder={configured ? t('App token (leave blank to keep)') : t('App token')} />
        <Input value={userKey} onChange={(e) => setUserKey(e.target.value)} placeholder={configured ? t('User/group key (leave blank to keep)') : t('User/group key')} />
      </div>
      <div className="flex justify-end">
        <Button onClick={save} loading={saving} size="sm">{t('Save')}</Button>
      </div>
    </ProviderRow>
  );
};

// ── Twilio SMS ──────────────────────────────────────────────────────────────
const TwilioProvider: React.FC<ProviderProps> = ({
  configured,
  enabled,
  refresh,
  open,
  onToggle,
}) => {
  const { t } = useTranslation();
  const [form, setForm] = useState({ account_sid: '', auth_token: '', from_phone: '', enabled: true });
  const [recipients, setRecipients] = useState<TwilioRecipient[]>([]);
  const [testTo, setTestTo] = useState('');
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);

  const loadRecipients = () => getTwilioRecipients().then(setRecipients).catch(() => {});

  useEffect(() => {
    getTwilioConfig()
      .then((c) => setForm((f) => ({ ...f, account_sid: c.account_sid || '', from_phone: c.from_phone || '', enabled: c.enabled ?? true })))
      .catch(() => {});
    loadRecipients();
  }, []);

  const save = async () => {
    setSaving(true);
    try {
      await updateTwilioConfig(form);
      toast.success(t('Twilio settings saved'));
      refresh();
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to save')));
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    if (!testTo.trim()) return;
    setTesting(true);
    try {
      await sendTestTwilioSMS(testTo.trim());
      toast.success(t('Test SMS sent'));
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to send test')));
    } finally {
      setTesting(false);
    }
  };

  return (
    <ProviderRow open={open} onToggle={onToggle} title={t('Twilio SMS')} configured={configured} enabled={enabled}>
      <div className="grid @pair/stage:grid-cols-2 gap-3">
        <Input value={form.account_sid} onChange={(e) => setForm((f) => ({ ...f, account_sid: e.target.value }))} placeholder={t('Account SID')} />
        <Input type="password" value={form.auth_token} onChange={(e) => setForm((f) => ({ ...f, auth_token: e.target.value }))} placeholder={configured ? t('Auth token (leave blank to keep)') : t('Auth token')} />
        <Input value={form.from_phone} onChange={(e) => setForm((f) => ({ ...f, from_phone: e.target.value }))} placeholder={t('From phone')} />
      </div>
      <div className="flex items-center gap-6">
        <Switch checked={form.enabled} onChange={(v) => setForm((f) => ({ ...f, enabled: v }))} label={t('Enabled')} size="sm" />
        <div className="ml-auto">
          <Button onClick={save} loading={saving} size="sm">{t('Save')}</Button>
        </div>
      </div>
      <div className="flex items-end gap-2 border-t border-border pt-3">
        <div className="flex-1">
          <Input value={testTo} onChange={(e) => setTestTo(e.target.value)} placeholder="+15551234567" />
        </div>
        <Button variant="secondary" size="sm" onClick={test} loading={testing} disabled={!testTo.trim()}>
          <PaperAirplaneIcon className="w-4 h-4" />
          {t('Test')}
        </Button>
      </div>
      <div className="border-t border-border pt-3">
        <RecipientList
          recipients={recipients}
          onAdd={async (r) => {
            await addTwilioRecipient(r);
            await loadRecipients();
          }}
          onDelete={async (id) => {
            await deleteTwilioRecipient(id);
            await loadRecipients();
          }}
          onToggle={async (id) => {
            await toggleTwilioRecipient(id);
            await loadRecipients();
          }}
        />
      </div>
    </ProviderRow>
  );
};

// ── WhatsApp ────────────────────────────────────────────────────────────────
const WhatsAppProvider: React.FC<ProviderProps> = ({
  configured,
  enabled,
  refresh,
  open,
  onToggle,
}) => {
  const { t } = useTranslation();
  const [form, setForm] = useState({ account_sid: '', auth_token: '', from_number: '', enabled: true });
  const [recipients, setRecipients] = useState<WhatsAppRecipient[]>([]);
  const [testTo, setTestTo] = useState('');
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);

  const loadRecipients = () => getWhatsAppRecipients().then(setRecipients).catch(() => {});

  useEffect(() => {
    getWhatsAppConfig()
      .then((c) => setForm((f) => ({ ...f, account_sid: c.account_sid || '', from_number: c.from_number || '', enabled: c.enabled ?? true })))
      .catch(() => {});
    loadRecipients();
  }, []);

  const save = async () => {
    setSaving(true);
    try {
      await updateWhatsAppConfig(form);
      toast.success(t('WhatsApp settings saved'));
      refresh();
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to save')));
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    if (!testTo.trim()) return;
    setTesting(true);
    try {
      await sendTestWhatsAppMessage(testTo.trim());
      toast.success(t('Test message sent'));
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to send test')));
    } finally {
      setTesting(false);
    }
  };

  return (
    <ProviderRow open={open} onToggle={onToggle} title={t('WhatsApp')} configured={configured} enabled={enabled}>
      <div className="grid @pair/stage:grid-cols-2 gap-3">
        <Input value={form.account_sid} onChange={(e) => setForm((f) => ({ ...f, account_sid: e.target.value }))} placeholder={t('Account SID')} />
        <Input type="password" value={form.auth_token} onChange={(e) => setForm((f) => ({ ...f, auth_token: e.target.value }))} placeholder={configured ? t('Auth token (leave blank to keep)') : t('Auth token')} />
        <Input value={form.from_number} onChange={(e) => setForm((f) => ({ ...f, from_number: e.target.value }))} placeholder={t('From number')} />
      </div>
      <div className="flex items-center gap-6">
        <Switch checked={form.enabled} onChange={(v) => setForm((f) => ({ ...f, enabled: v }))} label={t('Enabled')} size="sm" />
        <div className="ml-auto">
          <Button onClick={save} loading={saving} size="sm">{t('Save')}</Button>
        </div>
      </div>
      <div className="flex items-end gap-2 border-t border-border pt-3">
        <div className="flex-1">
          <Input value={testTo} onChange={(e) => setTestTo(e.target.value)} placeholder="+15551234567" />
        </div>
        <Button variant="secondary" size="sm" onClick={test} loading={testing} disabled={!testTo.trim()}>
          <PaperAirplaneIcon className="w-4 h-4" />
          {t('Test')}
        </Button>
      </div>
      <div className="border-t border-border pt-3">
        <RecipientList
          recipients={recipients}
          onAdd={async (r) => {
            await addWhatsAppRecipient(r);
            await loadRecipients();
          }}
          onDelete={async (id) => {
            await deleteWhatsAppRecipient(id);
            await loadRecipients();
          }}
          onToggle={async (id) => {
            await toggleWhatsAppRecipient(id);
            await loadRecipients();
          }}
        />
      </div>
    </ProviderRow>
  );
};

// ── Signal ──────────────────────────────────────────────────────────────────
const SignalProvider: React.FC<ProviderProps> = ({
  configured,
  enabled,
  refresh,
  open,
  onToggle,
}) => {
  const { t } = useTranslation();
  const [form, setForm] = useState({ api_url: '', sender_number: '', enabled: true });
  const [recipients, setRecipients] = useState<SignalRecipient[]>([]);
  const [testTo, setTestTo] = useState('');
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);

  const loadRecipients = () => getSignalRecipients().then(setRecipients).catch(() => {});

  useEffect(() => {
    getSignalConfig()
      .then((c) => setForm((f) => ({ ...f, api_url: c.api_url || '', sender_number: c.sender_number || '', enabled: c.enabled ?? true })))
      .catch(() => {});
    loadRecipients();
  }, []);

  const save = async () => {
    setSaving(true);
    try {
      await updateSignalConfig(form);
      toast.success(t('Signal settings saved'));
      refresh();
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to save')));
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    if (!testTo.trim()) return;
    setTesting(true);
    try {
      await sendTestSignalMessage(testTo.trim());
      toast.success(t('Test message sent'));
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to send test')));
    } finally {
      setTesting(false);
    }
  };

  return (
    <ProviderRow open={open} onToggle={onToggle} title={t('Signal')} configured={configured} enabled={enabled}>
      <div className="grid @pair/stage:grid-cols-2 gap-3">
        <Input value={form.api_url} onChange={(e) => setForm((f) => ({ ...f, api_url: e.target.value }))} placeholder={t('signal-cli REST API URL')} />
        <Input value={form.sender_number} onChange={(e) => setForm((f) => ({ ...f, sender_number: e.target.value }))} placeholder={t('Sender number')} />
      </div>
      <div className="flex items-center gap-6">
        <Switch checked={form.enabled} onChange={(v) => setForm((f) => ({ ...f, enabled: v }))} label={t('Enabled')} size="sm" />
        <div className="ml-auto">
          <Button onClick={save} loading={saving} size="sm">{t('Save')}</Button>
        </div>
      </div>
      <div className="flex items-end gap-2 border-t border-border pt-3">
        <div className="flex-1">
          <Input value={testTo} onChange={(e) => setTestTo(e.target.value)} placeholder="+15551234567" />
        </div>
        <Button variant="secondary" size="sm" onClick={test} loading={testing} disabled={!testTo.trim()}>
          <PaperAirplaneIcon className="w-4 h-4" />
          {t('Test')}
        </Button>
      </div>
      <div className="border-t border-border pt-3">
        <RecipientList
          recipients={recipients}
          onAdd={async (r) => {
            await addSignalRecipient(r);
            await loadRecipients();
          }}
          onDelete={async (id) => {
            await deleteSignalRecipient(id);
            await loadRecipients();
          }}
          onToggle={async (id) => {
            await toggleSignalRecipient(id);
            await loadRecipients();
          }}
        />
      </div>
    </ProviderRow>
  );
};

// ── Global behavior (quiet hours / rate limit / batching / health) ──────────
const BehaviorCard: React.FC = () => {
  const { t } = useTranslation();
  const [data, setData] = useState<NotificationSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getNotificationSettings()
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const set = <K extends keyof NotificationSettings>(key: K, value: NotificationSettings[K]) => {
    setData((d) => (d ? { ...d, [key]: value } : d));
  };

  const save = async () => {
    if (!data) return;
    setSaving(true);
    try {
      const next = await updateNotificationSettings(data);
      setData(next);
      toast.success(t('Notification behavior saved'));
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to save')));
    } finally {
      setSaving(false);
    }
  };

  if (loading || !data) return <div className="text-sm text-secondary">{t('Loading...')}</div>;

  return (
    <div className="pt-4 space-y-5">
      <Switch
        checked={data.quiet_hours_enabled}
        onChange={(v) => set('quiet_hours_enabled', v)}
        label={t('Quiet hours')}
        description={t('Suppress notifications during a daily window.')}
        reverse
      />
      {data.quiet_hours_enabled && (
        <div className="grid @pair/stage:grid-cols-2 gap-3 pl-1">
          <div>
            <label className="block text-[13px] font-medium text-ink mb-1">{t('Start (HH:MM)')}</label>
            <Input value={data.quiet_hours_start || ''} onChange={(e) => set('quiet_hours_start', e.target.value)} placeholder="22:00" />
          </div>
          <div>
            <label className="block text-[13px] font-medium text-ink mb-1">{t('End (HH:MM)')}</label>
            <Input value={data.quiet_hours_end || ''} onChange={(e) => set('quiet_hours_end', e.target.value)} placeholder="07:00" />
          </div>
        </div>
      )}

      <Switch
        checked={data.rate_limiting_enabled}
        onChange={(v) => set('rate_limiting_enabled', v)}
        label={t('Rate limiting')}
        description={t('Cap how many notifications go out per period.')}
        reverse
      />
      {data.rate_limiting_enabled && (
        <div className="grid @pair/stage:grid-cols-2 gap-3 pl-1">
          <div>
            <label className="block text-[13px] font-medium text-ink mb-1">{t('Max per period')}</label>
            <NumberInput min={1} value={data.rate_limit_max} onChange={(v) => set('rate_limit_max', v ?? 5)} />
          </div>
          <div>
            <label className="block text-[13px] font-medium text-ink mb-1">{t('Period')}</label>
            <Input value={data.rate_limit_period} onChange={(e) => set('rate_limit_period', e.target.value)} placeholder="hour" />
          </div>
        </div>
      )}

      <Switch
        checked={data.batch_enabled}
        onChange={(v) => set('batch_enabled', v)}
        label={t('Batch notifications')}
        description={t('Group changes detected close together into one message.')}
        reverse
      />
      {data.batch_enabled && (
        <div className="pl-1">
          <label className="block text-[13px] font-medium text-ink mb-1">{t('Batch window (minutes)')}</label>
          <NumberInput min={1} value={data.batch_window_minutes} onChange={(v) => set('batch_window_minutes', v ?? 5)} />
        </div>
      )}

      <div className="border-t border-border pt-4 space-y-4">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-tertiary">{t('Health alert thresholds')}</p>
        <Switch checked={data.target_health_enabled} onChange={(v) => set('target_health_enabled', v)} label={t('Monitor health alerts')} size="sm" reverse />
        <Switch checked={data.system_health_enabled} onChange={(v) => set('system_health_enabled', v)} label={t('Agent disconnect alerts')} size="sm" reverse />
      </div>

      <div className="flex justify-end">
        <Button onClick={save} loading={saving}>{t('Save')}</Button>
      </div>
    </div>
  );
};

/**
 * Notifications — provider configuration (Email/SMTP, Pushover, Twilio SMS,
 * WhatsApp, Signal) with per-provider test + recipients, plus global behavior
 * (quiet hours, rate limiting, batching, health thresholds).
 */
export const NotificationsSection: React.FC = () => {
  const { t } = useTranslation();
  const [status, setStatus] = useState<Record<string, { configured: boolean; enabled: boolean }>>({});
  // Accordion: one channel open at a time. Configuring a provider is a
  // one-at-a-time job, and letting several stand open rebuilds the wall of forms
  // this page was. `null` = all collapsed, which is the useful default — the
  // collapsed list reads as a menu of channels and their state.
  const [openProvider, setOpenProvider] = useState<string | null>(null);
  const toggleProvider = (key: string) =>
    setOpenProvider((cur) => (cur === key ? null : key));

  const refresh = () => {
    getAllProviders()
      .then((r) => {
        const p = r.providers;
        setStatus({
          email: p.email,
          pushover: p.pushover,
          twilio: p.twilio,
          whatsapp: p.whatsapp,
          signal: p.signal,
        });
      })
      .catch(() => {});
  };

  useEffect(() => {
    refresh();
  }, []);

  const st = (k: string) => status[k] || { configured: false, enabled: false };

  // Shared by all five rows; only `configured`/`enabled` differ per provider.
  const rowProps = (key: string) => ({
    configured: st(key).configured,
    enabled: st(key).enabled,
    refresh,
    open: openProvider === key,
    onToggle: () => toggleProvider(key),
  });

  return (
    // ONE SectionHead for the tab, then hairline-ruled groups — the rhythm
    // SectionHead's own contract describes. This page used to stack FOUR
    // SectionHeads, so four different things all announced themselves with the
    // same h2 and nothing read as subordinate to anything else.
    <div className="space-y-9">
      <SectionHead
        title={t('Notifications')}
        description={t('Where alerts reach you, which channels are set up, and how they are throttled.')}
      />

      <SettingGroup
        title={t("What you're notified about")}
        description={t('Choose the channels each platform event reaches you on.')}
      >
        <PreferenceMatrixCard />
      </SettingGroup>

      <SettingGroup
        title={t('Delivery channels')}
        description={t('Open a channel to set it up, send a test, or manage its recipients.')}
      >
        <EmailProvider {...rowProps('email')} />
        <PushoverProvider {...rowProps('pushover')} />
        <TwilioProvider {...rowProps('twilio')} />
        <WhatsAppProvider {...rowProps('whatsapp')} />
        <SignalProvider {...rowProps('signal')} />
      </SettingGroup>

      <SettingGroup title={t('Global behavior')} description={t('Applies across every channel.')}>
        <BehaviorCard />
      </SettingGroup>
    </div>
  );
};

export default NotificationsSection;
