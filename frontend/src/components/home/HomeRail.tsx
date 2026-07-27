import React from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  EyeIcon,
  CursorArrowRaysIcon,
  CodeBracketIcon,
  ClockIcon,
  BoltIcon,
  ChevronRightIcon,
} from '@heroicons/react/24/outline';
import { useUserAgents } from '../../hooks/useUserAgents';

/**
 * Right-rail cards for the self-hosted Command center: the primary "Create" card
 * (Writ's product model grouped into Watch / Automate / React) and a compact local
 * "System" status card below it. Create is a page anchor; System is deliberately
 * smaller. No AI assistant, no cloud, no earnings — self-hosted coordinator.
 */

type CreateItem = {
  icon: React.ElementType;
  label: string;
  /** Navigate to a creation flow. */
  to: string;
};

export const CreateRail: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const groups: { label: string; items: CreateItem[] }[] = [
    {
      label: t('Create an API'),
      items: [
        { icon: CodeBracketIcon, label: t('Convert a website'), to: '/workflows/new' },
        { icon: CursorArrowRaysIcon, label: t('Build an authenticated API'), to: '/workflows/new' },
        { icon: ClockIcon, label: t('Build a scheduled data API'), to: '/workflows/new?intent=extract' },
      ],
    },
    {
      label: t('Operate'),
      items: [
        { icon: EyeIcon, label: t('Monitor a website'), to: '/checks/new' },
      ],
    },
    {
      label: t('React'),
      items: [
        { icon: BoltIcon, label: t('Build an automation'), to: '/automations/new' },
      ],
    },
  ];

  return (
    // Heading sits OUTSIDE the card, matching NeedsAttention / RecentActivity in
    // the main column — both of those open with a bare h2 + one-line subtitle and
    // put their card underneath. With the heading inside the card it started
    // 15px lower (border + p-2 + pt-1.5) than the heading beside it, so the two
    // columns never lined up. Same structure here means they align by
    // construction, in every state of the left column, with no magic offset.
    <div>
      <div className="mb-2.5">
        <h2 className="text-base font-semibold text-ink tracking-tight">{t('Create and deploy')}</h2>
        <p className="text-[12px] text-secondary mt-0.5">{t('Start a new API, monitor, or automation.')}</p>
      </div>
      <div className="rounded-2xl border border-ink/20 bg-surface p-2 shadow">
        {groups.map((g) => (
          <div key={g.label} className="pb-1">
            <div className="px-2 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wider text-secondary">{g.label}</div>
            {g.items.map((item) => {
              const Icon = item.icon;
              return (
                <button
                  key={item.label}
                  onClick={() => navigate(item.to)}
                  className="group w-full flex items-center gap-2.5 px-2 py-1.5 rounded-md text-left hover:bg-chrome transition-colors"
                >
                  <Icon className="w-4 h-4 text-secondary group-hover:text-ink shrink-0 transition-colors" />
                  <span className="text-[13px] text-secondary group-hover:text-ink flex-1 truncate transition-colors">{item.label}</span>
                  <ChevronRightIcon className="w-3.5 h-3.5 text-tertiary/40 group-hover:text-ink group-hover:translate-x-0.5 transition-all shrink-0" />
                </button>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
};

/**
 * Compact local "System" status card — the self-hosted counterpart to the cloud's
 * status card. Reads the shared userAgents cache (live agent connectivity) plus a
 * static "self-hosted coordinator" row. No balance, no cloud, no earnings.
 */
export const FleetStatus: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { data: agentsData } = useUserAgents({ pollInterval: 15000 });

  const agents = agentsData?.agents ?? [];
  const total = agents.length;
  const online = agents.filter((a: any) => a.status === 'online').length;

  const rows: { label: string; value: React.ReactNode; dot?: 'ok' | 'idle'; onClick?: () => void }[] = [
    { label: t('Coordinator'), value: t('Connected'), dot: 'ok' },
    {
      label: t('Agents'),
      value: total === 0 ? t('None') : t('{{online}} / {{total}} online', { online, total }),
      dot: total > 0 && online > 0 ? 'ok' : 'idle',
      onClick: () => navigate('/fleet'),
    },
  ];

  const dotClass = (d?: 'ok' | 'idle') =>
    d === 'ok' ? 'bg-emerald-500' : 'bg-zinc-300';

  return (
    <div className="rounded-2xl border border-ink/20 bg-surface px-3.5 py-3 shadow-sm">
      <h2 className="text-[12px] font-semibold text-secondary uppercase tracking-wide mb-2">{t('Execution fleet')}</h2>
      <div className="space-y-0.5">
        {rows.map((r) => {
          const inner = (
            <>
              <span className="inline-flex items-center gap-2 text-[12px] text-tertiary">
                {r.dot && <span className={`w-1.5 h-1.5 rounded-full ${dotClass(r.dot)}`} />}
                {r.label}
              </span>
              <span className="text-[12px] font-medium text-ink tabular-nums">{r.value}</span>
            </>
          );
          return r.onClick ? (
            <button key={r.label} onClick={r.onClick} className="w-full flex items-center justify-between py-0.5 hover:opacity-70 transition-opacity">
              {inner}
            </button>
          ) : (
            <div key={r.label} className="flex items-center justify-between py-0.5">{inner}</div>
          );
        })}
      </div>
    </div>
  );
};
