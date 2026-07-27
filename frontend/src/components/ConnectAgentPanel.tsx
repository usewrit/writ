import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  CommandLineIcon,
  ClipboardDocumentIcon,
  ComputerDesktopIcon,
  CloudIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import type { RecorderCapability } from '../hooks/useRecorderCapability';

// Canonical connect story, in the order it actually has to happen: get the
// binary, point it at this coordinator, run it with a minted token.
//
// These were previously a single line reading
//   WRIT_SERVICE_TOKEN=<token> WRIT_COORDINATOR_URL=<url> writ-agent-fleet
// which could not work: the stock binary is `writ-agent`, it takes its
// coordinator URL from the `saas.url` CONFIG value, and it never reads
// WRIT_COORDINATOR_URL (see _build_connect_commands in routers/fleet.py). It
// also skipped the download entirely, so step one was a binary the reader did
// not have. Commands are literal shell — not translated.
const INSTALL_STEPS: { os: string; commands: string[] }[] = [
  {
    os: 'macOS / Linux',
    commands: [
      './writ-agent config set saas.url <coordinator-url>',
      'WRIT_SERVICE_TOKEN=<token> ./writ-agent start --headless',
    ],
  },
  {
    os: 'Windows (PowerShell)',
    commands: [
      '.\\writ-agent.exe config set saas.url <coordinator-url>',
      "$env:WRIT_SERVICE_TOKEN='<token>'; .\\writ-agent.exe start --headless",
    ],
  },
];

interface ConnectAgentPanelProps {
  /**
   * connect_local — cloud recording unavailable (always the case on self-host)
   *   and no local agent online. The user must connect a local agent to record.
   * waiting_cloud — paid plan, cloud recording is allowed but no infra agent is
   *   free right now. We auto-wait and start as soon as one frees up.
   */
  kind: 'connect_local' | 'waiting_cloud';
  capability: RecorderCapability | null;
}

/**
 * Blocking pre-flight gate shown by BrowserRecorder when recording can't start
 * yet. Either install/connect instructions for a local writ-agent worker,
 * or a calm "waiting for a cloud agent" state. Rendered on the
 * recorder's dot-grid canvas; monochrome to match the design language.
 */
export const ConnectAgentPanel: React.FC<ConnectAgentPanelProps> = ({ kind, capability }) => {
  const { t } = useTranslation();
  const [selectedOs, setSelectedOs] = useState(0);

  const copyCommand = (cmd: string) => {
    navigator.clipboard?.writeText(cmd).then(
      () => toast.success(t('Copied')),
      () => {},
    );
  };

  const used = capability?.cloud_quota_used ?? 0;
  const limit = capability?.cloud_quota_limit ?? null;

  if (kind === 'waiting_cloud') {
    return (
      <div className="absolute inset-0 flex items-center justify-center p-6 bg-surface/60 backdrop-blur-sm">
        <div className="max-w-sm w-full text-center">
          <div className="w-12 h-12 mx-auto mb-4 rounded-lg bg-white border border-zinc-200/80 flex items-center justify-center">
            <CloudIcon className="w-6 h-6 text-ink animate-pulse" />
          </div>
          <h3 className="text-[15px] font-semibold text-ink mb-1.5">
            {t('Waiting for an available cloud agent…')}
          </h3>
          <p className="text-[13px] text-secondary leading-relaxed">
            {t('All cloud recorders are busy right now. Your recording will start automatically as soon as one frees up — no need to do anything.')}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="absolute inset-0 overflow-y-auto flex items-start justify-center p-6 bg-surface/70 backdrop-blur-sm">
      <div className="max-w-lg w-full my-auto">
        <div className="text-center mb-5">
          <div className="w-12 h-12 mx-auto mb-4 rounded-lg bg-white border border-zinc-200/80 flex items-center justify-center">
            <ComputerDesktopIcon className="w-6 h-6 text-ink" />
          </div>
          <h3 className="text-[16px] font-semibold text-ink mb-1.5">
            {t('Connect a local agent to record')}
          </h3>
          <p className="text-[13px] text-secondary leading-relaxed max-w-sm mx-auto">
            {limit != null
              ? t("You've used your {{used}}/{{limit}} free cloud recordings this month. Install the local agent to keep recording on your own machine — it's free and unlimited.", { used, limit })
              : t('Recording runs on a connected local agent. Set one up on the Fleet page — it stays connected and picks up your sessions automatically.')}
          </p>
        </div>

        {/* Install steps */}
        <div className="bg-white border border-zinc-200/80 rounded-xl overflow-hidden">
          <div className="border-b border-border px-5 flex gap-0">
            {INSTALL_STEPS.map((step, i) => (
              <button
                key={step.os}
                onClick={() => setSelectedOs(i)}
                className={clsx(
                  'py-2.5 px-4 text-xs font-medium border-b-2 transition-colors -mb-px',
                  selectedOs === i
                    ? 'border-zinc-900 text-ink'
                    : 'border-transparent text-secondary hover:text-ink',
                )}
              >
                {step.os}
              </button>
            ))}
          </div>
          <div className="p-5 space-y-3">
            {INSTALL_STEPS[selectedOs].commands.map((cmd, i) => (
              <div key={i} className="flex items-center gap-3">
                <span className="flex-shrink-0 w-5 h-5 rounded-full bg-zinc-100 text-zinc-500 text-xs flex items-center justify-center font-mono">
                  {i + 1}
                </span>
                <div className="flex-1 flex items-center gap-2 bg-canvas border border-zinc-200 rounded-lg px-3 py-2 font-mono text-sm">
                  <CommandLineIcon className="w-4 h-4 text-zinc-400 flex-shrink-0" />
                  <code className="flex-1 text-zinc-800 truncate">{cmd}</code>
                  <button
                    onClick={() => copyCommand(cmd)}
                    className="flex-shrink-0 p-1 text-zinc-400 hover:text-zinc-700 transition-colors"
                    title={t('Copy')}
                  >
                    <ClipboardDocumentIcon className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Live waiting indicator — auto-resolves when the agent connects */}
        <div className="mt-4 flex items-center justify-center gap-2 text-[13px] text-secondary">
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-zinc-400 opacity-60" />
            <span className="relative inline-flex rounded-full h-2 w-2 bg-canvas0" />
          </span>
          {t('Waiting for your agent to connect — recording starts automatically.')}
        </div>

        {/* Primary way out of this gate. Fleet resolves the download command for
            the reader's platform and can install + start an agent on this
            machine in one click — the snippets above are reference for a
            binary that is already downloaded, with placeholders to fill. */}
        <div className="mt-5 flex flex-col items-center gap-2">
          <Link
            to="/fleet"
            className="inline-flex items-center gap-2 rounded-xl bg-accent-strong px-4 py-2 text-[13px] font-semibold text-accent-on shadow-sm transition-colors hover:bg-accent-strong/90"
          >
            {t('Set up an agent')}
          </Link>
          <p className="text-[11.5px] text-tertiary">
            {t('Get the download command for your platform, or run one on this machine in a click.')}
          </p>
        </div>
      </div>
    </div>
  );
};

export default ConnectAgentPanel;
