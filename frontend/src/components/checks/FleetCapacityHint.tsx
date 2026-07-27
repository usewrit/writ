import React from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useFleetCapacity, formatInterval } from '../../hooks/useFleetCapacity';

/**
 * Inline advisory under a monitor's check-interval picker. Explains the hard
 * relationship between the number of connected agents and the fastest supported
 * interval — a single agent can only check a target once per 60s (anti-detection),
 * so faster intervals need a fleet of staggered agents (≈ 60s ÷ agents). Warns
 * (amber) when the chosen interval is faster than the fleet can actually deliver,
 * so the user isn't silently running slower than they think.
 */
export const FleetCapacityHint: React.FC<{ checkPeriodMs: number | null }> = ({ checkPeriodMs }) => {
  const { t } = useTranslation();
  const cap = useFleetCapacity();
  if (!cap) return null;

  const period = checkPeriodMs ?? 60000;
  const min = formatInterval(cap.min_interval_ms);

  if (cap.agents_online === 0) {
    return (
      <p className="text-[11px] text-amber-600 mt-1.5 leading-relaxed">
        {t("No agents are connected yet — checks won't run until you")}{' '}
        <Link to="/fleet" className="underline hover:text-amber-700">{t('connect an agent')}</Link>
        {t('. Each agent lets monitors check up to once every 60s; more agents unlock faster intervals.')}
      </p>
    );
  }

  if (period < cap.min_interval_ms) {
    return (
      <p className="text-[11px] text-amber-600 mt-1.5 leading-relaxed">
        {t(
          'Your {{count}} connected agent(s) support checks about every {{min}}. This monitor will effectively run every {{min}} until you add more agents — ',
          { count: cap.agents_online, min },
        )}
        <Link to="/fleet" className="underline hover:text-amber-700">{t('add agents')}</Link>
        {t(' to check faster.')}
      </p>
    );
  }

  return (
    <p className="text-[11px] text-tertiary mt-1.5 leading-relaxed">
      {t('{{count}} agent(s) connected — supports checks as fast as every {{min}}.', {
        count: cap.agents_online,
        min,
      })}
    </p>
  );
};

export default FleetCapacityHint;
