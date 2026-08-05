import React, { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import { setAuth } from '../utils/auth';
import { safeInternalPath } from '../utils/safeRedirect';
import client, { apiErrorMessage } from '../api/client';
import { WritWordmark } from '../components/brand/WritMark';
import { SourceOfferLine } from '../components/common/SourceOffer';

/**
 * Single-owner login (self-host build).
 *
 * The cloud sign-up / social-login / WebAuthn / MFA / CAPTCHA flows are gone —
 * the coordinator is a single-admin box whose credentials are bootstrapped on
 * first boot (WRIT_ADMIN_EMAIL / WRIT_ADMIN_PASSWORD). This page just posts
 * email+password to /auth/login and stores the returned session.
 */
export const Login: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [showReset, setShowReset] = useState(false);

  // Fresh install has no owner yet — send the user to onboarding to create it
  // instead of showing a login form they have no credentials for.
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const { data } = await client.get('/auth/setup-status');
        if (alive && data?.needs_setup) navigate('/setup', { replace: true });
      } catch {
        /* probe failed — stay on the login form */
      }
    })();
    return () => {
      alive = false;
    };
  }, [navigate]);

  // `?redirect=` is attacker-controllable, so it is resolved against our own
  // origin and rejected if it lands anywhere else — see utils/safeRedirect.
  // The previous prefix test here ("starts with / but not //") was bypassable
  // with `/\\evil.com`: browsers treat the backslash as a slash, so it became
  // a protocol-relative URL and sent the user off-site after a successful
  // login. safeInternalPath also drops auth routes, so a stale nested
  // `?redirect=/login?…` cannot restart the bounce loop.
  const redirectTarget = () => safeInternalPath(searchParams.get('redirect'), '/');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    try {
      const response = await client.post('/auth/login', { email: email.trim(), password });
      const { access_token, user, organization } = response.data;
      setAuth(access_token, user, organization ?? null);
      navigate(redirectTarget(), { replace: true });
    } catch (err) {
      toast.error(apiErrorMessage(err, t('Sign in failed — check your email and password.')));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-[#EDEBE8] px-4">
      <div className="w-full max-w-sm">
        <div className="flex items-center justify-center mb-8">
          <WritWordmark size={28} className="text-ink" />
        </div>

        <div className="bg-surface rounded-2xl shadow-sm border border-ink/20 p-6">
          <h1 className="text-base font-semibold text-ink mb-1">{t('Sign in')}</h1>
          <p className="text-sm text-secondary mb-5">{t('Sign in to your self-host coordinator.')}</p>

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label htmlFor="email" className="block text-[13px] font-medium text-ink mb-1">{t('Email')}</label>
              <input
                id="email"
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="w-full px-3 py-2 text-sm bg-canvas border border-border rounded-lg focus:outline-none focus:border-ink/40 transition-colors"
                placeholder="you@example.com"
                autoFocus
              />
            </div>
            <div>
              <label htmlFor="password" className="block text-[13px] font-medium text-ink mb-1">{t('Password')}</label>
              <input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full px-3 py-2 text-sm bg-canvas border border-border rounded-lg focus:outline-none focus:border-ink/40 transition-colors"
                placeholder="••••••••"
              />
            </div>
            <button
              type="submit"
              disabled={submitting}
              className="w-full px-4 py-2.5 bg-accent-strong text-accent-on text-sm font-semibold rounded-lg shadow-sm hover:bg-accent-strong/90 transition-colors disabled:opacity-50"
            >
              {submitting ? t('Signing in…') : t('Sign in')}
            </button>
          </form>

          <div className="mt-4 pt-4 border-t border-border">
            <button
              type="button"
              onClick={() => setShowReset((v) => !v)}
              className="text-[12px] text-secondary hover:text-ink transition-colors"
            >
              {t('Forgot your password?')}
            </button>
            {showReset && (
              <div className="mt-2.5 text-[11px] text-secondary leading-relaxed space-y-2">
                <p>
                  {t('There is no email reset — this is a self-hosted, single-owner coordinator. Reset it on the server (where you run writ):')}
                </p>
                <div>
                  <p className="text-tertiary mb-0.5">{t('Docker:')}</p>
                  <pre className="font-mono text-[10.5px] bg-ink text-white/90 rounded-md p-2 overflow-x-auto whitespace-pre-wrap break-all">docker compose -f docker/docker-compose.yml exec coordinator python reset_password.py</pre>
                </div>
                <div>
                  <p className="text-tertiary mb-0.5">{t('Local install:')}</p>
                  <pre className="font-mono text-[10.5px] bg-ink text-white/90 rounded-md p-2 overflow-x-auto whitespace-pre-wrap break-all">bash reset-admin-password.sh</pre>
                </div>
                <p className="text-tertiary">
                  {t('It prints a new password (or pass --password to set your own). See the README for details.')}
                </p>
              </div>
            )}
          </div>
        </div>

        {/* AGPL-3.0 §13: the source offer has to reach anyone interacting with
            this coordinator over a network — including before they can log in. */}
        <p className="text-center text-[11px] text-tertiary mt-6">
          {t('Self-hosted writ coordinator')}
          <SourceOfferLine />
        </p>
      </div>
    </div>
  );
};

export default Login;
