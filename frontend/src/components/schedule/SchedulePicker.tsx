import React from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { ExclamationCircleIcon } from '@heroicons/react/24/outline';
import { uiLocale } from '../../utils/format';
import {
  ScheduleValue,
  ScheduleKind,
  WEEKDAYS,
  scheduleError,
  scheduleLabel,
  previewNextRun,
} from '../../utils/schedule';

// Default interval presets for the "Interval" tab. A surface can override these
// (e.g. monitors use a faster cadence vocabulary) via the `intervalPresets` prop.
const DEFAULT_INTERVAL_PRESETS: Array<{ label: string; ms: number }> = [
  { label: '5 min', ms: 300000 },
  { label: '15 min', ms: 900000 },
  { label: '1 hour', ms: 3600000 },
  { label: '6 hours', ms: 21600000 },
  { label: '24 hours', ms: 86400000 },
];

export interface SchedulePickerProps {
  value: ScheduleValue;
  onChange: (next: ScheduleValue) => void;
  /** Which recurrence kinds to offer. Defaults to all three. */
  kinds?: ScheduleKind[];
  /** Override the interval-tab presets (label + ms). */
  intervalPresets?: Array<{ label: string; ms: number }>;
  /** Extra content rendered under the interval preset row (e.g. a fleet capacity hint). */
  intervalExtra?: React.ReactNode;
  className?: string;
}

const KIND_LABEL_KEY: Record<ScheduleKind, string> = {
  interval: 'Interval',
  daily: 'Daily',
  weekly: 'Weekly',
};

/**
 * Controlled precise-schedule editor. Emits a full ScheduleValue on every change.
 * Enforces validity via scheduleError(); the caller disables submit while
 * scheduleError(value) is non-null and can surface the same hint.
 */
export const SchedulePicker: React.FC<SchedulePickerProps> = ({
  value,
  onChange,
  kinds = ['interval', 'daily', 'weekly'],
  intervalPresets = DEFAULT_INTERVAL_PRESETS,
  intervalExtra,
  className,
}) => {
  const { t } = useTranslation();

  const setKind = (kind: ScheduleKind) => onChange({ ...value, kind });
  const setInterval = (ms: number) => onChange({ ...value, intervalMs: ms });
  const setTime = (time: string) => onChange({ ...value, time });
  const toggleDay = (iso: number) => {
    const has = value.days.includes(iso);
    const days = has ? value.days.filter((d) => d !== iso) : [...value.days, iso];
    onChange({ ...value, days: days.sort((a, b) => a - b) });
  };

  const err = scheduleError(value);
  const preview = previewNextRun(value);

  return (
    <div className={clsx('space-y-3', className)}>
      {/* Segmented control: Interval · Daily · Weekly */}
      {kinds.length > 1 && (
        <div className="flex gap-0.5 bg-hover/50 rounded-full p-0.5">
          {kinds.map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => setKind(k)}
              className={clsx(
                'flex-1 px-3 py-2 rounded-full text-xs font-medium transition-all text-center',
                value.kind === k ? 'bg-ink text-white shadow-sm' : 'text-secondary hover:text-ink',
              )}
            >
              {t(KIND_LABEL_KEY[k])}
            </button>
          ))}
        </div>
      )}

      {/* Interval presets */}
      {value.kind === 'interval' && (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-1.5">
            {intervalPresets.map((p) => {
              const active = value.intervalMs === p.ms;
              return (
                <button
                  key={p.ms}
                  type="button"
                  onClick={() => setInterval(p.ms)}
                  className={clsx(
                    'px-3 py-1.5 text-xs font-medium rounded-lg border transition-colors',
                    active ? 'border-ink bg-hover text-ink' : 'border-border text-secondary hover:border-ink/20',
                  )}
                >
                  {t(p.label)}
                </button>
              );
            })}
          </div>
          {intervalExtra}
        </div>
      )}

      {/* Daily / Weekly */}
      {(value.kind === 'daily' || value.kind === 'weekly') && (
        <div className="space-y-3">
          {value.kind === 'weekly' && (
            <div>
              <p className="text-[11px] text-secondary mb-1.5">{t('On these days')}</p>
              <div className="flex flex-wrap gap-1.5">
                {WEEKDAYS.map((d) => {
                  const active = value.days.includes(d.iso);
                  return (
                    <button
                      key={d.iso}
                      type="button"
                      onClick={() => toggleDay(d.iso)}
                      className={clsx(
                        'w-11 px-0 py-1.5 text-xs font-medium rounded-lg border transition-colors text-center',
                        active ? 'border-ink bg-ink text-white' : 'border-border text-secondary hover:border-ink/20',
                      )}
                    >
                      {t(d.label)}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          <div>
            <p className="text-[11px] text-secondary mb-1.5">{t('At time')}</p>
            <input
              type="time"
              value={value.time}
              onChange={(e) => setTime(e.target.value)}
              className={clsx(
                'px-3 py-2 text-sm rounded-lg border bg-surface text-ink transition-colors',
                'focus:outline-none focus:border-ink/40',
                err && value.kind !== 'weekly' ? 'border-amber-500/60' : 'border-border',
              )}
            />
            <p className="text-[11px] text-tertiary mt-1.5">
              {t('Times in {{tz}}', { tz: value.tz })}
            </p>
          </div>
        </div>
      )}

      {/* Live preview + inline validation */}
      {err ? (
        <div className="flex items-center gap-1.5 text-[11px] text-amber-600">
          <ExclamationCircleIcon className="w-3.5 h-3.5 shrink-0" />
          <span>{t(err)}</span>
        </div>
      ) : (
        <div className="flex items-center gap-1.5 text-[11px] text-tertiary">
          <span className="font-medium text-secondary">{scheduleLabel(value)}</span>
          {preview && (
            <>
              <span className="text-border">·</span>
              <span>
                {t('Next: {{when}}', {
                  when: preview.toLocaleString(uiLocale(), {
                    weekday: 'short',
                    hour: '2-digit',
                    minute: '2-digit',
                    month: 'short',
                    day: 'numeric',
                  }),
                })}
              </span>
            </>
          )}
        </div>
      )}
    </div>
  );
};
