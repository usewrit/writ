import React, { useState } from 'react';
import {
  ArrowPathIcon,
  ArrowTopRightOnSquareIcon,
  CheckCircleIcon,
  CommandLineIcon,
  ComputerDesktopIcon,
  SparklesIcon,
} from '@heroicons/react/24/outline';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';

import { apiErrorMessage } from '../../api/client';
import {
  fleetApi,
  type FleetConnectInfo,
  type LocalAgentStatus,
  type MintedFleetToken,
  type PairCode,
} from '../../api/fleet';
import { useQuery } from '../../hooks/useQuery';
import { Button, Input } from '../ui';
import { Modal } from '../ui/Modal';
import { CopyButton } from './CopyButton';

/**
 * The connect-an-agent flow, in ONE place.
 *
 * It used to live inline in FleetPage. The recorder's blocking "no agent connected"
 * gate could not reach it, so that gate printed placeholder shell commands
 * (`./writ-agent config set saas.url <coordinator-url>`) the reader had to fill in
 * by hand — while the Fleet page two clicks away could start an agent on this
 * machine, or hand out a working one-liner, with no placeholders at all.
 * Duplicating the flow would have made a second copy to drift; this component is
 * the single source both surfaces render.
 *
 * Self-contained: it owns every piece of state the flow needs and talks only to
 * `fleetApi`. `onConnected` lets a host refresh its own lists when something
 * actually changes.
 */
export const FastConnectAgentModal: React.FC<{
  isOpen: boolean;
  onClose: () => void;
  /** Fired after an agent starts / a token or pairing code is minted. */
  onConnected?: () => void | Promise<void>;
}> = ({ isOpen, onClose, onConnected }) => {
  const { t } = useTranslation();

  const [mintName, setMintName] = useState('');
  const [minting, setMinting] = useState(false);
  const [minted, setMinted] = useState<MintedFleetToken | null>(null);
  // Which door the operator picked. `null` = still choosing.
  const [connectMode, setConnectMode] = useState<'local' | 'oneline' | 'command' | null>(null);
  const [localBusy, setLocalBusy] = useState(false);
  const [localResult, setLocalResult] = useState<LocalAgentStatus | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  // The one-liner path mints a PAIRING CODE, never a long-lived token. Minting both
  // would enrol the same machine twice and race two writers into the token registry.
  const [pairCode, setPairCode] = useState<PairCode | null>(null);
  const [pairBusy, setPairBusy] = useState(false);
  const [pairError, setPairError] = useState<string | null>(null);

  const { data: connectInfo } = useQuery<FleetConnectInfo>(
    'fleet:connect-info',
    () => fleetApi.connectInfo(),
    { silent: true },
  );
  const { data: localAgent, refresh: refreshLocalAgent } = useQuery<LocalAgentStatus>(
    'fleet:local-agent',
    () => fleetApi.localAgentStatus(),
    { silent: true },
  );

  const dockerCommand = minted?.docker_command ?? '';

  const handleMint = async () => {
    setMinting(true);
    try {
      setMinted(await fleetApi.mintToken(mintName.trim() || 'fleet-agent'));
      setMintName('');
      await onConnected?.();
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to mint fleet token')));
    } finally {
      setMinting(false);
    }
  };

  /** "Run one here" — the coordinator mints, downloads, configures and launches.
   *  Nothing is copied; the token never reaches the browser. */
  const handleStartLocal = async () => {
    setLocalBusy(true);
    setLocalError(null);
    try {
      const res = await fleetApi.startLocalAgent(mintName.trim() || 'local-agent');
      setLocalResult(res);
      await Promise.all([refreshLocalAgent(), onConnected?.()]);
      toast.success(res.status === 'already_running' ? t('Agent already running') : t('Agent started'));
    } catch (e) {
      // Keep the modal open on failure: the message names the actual blocker.
      setLocalError(apiErrorMessage(e, t('Could not start the local agent')));
    } finally {
      setLocalBusy(false);
    }
  };

  /** Mint the single-use pairing code behind the one-liner. */
  const handleMintPair = async () => {
    setPairBusy(true);
    setPairError(null);
    try {
      setPairCode(await fleetApi.mintPairCode());
      await onConnected?.();
    } catch (err) {
      setPairError(apiErrorMessage(err, t('Could not prepare a connect code.')));
    } finally {
      setPairBusy(false);
    }
  };

  const closeMint = () => {
    onClose();
    setMinted(null);
    setMintName('');
    setConnectMode(null);
    setLocalResult(null);
    setLocalError(null);
    // A pairing code is single-use and short-lived; never let a stale one from a
    // previous open be presented as if it were still good.
    setPairCode(null);
    setPairError(null);
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={closeMint}
      title={minted ? t('Agent token created') : t('Connect a new agent')}
      subtitle={minted ? t('Copy the command below — the raw token is shown only once.') : t('Mint a long-lived token, then run writ-agent-fleet with it.')}
    >
      {!minted && connectMode === null ? (
        /* ── The fork. Running one on this machine is the common first case,
              so it leads — but only when preflight says this host can host it
              (right platform, not a browser-less container). Otherwise the
              card explains why and the copy-paste path is the only option. ── */
        <div className="space-y-4">
          <div>
            <label className="mb-1 block text-sm font-medium text-ink">{t('Name')}</label>
            <Input
              value={mintName}
              onChange={e => setMintName(e.target.value)}
              placeholder={t('e.g. laptop-1, vps-eu')}
              autoFocus
            />
          </div>

          <div className="grid gap-2.5">
            <button
              onClick={() => { setConnectMode('local'); void handleStartLocal(); }}
              disabled={localAgent ? !localAgent.supported || localAgent.running : false}
              className="group flex items-start gap-3 rounded-xl border border-ink/30 bg-surface p-3.5 text-left transition-colors hover:border-ink/50 hover:bg-hover/40 disabled:cursor-not-allowed disabled:opacity-55 disabled:hover:border-ink/30 disabled:hover:bg-surface"
            >
              <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-chrome">
                <ComputerDesktopIcon className="h-4 w-4 text-ink" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-[13.5px] font-semibold text-ink">{t('Run one on this machine')}</span>
                <span className="mt-0.5 block text-[12px] leading-relaxed text-tertiary">
                  {localAgent?.running
                    ? t('An agent is already running here ({{name}}).', { name: localAgent.agent_name || t('local-agent') })
                    : localAgent && !localAgent.supported
                      ? localAgent.blockers.join(' ')
                      : t('Downloads the agent for {{platform}}, points it at this coordinator, and starts it. Nothing to copy.', { platform: localAgent?.platform || t('this host') })}
                </span>
              </span>
            </button>

            {/* The one-liner. It is the path onboarding leads with and the one that
                actually works for a machine that has NOTHING installed yet — the
                token doors below both assume a ./writ-agent the reader must first
                fetch. It was reachable only during first-run setup, so anyone adding
                a second machine later was sent down the long path. */}
            <button
              onClick={() => { setConnectMode('oneline'); void handleMintPair(); }}
              className="group flex items-start gap-3 rounded-xl border border-border bg-surface p-3.5 text-left transition-colors hover:border-ink/30 hover:bg-hover/40"
            >
              <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-chrome">
                <SparklesIcon className="h-4 w-4 text-ink" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-[13.5px] font-semibold text-ink">{t('One line, any machine')}</span>
                <span className="mt-0.5 block text-[12px] leading-relaxed text-tertiary">
                  {t('Paste a single command over there — it downloads the agent, connects it, and expires after one use. Nothing to install first.')}
                </span>
              </span>
            </button>

            <button
              onClick={() => setConnectMode('command')}
              className="group flex items-start gap-3 rounded-xl border border-border bg-surface p-3.5 text-left transition-colors hover:border-ink/30 hover:bg-hover/40"
            >
              <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-chrome">
                <CommandLineIcon className="h-4 w-4 text-ink" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-[13.5px] font-semibold text-ink">{t('Connect another machine')}</span>
                <span className="mt-0.5 block text-[12px] leading-relaxed text-tertiary">
                  {t('Mint a token and get the download + connect commands to run over there.')}
                </span>
              </span>
            </button>
          </div>

          {connectInfo?.ws_url ? (
            <p className="text-xs text-tertiary">
              {t('Agents will dial')}: <span className="font-mono text-secondary">{connectInfo.ws_url}</span>
            </p>
          ) : (
            <p className="text-xs text-amber-600">
              {t('Set WRIT_PUBLIC_URL on the coordinator so agents know where to dial in. Configure it under Settings → Network.')}
            </p>
          )}
          <div className="flex justify-end pt-1">
            <Button variant="secondary" onClick={closeMint}>{t('Cancel')}</Button>
          </div>
        </div>
      ) : connectMode === 'oneline' ? (
        /* ── One line: a single-use pairing code, same as first-run onboarding. ── */
        <div className="space-y-4">
          {pairError ? (
            <div className="rounded-lg border border-border bg-canvas px-3 py-2.5 text-[13px] text-secondary">{pairError}</div>
          ) : pairBusy || !pairCode ? (
            <div className="flex items-center gap-2 py-2 text-sm text-secondary">
              <div className="h-4 w-4 animate-spin rounded-full border-2 border-zinc-300 border-t-zinc-800" />
              {t('Preparing a connect code…')}
            </div>
          ) : (
            <>
              <p className="text-[12px] leading-relaxed text-secondary">
                {t('Run this on the machine that should do the browsing. It installs the agent and connects it. The code works once and expires.')}
              </p>
              <div className="relative">
                <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-lg bg-ink p-3 font-mono text-[11px] leading-relaxed text-white/90">{pairCode.install_command}</pre>
                <div className="absolute right-2 top-2">
                  <CopyButton value={pairCode.install_command} />
                </div>
              </div>
              {/* The agent list polls on its own, so an agent that dials in shows up
                  above without the operator re-opening anything. */}
              <p className="text-[11.5px] text-tertiary">
                {t('The agent appears in the fleet above as soon as it connects.')}
              </p>
            </>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <Button variant="secondary" onClick={() => { setConnectMode(null); setPairCode(null); setPairError(null); }}>{t('Back')}</Button>
            <Button onClick={closeMint}>{t('Done')}</Button>
          </div>
        </div>
      ) : connectMode === 'local' ? (
        /* ── Running one here: progress, then the outcome. ── */
        <div className="space-y-4">
          {localBusy && (
            <div className="flex items-center gap-3 rounded-xl border border-border bg-canvas p-4">
              <ArrowPathIcon className="h-4 w-4 shrink-0 animate-spin text-secondary" />
              <div className="min-w-0">
                <p className="text-[13px] font-medium text-ink">{t('Installing the agent on this machine…')}</p>
                <p className="mt-0.5 text-[12px] text-tertiary">{t('Resolving the release, downloading, then starting it.')}</p>
              </div>
            </div>
          )}

          {localError && (
            <div className="rounded-xl border border-amber-300 bg-amber-50 p-3.5">
              <p className="text-[13px] font-medium text-amber-900">{t('Could not start it here')}</p>
              <p className="mt-1 text-[12px] leading-relaxed text-amber-800">{localError}</p>
              <button
                onClick={() => { setLocalError(null); setConnectMode('command'); }}
                className="mt-2.5 text-[12px] font-medium text-ink underline underline-offset-2"
              >
                {t('Use the command instead')}
              </button>
            </div>
          )}

          {localResult && !localError && (
            <div className="space-y-2.5">
              <div className="flex items-start gap-3 rounded-xl border border-border bg-canvas p-3.5">
                <CheckCircleIcon className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
                <div className="min-w-0">
                  <p className="text-[13px] font-medium text-ink">
                    {localResult.status === 'already_running' ? t('Already running here') : t('Agent is running on this machine')}
                  </p>
                  <p className="mt-0.5 text-[12px] leading-relaxed text-tertiary">
                    {t('It appears in the fleet above and picks up work automatically.')}
                    {localResult.pid ? ` · PID ${localResult.pid}` : ''}
                  </p>
                </div>
              </div>
              {localResult.checksum_verified === false && (
                /* Say so rather than implying a verification that did not happen. */
                <p className="text-[11.5px] leading-relaxed text-tertiary">
                  {t('The release published no checksum for this asset, so its integrity was not verified.')}
                </p>
              )}
              <p className="text-[11.5px] text-tertiary">
                {t('Logs')}: <span className="font-mono">{localResult.log_path}</span>
              </p>
            </div>
          )}

          <div className="flex justify-end gap-2 pt-1">
            {!localBusy && !localResult && !localError && (
              <Button variant="secondary" onClick={() => setConnectMode(null)}>{t('Back')}</Button>
            )}
            <Button onClick={closeMint} disabled={localBusy}>{t('Done')}</Button>
          </div>
        </div>
      ) : !minted ? (
        <div className="space-y-4">
          {/* Step 1 — GET the binary. The connect commands below assume a
              ./writ-agent that a new operator does not have yet; leaving this
              out is what sent people to a releases page mid-onboarding. */}
          <div className="rounded-lg border border-border bg-canvas p-3">
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-xs font-medium uppercase tracking-wide text-tertiary">{t('1 · Download the agent (macOS / Linux)')}</span>
              {connectInfo?.install_commands?.unix && <CopyButton value={connectInfo.install_commands.unix} />}
            </div>
            <code className="block whitespace-pre-wrap break-all font-mono text-xs text-ink">
              {connectInfo?.install_commands?.unix || t('Unavailable — update the coordinator.')}
            </code>
            {connectInfo?.repo_url && (
              <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11.5px]">
                <a href={connectInfo.repo_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-secondary hover:text-ink hover:underline">
                  <ArrowTopRightOnSquareIcon className="h-3 w-3" /> {connectInfo.repo}
                </a>
                <a href={connectInfo.github_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-secondary hover:text-ink hover:underline">
                  <ArrowTopRightOnSquareIcon className="h-3 w-3" /> {t('Releases')}
                </a>
                <span className="font-mono text-tertiary">{connectInfo.docker_image}</span>
              </div>
            )}
          </div>
          <p className="text-[11.5px] text-tertiary">
            {t('Windows and build-from-source commands appear after the token is minted.')}
          </p>
          <div>
            <label className="mb-1 block text-sm font-medium text-ink">{t('Name')}</label>
            <Input
              value={mintName}
              onChange={e => setMintName(e.target.value)}
              placeholder={t('e.g. laptop-1, vps-eu')}
              autoFocus
            />
          </div>
          {connectInfo?.ws_url ? (
            <p className="text-xs text-tertiary">
              {t('Agents will dial')}: <span className="font-mono text-secondary">{connectInfo.ws_url}</span>
            </p>
          ) : (
            <p className="text-xs text-amber-600">
              {t('Set WRIT_PUBLIC_URL on the coordinator so agents know where to dial in. Configure it under Settings → Network.')}
            </p>
          )}
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="secondary" onClick={() => setConnectMode(null)}>{t('Back')}</Button>
            <Button onClick={handleMint} loading={minting}>{t('Mint token')}</Button>
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          {/* Native binary variant */}
          <div className="rounded-lg border border-border bg-canvas p-3">
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-xs font-medium uppercase tracking-wide text-tertiary">{t('Run the agent (binary)')}</span>
              <CopyButton value={minted.connect_command} />
            </div>
            <code className="block whitespace-pre-wrap break-all font-mono text-xs text-ink">{minted.connect_command}</code>
          </div>

          {/* Docker variant */}
          <div className="rounded-lg border border-border bg-canvas p-3">
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-xs font-medium uppercase tracking-wide text-tertiary">{t('Run the agent (Docker)')}</span>
              <CopyButton value={dockerCommand} />
            </div>
            <code className="block whitespace-pre-wrap break-all font-mono text-xs text-ink">{dockerCommand}</code>
          </div>

          {/* Other platforms — same download step, different shell. */}
          {minted.install_commands && (
            <details className="rounded-lg border border-border bg-canvas p-3">
              <summary className="cursor-pointer text-xs font-medium uppercase tracking-wide text-tertiary">
                {t('Download on Windows, or build from source')}
              </summary>
              <div className="mt-3 space-y-3">
                <div>
                  <div className="mb-1.5 flex items-center justify-between">
                    <span className="text-[11px] font-medium text-secondary">{t('Windows (PowerShell)')}</span>
                    <CopyButton value={minted.install_commands.windows} />
                  </div>
                  <code className="block whitespace-pre-wrap break-all font-mono text-xs text-ink">{minted.install_commands.windows}</code>
                </div>
                <div>
                  <div className="mb-1.5 flex items-center justify-between">
                    <span className="text-[11px] font-medium text-secondary">{t('Build from source')}</span>
                    <CopyButton value={minted.install_commands.source} />
                  </div>
                  <code className="block whitespace-pre-wrap break-all font-mono text-xs text-ink">{minted.install_commands.source}</code>
                </div>
              </div>
            </details>
          )}

          <p className="text-xs text-tertiary">
            {t('Run one of the commands above on each machine you want to add to the fleet. The raw token is shown only once.')}
          </p>
          {connectInfo?.github_url && (
            <a
              href={connectInfo.github_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-sm text-ink hover:underline"
            >
              <ArrowTopRightOnSquareIcon className="h-4 w-4" />
              {t('Get the writ-agent-fleet binary')}
            </a>
          )}
          <div className="flex justify-end pt-2">
            <Button onClick={closeMint}>{t('Done')}</Button>
          </div>
        </div>
      )}
    </Modal>
  );
};

export default FastConnectAgentModal;
