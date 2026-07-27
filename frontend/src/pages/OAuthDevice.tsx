import React, { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useRequireAuth } from '../hooks/useAuth';
import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import toast from 'react-hot-toast';
import client from '../api/client';
import { WritTile } from '../components/brand/WritMark';
import { ComputerDesktopIcon, CheckCircleIcon, ClipboardDocumentIcon, ClipboardDocumentCheckIcon, ShieldCheckIcon } from '@heroicons/react/24/outline';
import { useTranslation } from 'react-i18next';

export const OAuthDevice: React.FC = () => {
  const { t } = useTranslation();
  useRequireAuth();
  const [searchParams] = useSearchParams();

  const codeFromUrl = (searchParams.get('code') || '').toUpperCase().trim();

  const [code, setCode] = useState(codeFromUrl);
  const [loading, setLoading] = useState(false);
  const [approved, setApproved] = useState(false);
  const [email, setEmail] = useState('');
  const [serviceToken, setServiceToken] = useState('');
  const [copied, setCopied] = useState(false);

  const handleApprove = async (codeToApprove?: string) => {
    const finalCode = (codeToApprove || code).trim().toUpperCase();
    if (!finalCode) {
      toast.error(t('Please enter the code shown in your terminal'));
      return;
    }
    setLoading(true);
    try {
      const resp = await client.post('/oauth/device/approve', {
        user_code: finalCode,
      });
      setApproved(true);
      setEmail(resp.data.email || '');
      if (resp.data.service_token) {
        setServiceToken(resp.data.service_token);
      }
      toast.success(t('Agent authorized'));
    } catch (err: any) {
      const msg = err.response?.data?.detail || t('Invalid or expired code');
      toast.error(msg);
    } finally {
      setLoading(false);
    }
  };

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(serviceToken);
      setCopied(true);
      toast.success(t('Token copied'));
      setTimeout(() => setCopied(false), 3000);
    } catch {
      toast.error(t('Failed to copy'));
    }
  };

  // If code came from URL, show confirmation UI (don't auto-approve — user must click)
  const hasUrlCode = !!codeFromUrl;

  return (
    <div className="min-h-screen bg-canvas flex items-center justify-center p-4">
      <div className="w-full max-w-md">
        {/* The device-authorization screen is where the user confirms WHAT they
            are granting a device access to, so it carries the brand tile — not a
            generic glyph. */}
        <div className="flex justify-center mb-6">
          <WritTile size={36} />
        </div>
        <div className="bg-surface border border-ink/20 rounded-xl shadow-sm p-8 space-y-6">

          {!approved ? (
            <>
              {hasUrlCode ? (
                /* Code from URL — streamlined one-click flow */
                <>
                  <div className="text-center">
                    <div className="inline-flex items-center justify-center w-12 h-12 bg-hover rounded-lg mb-4">
                      <ShieldCheckIcon className="w-6 h-6 text-ink" />
                    </div>
                    <h1 className="text-lg font-semibold text-ink">{t('Authorize Agent')}</h1>
                    <p className="text-sm text-secondary mt-1">
                      {t('An agent is requesting access to your account')}
                    </p>
                  </div>

                  <div className="bg-canvas border border-border rounded-lg p-4 text-center">
                    <p className="text-xs text-tertiary mb-1">{t('Device code')}</p>
                    <p className="text-2xl font-mono font-bold tracking-widest text-ink">{codeFromUrl}</p>
                  </div>

                  <Button
                    onClick={() => handleApprove(codeFromUrl)}
                    loading={loading}
                    className="w-full"
                  >
                    {t('Authorize Connection')}
                  </Button>

                  <p className="text-xs text-tertiary text-center">
                    {t('This will allow the agent to execute workflows on your behalf.')}
                  </p>
                </>
              ) : (
                /* No code in URL — manual entry */
                <>
                  <div className="text-center">
                    <div className="inline-flex items-center justify-center w-12 h-12 bg-hover rounded-lg mb-4">
                      <ComputerDesktopIcon className="w-6 h-6 text-ink" />
                    </div>
                    <h1 className="text-lg font-semibold text-ink">{t('Link Agent')}</h1>
                    <p className="text-sm text-secondary mt-1">
                      {t('Enter the code shown in your terminal')}
                    </p>
                  </div>

                  <div>
                    <label className="block text-sm font-medium text-ink mb-1.5">
                      {t('Device Code')}
                    </label>
                    <Input
                      value={code}
                      onChange={(e) => setCode(e.target.value.toUpperCase())}
                      placeholder="ABCD-1234"
                      className="text-center text-lg font-mono tracking-widest"
                      maxLength={9}
                      autoFocus
                      onKeyDown={(e) => e.key === 'Enter' && handleApprove()}
                    />
                  </div>

                  <Button
                    onClick={() => handleApprove()}
                    loading={loading}
                    className="w-full"
                  >
                    {t('Approve Connection')}
                  </Button>

                  <p className="text-xs text-tertiary text-center">
                    {t('This will allow the agent to execute workflows on your behalf.')}
                  </p>
                </>
              )}
            </>
          ) : (
            <div className="text-center space-y-4">
              <CheckCircleIcon className="w-12 h-12 text-ink mx-auto" />
              <p className="text-sm text-ink">
                {email ? t('Agent authorized for {{email}}.', { email }) : t('Agent authorized.')}
              </p>
              <p className="text-xs text-tertiary">
                {t('Your terminal should confirm automatically. You can close this page.')}
              </p>

              {serviceToken && (
                <div className="mt-4 pt-4 border-t border-border space-y-2">
                  <p className="text-xs text-secondary">
                    {t("If your terminal didn't detect the approval, copy this token and paste it there:")}
                  </p>
                  <div className="relative">
                    <div className="bg-canvas border border-border rounded-lg p-3 pr-10 text-xs font-mono text-secondary break-all text-left max-h-20 overflow-y-auto select-all">
                      {serviceToken}
                    </div>
                    <button
                      onClick={handleCopy}
                      className="absolute top-2 right-2 p-1.5 rounded-md hover:bg-chrome transition-colors"
                      title={t('Copy token')}
                    >
                      {copied ? (
                        <ClipboardDocumentCheckIcon className="w-4 h-4 text-ink" />
                      ) : (
                        <ClipboardDocumentIcon className="w-4 h-4 text-tertiary" />
                      )}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
