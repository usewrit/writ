import { useEffect, useState, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { ShieldCheckIcon, TrashIcon } from '@heroicons/react/24/outline';
import { automationApi } from '../../../api/endpoints';
import type { AutomationWorkflow } from '../../../types/api';
import { Section } from './Section';
import { uiLocale } from '../../../utils/format';

/**
 * Auth section for the browserless HTTP lane: shows the sign-in recipe kind + any 2FA challenge, and
 * the reusable session (saved / expires / clear). Rendered only for workflows that authenticate over
 * HTTP (an AuthRecipe or a login_post / api_call step).
 */
export function AuthPanel({ workflow }: { workflow: AutomationWorkflow }) {
  const { t } = useTranslation();
  const steps = (workflow.steps as Array<{ type?: string }> | undefined) || [];
  const hasApiSteps = steps.some(s => s.type === 'api_call' || s.type === 'login_post');
  const auth = workflow.auth_config || null;

  // Every hook MUST run before the `!auth && !hasApiSteps` bail-out below.
  // Declared after it, they were skipped on renders where the panel hid itself,
  // so a workflow that later grew an auth config rendered MORE hooks than the
  // render before it — React error #310, which takes down the whole page.
  const [session, setSession] = useState<{ has_session: boolean; expires_at?: string | null; is_expired?: boolean } | null>(null);
  const [busy, setBusy] = useState(false);

  // Deliberately NOT an async function: an effect may not call one directly (the
  // caller can't tell a pre-await synchronous setState from a deferred one). This
  // form writes only from the promise continuations, and still returns a promise
  // so `clear()` below can await it.
  const load = useCallback(
    () =>
      automationApi
        .getWorkflowSession(workflow.id)
        .then(setSession)
        .catch(() => setSession(null)),
    [workflow.id],
  );

  useEffect(() => { load(); }, [load]);

  if (!auth && !hasApiSteps) return null;

  const kind = auth?.kind || (steps.some(s => s.type === 'login_post') ? 'inferred' : 'auto');
  const challenges = (auth?.login?.steps || [])
    .flatMap(s => s.challenges || [])
    .map(c => c.type)
    .filter(Boolean) as string[];

  const kindLabel: Record<string, string> = {
    http: t('HTTP recipe'), browser: t('Browser sign-in'), none: t('None'),
    inferred: t('Inferred from steps'), auto: t('Auto'),
  };

  const clear = async () => {
    setBusy(true);
    try {
      await automationApi.clearWorkflowSession(workflow.id);
      await load();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section
      title={t('Authentication')}
      description={t('How this workflow signs in when it runs without a browser.')}
    >
      <div className="bg-surface border border-ink/20 shadow-sm rounded-xl overflow-hidden">
        <div className="flex items-center gap-3 px-4 py-3">
          <ShieldCheckIcon className="w-4 h-4 text-tertiary shrink-0" />
          <div className="flex-1 min-w-0">
            <p className="text-[13px] font-medium text-ink">
              {t('Sign-in')}: <span className="font-normal text-secondary">{kindLabel[kind] || kind}</span>
            </p>
            {challenges.length > 0 && (
              <p className="text-[11px] text-tertiary mt-0.5">
                {t('2FA')}: {challenges.join(', ')}
              </p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-3 px-4 py-2.5 border-t border-border">
          <div className="flex-1 min-w-0">
            {session?.has_session ? (
              <p className="text-[12px] text-secondary">
                {session.is_expired ? t('Session saved (expired)') : t('Session saved')}
                {session.expires_at && (
                  <span className="text-tertiary"> · {t('expires {{date}}', { date: new Date(session.expires_at).toLocaleString(uiLocale()) })}</span>
                )}
              </p>
            ) : (
              <p className="text-[12px] text-tertiary">{t('No saved session — it will sign in on the next run.')}</p>
            )}
          </div>
          {session?.has_session && (
            <button
              onClick={clear}
              disabled={busy}
              className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium text-secondary border border-border rounded-lg hover:bg-zinc-50 disabled:opacity-50 transition-colors shrink-0"
            >
              <TrashIcon className="w-3.5 h-3.5" />
              {busy ? t('Clearing…') : t('Clear session')}
            </button>
          )}
        </div>
      </div>
    </Section>
  );
}
