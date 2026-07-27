import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { CpuChipIcon, CheckIcon } from '@heroicons/react/24/outline';
import { Modal } from '../ui/Modal';
import { RadioGroup, Switch } from '../ui';
import { useQuery } from '../../hooks/useQuery';
import { apiErrorMessage } from '../../api/client';
import {
  fleetApi,
  type FleetAgent,
  type FleetAgentsResponse,
  type DeployResponse,
  type WorkflowDepsPreview,
} from '../../api/fleet';

export interface SendToAgentModalProps {
  open: boolean;
  onClose: () => void;
  kind: 'workflow' | 'secret' | 'persona';
  entityId: number;
  entityName: string;
  onDeployed?: (r: DeployResponse) => void;
}

/**
 * SendToAgentModal — pick a connected fleet agent and push a workflow / secret /
 * persona to it (sealed under the agent's channel key by the coordinator). For
 * workflows, previews the referenced secrets + persona and lets the operator
 * choose whether to bundle them and whether to Mirror (keep a copy here) or Move
 * (transfer it fully, removing the coordinator copy + its private deps).
 */
export const SendToAgentModal: React.FC<SendToAgentModalProps> = ({
  open,
  onClose,
  kind,
  entityId,
  entityName,
  onDeployed,
}) => {
  const { t } = useTranslation();

  // Share FleetPage's poll (same cache key) so a newly-connected agent appears
  // here without a separate fetch cycle.
  const { data } = useQuery<FleetAgentsResponse>(
    'fleet:agents',
    () => fleetApi.listAgents(),
    { pollInterval: 4_000, staleTime: 3_000, enabled: open },
  );

  const localCapable = (a: FleetAgent) => a.online && a.local_capable !== false;
  const agents = (data?.agents ?? []).filter(localCapable);

  const [agentId, setAgentId] = useState<string | null>(null);
  const [includeDeps, setIncludeDeps] = useState(true);
  const [mode, setMode] = useState<'mirror' | 'move'>('mirror');
  const [deps, setDeps] = useState<WorkflowDepsPreview | null>(null);
  const [busy, setBusy] = useState(false);

  // Reset transient state each time the modal (re)opens or is pointed at another
  // entity. Adjusted DURING RENDER (React's derive-state-from-props escape hatch)
  // so the reopened modal never paints the previous target's choices first. The
  // key goes null while closed, so reopening on the SAME entity still resets.
  const seedKey = open ? `${kind}|${entityId}` : null;
  const [seededFor, setSeededFor] = useState<string | null>(seedKey);
  if (seededFor !== seedKey) {
    setSeededFor(seedKey);
    if (open) {
      setAgentId(null);
      setIncludeDeps(true);
      setMode('mirror');
      setDeps(null);
      setBusy(false);
    }
  }

  // Preview a workflow's referenced deps. Guarded — a 404 (before the P1
  // endpoint lands) simply degrades to "no preview", never blocks submit.
  useEffect(() => {
    if (!open || kind !== 'workflow' || !Number.isFinite(entityId)) return;
    let cancelled = false;
    fleetApi
      .workflowDeps(entityId)
      .then((d) => {
        if (!cancelled) setDeps(d);
      })
      .catch(() => {
        /* no preview */
      });
    return () => {
      cancelled = true;
    };
  }, [open, kind, entityId]);

  const handleSubmit = async () => {
    if (!agentId || busy) return;
    setBusy(true);
    try {
      const r = await fleetApi.deploy(agentId, {
        kind,
        id: entityId,
        include_deps: kind === 'workflow' ? includeDeps : undefined,
        mode: kind === 'workflow' ? mode : undefined,
      });
      if (kind === 'workflow' && mode === 'move') {
        toast.success(t('Moved to agent — removed from this coordinator.'));
      } else {
        toast.success(t('Sent to agent.'));
      }
      onDeployed?.(r);
      onClose();
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to send to agent')));
    } finally {
      setBusy(false);
    }
  };

  const footer = (
    <div className="flex items-center justify-end gap-2">
      <button
        type="button"
        onClick={onClose}
        className="px-3 py-1.5 text-sm text-secondary hover:text-ink rounded-lg hover:bg-hover transition-colors"
      >
        {t('Cancel')}
      </button>
      <button
        type="button"
        onClick={handleSubmit}
        disabled={!agentId || busy}
        className="px-3 py-1.5 text-sm bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 disabled:opacity-50 transition-colors"
      >
        {busy ? t('Sending…') : t('Send to agent')}
      </button>
    </div>
  );

  return (
    <Modal
      isOpen={open}
      onClose={onClose}
      size="md"
      title={t('Send to a fleet agent')}
      subtitle={t('“{{name}}” will run locally on the agent you pick.', { name: entityName })}
      footer={footer}
    >
      <div className="space-y-5">
        {/* Agent picker */}
        <div className="space-y-2">
          <div className="text-sm text-secondary">{t('Choose an agent')}</div>
          {agents.length === 0 ? (
            <div className="rounded-lg border border-border bg-hover/40 px-3 py-3 text-[12px] text-secondary">
              {t('No local-capable agents online — connect one from the ')}
              <Link to="/fleet" className="text-ink underline hover:no-underline" onClick={onClose}>
                {t('Fleet page')}
              </Link>
              .
            </div>
          ) : (
            <div className="space-y-2">
              {agents.map((a) => {
                const active = a.id === agentId;
                const free = a.capacity?.free_slots;
                const max = a.capacity?.max_sessions;
                return (
                  <button
                    key={a.id}
                    type="button"
                    onClick={() => setAgentId(a.id)}
                    className={clsx(
                      'flex w-full items-center gap-3 rounded-lg border px-3 py-2.5 text-left transition-colors',
                      active ? 'border-ink bg-hover' : 'border-border hover:border-ink/20',
                    )}
                  >
                    <CpuChipIcon
                      className={clsx('h-4 w-4 flex-shrink-0', active ? 'text-ink' : 'text-tertiary')}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="text-sm text-ink truncate">{a.name}</div>
                      <div className="font-mono text-[10px] text-tertiary truncate">{a.id}</div>
                    </div>
                    {free != null && max != null && (
                      <span className="shrink-0 text-[11px] text-tertiary">
                        {t('{{free}}/{{max}} free', { free, max })}
                      </span>
                    )}
                    {active && <CheckIcon className="h-3.5 w-3.5 flex-shrink-0 text-ink" />}
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {/* Workflow deps preview + include toggle */}
        {kind === 'workflow' && (
          <div className="space-y-2 rounded-lg border border-border p-3">
            <Switch
              checked={includeDeps}
              onChange={setIncludeDeps}
              reverse
              label={t('Include secrets & persona')}
              description={t('Bundle the credentials and identity this workflow needs so it can run standalone.')}
            />
            {deps && ((deps.secrets?.length ?? 0) > 0 || deps.persona) && (
              <div className="flex flex-wrap gap-1.5 pt-1">
                {deps.secrets?.map((s) => (
                  <span
                    key={s}
                    className="rounded bg-hover px-1.5 py-0.5 font-mono text-[10px] text-secondary"
                  >
                    {s}
                  </span>
                ))}
                {deps.persona && (
                  <span className="rounded bg-hover px-1.5 py-0.5 text-[10px] text-secondary">
                    {t('persona')}: {deps.persona}
                  </span>
                )}
              </div>
            )}
          </div>
        )}

        {/* Disposition — workflows only. Secrets/personas are always copied. */}
        {kind === 'workflow' && (
          <div className="space-y-2">
            <div className="text-sm text-secondary">{t('Disposition')}</div>
            <RadioGroup
              value={mode}
              onChange={setMode}
              options={[
                {
                  value: 'mirror',
                  label: t('Mirror'),
                  description: t('Keep a copy here; also run it on the agent.'),
                },
                {
                  value: 'move',
                  label: t('Move'),
                  description: t(
                    'Transfer it fully — removes it and its private secrets from this coordinator.',
                  ),
                },
              ]}
            />
          </div>
        )}
      </div>
    </Modal>
  );
};

export default SendToAgentModal;
