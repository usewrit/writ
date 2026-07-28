import React from 'react';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import { XMarkIcon, CheckIcon, ArrowPathIcon } from '@heroicons/react/24/outline';
import { WizardProvider, useWizard, type WizardSelector } from '../wizard/WizardContext';
import { ContentMonitorPanel } from '../wizard/panels/ContentMonitorPanel';
import { SchedulePicker } from '../schedule/SchedulePicker';
import { scheduleFromApi, scheduleToPayload, scheduleError, type ScheduleValue } from '../../utils/schedule';
import { targetsApi, selectorsApi } from '../../api/endpoints';

// Monitor check cadence presets for the interval tab (matches the wizard vocabulary).
const CHECK_INTERVAL_PRESETS = [
  { label: '10s', ms: 10000 },
  { label: '30s', ms: 30000 },
  { label: '1m', ms: 60000 },
  { label: '5m', ms: 300000 },
  { label: '15m', ms: 900000 },
  { label: '1h', ms: 3600000 },
  { label: '24h', ms: 86400000 },
];

/**
 * Full-screen "Edit target" workflow. Reuses the creation wizard's
 * ContentMonitorPanel (live RecorderPreview + selector/zone picking), seeded
 * from the existing check, and on Save diffs the selectors against what was
 * loaded → create new / update changed / delete removed, plus persists
 * target-level settings (url, interval, JS, region).
 *
 * Existing selectors are tagged with an id of `db_<id>` so the diff can tell a
 * persisted selector (update/keep) from one freshly picked in this session
 * (`sel_...`, create).
 */

const DB_PREFIX = 'db_';

interface EditTargetModalProps {
  targetId: number;
  isOpen: boolean;
  onClose: () => void;
  onSaved?: () => void;
}

function toWizardSelector(s: any): WizardSelector {
  const region = s.visual_region && typeof s.visual_region === 'object'
    ? {
        x: Number(s.visual_region.x) || 0,
        y: Number(s.visual_region.y) || 0,
        width: Number(s.visual_region.width) || 0,
        height: Number(s.visual_region.height) || 0,
        scroll_x: Number(s.visual_region.scroll_x) || 0,
        scroll_y: Number(s.visual_region.scroll_y) || 0,
        // Carry the capture viewport through the edit round-trip. Rebuilding the
        // region field-by-field without it would silently strip the provenance the
        // monitor check needs to clip the right pixels, re-breaking a zone the user
        // only opened to rename. Left undefined for pre-viewport rows so the agent
        // keeps applying its 1280x800 default instead of us inventing one.
        ...(s.visual_region.viewport &&
            Number(s.visual_region.viewport.width) > 0 &&
            Number(s.visual_region.viewport.height) > 0
          ? {
              viewport: {
                width: Number(s.visual_region.viewport.width),
                height: Number(s.visual_region.viewport.height),
              },
            }
          : {}),
      }
    : undefined;
  const checkType = (['text', 'html', 'visual'].includes(s.content_type) ? s.content_type : 'text') as WizardSelector['checkType'];
  return {
    id: `${DB_PREFIX}${s.id}`,
    name: s.name || '',
    selector: s.selector || '',
    description: s.description || '',
    checkType,
    ignoreRegex: s.ignore_regex || '',
    preview: '',
    enabled: s.enabled !== false,
    region: checkType === 'visual' ? region : undefined,
  };
}

/** Backend payload for create/update from a wizard selector. */
function toSelectorPayload(s: WizardSelector) {
  const payload: any = {
    name: s.name || 'Selector',
    selector: s.selector,
    content_type: s.checkType,
    ignore_regex: s.ignoreRegex || null,
    enabled: s.enabled !== false,
  };
  if (s.checkType === 'visual' && s.region) {
    payload.visual_region = {
      x: Math.round(s.region.x),
      y: Math.round(s.region.y),
      width: Math.round(s.region.width),
      height: Math.round(s.region.height),
      scroll_x: Math.round(s.region.scroll_x ?? 0),
      scroll_y: Math.round(s.region.scroll_y ?? 0),
    };
    if (s.region.viewport && s.region.viewport.width > 0 && s.region.viewport.height > 0) {
      payload.visual_region.viewport = {
        width: Math.round(s.region.viewport.width),
        height: Math.round(s.region.viewport.height),
      };
    }
  }
  return payload;
}

/** Inner editor — lives inside WizardProvider so it can drive ContentMonitorPanel. */
const EditTargetInner: React.FC<{
  targetId: number;
  onClose: () => void;
  onSaved?: () => void;
}> = ({ targetId, onClose, onSaved }) => {
  const { t } = useTranslation();
  const { state, setMode, goToStep, updateConfig } = useWizard();
  const [loading, setLoading] = React.useState(true);
  const [saving, setSaving] = React.useState(false);
  // Precise recurrence (interval | daily | weekly) for the check cadence. The
  // ContentMonitorPanel doesn't surface cadence, so the edit modal owns it here.
  const [schedule, setSchedule] = React.useState<ScheduleValue>(() => scheduleFromApi({}, 60000));
  // Selectors as loaded from the server (db ids) — the baseline for the save diff.
  const originalRef = React.useRef<WizardSelector[]>([]);
  const seededRef = React.useRef(false);

  // Seed the wizard once from the existing target + selectors.
  React.useEffect(() => {
    if (seededRef.current) return;
    seededRef.current = true;
    let cancelled = false;
    (async () => {
      try {
        const [target, selectors] = await Promise.all([
          targetsApi.getById(String(targetId)),
          selectorsApi.listForTarget(targetId),
        ]);
        if (cancelled) return;
        const mapped = (selectors || []).map(toWizardSelector);
        originalRef.current = mapped;
        setMode('content_monitor');
        goToStep('configure');
        const checkMs = (target as any).checkPeriodMs ?? (target as any).check_period_ms ?? 60000;
        updateConfig({
          url: (target as any).url || '',
          selectors: mapped,
          checkPeriodMs: checkMs,
          requiresPlaywright: Boolean((target as any).requiresPlaywright ?? (target as any).requires_playwright),
          preferredRegion: (target as any).preferredRegion ?? (target as any).preferred_region ?? null,
        });
        setSchedule(scheduleFromApi(target, checkMs));
      } catch (e: any) {
        toast.error(e?.response?.data?.detail || t('Failed to load monitor'));
        onClose();
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleSave = async () => {
    if (saving) return;
    setSaving(true);
    try {
      const current = state.config.selectors;
      const original = originalRef.current;
      const currentDbIds = new Set(
        current.filter(s => s.id.startsWith(DB_PREFIX)).map(s => Number(s.id.slice(DB_PREFIX.length)))
      );

      // 1) Persist target-level settings (incl. precise recurrence).
      await targetsApi.update(String(targetId), {
        url: state.config.url,
        check_period_ms: schedule.intervalMs,
        ...scheduleToPayload(schedule),
        requires_playwright: state.config.requiresPlaywright,
        preferred_region: state.config.preferredRegion,
      } as any);

      // 2) Delete selectors that were removed.
      const removed = original.filter(o => {
        const dbId = Number(o.id.slice(DB_PREFIX.length));
        return !currentDbIds.has(dbId);
      });
      for (const o of removed) {
        await selectorsApi.delete(targetId, Number(o.id.slice(DB_PREFIX.length)));
      }

      // 3) Create new + update existing.
      for (const s of current) {
        if (s.id.startsWith(DB_PREFIX)) {
          await selectorsApi.update(targetId, Number(s.id.slice(DB_PREFIX.length)), toSelectorPayload(s));
        } else {
          await selectorsApi.create(targetId, toSelectorPayload(s));
        }
      }

      toast.success(t('Monitor updated'));
      onSaved?.();
      onClose();
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || t('Failed to save changes'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-canvas flex flex-col">
      {/* Header */}
      <div className="flex items-center gap-3 h-14 px-4 sm:px-6 border-b border-border shrink-0 bg-surface">
        <button
          onClick={onClose}
          disabled={saving}
          className="p-1.5 text-tertiary hover:text-ink rounded-lg hover:bg-hover transition-colors disabled:opacity-50"
          aria-label={t('Close')}
        >
          <XMarkIcon className="w-5 h-5" />
        </button>
        <span className="text-sm font-semibold text-ink">{t('Edit monitor')}</span>
        <span className="hidden sm:inline text-[11px] text-tertiary">{t('Re-pick elements or re-draw visual zones on the live page')}</span>
        <div className="flex-1" />
        <button
          onClick={onClose}
          disabled={saving}
          className="px-3 py-1.5 text-[12px] font-medium text-secondary hover:text-ink rounded-lg hover:bg-hover transition-colors disabled:opacity-50"
        >
          {t('Cancel')}
        </button>
        <button
          onClick={handleSave}
          disabled={saving || loading || !!scheduleError(schedule)}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-accent-strong text-accent-on text-[12px] font-medium rounded-lg hover:bg-accent-strong/90 transition-colors disabled:opacity-50"
        >
          {saving ? <ArrowPathIcon className="w-3.5 h-3.5 animate-spin" /> : <CheckIcon className="w-3.5 h-3.5" />}
          {saving ? t('Saving...') : t('Save changes')}
        </button>
      </div>

      {/* Check cadence strip — interval / daily / weekly (ContentMonitorPanel omits cadence). */}
      {!loading && (
        <div className="px-4 sm:px-6 py-3 border-b border-border shrink-0 bg-surface">
          <p className="text-[11px] font-medium text-secondary mb-1.5">{t('How often to check')}</p>
          <SchedulePicker value={schedule} onChange={setSchedule} intervalPresets={CHECK_INTERVAL_PRESETS} />
        </div>
      )}

      {/* Editor */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center h-full text-sm text-secondary">
            {t('Loading monitor...')}
          </div>
        ) : (
          <ContentMonitorPanel />
        )}
      </div>
    </div>
  );
};

export const EditTargetModal: React.FC<EditTargetModalProps> = ({ targetId, isOpen, onClose, onSaved }) => {
  if (!isOpen) return null;
  return (
    <WizardProvider>
      <EditTargetInner targetId={targetId} onClose={onClose} onSaved={onSaved} />
    </WizardProvider>
  );
};
