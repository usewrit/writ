import React from 'react';
import { useTranslation } from 'react-i18next';
import { useUserAgents, UserAgentsData } from '../../hooks/useUserAgents';
import {
  CloudIcon,
  ComputerDesktopIcon,
  ArrowsRightLeftIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { Select } from '../ui';

type ExecutionTarget = 'auto' | 'local' | 'cloud';

/** Online user-hosted devices from the shared useUserAgents cache. */
interface OnlineDevice {
  agent_id: string;
  name: string;
}

function onlineDevicesFrom(agentData: UserAgentsData | null | undefined): OnlineDevice[] {
  const rows = (agentData?.agents ?? []) as Array<{ status?: string; agent_id?: string; name?: string }>;
  return rows
    .filter((a) => a?.status === 'online' && a?.agent_id)
    .map((a) => ({ agent_id: String(a.agent_id), name: String(a.name || a.agent_id) }));
}

interface ExecutionTargetPickerProps {
  value: ExecutionTarget;
  onChange: (value: ExecutionTarget) => void;
  compact?: boolean; // Inline pill selector vs full cards
  // Multi-device pin (optional; single/zero-device UX unchanged when omitted).
  // When target==='local' AND 2+ online devices exist, a device <Select> is
  // revealed so the caller can pin the run to one device.
  agentId?: string;
  onAgentIdChange?: (agentId: string | undefined) => void;
}

const OPTIONS: Array<{
  value: ExecutionTarget;
  label: string;
  description: string;
  icon: React.FC<any>;
}> = [
  {
    value: 'auto',
    label: 'Auto',
    description: 'Prefer local agent, fallback to cloud',
    icon: ArrowsRightLeftIcon,
  },
  {
    value: 'local',
    label: 'Local Agent',
    description: 'Run on your connected agent only',
    icon: ComputerDesktopIcon,
  },
  {
    value: 'cloud',
    label: 'Writ Cloud',
    description: 'Run on Writ infrastructure',
    icon: CloudIcon,
  },
];

export const ExecutionTargetPicker: React.FC<ExecutionTargetPickerProps> = ({
  value,
  onChange,
  compact = false,
  agentId,
  onAgentIdChange,
}) => {
  const { t } = useTranslation();
  // Shared subscription — no per-instance poll timer. Reads the warm
  // Q.userAgents() cache filled by the page-level/shared query.
  const { data: agentData } = useUserAgents();

  const onlineDevices = React.useMemo(() => onlineDevicesFrom(agentData), [agentData]);
  // "Has an agent to run on" = at least one agent is ONLINE (status 'online' =
  // WS-connected OR seen in the last ~2min — the same signal the device list and
  // agents page use). NOT `active_count`, which counts only strictly-WS-connected
  // agents right now and momentarily reads 0 for a recently-seen/reconnecting
  // agent — the cause of "no agent online" when one is actually connected.
  const hasAgent = onlineDevices.length > 0;
  // Device pinning only appears with 2+ online devices AND a caller that wants it.
  const multiDevice = onlineDevices.length > 1 && !!onAgentIdChange;

  if (compact) {
    return (
      <div className="space-y-2">
        <div className="flex items-center gap-1 bg-zinc-100 rounded-full p-0.5">
          {OPTIONS.map((opt) => {
            const Icon = opt.icon;
            const disabled = opt.value === 'local' && !hasAgent;
            return (
              <button
                key={opt.value}
                onClick={() => !disabled && onChange(opt.value)}
                disabled={disabled}
                title={disabled ? t('No agent connected') : t(opt.description)}
                className={clsx(
                  'flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium rounded-full transition-all',
                  value === opt.value
                    ? 'bg-white text-ink shadow-sm'
                    : disabled
                      ? 'text-zinc-300 cursor-not-allowed'
                      : 'text-zinc-500 hover:text-ink',
                )}
              >
                <Icon className="w-3.5 h-3.5" />
                {t(opt.label)}
              </button>
            );
          })}
        </div>
        {/* Device pin — only with 2+ online devices and a Local target. */}
        {multiDevice && value === 'local' && (
          <Select
            size="sm"
            value={agentId ?? ''}
            onChange={(v) => onAgentIdChange?.(v || undefined)}
            aria-label={t('Device')}
            placeholder={t('Any online device (server picks)')}
            options={[
              { value: '', label: t('Any online device (server picks)') },
              ...onlineDevices.map((d) => ({ value: d.agent_id, label: d.name })),
            ]}
          />
        )}
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <label className="text-xs font-medium text-zinc-500">{t('Run on')}</label>
      <div className="grid grid-cols-3 gap-2">
        {OPTIONS.map((opt) => {
          const Icon = opt.icon;
          const disabled = opt.value === 'local' && !hasAgent;
          const selected = value === opt.value;
          return (
            <button
              key={opt.value}
              onClick={() => !disabled && onChange(opt.value)}
              disabled={disabled}
              className={clsx(
                'flex flex-col items-center gap-2 p-3 rounded-xl border-2 transition-all text-center',
                selected
                  ? 'border-zinc-900 bg-zinc-900 text-white'
                  : disabled
                    ? 'border-border bg-canvas text-zinc-300 cursor-not-allowed'
                    : 'border-zinc-200 bg-white text-zinc-600 hover:border-zinc-400',
              )}
            >
              <Icon className="w-5 h-5" />
              <div>
                <div className="text-xs font-semibold">{t(opt.label)}</div>
                <div className={clsx(
                  'text-[10px] mt-0.5',
                  selected ? 'text-zinc-300' : 'text-zinc-400',
                )}>
                  {disabled ? t('No agent connected') : t(opt.description)}
                </div>
              </div>
            </button>
          );
        })}
      </div>
      {/* Device pin — only with 2+ online devices and a Local target. A blank
          value = let the server pick. Single/zero-device UX is unchanged. */}
      {multiDevice && value === 'local' && (
        <div className="space-y-1 pt-1">
          <label className="text-xs font-medium text-zinc-500">{t('Device')}</label>
          <Select
            value={agentId ?? ''}
            onChange={(v) => onAgentIdChange?.(v || undefined)}
            placeholder={t('Any online device (server picks)')}
            options={[
              { value: '', label: t('Any online device (server picks)') },
              ...onlineDevices.map((d) => ({ value: d.agent_id, label: d.name })),
            ]}
          />
        </div>
      )}
      {!hasAgent && (
        <p className="text-[10px] text-zinc-400">
          {t('Connect a local agent to enable local execution.')}{' '}
          <a href="/fleet" className="underline hover:text-zinc-600">{t('Setup agent')}</a>
        </p>
      )}
    </div>
  );
};

/**
 * Inline run button with target selector dropdown.
 */
export const RunWithTargetButton: React.FC<{
  // agentId is passed ONLY when the user pins a specific device (2+ online).
  onRun: (target: ExecutionTarget, agentId?: string) => void;
  loading?: boolean;
  size?: 'sm' | 'md';
  /** Idle-state label override (default "Run"). e.g. "Run now" for delegated jobs. */
  label?: string;
}> = React.memo(({ onRun, loading, size = 'md', label }) => {
  const { t } = useTranslation();
  const runLabel = label ?? t('Run');
  const [open, setOpen] = React.useState(false);
  const ref = React.useRef<HTMLDivElement>(null);

  // One RunWithTargetButton renders per list row. Each previously spun up its
  // own 10s userAgents poll timer — N rows = N timers hammering the same
  // endpoint. Use the shared subscription instead (no per-button timer); the
  // warm Q.userAgents() cache keeps `hasAgent` current.
  const { data: agentData } = useUserAgents();

  const onlineDevices = React.useMemo(() => onlineDevicesFrom(agentData), [agentData]);
  // Online = status 'online' (WS-connected OR seen <~2min), matching the device
  // list — not `active_count` (strictly-connected-now), which momentarily reads 0
  // for a recently-seen agent and wrongly hid the local-run option.
  const hasAgent = onlineDevices.length > 0;
  const multiDevice = onlineDevices.length > 1;

  // Close dropdown on outside click
  React.useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  // The split button + full venue menu ALWAYS render (even with no agent online) so
  // the choice never disappears — the local/BYO options are just disabled with a
  // "connect an agent" hint, and the primary click falls back to Cloud.
  return (
    <div className="relative" ref={ref} onClick={(e) => e.stopPropagation()}>
      <div className="flex">
        {/* Main run button — Auto (prefer local) when an agent is connected, else Cloud. */}
        <button
          onClick={() => onRun(hasAgent ? 'auto' : 'cloud')}
          disabled={loading}
          className={clsx(
            'flex items-center gap-1.5 font-medium bg-accent-strong text-accent-on rounded-l-lg hover:bg-accent-strong/90 active:scale-[0.97] transition-all disabled:opacity-50',
            size === 'sm' ? 'px-2.5 py-1.5 text-xs' : 'px-3 py-1.5 text-sm',
          )}
        >
          {hasAgent
            ? <ArrowsRightLeftIcon className={size === 'sm' ? 'w-3.5 h-3.5' : 'w-4 h-4'} />
            : <CloudIcon className={size === 'sm' ? 'w-3.5 h-3.5' : 'w-4 h-4'} />}
          {loading ? t('Running...') : runLabel}
        </button>
        {/* Dropdown toggle */}
        <button
          onClick={() => setOpen(!open)}
          disabled={loading}
          className={clsx(
            'font-medium bg-accent-strong text-accent-on border-l border-white/20 rounded-r-lg hover:bg-accent-strong/90 transition-all disabled:opacity-50',
            size === 'sm' ? 'px-1.5 py-1.5' : 'px-2 py-1.5',
          )}
        >
          <svg className="w-3 h-3" viewBox="0 0 12 12" fill="none">
            <path d="M3 5L6 8L9 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>
      {open && (
        <div className="absolute right-0 top-full mt-1 w-60 bg-white border border-zinc-200 rounded-xl shadow-lg z-50 overflow-hidden max-h-80 overflow-y-auto">
          <div className="px-3 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
            {t('Run this on')}
          </div>
          {/* Local Agent (BYO). When none is online it stays visible but disabled,
              with a hint — so the choice never disappears. */}
          <button
            onClick={() => { if (!hasAgent) return; setOpen(false); onRun('local'); }}
            disabled={!hasAgent}
            title={hasAgent ? undefined : t('No agent connected')}
            className={clsx(
              'flex items-center gap-2 w-full px-3 py-2.5 text-sm transition-colors',
              hasAgent ? 'hover:bg-hover' : 'cursor-not-allowed',
            )}
          >
            <ComputerDesktopIcon className={clsx('w-4 h-4', hasAgent ? 'text-zinc-500' : 'text-zinc-300')} />
            <span className={clsx('flex-1 text-left', hasAgent ? 'text-zinc-700' : 'text-zinc-300')}>{t('Run on Local Agent')}</span>
            {!hasAgent && <span className="text-[10px] text-zinc-400 shrink-0">{t('none online')}</span>}
          </button>
          {/* With 2+ online devices, offer to pin the run to a specific one. */}
          {hasAgent && multiDevice && (
            <div className="border-t border-border py-1">
              <div className="px-3 pt-1 pb-0.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
                {t('Run on a device')}
              </div>
              {onlineDevices.map((d) => (
                <button
                  key={d.agent_id}
                  onClick={() => { setOpen(false); onRun('local', d.agent_id); }}
                  className="flex items-center gap-2 w-full px-3 py-2 text-sm hover:bg-hover transition-colors"
                >
                  <ComputerDesktopIcon className="w-4 h-4 text-zinc-400 shrink-0" />
                  <span className="text-zinc-700 truncate" title={d.name}>{d.name}</span>
                </button>
              ))}
            </div>
          )}
          {/* Cloud — always available. */}
          <button
            onClick={() => { setOpen(false); onRun('cloud'); }}
            className="flex items-center gap-2 w-full px-3 py-2.5 text-sm hover:bg-hover transition-colors border-t border-border"
          >
            <CloudIcon className="w-4 h-4 text-zinc-500" />
            <span className="text-zinc-700">{t('Run on Writ Cloud')}</span>
          </button>
          {/* Auto — only meaningful when a local agent exists. */}
          {hasAgent && (
            <button
              onClick={() => { setOpen(false); onRun('auto'); }}
              className="flex items-center gap-2 w-full px-3 py-2.5 text-sm hover:bg-hover transition-colors border-t border-border"
            >
              <ArrowsRightLeftIcon className="w-4 h-4 text-zinc-500" />
              <span className="text-zinc-700">{t('Auto (prefer local)')}</span>
            </button>
          )}
          {/* Connect prompt when no BYO agent is online. */}
          {!hasAgent && (
            <a
              href="/fleet"
              onClick={() => setOpen(false)}
              className="flex items-center gap-2 w-full px-3 py-2 text-[11px] text-zinc-500 hover:bg-hover transition-colors border-t border-border"
            >
              {t('Connect a local agent to run on your own device →')}
            </a>
          )}
        </div>
      )}
    </div>
  );
});
RunWithTargetButton.displayName = 'RunWithTargetButton';
