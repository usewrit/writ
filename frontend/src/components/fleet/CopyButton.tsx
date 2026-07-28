import React, { useState } from 'react';
import { CheckCircleIcon, ClipboardDocumentIcon } from '@heroicons/react/24/outline';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';

/**
 * Copy-to-clipboard affordance for the fleet surfaces.
 *
 * Lifted out of FleetPage (where it was a private const) when the connect flow was
 * extracted into `FastConnectAgentModal` — both need it, and a second copy is how
 * two buttons end up drifting apart.
 */
export const CopyButton: React.FC<{ value: string; label?: string }> = ({ value, label }) => {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch {
          toast.error(t('Could not copy to clipboard'));
        }
      }}
      className="inline-flex items-center gap-1.5 text-xs text-secondary hover:text-ink transition-colors"
    >
      {copied ? <CheckCircleIcon className="w-4 h-4 text-emerald-500" /> : <ClipboardDocumentIcon className="w-4 h-4" />}
      {label || (copied ? t('Copied') : t('Copy'))}
    </button>
  );
};

export default CopyButton;
