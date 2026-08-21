import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import {
  ArrowPathIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  EyeIcon,
  KeyIcon,
  SparklesIcon,
  VideoCameraIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { useQuery } from '../../hooks/useQuery';
import { personasApi, automationApi } from '../../api/endpoints';
import { Q } from '../../stores/queryKeys';
import { useQueryCache } from '../../stores/queryCache';
import type { Persona } from '../../types/api';
import { Button, Select } from '../ui';
import { AIPreviewModal } from '../AIPreviewModal';

interface Props {
  /** The persona to configure. Null renders nothing (nothing to attach a login to). */
  persona: Persona | null;
  /** Called after the link changes or a sign-in finishes, so the parent can refetch. */
  onChanged?: () => void;
  /** Where the recorder should return to. Defaults to the current location. */
  returnTo?: string;
  /** Drop the heading — for places that already have their own section label. */
  hideHeading?: boolean;
  /**
   * Start the AI login recording immediately on mount (once). For the persona
   * wizard, where the user already chose "let AI sign in" before the persona
   * existed — the recording begins the moment it does.
   */
  autoStartAi?: boolean;
}

/** A session is only meaningful while unexpired; the API sends the expiry it stamped. */
function sessionState(p: Persona): 'live' | 'expired' | 'none' {
  if (!p.has_warm_session) return 'none';
  if (p.session_expires_at && new Date(p.session_expires_at).getTime() <= Date.now()) {
    return 'expired';
  }
  return 'live';
}

/**
 * Sign-in setup for a persona: attach or record the workflow that LOGS IT IN,
 * then run it on demand.
 *
 * Why this exists: a persona's warm session could previously only be CAPTURED from
 * something that had already signed in, so a persona created from credentials alone
 * could never satisfy the authenticated-crawl precondition and the crawl failed with
 * an error that had no route out of it. The login workflow is that route — and once
 * attached, the crawl seeder re-runs it automatically whenever it finds the session
 * stale, so an expired session stops being a user-visible failure at all.
 */
export const PersonaLoginWorkflow: React.FC<Props> = ({
  persona,
  onChanged,
  returnTo,
  hideHeading = false,
  autoStartAi = false,
}) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [busy, setBusy] = useState<'sign-in' | 'attach' | 'ai' | null>(null);
  const [signInError, setSignInError] = useState<string | null>(null);
  // Live AI login recording: the launched session id, its latest progress line,
  // and whether the spectate modal is open. Cleared when the session finishes.
  const [aiSessionId, setAiSessionId] = useState<number | null>(null);
  const [aiProgress, setAiProgress] = useState<string | null>(null);
  const [watchOpen, setWatchOpen] = useState(false);

  // Every workflow the tenant owns is a candidate. Crawl workflows — orchestrator
  // machinery rather than a recorded sign-in — are already excluded server-side by
  // GET /automation/workflows, so there is nothing to filter here.
  const { data: workflows } = useQuery(Q.workflows(), () => automationApi.listWorkflows());
  const attachable = useMemo(() => workflows || [], [workflows]);

  const refresh = useCallback(() => {
    // The persona list feeds the pickers. This app's invalidate() is EXACT-match
    // on string keys, so `invalidate(Q.personas())` would miss every cached
    // `personas:{domain}` variant — invalidateMatching is the prefix form here.
    useQueryCache.getState().invalidateMatching('personas');
    onChanged?.();
  }, [onChanged]);

  const attach = useCallback(
    async (workflowId: number | null) => {
      if (!persona) return;
      setBusy('attach');
      setSignInError(null);
      try {
        await personasApi.update(persona.id, { login_workflow_id: workflowId });
        refresh();
        toast.success(workflowId ? t('Login workflow attached') : t('Login workflow removed'));
      } catch (e: any) {
        toast.error(e?.response?.data?.detail || t('Could not update the login workflow'));
      } finally {
        setBusy(null);
      }
    },
    [persona, refresh, t],
  );

  const signIn = useCallback(async () => {
    if (!persona) return;
    setBusy('sign-in');
    setSignInError(null);
    try {
      // force: the button means "sign in now", so a session that merely LOOKS live
      // (but the site has already invalidated) must still be replaced — otherwise
      // the one control for a broken session would no-op.
      const res = await personasApi.signIn(persona.id, true);
      if (res.ok) {
        // ok is now true only when the backend VERIFIED real auth material was
        // captured (a session/auth cookie or token store) — not just any cookie —
        // so this success is trustworthy, not a "the steps ran" green light.
        toast.success(t('Signed in — session captured'));
        setSignInError(null);
      } else {
        // Rendered inline rather than only as a toast: these messages are the
        // instructions for fixing the login, and a toast disappears before the
        // user can act on one.
        setSignInError(res.error || t('Sign-in failed'));
      }
      refresh();
    } catch (e: any) {
      setSignInError(e?.response?.data?.detail || e?.message || t('Sign-in failed'));
    } finally {
      setBusy(null);
    }
  }, [persona, refresh, t]);

  const recordLogin = useCallback(() => {
    if (!persona) return;
    const q = new URLSearchParams({
      intent: 'callable',
      login_for_persona: String(persona.id),
      name: t('{{persona}} login', { persona: persona.name }),
    });
    if (persona.target_domain) q.set('url', `https://${persona.target_domain}`);
    // Where to come back to once the recording is saved and attached.
    q.set('login_return_to', returnTo || `${window.location.pathname}${window.location.search}`);
    navigate(`/workflows/new?${q.toString()}`);
  }, [persona, navigate, returnTo, t]);

  // "Let AI record it": launch the backend AI session that signs in as this
  // persona and records the flow. Everything after launch is backend-owned —
  // on completion the workflow is wired onto the persona server-side — so this
  // component only polls for the outcome (and offers a live Watch).
  const recordLoginAi = useCallback(async () => {
    if (!persona) return;
    setBusy('ai');
    setSignInError(null);
    try {
      const res = await personasApi.recordLoginAi(persona.id);
      setAiSessionId(res.session_id);
      setAiProgress(null);
      if (res.already_running) {
        toast(t('A login recording is already running — showing it'));
      }
    } catch (e: any) {
      setSignInError(e?.response?.data?.detail || e?.message || t('Could not start the AI recording'));
    } finally {
      setBusy(null);
    }
  }, [persona, t]);

  // Wizard hand-off: the user chose "let AI sign in" before the persona existed,
  // so start the recording as soon as it does — once per persona, and never on
  // top of a login this persona already has.
  const autoStartedRef = useRef<number | null>(null);
  useEffect(() => {
    if (!autoStartAi || !persona || persona.login_workflow_id) return;
    if (autoStartedRef.current === persona.id) return;
    autoStartedRef.current = persona.id;
    void recordLoginAi();
  }, [autoStartAi, persona, recordLoginAi]);

  // Poll the launched session to a terminal state, then refetch the persona —
  // the wired login workflow (or the recorded failure) lives on the persona row.
  // Kept current from an EFFECT, not written during render: a ref mutated while
  // rendering is not safe under concurrent rendering (a render React discards
  // would still have moved it), and the poll below only ever reads it from an
  // async tick that runs well after commit.
  const refreshRef = useRef(refresh);
  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);
  useEffect(() => {
    if (!aiSessionId) return;
    let stopped = false;
    const tick = async () => {
      try {
        const s = await automationApi.getAISession(aiSessionId);
        if (stopped) return;
        // The coordinator's session row has no progress column — surface the
        // agent's running message when there is one.
        setAiProgress((s as any).message || null);
        // AGENT STATUS VOCABULARY (stored verbatim by the coordinator): the
        // terminal set is complete | blocked | max_steps | stuck | error |
        // cancelled — anything that is not 'running' has ended. Only 'complete'
        // recorded a workflow; every other terminal state is a failure to show.
        const status = String((s as any).status || '');
        if (status && status !== 'running') {
          setAiSessionId(null);
          setAiProgress(null);
          setWatchOpen(false);
          if (status === 'complete') {
            toast.success(t('AI recorded the sign-in'));
          } else if (status !== 'cancelled') {
            setSignInError((s as any).error || (s as any).message || t('The AI could not record the sign-in'));
          }
          // The persona row now carries the outcome (login_workflow_id or
          // last_login_error) — make every consumer refetch it.
          refreshRef.current();
        }
      } catch {
        /* transient poll failure — next tick retries */
      }
    };
    const iv = window.setInterval(tick, 3000);
    void tick();
    return () => {
      stopped = true;
      window.clearInterval(iv);
    };
  }, [aiSessionId, t]);

  if (!persona) return null;

  const state = sessionState(persona);
  const hasLogin = !!persona.login_workflow_id;
  const loginName =
    persona.login_workflow_name ||
    (attachable.find((w: any) => w.id === persona.login_workflow_id) as any)?.name;
  // The AI signs in WITH the persona's stored credentials (masked from the
  // model) — without any there is nothing for it to type.
  const canAiRecord = !!(persona.has_password || persona.login_username);

  return (
    <div className="space-y-2.5">
      {!hideHeading && (
        <div className="flex items-baseline justify-between gap-3">
          <label className="flex items-center gap-1.5 text-xs font-medium text-secondary">
            <KeyIcon className="h-4 w-4" />
            {t('Sign-in')}
          </label>
          {hasLogin && (
            <button
              type="button"
              onClick={() => attach(null)}
              disabled={busy !== null}
              className="text-[11px] text-tertiary transition-colors hover:text-secondary disabled:opacity-50"
            >
              {t('Remove')}
            </button>
          )}
        </div>
      )}

      {/* Session status — the fact the user actually needs. */}
      <div className="flex items-center gap-1.5 text-[11px]">
        {state === 'live' ? (
          <>
            <CheckCircleIcon className="h-3.5 w-3.5 text-success-fg" />
            <span className="text-secondary">{t('Signed in')}</span>
          </>
        ) : (
          <>
            <ExclamationTriangleIcon className="h-3.5 w-3.5 text-tertiary" />
            <span className="text-tertiary">
              {state === 'expired' ? t('Session expired') : t('Not signed in yet')}
            </span>
          </>
        )}
        {hasLogin && state !== 'live' && (
          <span className="text-tertiary">· {t('will sign in automatically when a crawl needs it')}</span>
        )}
      </div>

      {aiSessionId != null ? (
        /* AI recording in flight — backend-owned; we surface progress + a live view. */
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-hover/20 px-2.5 py-1.5">
          <ArrowPathIcon className="h-3.5 w-3.5 shrink-0 animate-spin text-secondary" />
          <span className="min-w-0 flex-1 truncate text-[12px] text-secondary">
            {aiProgress || t('AI is signing in and recording the steps…')}
          </span>
          <Button variant="secondary" size="sm" onClick={() => setWatchOpen(true)} className="shrink-0">
            <EyeIcon className="h-3.5 w-3.5" />
            {t('Watch')}
          </Button>
        </div>
      ) : hasLogin ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="min-w-0 flex-1 truncate rounded-lg border border-border bg-hover/20 px-2.5 py-1.5 text-[12px] text-secondary">
            {loginName || t('Login workflow #{{id}}', { id: persona.login_workflow_id })}
          </span>
          <Button
            variant="secondary"
            size="sm"
            onClick={signIn}
            disabled={busy !== null}
            className="shrink-0"
          >
            <ArrowPathIcon className={clsx('h-3.5 w-3.5', busy === 'sign-in' && 'animate-spin')} />
            {busy === 'sign-in' ? t('Signing in…') : t('Sign in now')}
          </Button>
        </div>
      ) : (
        <div className="space-y-2">
          <p className="text-[12px] text-tertiary">
            {t("This persona can't sign itself in yet. Let AI sign in once with the saved credentials, record the sign-in yourself, or attach a workflow that already does it — after that it re-logins on its own whenever a session expires.")}
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={recordLoginAi}
              disabled={busy !== null || !canAiRecord}
              title={
                canAiRecord
                  ? undefined
                  : t('Add a username and password to this persona first — the AI signs in with them (it never sees the values).')
              }
            >
              <SparklesIcon className={clsx('h-3.5 w-3.5', busy === 'ai' && 'animate-pulse')} />
              {busy === 'ai' ? t('Starting…') : t('Let AI record it')}
            </Button>
            <Button variant="secondary" size="sm" onClick={recordLogin} disabled={busy !== null}>
              <VideoCameraIcon className="h-3.5 w-3.5" />
              {t('Record login')}
            </Button>
            <Select<string>
              value=""
              disabled={busy !== null}
              onChange={(v) => v && attach(Number(v))}
              wrapperClassName="flex-1 min-w-[180px]"
              className="flex-1"
              options={[
                { value: '', label: t('or attach an existing workflow…') },
                ...attachable.map((w: any) => ({ value: String(w.id), label: w.name })),
              ]}
            />
          </div>
        </div>
      )}

      {/* Why the last attempt failed. `signInError` is this session's attempt;
          `last_login_error` is what the server recorded (including an automatic
          re-login the user never watched). Show the live one when we have it. */}
      {(signInError || persona.last_login_error) && (
        <p className="flex items-start gap-1.5 text-[11px] text-[#B4300F]">
          <ExclamationTriangleIcon className="mt-px h-3.5 w-3.5 shrink-0" />
          <span>{signInError || persona.last_login_error}</span>
        </p>
      )}

      {/* Live view of the AI recording — the same spectate channel every AI
          session exposes. Mounted only while a recording is in flight. */}
      {aiSessionId != null && (
        <AIPreviewModal
          isOpen={watchOpen}
          onClose={() => setWatchOpen(false)}
          sessionId={`ai-${aiSessionId}`}
        />
      )}
    </div>
  );
};

export default PersonaLoginWorkflow;
