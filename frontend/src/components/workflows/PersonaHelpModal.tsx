import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  KeyIcon, ShieldCheckIcon, DevicePhoneMobileIcon, EnvelopeIcon,
  ChatBubbleLeftRightIcon, ArrowTopRightOnSquareIcon, BookOpenIcon,
} from '@heroicons/react/24/outline';
import { Modal } from '../ui/Modal';
import { docsUrl, DOCS_LINK_PROPS } from '../../utils/docs';

interface PersonaHelpModalProps {
  isOpen: boolean;
  onClose: () => void;
}

// Docs are not embedded in the coordinator — this opens the public site.
// Resolved per render, not at module load, so it follows a language change.
const docsLink = () => docsUrl('authentication', 'personas');

/**
 * A short, plain-language quick-start shown from the Personas page "?" button.
 * Deliberately concise — it links out to the full guide (docs persona-setup)
 * for the detailed walkthrough of each 2FA method.
 */
export const PersonaHelpModal: React.FC<PersonaHelpModalProps> = ({ isOpen, onClose }) => {
  const { t } = useTranslation();

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={t('How personas work')}
      subtitle={t('A persona is a saved login your workflows reuse — it signs in for you, passes 2FA, and reuses the session.')}
      size="lg"
    >
      <div className="space-y-6">
        {/* The quick way */}
        <section>
          <h4 className="text-sm font-semibold text-ink mb-2">{t('The quickest way to make one')}</h4>
          <ul className="space-y-2 text-[13px] text-secondary">
            <li className="flex gap-2">
              <KeyIcon className="w-4 h-4 text-tertiary shrink-0 mt-0.5" />
              <span><strong className="text-ink">{t('From a workflow you already built')}</strong> — {t('if you logged in by hand while recording, choose “Save login as persona” on that workflow to capture it.')}</span>
            </li>
            <li className="flex gap-2">
              <ShieldCheckIcon className="w-4 h-4 text-tertiary shrink-0 mt-0.5" />
              <span><strong className="text-ink">{t('Import from your authenticator app')}</strong> — {t('click Import, then scan or upload the QR export. 2FA comes set up automatically.')}</span>
            </li>
            <li className="flex gap-2">
              <DevicePhoneMobileIcon className="w-4 h-4 text-tertiary shrink-0 mt-0.5" />
              <span><strong className="text-ink">{t('Build one manually')}</strong> — {t('click New and follow five steps: Identity → Login → 2FA → Execution → Review.')}</span>
            </li>
          </ul>
        </section>

        {/* Setting up 2FA */}
        <section>
          <h4 className="text-sm font-semibold text-ink mb-1">{t('Setting up two-factor (2FA)')}</h4>
          <p className="text-[13px] text-secondary mb-3">
            {t('On the 2FA step, pick the method the site uses. Writ always satisfies it on the server during a run — your API keys can never read the secrets or codes.')}
          </p>

          <div className="space-y-3">
            {/* TOTP */}
            <div className="rounded-lg border border-border p-3">
              <div className="flex items-center gap-2 mb-1.5">
                <ShieldCheckIcon className="w-4 h-4 text-ink" />
                <span className="text-[13px] font-medium text-ink">{t('Authenticator app (rotating 6-digit code)')}</span>
              </div>
              <p className="text-xs text-tertiary mb-2">{t('A one-time setup on the website you automate, then paste the same key here:')}</p>
              <ol className="list-decimal list-inside text-[13px] text-secondary space-y-1 marker:text-tertiary">
                <li>{t('On the website, open Security / Two-factor settings and add an Authenticator app.')}</li>
                <li>{t('Click “Can’t scan?” / “Enter key manually” to reveal the setup key (e.g. JBSWY3DPEHPK3PXP) and copy it.')}</li>
                <li>{t('In the 2FA step here, choose Authenticator app and paste it into “TOTP secret”. (Or upload the QR image / paste the otpauth:// link.)')}</li>
                <li>{t('Confirm one code on the site to finish — add the key to your own authenticator app too, or read it from the verify box here.')}</li>
              </ol>
            </div>

            {/* Email */}
            <div className="rounded-lg border border-border p-3">
              <div className="flex items-center gap-2 mb-1.5">
                <EnvelopeIcon className="w-4 h-4 text-ink" />
                <span className="text-[13px] font-medium text-ink">{t('Email code')}</span>
              </div>
              <p className="text-[13px] text-secondary">
                {t('Connect the Gmail, Outlook, or IMAP inbox that receives the code — we read it automatically. Or forward the code emails to the relay address shown in the wizard.')}
              </p>
            </div>

            {/* SMS */}
            <div className="rounded-lg border border-border p-3">
              <div className="flex items-center gap-2 mb-1.5">
                <ChatBubbleLeftRightIcon className="w-4 h-4 text-ink" />
                <span className="text-[13px] font-medium text-ink">{t('Text message (SMS)')}</span>
              </div>
              <p className="text-[13px] text-secondary">
                {t('Give the relay number/token and forward the texts to Writ with an SMS-forwarder app, carrier gateway, or webhook.')}
              </p>
            </div>
          </div>
        </section>

        {/* Footer actions */}
        <div className="flex items-center justify-between pt-1">
          <a
            href={docsLink()}
            {...DOCS_LINK_PROPS}
            className="inline-flex items-center gap-1.5 text-[13px] text-ink font-medium hover:underline"
          >
            <BookOpenIcon className="w-4 h-4" />
            {t('Read the full guide')}
            <ArrowTopRightOnSquareIcon className="w-3.5 h-3.5 text-tertiary" />
          </a>
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm text-secondary hover:text-ink rounded-lg hover:bg-hover transition-colors"
          >
            {t('Got it')}
          </button>
        </div>
      </div>
    </Modal>
  );
};
