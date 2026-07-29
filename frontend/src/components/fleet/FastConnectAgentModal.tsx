import React, { useState } from 'react';
import {
  ArrowPathIcon,
  ArrowTopRightOnSquareIcon,
  CheckCircleIcon,
  ChevronRightIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
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
 * ONE path leads: the pairing-code one-liner. It is the only route that works on a
 * machine with nothing installed, and the only one where nothing can be pasted
 * wrong. The modal used to open on a three-card fork that gave a raw-token flow
 * equal billing with it, and that flow opened on a fifteen-line shell blob —
 * a `uname` case statement over a GitHub releases API call — above a name input.
 * Both alternatives still exist (a bakeable long-lived token is genuinely needed
 * for Docker and CI), but they are one disclosure down, where a reader who does
 * not need them never meets them.
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

  // One name for whichever path the operator takes. It is a real label now:
  // the coordinator stores it against the minted token and the fleet list
  // resolves it back, so a machine shows as "laptop-1" instead of its raw
  // `writ-3f9c…` id. (Nothing on the wire carries it TO the agent — the agent
  // never learns its own name — which is why the field used to do nothing.)
  const [name, setName] = useState('');

  // ── The one-liner: a single-use pairing code, never a long-lived token here.
  const [pairCode, setPairCode] = useState<PairCode | null>(null);
  const [pairBusy, setPairBusy] = useState(false);
  const [pairError, setPairError] = useState<string | null>(null);

  // ── Advanced. Collapsed by default; opened by the operator, or by a failed
  //    local start (which is exactly when the manual path becomes the answer).
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [localBusy, setLocalBusy] = useState(false);
  const [localResult, setLocalResult] = useState<LocalAgentStatus | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [minting, setMinting] = useState(false);
  const [minted, setMinted] = useState<MintedFleetToken | null>(null);

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

  const trimmedName = name.trim();
  const localBlocked = localAgent ? !localAgent.supported || localAgent.running : false;

  /** Mint the single-use pairing code behind the one-liner. */
  const handleMintPair = async () => {
    if (pairBusy) return;
    setPairBusy(true);
    setPairError(null);
    try {
      setPairCode(await fleetApi.mintPairCode(trimmedName || undefined));
      await onConnected?.();
    } catch (err) {
      setPairError(apiErrorMessage(err, t('Could not prepare a connect code.')));
    } finally {
      setPairBusy(false);
    }
  };

  /** "Run one here" — the coordinator mints, downloads, configures and launches.
   *  Nothing is copied; the token never reaches the browser. */
  const handleStartLocal = async () => {
    setLocalBusy(true);
    setLocalError(null);
    try {
      // Empty is meaningful: the coordinator names the registry entry itself and
      // leaves the fleet list showing the agent's own id, rather than labelling
      // every unnamed machine with a placeholder the operator never chose.
      const res = await fleetApi.startLocalAgent(trimmedName);
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

  const handleMintToken = async () => {
    setMinting(true);
    try {
      setMinted(await fleetApi.mintToken(trimmedName));
      await onConnected?.();
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to mint fleet token')));
    } finally {
      setMinting(false);
    }
  };

  const closeModal = () => {
    onClose();
    setName('');
    // A pairing code is single-use and short-lived; never let a stale one from a
    // previous open be presented as if it were still good. Same for a minted
    // token's raw value, which is shown exactly once.
    setPairCode(null);
    setPairError(null);
    setAdvancedOpen(false);
    setLocalResult(null);
    setLocalError(null);
    setMinted(null);
  };

  const expiryMinutes = pairCode ? Math.max(1, Math.round(pairCode.expires_in / 60)) : 0;

  const dialHint = connectInfo?.ws_url ? (
    <p className="text-[11.5px] text-tertiary">
      {t('Agents will dial')}: <span className="font-mono text-secondary">{connectInfo.ws_url}</span>
    </p>
  ) : (
    <p className="text-[11.5px] leading-relaxed text-amber-600">
      {t('Set WRIT_PUBLIC_URL on the coordinator so agents know where to dial in. Configure it under Settings → Network.')}
    </p>
  );

  return (
    <Modal
      isOpen={isOpen}
      onClose={closeModal}
      title={t('Connect a new agent')}
      subtitle={t('One line on the machine that should run the browsers.')}
    >
      <div className="space-y-4">
        {/* ── The one-liner. The only path that works on a machine with nothing
              installed, so it is the only one shown at this level. ── */}
        {!pairCode ? (
          <div className="space-y-3">
            <div>
              <label className="mb-1 block text-sm font-medium text-ink" htmlFor="fleet-agent-name">
                {t('Name this machine')}{' '}
                <span className="font-normal text-tertiary">{t('(optional)')}</span>
              </label>
              <Input
                id="fleet-agent-name"
                value={name}
                onChange={e => setName(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); void handleMintPair(); } }}
                placeholder={t('e.g. laptop-1, vps-eu')}
                autoFocus
              />
              <p className="mt-1 text-[11.5px] text-tertiary">
                {t('Labels it in the fleet list. Leave blank to use its agent id.')}
              </p>
            </div>
            {pairError && (
              <div className="rounded-lg border border-border bg-canvas px-3 py-2.5 text-[13px] text-secondary">{pairError}</div>
            )}
            <Button onClick={handleMintPair} loading={pairBusy} className="w-full">
              {t('Get the connect command')}
            </Button>
          </div>
        ) : (
          <div className="space-y-2.5">
            <p className="text-[12px] leading-relaxed text-secondary">
              {t('Run this on the machine that should do the browsing. It installs the agent and connects it — nothing to install first.')}
            </p>
            <div className="relative">
              <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-lg bg-ink p-3 pr-16 font-mono text-[11px] leading-relaxed text-white/90">{pairCode.install_command}</pre>
              <div className="absolute right-2 top-2">
                <CopyButton value={pairCode.install_command} tone="on-dark" />
              </div>
            </div>
            {/* The agent list polls on its own, so an agent that dials in shows up
                above without the operator re-opening anything. */}
            <p className="text-[11.5px] leading-relaxed text-tertiary">
              {t('Works once and expires in {{minutes}} minutes. The agent appears in the fleet as soon as it connects.', { minutes: expiryMinutes })}
            </p>
            <button
              type="button"
              onClick={() => { setPairCode(null); setName(''); }}
              className="text-[12px] font-medium text-ink underline underline-offset-2"
            >
              {t('Connect another machine')}
            </button>
          </div>
        )}

        {dialHint}

        {/* ── Everything else, one disclosure down. ── */}
        <div className="overflow-hidden rounded-xl border border-border">
          <button
            type="button"
            onClick={() => setAdvancedOpen(o => !o)}
            aria-expanded={advancedOpen}
            className="flex w-full items-center gap-2 px-3 py-2.5 text-left transition-colors hover:bg-hover/40"
          >
            <ChevronRightIcon className={clsx('h-3.5 w-3.5 shrink-0 text-tertiary transition-transform', advancedOpen && 'rotate-90')} />
            <span className="text-[12.5px] font-medium text-ink">{t('Other ways to connect')}</span>
            <span className="ml-auto truncate text-[11px] text-tertiary">{t('This machine · Docker · long-lived token')}</span>
          </button>

          {advancedOpen && (
            <div className="space-y-4 border-t border-border p-3">
              {/* ── Run one on the coordinator's own host. Preflight decides
                    whether this host can host it at all (right platform, not a
                    browser-less container). ── */}
              <section className="space-y-2">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-[13px] font-semibold text-ink">{t('Run one on this machine')}</p>
                    <p className="mt-0.5 text-[12px] leading-relaxed text-tertiary">
                      {localAgent?.running
                        ? t('An agent is already running here ({{name}}).', { name: localAgent.agent_name || t('local-agent') })
                        : localAgent && !localAgent.supported
                          ? localAgent.blockers.join(' ')
                          : t('Downloads the agent for {{platform}}, points it at this coordinator, and starts it. Nothing to copy.', { platform: localAgent?.platform || t('this host') })}
                    </p>
                  </div>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={handleStartLocal}
                    loading={localBusy}
                    disabled={localBlocked || localBusy}
                    className="shrink-0"
                  >
                    {t('Start')}
                  </Button>
                </div>

                {localBusy && (
                  <div className="flex items-center gap-3 rounded-lg border border-border bg-canvas p-3">
                    <ArrowPathIcon className="h-4 w-4 shrink-0 animate-spin text-secondary" />
                    <div className="min-w-0">
                      <p className="text-[12.5px] font-medium text-ink">{t('Installing the agent on this machine…')}</p>
                      <p className="mt-0.5 text-[11.5px] text-tertiary">{t('Resolving the release, downloading, then starting it.')}</p>
                    </div>
                  </div>
                )}

                {localError && (
                  <div className="rounded-lg border border-amber-300 bg-amber-50 p-3">
                    <p className="text-[12.5px] font-medium text-amber-900">{t('Could not start it here')}</p>
                    <p className="mt-1 text-[11.5px] leading-relaxed text-amber-800">{localError}</p>
                  </div>
                )}

                {localResult && !localError && (
                  <div className="space-y-2">
                    <div className="flex items-start gap-3 rounded-lg border border-border bg-canvas p-3">
                      <CheckCircleIcon className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
                      <div className="min-w-0">
                        <p className="text-[12.5px] font-medium text-ink">
                          {localResult.status === 'already_running' ? t('Already running here') : t('Agent is running on this machine')}
                        </p>
                        <p className="mt-0.5 text-[11.5px] leading-relaxed text-tertiary">
                          {t('It appears in the fleet above and picks up work automatically.')}
                          {localResult.pid ? ` · PID ${localResult.pid}` : ''}
                        </p>
                      </div>
                    </div>
                    {localResult.checksum_verified === false && (
                      /* Say so rather than implying a verification that did not happen. */
                      <p className="text-[11px] leading-relaxed text-tertiary">
                        {t('The release published no checksum for this asset, so its integrity was not verified.')}
                      </p>
                    )}
                    <p className="text-[11px] text-tertiary">
                      {t('Logs')}: <span className="font-mono">{localResult.log_path}</span>
                    </p>
                  </div>
                )}
              </section>

              {/* ── A long-lived token: for Docker, CI, or an image you bake. ── */}
              <section className="space-y-2 border-t border-border pt-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-[13px] font-semibold text-ink">{t('Mint a long-lived token')}</p>
                    <p className="mt-0.5 text-[12px] leading-relaxed text-tertiary">
                      {t('For Docker, CI, or a machine you provision from a script — where a single-use interactive code is the wrong shape.')}
                    </p>
                  </div>
                  {!minted && (
                    <Button variant="secondary" size="sm" onClick={handleMintToken} loading={minting} className="shrink-0">
                      {t('Mint')}
                    </Button>
                  )}
                </div>

                {minted && (
                  <div className="space-y-3">
                    {/* Step 1 — GET the binary. The run command below assumes a
                        writ-agent-fleet that a new operator does not have yet. */}
                    <div className="rounded-lg border border-border bg-canvas p-3">
                      <div className="mb-1.5 flex items-center justify-between gap-3">
                        <span className="text-[11px] font-medium uppercase tracking-wide text-tertiary">{t('1 · Download it (macOS / Linux)')}</span>
                        {minted.install_commands?.unix && <CopyButton value={minted.install_commands.unix} />}
                      </div>
                      <code className="block whitespace-pre-wrap break-all font-mono text-[11px] text-ink">
                        {minted.install_commands?.unix || t('Unavailable — update the coordinator.')}
                      </code>
                    </div>

                    {/* Step 2 — run THAT binary, with the token in its environment. */}
                    <div className="rounded-lg border border-border bg-canvas p-3">
                      <div className="mb-1.5 flex items-center justify-between gap-3">
                        <span className="text-[11px] font-medium uppercase tracking-wide text-tertiary">{t('2 · Run it')}</span>
                        <CopyButton value={minted.connect_command} />
                      </div>
                      <code className="block whitespace-pre-wrap break-all font-mono text-[11px] text-ink">{minted.connect_command}</code>
                    </div>

                    <details className="rounded-lg border border-border bg-canvas p-3">
                      <summary className="cursor-pointer text-[11px] font-medium uppercase tracking-wide text-tertiary">
                        {t('Docker, Windows, or build from source')}
                      </summary>
                      <div className="mt-3 space-y-3">
                        <div>
                          <div className="mb-1.5 flex items-center justify-between gap-3">
                            <span className="text-[11px] font-medium text-secondary">{t('Docker')}</span>
                            <CopyButton value={minted.docker_command} />
                          </div>
                          <code className="block whitespace-pre-wrap break-all font-mono text-[11px] text-ink">{minted.docker_command}</code>
                        </div>
                        {minted.install_commands && (
                          <>
                            <div>
                              <div className="mb-1.5 flex items-center justify-between gap-3">
                                <span className="text-[11px] font-medium text-secondary">{t('Windows (PowerShell)')}</span>
                                <CopyButton value={minted.install_commands.windows} />
                              </div>
                              <code className="block whitespace-pre-wrap break-all font-mono text-[11px] text-ink">{minted.install_commands.windows}</code>
                            </div>
                            <div>
                              <div className="mb-1.5 flex items-center justify-between gap-3">
                                <span className="text-[11px] font-medium text-secondary">{t('Build from source')}</span>
                                <CopyButton value={minted.install_commands.source} />
                              </div>
                              <code className="block whitespace-pre-wrap break-all font-mono text-[11px] text-ink">{minted.install_commands.source}</code>
                            </div>
                          </>
                        )}
                      </div>
                    </details>

                    <p className="text-[11px] leading-relaxed text-tertiary">
                      {t('The raw token is shown only once. Revoke it any time from Fleet tokens.')}
                    </p>
                  </div>
                )}

                {connectInfo?.repo_url && (
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
                    <a href={connectInfo.repo_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-secondary hover:text-ink hover:underline">
                      <ArrowTopRightOnSquareIcon className="h-3 w-3" /> {connectInfo.repo}
                    </a>
                    <a href={connectInfo.github_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-secondary hover:text-ink hover:underline">
                      <ArrowTopRightOnSquareIcon className="h-3 w-3" /> {t('Releases')}
                    </a>
                    <span className="font-mono text-tertiary">{connectInfo.docker_image}</span>
                  </div>
                )}
              </section>
            </div>
          )}
        </div>

        <div className="flex justify-end pt-1">
          <Button variant={pairCode || minted || localResult ? 'primary' : 'secondary'} onClick={closeModal} disabled={localBusy}>
            {pairCode || minted || localResult ? t('Done') : t('Cancel')}
          </Button>
        </div>
      </div>
    </Modal>
  );
};

export default FastConnectAgentModal;
