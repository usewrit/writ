import React, { useEffect, useRef, useState } from 'react';
import { CheckCircleIcon, ClipboardDocumentIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';

/**
 * Copy-to-clipboard affordance for the fleet surfaces.
 *
 * Lifted out of FleetPage (where it was a private const) when the connect flow was
 * extracted into `FastConnectAgentModal` — both need it, and a second copy is how
 * two buttons end up drifting apart.
 *
 * `tone="on-dark"` is for the ink-filled code blocks. The default ramp is tuned
 * for light surfaces: `secondary` (#565656) on `ink` (#0D0D0D) is ~2.6:1, which
 * is exactly where the copy button sits on the connect command — the one control
 * on that surface, and the one thing the operator came to press.
 */
export const CopyButton: React.FC<{
  value: string;
  label?: string;
  tone?: 'default' | 'on-dark';
}> = ({ value, label, tone = 'default' }) => {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  // Copying often IS the last thing done in a modal, so the reset timer
  // routinely outlives the component. Keep the handle and clear it on unmount.
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (resetTimer.current) clearTimeout(resetTimer.current); }, []);
  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setCopied(true);
          if (resetTimer.current) clearTimeout(resetTimer.current);
          resetTimer.current = setTimeout(() => setCopied(false), 1500);
        } catch {
          toast.error(t('Could not copy to clipboard'));
        }
      }}
      className={clsx(
        'inline-flex items-center gap-1.5 text-xs transition-colors',
        tone === 'on-dark'
          ? 'rounded bg-white/15 px-2 py-0.5 font-medium text-white hover:bg-white/25'
          : 'text-secondary hover:text-ink',
      )}
    >
      {copied ? <CheckCircleIcon className={clsx('w-4 h-4', tone === 'on-dark' ? 'text-emerald-300' : 'text-emerald-500')} /> : <ClipboardDocumentIcon className="w-4 h-4" />}
      {label || (copied ? t('Copied') : t('Copy'))}
    </button>
  );
};

export default CopyButton;
