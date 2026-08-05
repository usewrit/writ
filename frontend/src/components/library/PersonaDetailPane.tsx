import React, { useState, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import {
  IdentificationIcon,
  ShieldCheckIcon,
  KeyIcon,
  PencilSquareIcon,
  TrashIcon,
  CheckBadgeIcon,
  ClockIcon,
  GlobeAltIcon,
  FingerPrintIcon,
  ArrowTopRightOnSquareIcon,
  UserCircleIcon,
  SignalIcon,
  EnvelopeIcon,
  CpuChipIcon,
} from '@heroicons/react/24/outline';
import { useQuery } from '../../hooks/useQuery';
import { personasApi } from '../../api/endpoints';
import type { Persona, TwoFactorMethod, PersonaRun } from '../../types/api';
import { formatRelativeTime, formatDate } from '../../utils/format';
import { tintStyle } from '../../utils/tint';
import { statusStyle } from '../../utils/statusStyle';
import { EmptyHero } from '../ui';

const TWOFA_LABELS: Record<TwoFactorMethod, string> = {
  none: 'No 2FA',
  totp: 'Authenticator (TOTP)',
  email_otp: 'Email code',
  sms: 'Text message (SMS)',
};

interface SessionState { dot: string; label: string; detail?: string; }
function sessionState(p: Persona, t: (k: string, o?: any) => string): SessionState {
  if (!p.has_warm_session) return { dot: 'bg-active', label: t('No saved session') };
  if (p.validation_status === 'expired') {
    return {
      dot: 'bg-warning',
      label: t('Session expired'),
      detail: p.last_login_at ? t('Saved {{when}}', { when: formatRelativeTime(p.last_login_at) }) : undefined,
    };
  }
  return {
    dot: 'bg-success',
    label: t('Session valid'),
    detail: p.session_expires_at ? t('Expires {{when}}', { when: formatRelativeTime(p.session_expires_at) }) : undefined,
  };
}

const SectionLabel: React.FC<{ children: React.ReactNode; right?: React.ReactNode }> = ({ children, right }) => (
  <div className="flex items-center justify-between mb-2">
    <span className="text-[10px] font-semibold uppercase tracking-wider text-secondary">{children}</span>
    {right}
  </div>
);

const Fact: React.FC<{ icon: React.FC<any>; label: string; children: React.ReactNode }> = ({ icon: Icon, label, children }) => (
  <div className="flex items-center gap-2 min-w-0">
    <Icon className="h-3.5 w-3.5 text-tertiary shrink-0" />
    <span className="text-[11px] text-tertiary shrink-0">{label}</span>
    <span className="ml-auto text-[12px] text-ink text-right truncate min-w-0">{children}</span>
  </div>
);

/** Compact recent-runs feed for the selected persona. */
const PersonaRuns: React.FC<{ personaId: number }> = ({ personaId }) => {
  const { t } = useTranslation();
  const { data: runs, loading } = useQuery<PersonaRun[]>(
    `persona-runs:${personaId}`,
    () => personasApi.runs(personaId).catch(() => []),
  );
  if (loading && !runs) return <div className="h-12 rounded-lg bg-hover animate-pulse" />;
  if (!runs || runs.length === 0) return <p className="text-[12px] text-tertiary">{t('No runs with this persona yet.')}</p>;
  return (
    <div className="rounded-xl border border-ink/20 bg-surface divide-y divide-border overflow-hidden">
      {runs.slice(0, 6).map((r) => (
        <div key={r.task_id} className="flex items-center justify-between gap-3 px-3 py-2.5 text-xs">
          <div className="min-w-0 flex items-center gap-2">
            <span className={clsx('w-2 h-2 rounded-full shrink-0', statusStyle(r.success ? 'success' : r.status).dot)} />
            <span className="text-ink truncate">{r.workflow_name || t('Workflow {{id}}', { id: r.workflow_id ?? '' })}</span>
          </div>
          <span className="text-[11px] text-tertiary shrink-0">{(r.completed_at || r.started_at) ? formatRelativeTime(r.completed_at || r.started_at || '') : ''}</span>
        </div>
      ))}
    </div>
  );
};

interface PersonaDetailPaneProps {
  persona: Persona | null;
  onEdit: (p: Persona) => void;
  onDelete: (p: Persona) => void;
  onSend: (p: Persona) => void;
}

/**
 * Right pane of the personas master–detail. Shows the selected identity at a
 * glance — saved-session health, 2FA method (with a one-tap test), credential /
 * fingerprint / proxy facts, the workflows it's attached to, and recent runs.
 *
 * (There is no mailbox-connect surface; any mailbox recorded earlier shows
 * read-only.)
 */
export const PersonaDetailPane: React.FC<PersonaDetailPaneProps> = ({ persona: p, onEdit, onDelete, onSend }) => {
  const { t } = useTranslation();
  const [testing, setTesting] = useState(false);

  // There is no mailbox-connect flow (Gmail/Outlook OAuth + IMAP) in this build.
  // Any mailbox already recorded on the persona is shown read-only below.

  const handleTest = useCallback(async () => {
    if (!p) return;
    setTesting(true);
    const toastId = toast.loading(t('Testing 2FA…'));
    try {
      const r = await personasApi.test2fa(p.id);
      toast.success(r.message || t('2FA OK ({{detail}})', { detail: `${r.method}${r.kind ? `, ${r.kind}` : ''}` }), { id: toastId });
    } catch (err: any) {
      const d = err?.response?.data?.detail;
      toast.error(typeof d === 'string' ? d : t('2FA test failed'), { id: toastId });
    } finally {
      setTesting(false);
    }
  }, [p, t]);

  if (!p) {
    return (
      <EmptyHero
        icon={IdentificationIcon}
        title={t('Select a persona')}
        description={t('Pick one on the left to see its saved session, 2FA, and where it’s used.')}
        className="flex-1"
      />
    );
  }

  const session = sessionState(p, t);
  const wfs = p.linked_workflows || [];
  const has2fa = p.twofa_method !== 'none';
  const showMailbox = p.twofa_method === 'email_otp';

  return (
    <div className="flex flex-1 flex-col min-h-0">
      {/* ── Header ── */}
      <div className="px-5 pt-5 pb-4 border-b border-border">
        <div className="flex items-start gap-3">
          <div
            style={tintStyle('neutral')}
            className="relative w-10 h-10 rounded-lg flex items-center justify-center shrink-0"
          >
            <IdentificationIcon className="h-5 w-5" />
            <span className={clsx('absolute -bottom-1 -right-1 w-3.5 h-3.5 rounded-full border-2 border-surface', session.dot)} />
          </div>

          <div className="flex-1 min-w-0">
            <div className="text-[15px] font-semibold text-ink truncate" title={p.name}>{p.name}</div>
            <div className="flex items-center gap-2 mt-1 text-[11px] text-tertiary flex-wrap">
              {p.target_domain && <span className="truncate">{p.target_domain}</span>}
              {p.target_domain && <span className="text-tertiary/50">·</span>}
              <span className="inline-flex items-center gap-1"><ShieldCheckIcon className="w-3 h-3" />{TWOFA_LABELS[p.twofa_method]}</span>
            </div>
          </div>

          {has2fa && (
            <button
              onClick={handleTest}
              disabled={testing}
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-border text-[11px] font-medium text-ink hover:bg-chrome shrink-0 transition-colors disabled:opacity-50"
            >
              <ShieldCheckIcon className="h-3.5 w-3.5" />
              {testing ? t('Testing…') : t('Test 2FA')}
            </button>
          )}
        </div>

        {/* Session status */}
        <div className="flex items-center gap-2 mt-4">
          <span className={clsx('w-2.5 h-2.5 rounded-full shrink-0', session.dot)} />
          <span className="text-[13px] font-medium text-ink">{session.label}</span>
          {session.detail && <span className="text-[11px] text-tertiary ml-auto shrink-0">{session.detail}</span>}
        </div>

        {/* Actions */}
        <div className="flex items-center gap-1.5 mt-3">
          <button
            onClick={() => onEdit(p)}
            className="flex items-center gap-1.5 px-2.5 py-1.5 text-[12px] font-medium text-secondary border border-border rounded-lg hover:bg-chrome hover:text-ink transition-colors"
          >
            <PencilSquareIcon className="h-3.5 w-3.5" />
            {t('Edit')}
          </button>
          <button
            onClick={() => onSend(p)}
            className="flex items-center gap-1.5 px-2.5 py-1.5 text-[12px] font-medium text-secondary border border-border rounded-lg hover:bg-chrome hover:text-ink transition-colors"
          >
            <CpuChipIcon className="h-3.5 w-3.5" />
            {t('Send to agent')}
          </button>
          <div className="flex-1" />
          <button
            onClick={() => onDelete(p)}
            className="flex items-center gap-1.5 px-2.5 py-1.5 text-[12px] font-medium text-danger rounded-lg hover:bg-danger-bg hover:text-danger-fg transition-colors"
          >
            <TrashIcon className="h-3.5 w-3.5" />
            {t('Delete')}
          </button>
        </div>
      </div>

      {/* ── Body ── */}
      <div className="flex-1 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden px-5 py-4 space-y-6">
        {/* Facts */}
        <div>
          <SectionLabel>{t('Identity')}</SectionLabel>
          <div className="space-y-2 rounded-xl border border-ink/20 bg-surface p-3">
            {p.target_domain && <Fact icon={GlobeAltIcon} label={t('Domain')}>{p.target_domain}</Fact>}
            {p.login_username && <Fact icon={KeyIcon} label={t('Username')}>{p.login_username}</Fact>}
            <Fact icon={ShieldCheckIcon} label={t('2FA')}>{TWOFA_LABELS[p.twofa_method]}</Fact>
            <Fact icon={KeyIcon} label={t('Password')}>{p.has_password ? t('Saved') : t('None')}</Fact>
            <Fact icon={FingerPrintIcon} label={t('Fingerprint')}>{p.has_fingerprint ? t('Set') : t('Default')}</Fact>
            <Fact icon={SignalIcon} label={t('Proxy')}>{p.has_proxy ? t('Configured') : t('None')}</Fact>
            {p.connected_mailbox && <Fact icon={UserCircleIcon} label={t('Mailbox')}>{p.connected_mailbox}</Fact>}
            <Fact icon={ClockIcon} label={t('Last used')}>
              {p.last_used_at ? <span title={formatDate(p.last_used_at)}>{formatRelativeTime(p.last_used_at)}</span> : t('Never')}
            </Fact>
          </div>
        </div>

        {/* Saved session */}
        <div>
          <SectionLabel>{t('Saved login')}</SectionLabel>
          <div className="rounded-xl border border-ink/20 bg-surface p-3 space-y-2">
            <div className="flex items-center gap-2">
              <CheckBadgeIcon className={clsx('w-4 h-4 shrink-0', p.has_warm_session ? (p.validation_status === 'expired' ? 'text-warning' : 'text-success') : 'text-tertiary')} />
              <span className="text-[12px] text-ink">{session.label}</span>
            </div>
            <p className="text-[11px] text-tertiary">
              {p.has_warm_session
                ? t('A logged-in session is reused so this persona skips the login step on the next run.')
                : t('No warm session yet — the first run will log in and save one.')}
            </p>
          </div>
        </div>

        {/* Connected mailbox (email-OTP source), shown read-only. */}
        {showMailbox && p.connected_mailbox && (
          <div>
            <SectionLabel>{t('Connected mailbox')}</SectionLabel>
            <div className="rounded-xl border border-ink/20 bg-surface p-3">
              <span className="flex items-center gap-1.5 text-[12px] text-ink truncate">
                <EnvelopeIcon className="w-4 h-4 text-tertiary shrink-0" />{p.connected_mailbox}
              </span>
            </div>
          </div>
        )}

        {/* Linked workflows */}
        <div>
          <SectionLabel>{t('Attached to')}</SectionLabel>
          {wfs.length === 0 ? (
            <p className="text-[12px] text-tertiary">{t('Not attached to any workflow.')}</p>
          ) : (
            <div className="rounded-xl border border-ink/20 bg-surface divide-y divide-border overflow-hidden">
              {wfs.map((w) => (
                <Link key={w.id} to={`/workflows/${w.id}`} className="flex items-center gap-2 px-3 py-2.5 text-[12px] text-ink hover:bg-chrome transition-colors">
                  <ArrowTopRightOnSquareIcon className="w-3.5 h-3.5 text-tertiary shrink-0" />
                  <span className="truncate">{w.name}</span>
                </Link>
              ))}
            </div>
          )}
        </div>

        {/* Recent runs — admin-only, re-homed */}
        <div>
          <SectionLabel>{t('Recent runs')}</SectionLabel>
          <PersonaRuns personaId={p.id} />
        </div>
      </div>
    </div>
  );
};
