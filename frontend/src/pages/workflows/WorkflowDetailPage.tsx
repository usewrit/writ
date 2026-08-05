import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams, Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useRequireAuth } from '../../hooks/useAuth';
import { useQuery } from '../../hooks/useQuery';
import { Q } from '../../stores/queryKeys';
import { automationApi, triggersApi, streamingApi } from '../../api/endpoints';
import { WorkflowDetails } from '../../components/WorkflowDetails';
import {
  ArrowLeftIcon,
  CursorArrowRaysIcon,
  PencilSquareIcon,
  StopIcon,
  SignalIcon,
  PlayIcon,
  TrashIcon,
  CpuChipIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { RunWithTargetButton } from '../../components/workflows/ExecutionTargetPicker';
import { RunWorkflowModal, workflowNeedsInput } from '../../components/workflows/RunWorkflowModal';
import { Modal } from '../../components/ui/Modal';
import { ViewSwitch } from '../../components/ui/ViewSwitch';
import { TYPE_META } from './detail/meta';
import { DetailTab } from './detail/DetailTab';
import { ConnectTab } from './detail/ConnectTab';
import { DataTab } from './detail/DataTab';
import { SummaryRail } from './detail/SummaryRail';
import { EditDetailsModal } from './detail/EditDetailsModal';
import { SendToAgentModal } from '../../components/fleet/SendToAgentModal';
import { apiErrorMessage } from '../../api/client';

// The 3-page model + a Data workspace. 'detail' = what is it (folds in the old
// overview/runs/settings); 'steps' = what it does; 'connect' = how it's invoked
// & wired; 'data' = what it produced.
type TabId = 'detail' | 'steps' | 'connect' | 'data';

// Old deep-links (overview/runs/settings) fold into 'detail'; settings opens the
// Advanced section. Unknown values fall back to 'detail'.
const TAB_ALIAS: Record<string, TabId> = { overview: 'detail', runs: 'detail', settings: 'detail' };
const resolveTab = (raw: string | null, valid: TabId[]): TabId => {
  const mapped = (TAB_ALIAS[raw || ''] || raw || 'detail') as TabId;
  return valid.includes(mapped) ? mapped : 'detail';
};

export const WorkflowDetailPage: React.FC = () => {
  useRequireAuth();
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const [detailPoll, setDetailPoll] = useState<number | undefined>(undefined);
  const { data: workflow, loading, refresh: refreshWorkflow } = useQuery(Q.workflow(id!), () => automationApi.getWorkflow(Number(id)), { pollInterval: detailPoll });
  const { data: tasks } = useQuery(Q.workflowTasks(id!), () => automationApi.listTasks({ workflow_id: Number(id), limit: 20, summary: true }), { pollInterval: detailPoll });
  const { data: allTriggers } = useQuery(Q.triggers(), () => triggersApi.listAll());

  const isWorkflowRunning = ['running', 'pending', 'assigned', 'queued'].includes(workflow?.last_run_status || '');

  // Poll fast only while a run is in flight. The cadence depends on the very
  // payload the query returns, so it can't be computed before the call above —
  // it's reconciled DURING RENDER (React's adjust-state escape hatch) rather than
  // from an effect, which would cost a second render pass on every poll.
  const wantedPoll = isWorkflowRunning ? 3000 : undefined;
  if (detailPoll !== wantedPoll) setDetailPoll(wantedPoll);

  const isStreaming = workflow?.workflow_type === 'streaming';

  // Streaming has no Data tab (no per-run extracted data). Old ?tab=runs/settings → detail.
  const validTabs: TabId[] = isStreaming
    ? ['detail', 'steps', 'connect']
    : ['detail', 'steps', 'connect', 'data'];
  const rawTab = searchParams.get('tab');
  const activeTab: TabId = resolveTab(rawTab, validTabs);
  // Deep-linking to the old Settings tab opens the Advanced "How it runs" section.
  const defaultAdvancedOpen = rawTab === 'settings';
  const setActiveTab = (tab: string) => {
    setSearchParams(tab === 'detail' ? {} : { tab }, { replace: true });
  };

  // Active streaming session — keeps the Start/End button in sync.
  const { data: streamingSessions, refresh: refreshSessions } = useQuery(
    Q.streamingSessions(),
    () => streamingApi.listSessions(),
    { enabled: isStreaming, pollInterval: isStreaming ? 5000 : undefined, silent: true },
  );
  const workflowSessions = isStreaming
    ? (streamingSessions || []).filter((s: any) => s.workflow_id === Number(id))
    : [];
  const activeSession = workflowSessions.find((s: any) => ['starting', 'running'].includes(s.status));

  const navigate = useNavigate();
  const [running, setRunning] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [endingSession, setEndingSession] = useState(false);
  const [showRunModal, setShowRunModal] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [showEditDetails, setShowEditDetails] = useState(false);
  const [showSendAgent, setShowSendAgent] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const handleCancel = async () => {
    if (!workflow?.last_run_task_id) return;
    setCancelling(true);
    try {
      await automationApi.cancelTask(workflow.last_run_task_id);
      toast.success(t('Task cancelled'));
      refreshWorkflow();
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      toast.error(typeof detail === 'string' ? detail : t('Failed to cancel'));
    } finally {
      setCancelling(false);
    }
  };

  const handleRun = async (target?: 'auto' | 'local' | 'cloud') => {
    if (!workflow) return;
    if (workflowNeedsInput(workflow) || workflow.default_persona_id) { setShowRunModal(true); return; }
    setRunning(true);
    try {
      const result = await automationApi.runWorkflow(workflow.id, target);
      toast.success(`${result.message}`, { duration: 5000 });
      refreshWorkflow();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('Failed to run workflow'));
    } finally { setRunning(false); }
  };

  const handleStartSession = async () => {
    if (!workflow) return;
    if (!workflow.entry_url) {
      toast.error(t('This workflow has no entry URL — set one before starting a session.'));
      return;
    }
    setRunning(true);
    try {
      await streamingApi.startSession({ workflow_id: workflow.id, target_url: workflow.entry_url });
      toast.success(t('Session started'));
      await refreshSessions();
    } catch (err: any) { toast.error(err?.response?.data?.detail || t('Failed to start')); }
    finally { setRunning(false); }
  };

  const handleEndSession = async () => {
    if (!activeSession) return;
    setEndingSession(true);
    try {
      await streamingApi.endSession(activeSession.session_key);
      toast.success(t('Session ended'));
      await refreshSessions();
    } catch (err: any) { toast.error(err?.response?.data?.detail || t('Failed to end session')); }
    finally { setEndingSession(false); }
  };

  const handleDelete = async () => {
    if (!workflow) return;
    setDeleting(true);
    try {
      await automationApi.deleteWorkflow(workflow.id);
      toast.success(t('Workflow deleted'));
      navigate('/workflows');
    } catch (err: any) {
      toast.error(apiErrorMessage(err, t('Failed to delete workflow')));
    } finally {
      setDeleting(false);
    }
  };

  if (loading || !workflow) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-sm text-tertiary">{t('Loading workflow...')}</div>
      </div>
    );
  }

  const typeMeta = TYPE_META[workflow.workflow_type] || { label: workflow.workflow_type, icon: CursorArrowRaysIcon };
  const TypeIcon = typeMeta.icon;
  const taskList = tasks || [];
  const linkedTriggers = (allTriggers || []).filter((tr: any) =>
    tr.workflow_id === Number(id) ||
    tr.blocks?.some((b: any) => b.blockType === 'workflow' && b.config?.workflow_id === Number(id))
  );

  // Rendered via ViewSwitch (the shared segmented control): one white thumb
  // GLIDES between tabs on the expo curve instead of bg+shadow snapping from
  // one button to the next.
  const TABS: { id: TabId; label: string; count?: number; dataTour: string }[] = isStreaming
    ? [
        { id: 'detail', label: t('Detail'), dataTour: 'wf-tab-detail' },
        { id: 'steps', label: t('Setup Steps'), count: workflow.streaming_config?.setup_steps_count || workflow.steps?.length || 0, dataTour: 'wf-tab-steps' },
        { id: 'connect', label: t('Connect'), dataTour: 'wf-tab-connect' },
      ]
    : [
        { id: 'detail', label: t('Detail'), dataTour: 'wf-tab-detail' },
        { id: 'steps', label: t('Steps'), count: workflow.steps?.length || 0, dataTour: 'wf-tab-steps' },
        { id: 'connect', label: t('Connect'), dataTour: 'wf-tab-connect' },
        { id: 'data', label: t('Data'), dataTour: 'wf-tab-data' },
      ];

  return (
    <>
      <div className="flex flex-col h-full">
        {/* Toolbar */}
        <div className="flex items-center gap-3 h-12 px-4 sm:px-6 bg-chrome chrome-topbar border-b border-border shrink-0">
          <Link to={isStreaming ? '/streaming' : '/workflows'} className="p-1 text-tertiary hover:text-ink transition-colors">
            <ArrowLeftIcon className="w-3.5 h-3.5" />
          </Link>
          <TypeIcon className="w-4 h-4 text-tertiary shrink-0" />
          <span className="text-[13px] font-semibold text-ink truncate">{workflow.name}</span>
          <span className="hidden @pair/stage:inline text-[11px] text-tertiary">{t(typeMeta.label)}</span>

          <div className="hidden @pair/stage:flex items-center ml-2">
            <ViewSwitch value={activeTab} onChange={setActiveTab} options={TABS} />
          </div>

          <div className="flex-1" />

          <div className="flex items-center gap-1.5 shrink-0">
            {isStreaming ? (
              activeSession ? (
                <>
                  <Link
                    to={`/streaming/${activeSession.session_key}`}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium text-ink border border-border rounded-lg hover:bg-chrome transition-colors"
                  >
                    <SignalIcon className="w-3.5 h-3.5" />
                    {t('Open Session')}
                  </Link>
                  <button
                    onClick={handleEndSession}
                    disabled={endingSession}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium text-secondary border border-border rounded-lg hover:bg-chrome disabled:opacity-50 transition-colors"
                  >
                    <StopIcon className="w-3.5 h-3.5" />
                    {endingSession ? t('Ending...') : t('End Session')}
                  </button>
                </>
              ) : (
                <button
                  onClick={handleStartSession}
                  disabled={running}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-semibold shadow-sm bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 disabled:opacity-50 transition-colors"
                >
                  <PlayIcon className="w-3.5 h-3.5" />
                  {running ? t('Starting...') : t('Start Session')}
                </button>
              )
            ) : (
              <span data-tour="wf-run" className="inline-flex">
                {isWorkflowRunning ? (
                  <button onClick={handleCancel} disabled={cancelling}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium text-secondary border border-border rounded-lg hover:bg-chrome transition-all disabled:opacity-50">
                    <StopIcon className="w-3.5 h-3.5" />
                    {cancelling ? t('Stopping...') : t('Stop')}
                  </button>
                ) : (
                  <RunWithTargetButton onRun={(target) => handleRun(target)} loading={running} />
                )}
              </span>
            )}
            {!isStreaming && (
              <button
                onClick={() => setShowSendAgent(true)}
                className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium text-ink border border-border rounded-lg hover:bg-chrome transition-all"
                title={t('Send to a fleet agent')}
              >
                <CpuChipIcon className="w-3.5 h-3.5" />
                {t('Send to agent')}
              </button>
            )}
            <button onClick={() => setShowEditDetails(true)}
              className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium text-ink border border-border rounded-lg hover:bg-chrome transition-all">
              <PencilSquareIcon className="w-3.5 h-3.5" />
              {t('Edit')}
            </button>
            <button
              onClick={() => setShowDeleteConfirm(true)}
              className="p-1.5 text-tertiary hover:text-red-500 rounded-lg hover:bg-red-50 transition-colors"
              title={t('Delete workflow')}
            >
              <TrashIcon className="w-3.5 h-3.5" />
            </button>
            <div className={clsx('w-2 h-2 rounded-full ml-1', workflow.is_active ? 'bg-ink' : 'bg-active')} />
          </div>
        </div>

        {/* Narrow-stage tab row (the toolbar tabs are hidden below `pair`) */}
        <div className="@pair/stage:hidden flex items-center px-3 py-1.5 border-b border-border bg-chrome overflow-x-auto shrink-0">
          <ViewSwitch value={activeTab} onChange={setActiveTab} options={TABS} />
        </div>

        {/* Content: main column + at-a-glance rail */}
        <div className="flex flex-1 min-h-0">
          <div className="flex-1 overflow-auto min-w-0">
            {/* Keyed on the active tab: switching replays a 160ms opacity settle
                (content-in) so panels swap smoothly, never snap. */}
            <div key={activeTab} className="px-4 sm:px-6 py-4 animate-content-in">
              {activeTab === 'detail' && (
                <DetailTab
                  workflow={workflow}
                  tasks={taskList}
                  linkedTriggers={linkedTriggers}
                  isStreaming={isStreaming}
                  activeSession={activeSession}
                  recentSessions={workflowSessions}
                  busy={running || endingSession}
                  onStartSession={handleStartSession}
                  onEndSession={handleEndSession}
                  onNavigateTab={setActiveTab}
                  onRefresh={refreshWorkflow}
                  isOwner
                  defaultAdvancedOpen={defaultAdvancedOpen}
                />
              )}

              {activeTab === 'steps' && (
                <WorkflowDetails workflow={workflow} onUpdate={refreshWorkflow} hideRunSettings />
              )}

              {activeTab === 'connect' && (
                <ConnectTab
                  workflow={workflow}
                  linkedTriggers={linkedTriggers}
                  isStreaming={isStreaming}
                  onRefresh={refreshWorkflow}
                />
              )}

              {activeTab === 'data' && !isStreaming && (
                <DataTab workflow={workflow} />
              )}
            </div>
          </div>

          {/* "At a glance" rail is the Detail companion; other tabs own full width. */}
          {activeTab === 'detail' && (
            <SummaryRail
              workflow={workflow}
              tasks={taskList}
              linkedTriggers={linkedTriggers}
              isStreaming={isStreaming}
              activeSession={activeSession}
              recentSessions={workflowSessions}
              isRunning={isWorkflowRunning}
              busy={running || cancelling || endingSession}
              onRun={() => handleRun()}
              onStop={handleCancel}
              onNavigateTab={setActiveTab}
            />
          )}
        </div>
      </div>

      {workflow && (
        <RunWorkflowModal workflow={workflow} isOpen={showRunModal}
          onClose={() => setShowRunModal(false)} onDispatched={() => setShowRunModal(false)} />
      )}

      {workflow && (
        <EditDetailsModal
          workflow={workflow}
          isOpen={showEditDetails}
          onClose={() => setShowEditDetails(false)}
          onSaved={() => { setShowEditDetails(false); refreshWorkflow(); }}
        />
      )}

      {workflow && (
        <SendToAgentModal
          open={showSendAgent}
          onClose={() => setShowSendAgent(false)}
          kind="workflow"
          entityId={workflow.id}
          entityName={workflow.name}
        />
      )}

      <Modal isOpen={showDeleteConfirm} onClose={() => setShowDeleteConfirm(false)} title={t('Delete Workflow')} size="sm">
        <p className="text-sm text-secondary mb-1">
          {t('Are you sure you want to delete')} <strong className="text-ink">{workflow?.name}</strong>?
        </p>
        <p className="text-xs text-tertiary mb-5">
          {t('This will permanently remove the workflow, all its steps, and run history. This action cannot be undone.')}
        </p>
        <div className="flex items-center justify-end gap-2">
          <button
            onClick={() => setShowDeleteConfirm(false)}
            className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg hover:bg-chrome transition-colors"
          >
            {t('Cancel')}
          </button>
          <button
            onClick={handleDelete}
            disabled={deleting}
            className="px-4 py-2 text-sm font-medium bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors disabled:opacity-50"
          >
            {deleting ? t('Deleting...') : t('Delete Workflow')}
          </button>
        </div>
      </Modal>
    </>
  );
};
