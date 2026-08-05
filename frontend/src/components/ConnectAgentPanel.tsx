import React, { useState } from 'react';
import { ComputerDesktopIcon, CloudIcon } from '@heroicons/react/24/outline';
import { useTranslation } from 'react-i18next';
import type { RecorderCapability } from '../hooks/useRecorderCapability';
import { FastConnectAgentModal } from './fleet/FastConnectAgentModal';

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
  const [connectOpen, setConnectOpen] = useState(false);

  const used = capability?.cloud_quota_used ?? 0;
  // A self-host coordinator reports `cloud_quota_limit: 0` — there IS no cloud
  // recording lane here. Treating "not null" as "has a quota" rendered the cloud
  // upsell copy as "You've used your 0/0 free cloud recordings this month", which
  // is both meaningless and about a product this build does not have. Only speak
  // about a quota when there is a real, positive one.
  const rawLimit = capability?.cloud_quota_limit ?? null;
  const limit = rawLimit != null && rawLimit > 0 ? rawLimit : null;

  if (kind === 'waiting_cloud') {
    return (
      <div className="absolute inset-0 flex items-center justify-center p-6 bg-surface/60 backdrop-blur-sm">
        <div className="max-w-sm w-full text-center">
          <div className="w-12 h-12 mx-auto mb-4 rounded-lg bg-surface border border-border/80 flex items-center justify-center">
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
          <div className="w-12 h-12 mx-auto mb-4 rounded-lg bg-surface border border-border/80 flex items-center justify-center">
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

        {/* The connect flow itself — the SAME modal the Fleet page opens (run one on
            this machine / one-line / mint a token). This used to be two hand-fill
            shell snippets (`saas.url <coordinator-url>`, `WRIT_SERVICE_TOKEN=<token>`)
            that nobody could run as printed, while the real flow — which can start an
            agent here in one click, or hand out a working one-liner — sat two clicks
            away on /fleet. */}
        <div className="flex flex-col items-center gap-2">
          <button
            onClick={() => setConnectOpen(true)}
            className="inline-flex items-center gap-2 rounded-xl bg-accent-strong px-4 py-2 text-[13px] font-semibold text-accent-on shadow-sm transition-colors hover:bg-accent-strong/90"
          >
            <ComputerDesktopIcon className="h-4 w-4" />
            {t('Connect an agent')}
          </button>
          <p className="text-[11.5px] text-tertiary text-center">
            {t('Start one on this machine in a click, or copy a one-line command for another machine.')}
          </p>
        </div>


        {/* Live waiting indicator — auto-resolves when the agent connects */}
        <div className="mt-4 flex items-center justify-center gap-2 text-[13px] text-secondary">
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-tertiary opacity-60" />
            <span className="relative inline-flex rounded-full h-2 w-2 bg-canvas0" />
          </span>
          {t('Waiting for your agent to connect — recording starts automatically.')}
        </div>

        {/* Refreshing capability is the recorder's job (it polls); connecting here
            simply makes an agent appear, and the waiting indicator above resolves. */}
        <FastConnectAgentModal isOpen={connectOpen} onClose={() => setConnectOpen(false)} />

      </div>
    </div>
  );
};

export default ConnectAgentPanel;
