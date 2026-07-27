import React, { useState } from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import { ClockIcon, ChevronDownIcon } from '@heroicons/react/24/outline';
import { automationApi } from '../../api/endpoints';
import { Switch } from '../ui';
import { SchedulePicker } from './SchedulePicker';
import {
  scheduleFromApi,
  scheduleToPayload,
  scheduleError,
  scheduleLabel,
  type ScheduleValue,
} from '../../utils/schedule';

interface InlineScheduleControlProps {
  /** The workflow row (list or full) — reads schedule_* fields, writes via updateWorkflow. */
  workflow: any;
  /** Refetch the full workflow so the label/toggle reflect the saved schedule. */
  refresh: () => void;
  /** Refresh the list query so the row mirrors the change. */
  onChanged: () => void;
  className?: string;
}

/**
 * Inline fast-schedule control for the workflow detail pane. Replaces the old
 * "Schedule" tile that punted to the Connect tab: the recurrence editor
 * (SchedulePicker) opens right here, and the switch on the right flips the
 * schedule on/off in one click — no navigation. Writes go straight to
 * `automationApi.updateWorkflow`, the same endpoint the Connect-tab fast action
 * uses, so behaviour is identical, just closer to hand.
 */
export const InlineScheduleControl: React.FC<InlineScheduleControlProps> = ({
  workflow,
  refresh,
  onChanged,
  className,
}) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [schedule, setSchedule] = useState<ScheduleValue>(() =>
    scheduleFromApi(workflow, workflow.schedule_interval_ms || 3600000),
  );

  const enabled = !!workflow.schedule_enabled;

  // Re-seed the editor when a different workflow is selected (the pane reuses
  // this instance across selections). We intentionally key on id only so a
  // background poll can't stomp what the user is mid-editing. The re-seed runs
  // DURING render off the previously seeded id — from an effect it would land a
  // commit late, so the picker would show the previous workflow's recurrence for
  // a frame after the selection changed.
  const [seededId, setSeededId] = useState(workflow.id);
  if (seededId !== workflow.id) {
    setSeededId(workflow.id);
    setSchedule(scheduleFromApi(workflow, workflow.schedule_interval_ms || 3600000));
    setOpen(false);
  }

  const err = scheduleError(schedule);

  const apply = async (patch: Record<string, any>, okMsg: string): Promise<boolean> => {
    if (saving) return false;
    setSaving(true);
    try {
      await automationApi.updateWorkflow(workflow.id, patch as any);
      toast.success(okMsg);
      refresh();
      onChanged();
      return true;
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || t('Failed'));
      return false;
    } finally {
      setSaving(false);
    }
  };

  const enablePayload = () => ({
    schedule_enabled: true,
    schedule_interval_ms:
      schedule.kind === 'interval' ? schedule.intervalMs : workflow.schedule_interval_ms || 3600000,
    ...scheduleToPayload(schedule),
  });

  const save = async () => {
    if (err) return;
    const ok = await apply(enablePayload(), t('Schedule enabled'));
    if (ok) setOpen(false);
  };

  const quickToggle = async (next: boolean) => {
    if (saving) return;
    if (next) {
      // Fast-enable with the current recurrence; open the editor so the cadence
      // can be tuned right away. Invalid config (e.g. weekly with no day) just
      // opens the editor without writing.
      if (err) {
        setOpen(true);
        return;
      }
      const ok = await apply(enablePayload(), t('Schedule enabled'));
      if (ok) setOpen(true);
    } else {
      await apply({ schedule_enabled: false }, t('Schedule turned off'));
    }
  };

  return (
    <div
      className={clsx(
        '[grid-column:1/-1] rounded-xl border overflow-hidden transition-colors',
        enabled ? 'border-ink/20 bg-ink/[0.04]' : 'border-border bg-surface',
        className,
      )}
    >
      <div className="flex items-center gap-2.5 p-2.5">
        <button
          onClick={() => setOpen((o) => !o)}
          className="group flex min-w-0 flex-1 items-center gap-2.5 rounded-lg text-left outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ink/30"
        >
          <span
            className={clsx(
              'flex h-8 w-8 shrink-0 items-center justify-center rounded-lg',
              enabled ? 'bg-ink text-surface' : 'bg-chrome text-ink',
            )}
          >
            <ClockIcon className="h-4 w-4" />
          </span>
          <span className="min-w-0">
            <span className="block text-[12px] font-medium text-ink truncate">{t('Schedule')}</span>
            <span className="block text-[10.5px] text-tertiary truncate">
              {enabled
                ? scheduleLabel(scheduleFromApi(workflow, workflow.schedule_interval_ms || 3600000))
                : t('Run on a recurring schedule')}
            </span>
          </span>
          <ChevronDownIcon
            className={clsx('ml-1 h-3.5 w-3.5 shrink-0 text-tertiary transition-transform', open && 'rotate-180')}
          />
        </button>
        <Switch
          checked={enabled}
          onChange={quickToggle}
          disabled={saving}
          size="sm"
          title={enabled ? t('Turn scheduling off') : t('Turn scheduling on')}
          aria-label={t('Toggle schedule')}
        />
      </div>

      {open && (
        <div className="border-t border-border px-3 py-3 space-y-3">
          <SchedulePicker value={schedule} onChange={setSchedule} />
          <div className="flex items-center gap-2">
            <button
              onClick={save}
              disabled={saving || !!err}
              className={clsx(
                'flex-1 px-4 py-2 text-[13px] font-medium rounded-lg transition-colors',
                saving || !!err ? 'bg-hover text-tertiary' : 'bg-accent-strong text-accent-on hover:bg-accent-strong/90',
              )}
            >
              {saving ? t('Saving…') : enabled ? t('Update schedule') : t('Enable schedule')}
            </button>
            {enabled && (
              <button
                onClick={() => quickToggle(false)}
                disabled={saving}
                className="px-3 py-2 text-[13px] font-medium rounded-lg border border-border text-secondary hover:bg-chrome hover:text-ink transition-colors disabled:opacity-50"
              >
                {t('Turn off')}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
};
