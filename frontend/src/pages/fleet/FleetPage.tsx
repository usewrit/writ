import React, { useCallback, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import {
  CpuChipIcon,
  PlusIcon,
  TrashIcon,
  ClipboardDocumentIcon,
  CheckIcon,
  SignalIcon,
  SignalSlashIcon,
  KeyIcon,
} from '@heroicons/react/24/outline';
import { useRequireAuth } from '../../hooks/useAuth';
import { useDocumentTitle } from '../../hooks/useDocumentTitle';
import { useQuery } from '../../hooks/useQuery';
import { SHELF_TOPBAR, RowsSkeleton } from '../../components/library/shelf';
import { shelfFilterChipClass, shelfFilterCountClass } from '../../components/library/shelf';
import { StatBar } from '../../components/ui/StatBar';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { apiErrorMessage } from '../../api/client';
import { FastConnectAgentModal } from '../../components/fleet/FastConnectAgentModal';
import {
  fleetApi,
  type FleetAgent,
  type FleetAgentsResponse,
  type FleetTokenMeta,
} from '../../api/fleet';

function relTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso).getTime();
  if (Number.isNaN(d)) return '—';
  const secs = Math.round((Date.now() - d) / 1000);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

// ── Confirmation flow ──────────────────────────────────────────────────────
// One reusable ConfirmDialog drives every destructive path (single remove, bulk
// remove, prune offline, token revoke) so the page never falls back to a raw
// window.confirm (which broke the design DNA and blocked the UI thread).
interface Pending {
  title: string;
  message: string;
  confirmText: string;
  run: () => Promise<void>;
}

// ── Inline row checkbox ────────────────────────────────────────────────────
// Hidden until the row is hovered — or until ANY row is checked (revealAll), so
// the whole selection column appears together once a bulk action begins.
const RowCheck: React.FC<{
  checked: boolean;
  revealAll: boolean;
  onToggle: () => void;
  label: string;
}> = ({ checked, revealAll, onToggle, label }) => (
  <button
    type="button"
    onClick={(e) => { e.stopPropagation(); onToggle(); }}
    aria-pressed={checked}
    aria-label={label}
    className={clsx(
      'flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-all',
      'outline-none focus-visible:ring-2 focus-visible:ring-ink/30',
      checked
        ? 'border-ink bg-ink text-surface opacity-100'
        : clsx('border-border bg-surface text-transparent', revealAll ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'),
    )}
  >
    <CheckIcon className="h-3 w-3" />
  </button>
);

// ── Agent row ──────────────────────────────────────────────────────────────
const AgentRow: React.FC<{
  a: FleetAgent;
  checked: boolean;
  revealAll: boolean;
  onToggle: (id: string) => void;
  onRemove: (a: FleetAgent) => void;
}> = React.memo(({ a, checked, revealAll, onToggle, onRemove }) => {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const cap = a.capacity;
  const hasCap = cap.max_sessions != null && cap.max_sessions > 0;
  const usedPct = hasCap && cap.active_sessions != null
    ? Math.min(100, Math.round((cap.active_sessions / (cap.max_sessions as number)) * 100))
    : 0;

  const copyId = () => {
    navigator.clipboard.writeText(a.id).then(
      () => { setCopied(true); setTimeout(() => setCopied(false), 1500); },
      () => toast.error(t('Could not copy to clipboard')),
    );
  };

  return (
    <div className={clsx(
      'group relative flex items-center gap-3 rounded-lg px-2.5 py-2.5 transition-colors',
      checked ? 'bg-chrome' : 'hover:bg-chrome',
    )}>
      <RowCheck checked={checked} revealAll={revealAll} onToggle={() => onToggle(a.id)} label={t('Select agent')} />

      {a.online
        ? <SignalIcon className="h-4 w-4 shrink-0 text-emerald-600" />
        : <SignalSlashIcon className="h-4 w-4 shrink-0 text-tertiary" />}

      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[13px] font-medium text-ink">{a.name}</span>
          {!a.online && <span className="text-[10px] text-tertiary">{t('Offline')}</span>}
          {a.is_trusted && <span className="rounded bg-ink/10 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide text-secondary">{t('Trusted')}</span>}
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-tertiary">
          <span className="font-mono">{a.id}</span>
          <span className="text-tertiary/50">·</span>
          <span className="capitalize">{a.platform}</span>
          <span className="text-tertiary/50">·</span>
          <span>{t('Seen {{time}}', { time: relTime(a.last_seen) })}</span>
        </div>
      </div>

      {/* Capacity meter — only meaningful while online with a known slot count. */}
      {a.online && hasCap && (
        <div className="hidden items-center gap-1.5 @pair/stage:flex" title={t('{{free}} of {{max}} session slots free', { free: cap.free_slots ?? 0, max: cap.max_sessions })}>
          <div className="h-1.5 w-16 overflow-hidden rounded-full bg-border">
            <div className="h-full rounded-full bg-ink/40" style={{ width: `${usedPct}%` }} />
          </div>
          <span className="w-9 text-right text-[10px] tabular-nums text-tertiary">{cap.free_slots ?? 0}/{cap.max_sessions}</span>
        </div>
      )}

      {/* Inline actions — revealed on hover / focus-within. */}
      <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
        <button
          onClick={copyId}
          title={t('Copy agent id')}
          aria-label={t('Copy agent id')}
          className="flex h-7 w-7 items-center justify-center rounded-lg text-tertiary transition-colors hover:bg-surface hover:text-ink"
        >
          {copied ? <CheckIcon className="h-4 w-4 text-emerald-500" /> : <ClipboardDocumentIcon className="h-4 w-4" />}
        </button>
        <button
          onClick={() => onRemove(a)}
          title={t('Remove agent')}
          aria-label={t('Remove agent')}
          className="flex h-7 w-7 items-center justify-center rounded-lg text-tertiary transition-colors hover:bg-danger-bg hover:text-danger-fg"
        >
          <TrashIcon className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
});
AgentRow.displayName = 'AgentRow';

// ── Token row ──────────────────────────────────────────────────────────────
const TokenRow: React.FC<{
  tk: FleetTokenMeta;
  checked: boolean;
  revealAll: boolean;
  onToggle: (id: string) => void;
  onRevoke: (tk: FleetTokenMeta) => void;
}> = React.memo(({ tk, checked, revealAll, onToggle, onRevoke }) => {
  const { t } = useTranslation();
  return (
    <div className={clsx(
      'group relative flex items-center gap-3 rounded-lg px-2.5 py-2.5 transition-colors',
      checked ? 'bg-chrome' : 'hover:bg-chrome',
    )}>
      <RowCheck checked={checked} revealAll={revealAll} onToggle={() => onToggle(tk.token_id)} label={t('Select token')} />
      <KeyIcon className="h-4 w-4 shrink-0 text-tertiary" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-[13px] font-medium text-ink">{tk.name}</div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[11px] text-tertiary">
          <span className="font-mono">{tk.agent_id}</span>
          <span className="text-tertiary/50">·</span>
          <span className="font-mono">{tk.token_prefix}…</span>
          <span className="text-tertiary/50">·</span>
          <span>{t('Created {{time}}', { time: relTime(tk.created_at) })}</span>
        </div>
      </div>
      <button
        onClick={() => onRevoke(tk)}
        className="flex shrink-0 items-center gap-1.5 rounded-lg px-2.5 py-1 text-[12px] font-medium text-danger opacity-0 transition-all hover:bg-danger-bg hover:text-danger-fg focus-visible:opacity-100 group-hover:opacity-100"
      >
        <TrashIcon className="h-3.5 w-3.5" />
        {t('Revoke')}
      </button>
    </div>
  );
});
TokenRow.displayName = 'TokenRow';

// ── Bulk action bar ────────────────────────────────────────────────────────
const BulkBar: React.FC<{
  count: number;
  allChecked: boolean;
  onSelectAll: () => void;
  onClear: () => void;
  actionLabel: string;
  onAction: () => void;
  busy?: boolean;
}> = ({ count, allChecked, onSelectAll, onClear, actionLabel, onAction, busy }) => {
  const { t } = useTranslation();
  return (
    <div className="bulkbar-enter mb-2 flex items-center justify-between gap-2 rounded-lg border border-border bg-chrome px-3 py-2">
      <div className="flex items-center gap-2 text-[12px]">
        <button
          onClick={onSelectAll}
          aria-label={t('Select all')}
          className={clsx('flex h-4 w-4 items-center justify-center rounded border transition-colors', allChecked ? 'border-ink bg-ink text-surface' : 'border-border bg-surface')}
        >
          {allChecked && <CheckIcon className="h-3 w-3" />}
        </button>
        <span className="font-medium text-ink">{t('{{n}} selected', { n: count })}</span>
        <button onClick={onClear} className="text-tertiary transition-colors hover:text-ink">{t('Clear')}</button>
      </div>
      <button
        onClick={onAction}
        disabled={busy}
        className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-[12px] font-medium text-danger transition-colors hover:bg-danger-bg hover:text-danger-fg disabled:opacity-50"
      >
        <TrashIcon className="h-3.5 w-3.5" />
        {actionLabel}
      </button>
    </div>
  );
};

type AgentFilter = 'all' | 'online' | 'offline';

export const FleetPage: React.FC = () => {
  useRequireAuth();
  const { t } = useTranslation();
  useDocumentTitle(t('Fleet'));

  // Poll the live fleet on a short interval (low staleTime so the poll actually
  // re-fetches) — a newly-dialed-in writ-agent shows up within a few seconds.
  // Polling pauses while the tab is hidden (see useQuery).
  const { data: agentsData, loading: agentsLoading, refresh: refreshAgents } = useQuery<FleetAgentsResponse>(
    'fleet:agents',
    () => fleetApi.listAgents(),
    { pollInterval: 4_000, staleTime: 3_000 },
  );
  const { data: tokens, refresh: refreshTokens } = useQuery<FleetTokenMeta[]>(
    'fleet:tokens',
    () => fleetApi.listTokens(),
    { silent: true },
  );

  const [mintOpen, setMintOpen] = useState(false);
  // Which door the operator picked in the connect modal. `null` = still choosing.
  // Most first installs are the operator sitting AT the coordinator's machine, so
  // running one right here is offered beside the copy-paste path rather than
  // behind it — but only when preflight says this host can actually host one.

  const [agentFilter, setAgentFilter] = useState<AgentFilter>('all');
  const [rawCheckedAgents, setCheckedAgents] = useState<Set<string>>(new Set());
  const [rawCheckedTokens, setCheckedTokens] = useState<Set<string>>(new Set());

  const [pending, setPending] = useState<Pending | null>(null);
  const [pendingBusy, setPendingBusy] = useState(false);

  const agents: FleetAgent[] = useMemo(() => agentsData?.agents ?? [], [agentsData]);
  const onlineCount = agentsData?.online_count ?? agents.filter(a => a.online).length;
  const offlineAgents = useMemo(() => agents.filter(a => !a.online), [agents]);
  const activeTokens = useMemo(() => (tokens ?? []).filter(tk => !tk.revoked_at), [tokens]);

  const visibleAgents = useMemo(() => {
    if (agentFilter === 'online') return agents.filter(a => a.online);
    if (agentFilter === 'offline') return offlineAgents;
    return agents;
  }, [agents, offlineAgents, agentFilter]);

  // Aggregate free/total session capacity across the online fleet.
  const capacity = useMemo(() => {
    let free = 0; let max = 0;
    for (const a of agents) {
      if (a.online && a.capacity.max_sessions != null) {
        max += a.capacity.max_sessions;
        free += a.capacity.free_slots ?? 0;
      }
    }
    return { free, max };
  }, [agents]);

  // Keep selections pruned to ids that still exist (agents drop off, tokens
  // revoke) — pruned ON READ. The 4s agent poll means an effect-based prune
  // would fire a state write, and a second render pass, on every tick where a
  // machine dropped off; deriving it costs nothing.
  const checkedAgents = useMemo(() => {
    const live = new Set(agents.map(a => a.id));
    return new Set([...rawCheckedAgents].filter(id => live.has(id)));
  }, [rawCheckedAgents, agents]);
  const checkedTokens = useMemo(() => {
    const live = new Set(activeTokens.map(tk => tk.token_id));
    return new Set([...rawCheckedTokens].filter(id => live.has(id)));
  }, [rawCheckedTokens, activeTokens]);

  const toggleAgent = useCallback((id: string) => setCheckedAgents(prev => {
    const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n;
  }), []);
  const toggleToken = useCallback((id: string) => setCheckedTokens(prev => {
    const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n;
  }), []);
  const clearAgentChecks = useCallback(() => setCheckedAgents(new Set()), []);
  const clearTokenChecks = useCallback(() => setCheckedTokens(new Set()), []);

  const allAgentsChecked = visibleAgents.length > 0 && visibleAgents.every(a => checkedAgents.has(a.id));
  const allTokensChecked = activeTokens.length > 0 && activeTokens.every(tk => checkedTokens.has(tk.token_id));

  const runPending = async () => {
    if (!pending) return;
    setPendingBusy(true);
    try { await pending.run(); } finally { setPendingBusy(false); setPending(null); }
  };

  // ── Mint ──
  /** "Run one here" — the coordinator mints, downloads, configures and launches.
   *  Nothing is copied; the token never reaches the browser. */


  /// Mint the single-use pairing code behind the one-liner. Separate from
  /// `handleMint` on purpose — see the note on `pairCode`.

  // ── Destructive actions (all routed through the ConfirmDialog) ──
  const askRemoveAgent = (a: FleetAgent) => setPending({
    title: t('Remove agent'),
    message: t('Remove “{{name}}” from the fleet? Any bound token is revoked and it stops receiving work immediately.', { name: a.name }),
    confirmText: t('Remove'),
    run: async () => {
      try {
        await fleetApi.removeAgent(a.id);
        toast.success(t('Agent removed'));
        await Promise.all([refreshAgents(), refreshTokens()]);
      } catch (e) {
        toast.error(apiErrorMessage(e, t('Failed to remove agent')));
      }
    },
  });

  const askBulkRemoveAgents = () => {
    const ids = [...checkedAgents];
    if (!ids.length) return;
    setPending({
      title: t('Remove agents'),
      message: t('Remove {{n}} agent(s) from the fleet? Their bound tokens are revoked and they stop receiving work immediately.', { n: ids.length }),
      confirmText: t('Remove {{n}}', { n: ids.length }),
      run: async () => {
        try {
          const r = await fleetApi.pruneAgents(ids);
          clearAgentChecks();
          toast.success(t('Removed {{n}}', { n: r.removed }));
          await Promise.all([refreshAgents(), refreshTokens()]);
        } catch (e) {
          toast.error(apiErrorMessage(e, t('Failed to remove agents')));
        }
      },
    });
  };

  const askPruneOffline = () => {
    const ids = offlineAgents.map(a => a.id);
    if (!ids.length) return;
    setPending({
      title: t('Remove offline agents'),
      message: t('Remove all {{n}} offline agent(s)? This clears out stale machines that are no longer connected.', { n: ids.length }),
      confirmText: t('Remove {{n}}', { n: ids.length }),
      run: async () => {
        try {
          const r = await fleetApi.pruneAgents(ids);
          clearAgentChecks();
          toast.success(t('Removed {{n}}', { n: r.removed }));
          await Promise.all([refreshAgents(), refreshTokens()]);
        } catch (e) {
          toast.error(apiErrorMessage(e, t('Failed to remove agents')));
        }
      },
    });
  };

  const askRevokeToken = (tk: FleetTokenMeta) => setPending({
    title: t('Revoke fleet token'),
    message: t('Revoke “{{name}}”? The bound agent is disconnected and can no longer reconnect with it.', { name: tk.name }),
    confirmText: t('Revoke'),
    run: async () => {
      try {
        await fleetApi.revokeToken(tk.token_id);
        toast.success(t('Token revoked'));
        await Promise.all([refreshTokens(), refreshAgents()]);
      } catch (e) {
        toast.error(apiErrorMessage(e, t('Failed to revoke token')));
      }
    },
  });

  const askBulkRevokeTokens = () => {
    const ids = [...checkedTokens];
    if (!ids.length) return;
    setPending({
      title: t('Revoke fleet tokens'),
      message: t('Revoke {{n}} token(s)? Their bound agents are disconnected and can no longer reconnect.', { n: ids.length }),
      confirmText: t('Revoke {{n}}', { n: ids.length }),
      run: async () => {
        let ok = 0; let fail = 0;
        for (const id of ids) {
          try { await fleetApi.revokeToken(id); ok += 1; } catch { fail += 1; }
        }
        clearTokenChecks();
        await Promise.all([refreshTokens(), refreshAgents()]);
        if (fail === 0) toast.success(t('Revoked {{n}}', { n: ok }));
        else toast.error(t('{{ok}} revoked · {{fail}} failed', { ok, fail }));
      },
    });
  };

  const selectAllAgents = () => setCheckedAgents(allAgentsChecked ? new Set() : new Set(visibleAgents.map(a => a.id)));
  const selectAllTokens = () => setCheckedTokens(allTokensChecked ? new Set() : new Set(activeTokens.map(tk => tk.token_id)));

  const FILTERS: { id: AgentFilter; label: string; count: number }[] = [
    { id: 'all', label: t('All'), count: agents.length },
    { id: 'online', label: t('Online'), count: onlineCount },
    { id: 'offline', label: t('Offline'), count: offlineAgents.length },
  ];

  return (
    <div className="flex h-full flex-col">
      {/* ── Chrome topbar ── */}
      <div className={SHELF_TOPBAR}>
        <CpuChipIcon className="h-4 w-4 shrink-0 text-secondary" />
        <span className="shrink-0 text-[13px] font-semibold text-ink">{t('Fleet')}</span>
        <span className="ml-1 hidden text-[11px] text-secondary @pair/stage:inline">
          {t('{{online}} online · {{total}} total', { online: onlineCount, total: agents.length })}
        </span>
        <span className="ml-1 hidden truncate text-[11px] text-secondary @rail/stage:inline">
          {t('writ-agent-fleet workers connected to this coordinator')}
        </span>
        <div className="flex-1" />
        <button
          onClick={() => setMintOpen(true)}
          className="flex shrink-0 items-center gap-1.5 rounded-lg bg-accent-strong px-3 py-1.5 text-[12px] font-semibold text-accent-on shadow-sm transition-colors hover:bg-accent-strong/90"
        >
          <PlusIcon className="h-3.5 w-3.5" />
          {t('Connect agent')}
        </button>
      </div>

      {/* ── Body ── */}
      <div className="flex-1 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        <div className="mx-auto w-full max-w-5xl px-4 py-6 sm:px-6">
          <StatBar
            className="mb-8"
            items={[
              { label: t('Online'), value: onlineCount, sub: t('receiving work') },
              { label: t('Offline'), value: offlineAgents.length, sub: offlineAgents.length ? t('stale / disconnected') : t('all connected') },
              { label: t('Free capacity'), value: capacity.max ? `${capacity.free}/${capacity.max}` : '—', sub: t('session slots') },
              { label: t('Active tokens'), value: activeTokens.length, sub: t('connect tokens') },
            ]}
          />

          {/* ── Connected agents ── */}
          <section>
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-[13px] font-semibold text-ink">{t('Connected agents')}</h2>
              <div className="flex items-center gap-1">
                {FILTERS.map(f => (
                  <button
                    key={f.id}
                    onClick={() => setAgentFilter(f.id)}
                    aria-pressed={agentFilter === f.id}
                    className={shelfFilterChipClass(agentFilter === f.id)}
                  >
                    {f.label}
                    <span className={shelfFilterCountClass(agentFilter === f.id)}>{f.count}</span>
                  </button>
                ))}
                {offlineAgents.length > 0 && (
                  <button
                    onClick={askPruneOffline}
                    className="ml-1 inline-flex items-center gap-1.5 rounded-lg border border-border bg-surface px-2 py-1 text-[11px] font-medium text-secondary transition-colors hover:border-danger/40 hover:text-danger"
                  >
                    <TrashIcon className="h-3.5 w-3.5" />
                    {t('Remove offline')}
                  </button>
                )}
              </div>
            </div>

            {checkedAgents.size > 0 && (
              <BulkBar
                count={checkedAgents.size}
                allChecked={allAgentsChecked}
                onSelectAll={selectAllAgents}
                onClear={clearAgentChecks}
                actionLabel={t('Remove {{n}}', { n: checkedAgents.size })}
                onAction={askBulkRemoveAgents}
              />
            )}

            {agentsLoading && agents.length === 0 ? (
              <RowsSkeleton rows={4} label={t('Loading agents…')} />
            ) : visibleAgents.length === 0 ? (
              <div className="rounded-xl border border-dashed border-border px-6 py-12 text-center">
                <CpuChipIcon className="mx-auto mb-3 h-8 w-8 text-tertiary" />
                <p className="text-[13px] font-medium text-ink">
                  {agents.length === 0 ? t('No agents connected yet') : t('Nothing here')}
                </p>
                <p className="mx-auto mt-1 max-w-sm text-[12px] text-secondary">
                  {agents.length === 0
                    ? t('Connect a writ-agent-fleet worker to this coordinator to start dispatching workflows and monitor checks.')
                    : t('No agents match this filter.')}
                </p>
                {agents.length === 0 && (
                  <button
                    onClick={() => setMintOpen(true)}
                    className="mt-4 inline-flex items-center gap-1.5 rounded-lg bg-accent-strong px-3.5 py-2 text-[12px] font-semibold text-accent-on shadow-sm transition-colors hover:bg-accent-strong/90"
                  >
                    <PlusIcon className="h-3.5 w-3.5" />
                    {t('Connect a new agent')}
                  </button>
                )}
              </div>
            ) : (
              <div className="grid gap-0.5">
                {visibleAgents.map(a => (
                  <AgentRow
                    key={a.id}
                    a={a}
                    checked={checkedAgents.has(a.id)}
                    revealAll={checkedAgents.size > 0}
                    onToggle={toggleAgent}
                    onRemove={askRemoveAgent}
                  />
                ))}
              </div>
            )}
          </section>

          {/* ── Fleet tokens ── */}
          <section className="mt-10">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-[13px] font-semibold text-ink">{t('Fleet tokens')}</h2>
              <span className="text-[11px] text-tertiary">{t('Long-lived connect tokens minted for agents')}</span>
            </div>

            {checkedTokens.size > 0 && (
              <BulkBar
                count={checkedTokens.size}
                allChecked={allTokensChecked}
                onSelectAll={selectAllTokens}
                onClear={clearTokenChecks}
                actionLabel={t('Revoke {{n}}', { n: checkedTokens.size })}
                onAction={askBulkRevokeTokens}
              />
            )}

            {activeTokens.length === 0 ? (
              <p className="rounded-xl border border-dashed border-border px-4 py-6 text-center text-[12px] text-tertiary">
                {t('No active fleet tokens. Mint one to connect an agent.')}
              </p>
            ) : (
              <div className="grid gap-0.5">
                {activeTokens.map(tk => (
                  <TokenRow
                    key={tk.token_id}
                    tk={tk}
                    checked={checkedTokens.has(tk.token_id)}
                    revealAll={checkedTokens.size > 0}
                    onToggle={toggleToken}
                    onRevoke={askRevokeToken}
                  />
                ))}
              </div>
            )}
          </section>
        </div>
      </div>

      {/* ── Confirm dialog (single + bulk destructive paths) ── */}
      <ConfirmDialog
        isOpen={!!pending}
        onClose={() => (pendingBusy ? undefined : setPending(null))}
        onConfirm={runPending}
        title={pending?.title ?? ''}
        message={pending?.message ?? ''}
        confirmText={pending?.confirmText}
        variant="danger"
        isLoading={pendingBusy}
      />

      {/* ── Connect modal — the SHARED flow (also rendered by the recorder gate). ── */}
      <FastConnectAgentModal
        isOpen={mintOpen}
        onClose={() => setMintOpen(false)}
        onConnected={async () => { await Promise.all([refreshAgents(), refreshTokens()]); }}
      />
    </div>
  );
};

export default FleetPage;
