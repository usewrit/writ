import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import {
  EyeIcon,
  ArrowsRightLeftIcon,
  CpuChipIcon,
  Cog6ToothIcon,
  PlusIcon,
  ArrowLeftIcon,
  PlayIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  ClockIcon,
  SignalIcon,
  SignalSlashIcon,
  ArrowPathIcon,
  TableCellsIcon,
  DocumentArrowUpIcon,
} from '@heroicons/react/24/outline';
import { useFlowBuilder } from '../FlowBuilderContext';
import { isBlockAvailable } from '../blockCatalog';
import { APP_PLATFORM } from '../../../api/aiAssist';
import i18n from '../../../i18n';

type PickerView = 'home' | 'new' | 'existing' | 'pick_monitor_event' | 'pick_monitor' | 'pick_event';

// Which monitor signal an automation reacts to. "Content changed" is the classic
// change_detected (needs a selector, can create a new monitor); the health events
// bind to an EXISTING monitor and need no browser.
// `needsMonitor` events bind an EXISTING monitor, so they're unreachable until the
// account has one — the picker disables them rather than walking the user into a
// dead end. change_detected can create its monitor on the way through.
const MONITOR_EVENTS: { key: string; label: string; description: string; icon: any; needsMonitor?: boolean }[] = [
  { key: 'change_detected', label: 'Content changed', description: 'When the watched content changes', icon: ArrowsRightLeftIcon },
  { key: 'monitor_down', label: 'Went down', description: 'When the page becomes unreachable', icon: SignalSlashIcon, needsMonitor: true },
  { key: 'monitor_stale', label: 'Went stale', description: 'When it stops updating on schedule', icon: ClockIcon, needsMonitor: true },
  { key: 'monitor_recovered', label: 'Recovered', description: 'When it’s healthy again after down or stale', icon: ArrowPathIcon, needsMonitor: true },
];

type ExistingCategory = 'monitors' | 'workflows' | 'ai_sessions';

const CATEGORY_META: Record<ExistingCategory, { label: string; icon: any }> = {
  monitors: { label: 'Content Monitors', icon: EyeIcon },
  workflows: { label: 'Workflows', icon: Cog6ToothIcon },
  ai_sessions: { label: 'AI Sessions', icon: CpuChipIcon },
};

// `requires` names the thing the source must bind to. With none of them on the
// account the source can only ever produce a trigger that never fires, so it's shown
// disabled with the reason instead of leading to an empty picker.
const SOURCE_TYPES: { id: string; blockType: string; label: string; description: string; icon: any; requires?: 'workflow' | 'ai_session' }[] = [
  { id: 'content_monitor', blockType: 'change_detected', label: 'Content Monitor', description: 'Watch a page — changes, downtime, or staleness', icon: EyeIcon },
  { id: 'webhook', blockType: 'webhook_received', label: 'Incoming Webhook', description: 'Receive external HTTP calls', icon: ArrowsRightLeftIcon },
  { id: 'scheduled', blockType: 'scheduled', label: 'Schedule', description: 'Run on a time interval', icon: ClockIcon },
  { id: 'streaming_session_started', blockType: 'streaming_session_started', label: 'Streaming session', description: 'When a live session starts or ends', icon: SignalIcon, requires: 'workflow' },
  { id: 'data_extracted', blockType: 'data_extracted', label: 'Data Extracted', description: 'When a workflow produces new rows', icon: TableCellsIcon, requires: 'workflow' },
  { id: 'file_uploaded', blockType: 'file_uploaded', label: 'File Received', description: 'When a file is uploaded or arrives', icon: DocumentArrowUpIcon },
  { id: 'workflow_completed', blockType: 'workflow_completed', label: 'Workflow Completed', description: 'When a workflow finishes', icon: CheckCircleIcon, requires: 'workflow' },
  { id: 'ai_session_completed', blockType: 'ai_session_completed', label: 'AI Session Completed', description: 'When an AI session finishes', icon: CheckCircleIcon, requires: 'ai_session' },
];

// Fresh id for the source block a pick creates. The clock read lives at MODULE
// scope because a `Date.now()` written inside a handler that is itself declared
// in the render body still counts as a render-time impure call
// (`react-hooks/purity`) — the analyser cannot tell the handler only ever runs
// on a click. Minting stays exactly where it was: at pick time, never at render.
const newBlockId = (): string => `block_${Date.now()}`;

interface SourceBlockPickerProps {
  onSelected?: () => void;
}

export const SourceBlockPicker: React.FC<SourceBlockPickerProps> = ({ onSelected }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { state, dispatch } = useFlowBuilder();
  const { targets, workflows, sessions } = state;
  const [view, setView] = useState<PickerView>('home');
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [pendingItem, setPendingItem] = useState<{ type: 'workflow' | 'session'; item: any } | null>(null);
  // Which monitor event the source will listen to (chosen in 'pick_monitor_event').
  const [monitorEvent, setMonitorEvent] = useState<string>('change_detected');
  const isHealthEvent = monitorEvent !== 'change_detected';

  const contentMonitors = (targets || []).filter((t: any) => t.check_type === 'content');
  const hasExisting = contentMonitors.length > 0 || (workflows || []).length > 0 || (sessions || []).length > 0;

  // Why a source type can't be picked yet (null = it can). Drives the disabled state
  // in the 'new' view so nothing in the list dead-ends on an empty binder.
  const blockedReason = (source: typeof SOURCE_TYPES[number]): string | null => {
    if (source.requires === 'workflow' && (workflows || []).length === 0) return t('No workflows yet — create one first');
    if (source.requires === 'ai_session' && (sessions || []).length === 0) return t('No AI sessions yet — create one first');
    return null;
  };

  // --- Handlers ---

  const handleCreateNewMonitor = () => {
    // Don't embed the recorder inline — hand off to the real monitor-creation wizard.
    // On completion it redirects to /automations/new?source=check&checkId=<id>, which
    // seeds this automation's source bound to the new monitor (compact, no browser).
    const wizardRoute = APP_PLATFORM === 'desktop' ? '/monitors/new' : '/checks/new';
    navigate(`${wizardRoute}?intent=automation_source`);
  };

  const handleSelectNew = (source: typeof SOURCE_TYPES[number]) => {
    // Content Monitor: first choose WHICH signal (content change vs health), then
    // the specific monitor. Health events bind an existing monitor with no browser.
    if (source.id === 'content_monitor') {
      setMonitorEvent('change_detected');
      setView('pick_monitor_event');
      return;
    }

    const config: any = {};
    // Seed a sensible default cadence for a schedule source so the block is valid immediately
    // (the daemon floors this to the 60s anti-detection cadence regardless).
    if (source.blockType === 'scheduled') config.interval_ms = 60 * 60 * 1000; // hourly

    dispatch({
      type: 'SET_BLOCKS',
      blocks: [{ id: newBlockId(), type: 'event', blockType: source.blockType, config }],
    });
    onSelected?.();
  };

  // eventOverride lets the "Use Existing" home path force change_detected without
  // depending on the monitorEvent picker state.
  const handleSelectExistingMonitor = (target: any, eventOverride?: string) => {
    const blockType = eventOverride || monitorEvent;
    dispatch({
      type: 'SET_BLOCKS',
      blocks: [{ id: newBlockId(), type: 'event', blockType, config: { target_id: target.id, _existingTarget: true } }],
    });
    dispatch({ type: 'SET_META', name: t('Automation for {{url}}', { url: target.url }) });
    onSelected?.();
  };

  const handleSelectMonitorEvent = (key: string) => {
    setMonitorEvent(key);
    if (key === 'change_detected') {
      // Classic content change — can create a new monitor (browser) or pick one.
      if (contentMonitors.length > 0) setView('pick_monitor');
      else handleCreateNewMonitor();
    } else {
      // Health events bind an EXISTING monitor — no browser, no selector.
      setView('pick_monitor');
    }
  };

  const handleSelectExistingWorkflow = (workflow: any) => {
    setPendingItem({ type: 'workflow', item: workflow });
    setView('pick_event');
  };

  const handleSelectExistingSession = (session: any) => {
    setPendingItem({ type: 'session', item: session });
    setView('pick_event');
  };

  const handleSelectEvent = (eventKey: string) => {
    if (!pendingItem) return;
    const isWorkflow = pendingItem.type === 'workflow';

    let blockType: string;
    const config: any = isWorkflow
      ? { workflow_id: pendingItem.item.id }
      : { ai_session_id: pendingItem.item.id };

    if (eventKey === 'started') {
      blockType = isWorkflow ? 'workflow_started' : 'ai_session_started';
    } else if (eventKey === 'error') {
      blockType = isWorkflow ? 'workflow_completed' : 'ai_session_completed';
      config.status_condition = 'error';
    } else {
      blockType = isWorkflow ? 'workflow_completed' : 'ai_session_completed';
    }

    dispatch({
      type: 'SET_BLOCKS',
      blocks: [{ id: newBlockId(), type: 'event', blockType, config }],
    });
    dispatch({ type: 'SET_META', name: t('Automation for {{name}}', { name: pendingItem.item.name }) });
    setPendingItem(null);
    onSelected?.();
  };

  // --- HOME VIEW ---
  if (view === 'home') {
    return (
      <div className="flex flex-col items-center justify-center min-h-[50vh] px-4">
        <div className="text-center mb-8">
          <h2 className="text-xl font-semibold text-ink">{t('How does this automation start?')}</h2>
          <p className="text-sm text-secondary mt-2">{t('Start fresh or build on something you already have.')}</p>
        </div>

        <div className="flex gap-4 max-w-xl w-full">
          <PickerCard
            id="new"
            hoveredId={hoveredId}
            onHover={setHoveredId}
            onClick={() => setView('new')}
            icon={PlusIcon}
            title={t('Create New')}
            description={t('Start from scratch with a new source')}
          />
          <PickerCard
            id="existing"
            hoveredId={hoveredId}
            onHover={setHoveredId}
            onClick={() => hasExisting ? setView('existing') : undefined}
            disabled={!hasExisting}
            icon={EyeIcon}
            title={t('Use Existing')}
            description={hasExisting ? t('Pick from your monitors, workflows, or AI sessions') : t('No items yet — create one first')}
          >
            {hasExisting && (
              <div className="flex gap-1.5 mt-2">
                {contentMonitors.length > 0 && <CountBadge count={contentMonitors.length} label={t('monitors')} />}
                {(workflows || []).length > 0 && <CountBadge count={workflows.length} label={t('workflows')} />}
                {(sessions || []).length > 0 && <CountBadge count={sessions.length} label={t('AI sessions')} />}
              </div>
            )}
          </PickerCard>
        </div>
      </div>
    );
  }

  // --- NEW SOURCE TYPE PICKER ---
  if (view === 'new') {
    return (
      <div className="flex flex-col items-center justify-center min-h-[50vh] px-4">
        <ViewHeader onBack={() => setView('home')} title={t('Choose a source type')} subtitle={t('What triggers this automation?')} />
        <div className="grid grid-cols-2 gap-4 max-w-xl w-full">
          {SOURCE_TYPES.filter((s) => isBlockAvailable(s.blockType, APP_PLATFORM)).map((source) => {
            const Icon = source.icon;
            const blocked = blockedReason(source);
            const isHovered = hoveredId === source.id && !blocked;
            const hasItems = source.id === 'content_monitor' && contentMonitors.length > 0;
            return (
              <button
                key={source.id}
                type="button"
                data-tour={`flow-source-${source.id}`}
                onClick={() => handleSelectNew(source)}
                disabled={!!blocked}
                title={blocked || undefined}
                onMouseEnter={() => setHoveredId(source.id)}
                onMouseLeave={() => setHoveredId(null)}
                className={clsx(
                  'relative p-6 rounded-xl border-2 text-left transition-all duration-200 bg-surface',
                  blocked
                    ? 'opacity-50 cursor-not-allowed border-border'
                    : isHovered
                      ? 'border-ink shadow-sm scale-[1.02] hover:shadow-md'
                      : 'border-border hover:border-secondary hover:shadow-md',
                )}
              >
                <div className={clsx(
                  'w-10 h-10 rounded-lg flex items-center justify-center mb-3 transition-colors',
                  isHovered ? 'bg-ink text-white' : 'bg-hover text-ink',
                )}>
                  <Icon className="h-5 w-5" />
                </div>
                <div className="text-sm font-medium text-ink">{t(source.label)}</div>
                <div className="text-xs text-secondary mt-1">{blocked || t(source.description)}</div>
                {hasItems && (
                  <div className="text-[10px] text-tertiary mt-2">{t('{{n}} existing — click to pick or create new', { n: contentMonitors.length })}</div>
                )}
              </button>
            );
          })}
        </div>
      </div>
    );
  }

  // --- PICK MONITOR EVENT (which signal: content change vs health) ---
  if (view === 'pick_monitor_event') {
    return (
      <div className="flex flex-col items-center justify-center min-h-[50vh] px-4">
        <ViewHeader onBack={() => setView('new')} title={t('Content Monitor')} subtitle={t('What should this react to?')} />
        <div className="grid grid-cols-2 gap-3 max-w-xl w-full">
          {MONITOR_EVENTS.map((evt) => {
            const Icon = evt.icon;
            // A health event watches a monitor that already exists — with none on the
            // account there is nothing to bind, so it stays disabled and says why.
            const blocked = evt.needsMonitor && contentMonitors.length === 0;
            const isHovered = hoveredId === evt.key && !blocked;
            return (
              <button
                key={evt.key}
                type="button"
                data-tour={`flow-monitor-event-${evt.key}`}
                onClick={() => handleSelectMonitorEvent(evt.key)}
                disabled={!!blocked}
                title={blocked ? t('No monitors yet') : undefined}
                onMouseEnter={() => setHoveredId(evt.key)}
                onMouseLeave={() => setHoveredId(null)}
                className={clsx(
                  'relative p-5 rounded-xl border-2 text-left transition-all duration-200 bg-surface',
                  blocked
                    ? 'opacity-50 cursor-not-allowed border-border'
                    : isHovered
                      ? 'border-ink shadow-sm scale-[1.02] hover:shadow-md'
                      : 'border-border hover:border-secondary hover:shadow-md',
                )}
              >
                <div className={clsx(
                  'w-9 h-9 rounded-xl flex items-center justify-center mb-3 transition-colors',
                  isHovered ? 'bg-ink text-white' : 'bg-hover text-ink',
                )}>
                  <Icon className="h-4 w-4" />
                </div>
                <div className="text-sm font-medium text-ink">{t(evt.label)}</div>
                <div className="text-xs text-secondary mt-1">
                  {blocked ? t('No monitors yet') : t(evt.description)}
                </div>
              </button>
            );
          })}
        </div>
      </div>
    );
  }

  // --- PICK MONITOR (event-aware). change_detected can create a new monitor;
  //     health events bind an EXISTING monitor only (no browser). ---
  if (view === 'pick_monitor') {
    const evtMeta = MONITOR_EVENTS.find((e) => e.key === monitorEvent);
    return (
      <div className="flex flex-col items-center justify-center min-h-[50vh] px-4">
        <ViewHeader
          onBack={() => setView('pick_monitor_event')}
          title={evtMeta ? t(evtMeta.label) : t('Content Monitor')}
          subtitle={isHealthEvent ? t('Pick which monitor to watch.') : t('Pick an existing monitor or start fresh.')}
        />
        <div className="w-full max-w-xl space-y-3">
          {/* Create from scratch — only for content-change monitors (health needs
              an existing monitor to watch). */}
          {!isHealthEvent && (
            <>
              <button
                type="button"
                onClick={handleCreateNewMonitor}
                className="w-full text-left bg-surface border-2 border-dashed border-border rounded-xl p-4 hover:border-ink hover:shadow-sm transition-all group"
              >
                <div className="flex items-center gap-3">
                  <div className="w-9 h-9 rounded-lg bg-hover group-hover:bg-ink group-hover:text-white text-ink flex items-center justify-center transition-colors">
                    <PlusIcon className="h-5 w-5" />
                  </div>
                  <div>
                    <div className="text-sm font-medium text-ink">{t('Create new monitor')}</div>
                    <div className="text-xs text-secondary">{t('Set up a new URL to watch')}</div>
                  </div>
                </div>
              </button>
              {contentMonitors.length > 0 && (
                <div className="flex items-center gap-3 py-1">
                  <div className="h-px flex-1 bg-border" />
                  <span className="text-[10px] text-tertiary">{t('or use existing')}</span>
                  <div className="h-px flex-1 bg-border" />
                </div>
              )}
            </>
          )}

          {/* Empty state for health events with no monitors yet. */}
          {isHealthEvent && contentMonitors.length === 0 && (
            <div className="w-full text-center bg-surface border border-dashed border-border rounded-xl p-6">
              <div className="text-sm font-medium text-ink">{t('No monitors yet')}</div>
              <div className="text-xs text-secondary mt-1">{t('Create a content monitor first, then come back to alert on its health.')}</div>
              <button
                type="button"
                onClick={handleCreateNewMonitor}
                className="mt-3 inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg bg-accent-strong text-accent-on hover:bg-accent-strong/90 transition-colors"
              >
                <PlusIcon className="h-3.5 w-3.5" />
                {t('Create a monitor')}
              </button>
            </div>
          )}

          {/* Existing monitors */}
          {contentMonitors.map((mon: any) => (
            <button
              key={mon.id}
              type="button"
              onClick={() => handleSelectExistingMonitor(mon)}
              className="w-full text-left bg-surface border border-border rounded-xl p-4 hover:border-ink hover:shadow-sm transition-all group"
            >
              <div className="flex items-center justify-between">
                <div className="min-w-0">
                  <div className="text-sm font-medium text-ink truncate transition-colors">{mon.url}</div>
                  <div className="text-xs text-secondary mt-0.5">{i18n.t('Content monitor')}</div>
                </div>
                <div className={clsx('w-2 h-2 rounded-full shrink-0 ml-3', mon.enabled !== false ? 'bg-ink' : 'bg-active')} />
              </div>
            </button>
          ))}
        </div>
      </div>
    );
  }

  // --- EVENT TYPE PICKER (after selecting a workflow or session) ---
  if (view === 'pick_event' && pendingItem) {
    const isWorkflow = pendingItem.type === 'workflow';
    const noun = isWorkflow ? t('workflow') : t('AI session');
    const EVENTS = [
      { key: 'started', label: t('Started'), description: t('When the {{noun}} begins execution', { noun }), icon: PlayIcon },
      { key: 'completed', label: t('Completed'), description: t('When the {{noun}} finishes (success or error)', { noun }), icon: CheckCircleIcon },
      { key: 'error', label: t('Error'), description: t('Only when the {{noun}} fails', { noun }), icon: ExclamationTriangleIcon },
    ];

    return (
      <div className="flex flex-col items-center justify-center min-h-[50vh] px-4">
        <ViewHeader
          onBack={() => { setView('existing'); setPendingItem(null); }}
          title={t('Choose an event')}
          subtitle={pendingItem.item.name}
        />
        <div className="grid grid-cols-3 gap-3 max-w-xl w-full">
          {EVENTS.map(evt => {
            const Icon = evt.icon;
            const isHovered = hoveredId === evt.key;
            return (
              <button
                key={evt.key}
                type="button"
                onClick={() => handleSelectEvent(evt.key)}
                onMouseEnter={() => setHoveredId(evt.key)}
                onMouseLeave={() => setHoveredId(null)}
                className={clsx(
                  'relative p-5 rounded-xl border-2 text-left transition-all duration-200 bg-surface hover:shadow-md',
                  isHovered ? 'border-ink shadow-sm scale-[1.02]' : 'border-border hover:border-secondary',
                )}
              >
                <div className={clsx(
                  'w-9 h-9 rounded-xl flex items-center justify-center mb-3 transition-colors',
                  isHovered ? 'bg-ink text-white' : 'bg-hover text-ink',
                )}>
                  <Icon className="h-4 w-4" />
                </div>
                <div className="text-sm font-medium text-ink">{t(evt.label)}</div>
                <div className="text-xs text-secondary mt-1">{t(evt.description)}</div>
              </button>
            );
          })}
        </div>
      </div>
    );
  }

  // --- EXISTING ITEMS PICKER ---
  return (
    <div className="flex flex-col items-center justify-center min-h-[50vh] px-4">
      <ViewHeader onBack={() => setView('home')} title={t('Pick an existing item')} subtitle={t('Select a monitor, workflow, or AI session to build on.')} />
      <div className="w-full max-w-xl space-y-5">
        {contentMonitors.length > 0 && (
          <ExistingSection category="monitors" items={contentMonitors} renderItem={(t: any) => (
            <button key={t.id} type="button" onClick={() => handleSelectExistingMonitor(t, 'change_detected')}
              className="w-full text-left bg-surface border border-border rounded-xl p-4 hover:border-ink hover:shadow-sm transition-all group"
            >
              <div className="flex items-center justify-between">
                <div className="min-w-0">
                  <div className="text-sm font-medium text-ink truncate transition-colors">{t.url}</div>
                  <div className="text-xs text-secondary mt-0.5">{i18n.t('Content monitor')}</div>
                </div>
                <div className={clsx('w-2 h-2 rounded-full shrink-0 ml-3', t.enabled !== false ? 'bg-ink' : 'bg-active')} />
              </div>
            </button>
          )} />
        )}

        {(workflows || []).length > 0 && (
          <ExistingSection category="workflows" items={workflows} renderItem={(w: any) => (
            <button key={w.id} type="button" onClick={() => handleSelectExistingWorkflow(w)}
              className="w-full text-left bg-surface border border-border rounded-xl p-4 hover:border-ink hover:shadow-sm transition-all group"
            >
              <div className="flex items-center justify-between">
                <div className="min-w-0">
                  <div className="text-sm font-medium text-ink truncate transition-colors">{w.name}</div>
                  <div className="flex items-center gap-2 mt-0.5">
                    <span className="text-xs text-secondary capitalize">{w.workflow_type?.replace('_', ' ')}</span>
                    {w.steps && <span className="text-xs text-tertiary">{t('{{n}} steps', { n: w.steps.length })}</span>}
                  </div>
                </div>
                <div className={clsx('w-2 h-2 rounded-full shrink-0 ml-3', w.is_active ? 'bg-ink' : 'bg-active')} />
              </div>
            </button>
          )} />
        )}

        {(sessions || []).length > 0 && (
          <ExistingSection category="ai_sessions" items={sessions} renderItem={(s: any) => (
            <button key={s.id} type="button" onClick={() => handleSelectExistingSession(s)}
              className="w-full text-left bg-surface border border-border rounded-xl p-4 hover:border-ink hover:shadow-sm transition-all group"
            >
              <div className="flex items-center justify-between">
                <div className="min-w-0">
                  <div className="text-sm font-medium text-ink truncate transition-colors">{s.name}</div>
                  <div className="flex items-center gap-2 mt-0.5">
                    <span className="text-xs text-secondary">{s.mode}</span>
                    {s.goal && <span className="text-xs text-tertiary truncate max-w-[200px]">{s.goal}</span>}
                  </div>
                </div>
                <div className="shrink-0 ml-3 capitalize text-xs text-secondary">{s.status}</div>
              </div>
            </button>
          )} />
        )}
      </div>
    </div>
  );
};

// --- Small sub-components ---

function ViewHeader({ onBack, title, subtitle }: { onBack: () => void; title: string; subtitle: string }) {
  return (
    <div className="flex items-center gap-3 mb-8">
      <button type="button" onClick={onBack} className="p-1.5 rounded-lg hover:bg-hover text-secondary hover:text-ink transition-colors">
        <ArrowLeftIcon className="h-4 w-4" />
      </button>
      <div>
        <h2 className="text-xl font-semibold text-ink">{title}</h2>
        <p className="text-sm text-secondary mt-0.5">{subtitle}</p>
      </div>
    </div>
  );
}

function PickerCard({ id, hoveredId, onHover, onClick, disabled, icon: Icon, title, description, children }: {
  id: string; hoveredId: string | null; onHover: (id: string | null) => void;
  onClick: () => void; disabled?: boolean; icon: any; title: string; description: string; children?: React.ReactNode;
}) {
  const isHovered = hoveredId === id;
  return (
    <button
      type="button"
      onClick={onClick}
      onMouseEnter={() => onHover(id)}
      onMouseLeave={() => onHover(null)}
      disabled={disabled}
      className={clsx(
        'flex-1 p-6 rounded-xl border-2 text-left transition-all duration-200 bg-surface',
        disabled && 'opacity-50 cursor-not-allowed',
        !disabled && 'hover:shadow-md',
        isHovered && !disabled ? 'border-ink shadow-sm scale-[1.02]' : 'border-border hover:border-secondary',
      )}
    >
      <div className={clsx(
        'w-10 h-10 rounded-lg flex items-center justify-center mb-3 transition-colors',
        isHovered && !disabled ? 'bg-ink text-white' : 'bg-hover text-ink',
      )}>
        <Icon className="h-5 w-5" />
      </div>
      <div className="text-sm font-medium text-ink">{title}</div>
      <div className="text-xs text-secondary mt-1">{description}</div>
      {children}
    </button>
  );
}

function CountBadge({ count, label }: { count: number; label: string }) {
  return <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-hover text-secondary">{count} {label}</span>;
}

function ExistingSection({ category, items, renderItem }: {
  category: ExistingCategory; items: any[]; renderItem: (item: any) => React.ReactNode;
}) {
  const { t } = useTranslation();
  const meta = CATEGORY_META[category];
  const Icon = meta.icon;
  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <Icon className="h-4 w-4 text-secondary" />
        <span className="text-xs font-medium text-secondary">{t(meta.label)}</span>
        <span className="text-[10px] text-tertiary">({items.length})</span>
      </div>
      <div className="space-y-2">{items.map(renderItem)}</div>
    </div>
  );
}
