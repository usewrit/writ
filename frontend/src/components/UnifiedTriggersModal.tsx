import React, { useState, useEffect, Fragment } from 'react';
import { Dialog, Transition, Disclosure } from '@headlessui/react';
import {
  XMarkIcon,
  PlusIcon,
  TrashIcon,
  PencilIcon,
  PlayIcon,
  ClockIcon,
  BoltIcon,
  CheckCircleIcon,
  XCircleIcon,
  ArrowPathIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  BellIcon,
  CpuChipIcon,
  Cog6ToothIcon,
  BeakerIcon,
  FunnelIcon,
  InformationCircleIcon,
  ExclamationTriangleIcon,
  Squares2X2Icon,
  SparklesIcon,
  ArrowsRightLeftIcon,
  CheckIcon,
  LockClosedIcon,
  EyeIcon,
  LightBulbIcon,
} from '@heroicons/react/24/outline';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { triggersApi, aiSessionsApi, selectorsApi, automationApi, recipientsApi, webhookTriggersApi, targetsApi } from '../api/endpoints';
import { UserGroupIcon, LinkIcon, ClipboardIcon, ClipboardDocumentCheckIcon, ArrowUturnLeftIcon } from '@heroicons/react/24/outline';
import { ConfirmDialog } from './ConfirmDialog';
import { channelMeta } from './notifications/channelMeta';
import { Checkbox, NumberInput, Select, Switch } from './ui';
import { uiLocale } from '../utils/format';

// Event types for triggers
const EVENT_TYPES = [
  { value: 'change_detected', label: 'Content Change', description: 'When target/selector content changes' },
  { value: 'webhook_received', label: 'Webhook Received', description: 'When external webhook calls this target' },
  { value: 'ai_session_started', label: 'AI Session Started', description: 'When an AI session begins execution' },
  { value: 'ai_session_completed', label: 'AI Session Completed', description: 'When an AI session finishes (success or error)' },
  { value: 'workflow_started', label: 'Workflow Started', description: 'When a workflow begins execution' },
  { value: 'workflow_completed', label: 'Workflow Completed', description: 'When a workflow finishes (success or error)' },
];

interface TriggerRule {
  id: number;
  target_id?: number;
  event_type: string;
  target_selector_id?: number;
  ai_session_id?: number;
  workflow_id?: number;
  webhook_trigger_id?: number;
  webhook_trigger_token?: string;
  name: string;
  description?: string;
  enabled: boolean;
  priority: number;
  conditions?: Record<string, any>;
  actions: Array<{ type: string; config: any }>;
  blocks?: Array<{ id: string; type: string; blockType: string; config: any }>;
  last_triggered_at?: string;
  trigger_count: number;
  created_at?: string;
  updated_at?: string;
}

interface TriggerExecution {
  id: number;
  trigger_rule_id: number;
  detected_change_id?: number;
  status: string;
  trigger_context?: any;
  action_results?: any[];
  triggered_at?: string;
  completed_at?: string;
  error_message?: string;
}

interface AISession {
  id: number;
  name: string;
  goal: string;
  mode: string;
  status: string;
  entry_url?: string;
}

interface TargetSelector {
  id: number;
  name: string;
  selector: string;
  enabled: boolean;
  content_type?: string;
}

interface Workflow {
  id: number;
  name: string;
  workflow_type: string;
  steps?: any[];
  is_active: boolean;
  is_installed?: boolean;
}

interface UnifiedTriggersModalProps {
  isOpen: boolean;
  onClose: () => void;
  targetId: string;
  targetUrl: string;
  /** Optional: scope triggers to a specific workflow */
  workflowId?: number;
  /** Optional: default event type when creating new triggers */
  defaultEventType?: string;
  /** When true, renders inline without Dialog wrapper and auto-enters creation mode */
  embedded?: boolean;
  /** When true, auto-adds a return_data block to the flow (workflow has extract steps) */
  autoAddReturnBlock?: boolean;
}

const ACTION_TYPES = [
  {
    value: 'notification',
    label: 'Notification',
    icon: BellIcon,
    color: 'gray',
    description: 'Send alerts via Pushover, Email, SMS, etc.'
  },
  {
    value: 'ai_session',
    label: 'AI Session',
    icon: CpuChipIcon,
    color: 'gray',
    description: 'Launch intelligent AI automation sessions'
  },
  {
    value: 'workflow',
    label: 'Workflow',
    icon: Cog6ToothIcon,
    color: 'gray',
    description: 'Execute browser automation workflows'
  },
];

export const UnifiedTriggersModal: React.FC<UnifiedTriggersModalProps> = ({
  isOpen,
  onClose,
  targetId,
  targetUrl,
  workflowId,
  defaultEventType,
  embedded = false,
  autoAddReturnBlock = false,
}) => {
  const { t } = useTranslation();
  const [triggers, setTriggers] = useState<TriggerRule[]>([]);
  const [availableSessions, setAvailableSessions] = useState<AISession[]>([]);
  const [selectors, setSelectors] = useState<TargetSelector[]>([]);
  const [allTargets, setAllTargets] = useState<Array<{ id: number; url: string; check_type: string }>>([]);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [availableRecipients, setAvailableRecipients] = useState<Array<{
    id: number;
    provider: string;
    name: string;
    identifier_preview: string;
    enabled: boolean;
  }>>([]);
  const [loading, setLoading] = useState(true);
  const [editingTrigger, setEditingTrigger] = useState<TriggerRule | null>(null);
  const [isCreating, setIsCreating] = useState(embedded ? true : false);
  const [saving, setSaving] = useState(false);
  const [expandedTriggerId, setExpandedTriggerId] = useState<number | null>(null);
  const [deleteTargetId, setDeleteTargetId] = useState<number | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [executions, setExecutions] = useState<Record<number, TriggerExecution[]>>({});
  const [expandedAdvancedBlocks, setExpandedAdvancedBlocks] = useState<Set<string>>(new Set());

  // Webhook triggers state
  const [copiedWebhookToken, setCopiedWebhookToken] = useState<string | null>(null);

  // Form state
  const [formData, setFormData] = useState({
    name: '',
    description: '',
    event_type: defaultEventType || 'change_detected',
    enabled: true,
    priority: 0,
    target_selector_id: null as number | null,
    ai_session_id: null as number | null,
    workflow_id: workflowId || null as number | null,
    conditions: [] as Array<{ field: string; operator: string; value: string }>,
    schedule: {
      time_window_start: '',
      time_window_end: '',
      days_of_week: [] as number[],
      cooldown_minutes: '',
    },
    actions: [] as Array<{ type: string; config: any }>,
  });

  // Fetching and applying are separate so the open effect further down can use
  // React's documented shape — start the request, update state in the promise
  // callback — instead of calling a helper that pushes state in the same tick the
  // effect runs (`react-hooks/set-state-in-effect`). It also gets the effect a
  // real cancellation flag, so switching target mid-flight can no longer land a
  // stale response over the newer one.
  const fetchModalData = async () => {
    const targetIdNum = targetId ? parseInt(targetId) : 0;

    const [triggersData, sessionsData, selectorsData, workflowsData, recipientsData, targetsData] = await Promise.all([
      targetIdNum ? triggersApi.listForTarget(targetIdNum) : triggersApi.listAll().catch(() => []),
      aiSessionsApi.listAll(),
      targetIdNum ? selectorsApi.listForTarget(targetIdNum) : Promise.resolve([]),
      automationApi.listWorkflows().catch(() => []),
      recipientsApi.getAll(true).catch(() => []),
      targetsApi.getAll().catch(() => []),
    ]);

    // If workflow-scoped, filter triggers to this workflow
    const filteredTriggers = workflowId
      ? triggersData.filter((t: any) => t.workflow_id === workflowId)
      : triggersData;

    return { filteredTriggers, sessionsData, selectorsData, workflowsData, recipientsData, targetsData };
  };

  const applyModalData = (data: Awaited<ReturnType<typeof fetchModalData>>) => {
    setTriggers(data.filteredTriggers);
    setAvailableSessions(data.sessionsData);
    setAvailableRecipients(data.recipientsData);
    setSelectors(data.selectorsData);
    setWorkflows(data.workflowsData);
    setAllTargets((data.targetsData || []).map((t: any) => ({ id: t.id, url: t.url, check_type: t.check_type })));
    setLoading(false);
  };

  const reportLoadFailure = (error: unknown) => {
    console.error('Failed to load data:', error);
    toast.error(t('Failed to load triggers'));
    setLoading(false);
  };

  // Reload after a mutation. `setLoading(true)` lives at the call sites rather
  // than here so the open path can raise the flag in its own render pass.
  const loadData = async () => {
    try {
      applyModalData(await fetchModalData());
    } catch (error) {
      reportLoadFailure(error);
    }
  };

  const loadExecutions = async (triggerId: number) => {
    try {
      const data = await triggersApi.getExecutions(triggerId, 10);
      setExecutions(prev => ({ ...prev, [triggerId]: data }));
    } catch (error) {
      console.error('Failed to load executions:', error);
    }
  };

  const resetForm = () => {
    setFormData({
      name: '',
      description: '',
      event_type: defaultEventType || 'change_detected',
      enabled: true,
      priority: 0,
      target_selector_id: null,
      ai_session_id: null,
      workflow_id: workflowId || null,
      conditions: [],
      schedule: {
        time_window_start: '',
        time_window_end: '',
        days_of_week: [],
        cooldown_minutes: '',
      },
      actions: [],
    });
    // Reset flow blocks to initial state
    setFlowBlocks([
      { id: 'block_1', type: 'event', blockType: defaultEventType || 'change_detected', config: {} }
    ]);
    setEditingTrigger(null);
  };

  const populateForm = (trigger: TriggerRule) => {
    // Parse conditions from object to array format
    const conditionsArray: Array<{ field: string; operator: string; value: string }> = [];
    const scheduleData = {
      time_window_start: '',
      time_window_end: '',
      days_of_week: [] as number[],
      cooldown_minutes: '',
    };

    if (trigger.conditions) {
      Object.entries(trigger.conditions).forEach(([key, spec]: [string, any]) => {
        if (key === 'schedule') {
          if (spec.time_window) {
            scheduleData.time_window_start = spec.time_window.start || '';
            scheduleData.time_window_end = spec.time_window.end || '';
          }
          if (spec.days_of_week) {
            scheduleData.days_of_week = spec.days_of_week;
          }
          if (spec.cooldown_minutes) {
            scheduleData.cooldown_minutes = spec.cooldown_minutes.toString();
          }
        } else {
          conditionsArray.push({
            field: key,
            operator: spec.operator || 'contains',
            value: spec.value?.toString() || '',
          });
        }
      });
    }

    setFormData({
      name: trigger.name,
      description: trigger.description || '',
      event_type: trigger.event_type || 'change_detected',
      enabled: trigger.enabled,
      priority: trigger.priority,
      target_selector_id: trigger.target_selector_id || null,
      ai_session_id: trigger.ai_session_id || null,
      workflow_id: trigger.workflow_id || null,
      conditions: conditionsArray,
      schedule: scheduleData,
      actions: trigger.actions || [],
    });
  };

  const buildConditionsObject = () => {
    const conditions: Record<string, any> = {};

    // Add field conditions, read straight off the flow's condition blocks. They
    // used to be mirrored into formData by an effect that ran on every flowBlocks
    // change; deriving them where they're consumed removes that cascading render
    // (`react-hooks/set-state-in-effect`) and the second source of truth with it.
    flowBlocks.filter(b => b.type === 'condition').forEach(b => {
      const field: string = b.config.field || '';
      const operator: string = b.config.operator || 'contains';
      const value: string = b.config.value || '';
      if (field && (value || operator === 'changed')) {
        conditions[field] = {
          operator,
          value: operator === 'changed' ? undefined : value,
        };
      }
    });

    // Add schedule conditions
    const hasSchedule = formData.schedule.time_window_start ||
                       formData.schedule.days_of_week.length > 0 ||
                       formData.schedule.cooldown_minutes;

    if (hasSchedule) {
      const schedule: any = {};
      if (formData.schedule.time_window_start && formData.schedule.time_window_end) {
        schedule.time_window = {
          start: formData.schedule.time_window_start,
          end: formData.schedule.time_window_end,
        };
      }
      if (formData.schedule.days_of_week.length > 0) {
        schedule.days_of_week = formData.schedule.days_of_week;
      }
      if (formData.schedule.cooldown_minutes) {
        schedule.cooldown_minutes = parseInt(formData.schedule.cooldown_minutes);
      }
      conditions.schedule = schedule;
    }

    return Object.keys(conditions).length > 0 ? conditions : undefined;
  };

  const handleSave = async () => {
    if (!formData.name.trim()) {
      toast.error(t('Name is required'));
      return;
    }

    // Check for actions in flowBlocks
    const actionBlocks = flowBlocks.filter(b => b.type === 'action');
    if (actionBlocks.length === 0) {
      toast.error(t('At least one action block is required'));
      return;
    }

    // Validate action blocks
    for (const block of actionBlocks) {
      if (block.blockType === 'ai_session' && (!block.config.session_ids || block.config.session_ids.length === 0)) {
        toast.error(t('Please select at least one AI session for the AI Session action'));
        return;
      }
      if (block.blockType === 'workflow' && !block.config.workflow_id) {
        toast.error(t('Please select a workflow for the Workflow action'));
        return;
      }
    }

    setSaving(true);
    try {
      // Get first event block for the primary event type
      const eventBlock = flowBlocks.find(b => b.type === 'event');
      const eventType = eventBlock?.blockType || 'change_detected';

      // Get target_id from prop OR from the change_detected block config
      const blockTargetId = eventBlock?.config?.target_id;
      const resolvedTargetId = targetId ? parseInt(targetId) : blockTargetId || undefined;

      const payload: any = {
        target_id: resolvedTargetId,
        event_type: eventType,
        name: formData.name.trim(),
        description: formData.description.trim() || undefined,
        enabled: formData.enabled,
        priority: formData.priority,
        conditions: buildConditionsObject(),
        // Same story as the conditions above — derived from the blocks rather
        // than from an effect-maintained mirror in formData.
        actions: actionBlocks.map(b => ({ type: b.blockType, config: b.config })),
        // Include the full block chain for visual workflow builder
        blocks: flowBlocks,
      };

      // Add entity-specific fields based on first event block
      if (eventType === 'change_detected') {
        payload.target_selector_id = eventBlock?.config?.selector_id || undefined;
      } else if (eventType.startsWith('ai_session')) {
        payload.ai_session_id = eventBlock?.config?.ai_session_id || undefined;
      } else if (eventType.startsWith('workflow')) {
        payload.workflow_id = workflowId || eventBlock?.config?.workflow_id || undefined;
      }

      // For webhook_received, create webhook entry point if not already created
      if (eventType === 'webhook_received' && !eventBlock?.config?.webhook_trigger_token) {
        try {
          const resolvedWfId = workflowId || eventBlock?.config?.workflow_id;
          const createData: any = {
            name: t('Trigger: {{name}}', { name: formData.name.trim() }),
            action: resolvedWfId ? 'run_workflow' : 'check_target',
            enabled: true,
          };
          // Only include non-null values
          const tid = targetId ? parseInt(targetId) : (eventBlock?.config?.target_id || null);
          if (tid) createData.target_id = tid;
          if (resolvedWfId) createData.workflow_id = resolvedWfId;
          if (eventBlock?.config?.webhook_secret) createData.secret = eventBlock.config.webhook_secret;
          if (eventBlock?.config?.custom_path?.trim()) createData.custom_path = eventBlock.config.custom_path.trim();

          const response = await webhookTriggersApi.create(createData);
          const webhookTriggerId = response.trigger?.id;
          const webhookTriggerToken = response.trigger?.token;
          // Store webhook trigger ID and token in the block config
          if (eventBlock && webhookTriggerId && webhookTriggerToken) {
            eventBlock.config.webhook_trigger_id = webhookTriggerId;
            eventBlock.config.webhook_trigger_token = webhookTriggerToken;
            payload.blocks = flowBlocks; // Update blocks with new webhook info
            // Also set webhook_trigger_id on the payload directly
            payload.webhook_trigger_id = webhookTriggerId;
          }
          toast.success(t('Webhook endpoint created'));
        } catch (webhookError: any) {
          console.error('Failed to create webhook trigger:', webhookError);
          toast.error(webhookError.response?.data?.detail || t('Failed to create webhook endpoint'));
          return;
        }
      }

      if (editingTrigger) {
        await triggersApi.update(editingTrigger.id, payload);
        toast.success(t('Trigger updated'));
      } else {
        await triggersApi.create(payload);
        toast.success(t('Trigger created'));
      }

      setEditingTrigger(null);
      setIsCreating(false);
      resetForm();
      setFlowBlocks([{ id: 'block_1', type: 'event', blockType: 'change_detected', config: {} }]);
      setLoading(true);
      loadData();
    } catch (error: any) {
      console.error('Failed to save trigger:', error);
      toast.error(error.response?.data?.detail || t('Failed to save trigger'));
    } finally {
      setSaving(false);
    }
  };

  const confirmDelete = async () => {
    if (deleteTargetId == null) return;
    setDeleting(true);
    try {
      await triggersApi.delete(deleteTargetId);
      toast.success(t('Trigger deleted'));
      setDeleteTargetId(null);
      setLoading(true);
      loadData();
    } catch (error) {
      console.error('Failed to delete trigger:', error);
      toast.error(t('Failed to delete trigger'));
    } finally {
      setDeleting(false);
    }
  };

  const handleToggle = async (trigger: TriggerRule) => {
    try {
      await triggersApi.toggle(trigger.id);
      toast.success(trigger.enabled ? t('Trigger disabled') : t('Trigger enabled'));
      setLoading(true);
      loadData();
    } catch (error) {
      console.error('Failed to toggle trigger:', error);
      toast.error(t('Failed to toggle trigger'));
    }
  };

  const handleTest = async (triggerId: number) => {
    try {
      const result = await triggersApi.test(triggerId, {
        test_content: 'Test content for trigger evaluation. Price: $99.99, Status: In Stock',
        test_extracted: { price: '99.99', status: 'In Stock', count: '5' },
      });

      if (result.would_fire) {
        toast.success(t('Trigger would FIRE!\n{{reason}}', { reason: result.match_reason }), { duration: 5000 });
      } else {
        toast.error(t('Trigger would NOT fire:\n{{reason}}', { reason: result.match_reason }), { duration: 5000 });
      }
    } catch (error) {
      console.error('Failed to test trigger:', error);
      toast.error(t('Failed to test trigger'));
    }
  };

  const toggleExpanded = (triggerId: number) => {
    if (expandedTriggerId === triggerId) {
      setExpandedTriggerId(null);
    } else {
      setExpandedTriggerId(triggerId);
      loadExecutions(triggerId);
    }
  };

  const copyWebhookUrl = async (token: string) => {
    const url = webhookTriggersApi.getWebhookUrl(token);
    try {
      await navigator.clipboard.writeText(url);
      setCopiedWebhookToken(token);
      toast.success(t('Webhook URL copied to clipboard'));
      setTimeout(() => setCopiedWebhookToken(null), 2000);
    } catch (error) {
      toast.error(t('Failed to copy URL'));
    }
  };

  // Block types for the flow builder
  type BlockType = 'event' | 'condition' | 'action';

  interface FlowBlock {
    id: string;
    type: BlockType;
    blockType: string; // e.g., 'change_detected', 'notification', 'ai_session'
    config: any;
    parentId?: string; // Parent block ID for tree structure
    children?: string[]; // Child block IDs (for parallel branches)
  }

  // Flow blocks state - replaces the fixed 3-step approach
  const [flowBlocks, setFlowBlocks] = useState<FlowBlock[]>([
    { id: 'block_1', type: 'event', blockType: 'change_detected', config: {} }
  ]);

  // Opening the modal — or re-pointing it at another target/workflow — puts it
  // back on the list view with an empty form. This runs in RENDER off that edge
  // rather than in an effect: setState inside an effect body only lands after
  // paint, so the previous session's half-filled form would flash first
  // (`react-hooks/set-state-in-effect`). It has to sit below the flowBlocks state
  // because `resetForm` calls `setFlowBlocks` and would otherwise read it from
  // the temporal dead zone. Fetching stays in the effect — that IS a side effect.
  const openKey = isOpen ? `${targetId ?? ''}|${workflowId ?? ''}` : null;
  const [openedFor, setOpenedFor] = useState<string | null>(null);
  if (openedFor !== openKey) {
    setOpenedFor(openKey);
    if (openKey !== null) {
      setIsCreating(false);
      setEditingTrigger(null);
      setLoading(true);
      resetForm();
    }
  }

  useEffect(() => {
    if (!isOpen) return;
    let ignore = false;
    fetchModalData().then(
      (data) => { if (!ignore) applyModalData(data); },
      (error) => { if (!ignore) reportLoadFailure(error); },
    );
    return () => { ignore = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen, targetId, workflowId]);

  // Auto-add return_data block when workflow has extract steps
  const autoReturnAdded = React.useRef(false);
  React.useEffect(() => {
    if (autoAddReturnBlock && !autoReturnAdded.current) {
      const hasReturnBlock = flowBlocks.some(b => b.blockType === 'return_data');
      if (!hasReturnBlock && flowBlocks.length > 0) {
        // Find the last block (deepest leaf) to add return_data as its child
        const lastBlock = flowBlocks[flowBlocks.length - 1];
        setFlowBlocks(prev => [...prev, {
          id: `block_return_${Date.now()}`,
          type: 'action' as BlockType,
          blockType: 'return_data',
          config: {},
          parentId: lastBlock.id,
        }]);
        autoReturnAdded.current = true;
      }
    }
  }, [autoAddReturnBlock, flowBlocks.length]);

  // Helper to get direct children of a block
  const getChildBlocks = (parentId: string): FlowBlock[] => {
    return flowBlocks.filter(b => b.parentId === parentId);
  };


  // Get available blocks based on what's already in the flow
  // insertIndex is the index of the block AFTER which we're inserting
  const getAvailableBlocks = (parentBlockId: string): Array<{ type: BlockType; blockType: string; label: string; icon: any; color: string; description: string; priority: number }> => {
    const blocks: Array<{ type: BlockType; blockType: string; label: string; icon: any; color: string; description: string; priority: number }> = [];

    // Get the parent block (the block we're adding a child to)
    const parentBlock = flowBlocks.find(b => b.id === parentBlockId);
    if (!parentBlock) return blocks;

    // Collect all ancestor blocks (including parent) to understand context
    const getAncestorChain = (blockId: string): FlowBlock[] => {
      const result: FlowBlock[] = [];
      let current = flowBlocks.find(b => b.id === blockId);
      while (current) {
        result.push(current);
        if (current.parentId) {
          current = flowBlocks.find(b => b.id === current!.parentId);
        } else {
          break;
        }
      }
      return result;
    };

    const blocksBeforeInsertion = getAncestorChain(parentBlockId);
    const lastBlock = parentBlock;

    // Check what actions exist in the ancestor chain
    const hasAiSessionAction = blocksBeforeInsertion.some(b => b.type === 'action' && b.blockType === 'ai_session');
    const hasWorkflowAction = blocksBeforeInsertion.some(b => b.type === 'action' && b.blockType === 'workflow');

    // Check if the parent block is an action that needs a completion event
    const lastIsAiSession = lastBlock?.type === 'action' && lastBlock?.blockType === 'ai_session';
    const lastIsWorkflow = lastBlock?.type === 'action' && lastBlock?.blockType === 'workflow';

    // Priority: Higher = shown first
    // If last block is AI Session, show AI Session Completed first
    if (lastIsAiSession) {
      blocks.push({
        type: 'event',
        blockType: 'ai_session_completed',
        label: t('Wait for AI Session to Complete'),
        icon: CheckCircleIcon,
        color: 'gray',
        description: t('Continue when the AI session finishes'),
        priority: 100
      });
    }

    // If last block is Workflow, show Workflow Completed first
    if (lastIsWorkflow) {
      blocks.push({
        type: 'event',
        blockType: 'workflow_completed',
        label: t('Wait for Workflow to Complete'),
        icon: CheckCircleIcon,
        color: 'gray',
        description: t('Continue when the workflow finishes'),
        priority: 100
      });
    }

    // If there's an AI Session action earlier (not the last one), also offer completion event
    if (hasAiSessionAction && !lastIsAiSession) {
      blocks.push({
        type: 'event',
        blockType: 'ai_session_completed',
        label: t('AI Session Completed'),
        icon: CheckCircleIcon,
        color: 'gray',
        description: t('When an AI session above finishes'),
        priority: 50
      });
    }

    // If there's a Workflow action earlier (not the last one), also offer completion event
    if (hasWorkflowAction && !lastIsWorkflow) {
      blocks.push({
        type: 'event',
        blockType: 'workflow_completed',
        label: t('Workflow Completed'),
        icon: CheckCircleIcon,
        color: 'gray',
        description: t('When a workflow above finishes'),
        priority: 50
      });
    }

    // If last block is workflow_completed, offer Return Data (returns extracted data to webhook caller)
    const lastIsWorkflowCompleted = lastBlock?.type === 'event' && lastBlock?.blockType === 'workflow_completed';
    if (lastIsWorkflowCompleted) {
      blocks.push({
        type: 'action',
        blockType: 'return_data',
        label: t('Return Data to Caller'),
        icon: ArrowUturnLeftIcon,
        color: 'gray',
        description: t('Return extracted data to the webhook/API caller'),
        priority: 90
      });
    }

    // Actions - always available
    blocks.push(
      { type: 'action', blockType: 'notification', label: t('Send Notification'), icon: BellIcon, color: 'gray', description: t('Send email, push, or webhook'), priority: 30 },
      { type: 'action', blockType: 'ai_session', label: t('Run AI Session'), icon: CpuChipIcon, color: 'gray', description: t('Execute an AI workflow'), priority: 30 },
      { type: 'action', blockType: 'workflow', label: t('Run Workflow'), icon: Cog6ToothIcon, color: 'gray', description: t('Execute an automation workflow'), priority: 30 },
    );

    // Condition - can add after event or action
    if (lastBlock && (lastBlock.type === 'event' || lastBlock.type === 'action')) {
      blocks.push({
        type: 'condition',
        blockType: 'condition',
        label: t('Add Condition'),
        icon: FunnelIcon,
        color: 'gray',
        description: t('Filter based on data values'),
        priority: 20
      });
    }

    // Sort by priority (highest first)
    return blocks.sort((a, b) => b.priority - a.priority);
  };

  // Add a block as a child of a parent block
  const addBlock = (block: { type: BlockType; blockType: string }, parentBlockId: string) => {
    let config: any = {};

    // Default config for condition blocks
    if (block.type === 'condition') {
      config = { field: '', operator: 'contains', value: '' };
    }

    // Helper to get ancestor chain for a block
    const getAncestorChain = (blockId: string): FlowBlock[] => {
      const result: FlowBlock[] = [];
      let current = flowBlocks.find(b => b.id === blockId);
      while (current) {
        result.push(current);
        if (current.parentId) {
          current = flowBlocks.find(b => b.id === current!.parentId);
        } else {
          break;
        }
      }
      return result;
    };

    // Auto-link completion events to the preceding action block
    if (block.blockType === 'ai_session_completed') {
      // Find the most recent AI Session action block in ancestor chain
      const ancestors = getAncestorChain(parentBlockId);
      for (const b of ancestors) {
        if (b.type === 'action' && b.blockType === 'ai_session' && b.config.session_ids?.length > 0) {
          // Auto-link to the AI session from the preceding action
          config.ai_session_id = b.config.session_ids[0];
          config.linked_to_block = b.id;
          break;
        }
      }
    }

    if (block.blockType === 'workflow_completed') {
      // Find the most recent Workflow action block in ancestor chain
      const ancestors = getAncestorChain(parentBlockId);
      for (const b of ancestors) {
        if (b.type === 'action' && b.blockType === 'workflow' && b.config.workflow_id) {
          // Auto-link to the workflow from the preceding action
          config.workflow_id = b.config.workflow_id;
          config.linked_to_block = b.id;
          break;
        }
      }
    }

    const newBlock: FlowBlock = {
      id: `block_${Date.now()}`,
      type: block.type,
      blockType: block.blockType,
      config,
      parentId: parentBlockId, // Link to parent block for tree structure
    };
    // Add the new block to the array
    setFlowBlocks([...flowBlocks, newBlock]);
  };

  // Remove a block and all its descendants
  const removeBlock = (blockId: string) => {
    const block = flowBlocks.find(b => b.id === blockId);
    if (!block) return;

    // Can't remove the root block if it's the only one
    if (!block.parentId && flowBlocks.filter(b => !b.parentId).length === 1) return;

    // Collect all descendant IDs recursively
    const getDescendantIds = (parentId: string): string[] => {
      const children = flowBlocks.filter(b => b.parentId === parentId);
      const ids: string[] = [];
      for (const child of children) {
        ids.push(child.id);
        ids.push(...getDescendantIds(child.id));
      }
      return ids;
    };

    const idsToRemove = new Set([blockId, ...getDescendantIds(blockId)]);
    const newBlocks = flowBlocks.filter(b => !idsToRemove.has(b.id));
    setFlowBlocks(newBlocks);
  };

  // Update a block's config by block ID
  const updateBlockConfig = (blockId: string, config: any) => {
    const newBlocks = flowBlocks.map(b =>
      b.id === blockId ? { ...b, config } : b
    );
    setFlowBlocks(newBlocks);
  };

  // Get block color
  const getBlockColor = (block: FlowBlock) => {
    if (block.type === 'event') return 'gray';
    if (block.type === 'condition') return 'gray';
    if (block.blockType === 'notification') return 'gray';
    if (block.blockType === 'ai_session') return 'gray';
    if (block.blockType === 'workflow') return 'gray';
    if (block.blockType === 'return_data') return 'gray';
    return 'gray';
  };

  // Get block icon
  const getBlockIcon = (block: FlowBlock) => {
    if (block.blockType === 'change_detected') return BoltIcon;
    if (block.blockType === 'ai_session_started') return CpuChipIcon;
    if (block.blockType === 'ai_session_completed') return CheckCircleIcon;
    if (block.blockType === 'workflow_started') return Cog6ToothIcon;
    if (block.blockType === 'workflow_completed') return CheckCircleIcon;
    if (block.blockType === 'condition') return FunnelIcon;
    if (block.blockType === 'notification') return BellIcon;
    if (block.blockType === 'ai_session') return CpuChipIcon;
    if (block.blockType === 'workflow') return Cog6ToothIcon;
    if (block.blockType === 'return_data') return ArrowUturnLeftIcon;
    return BoltIcon;
  };

  // Get block label
  const getBlockLabel = (block: FlowBlock) => {
    const labels: Record<string, string> = {
      'change_detected': t('Content Change'),
      'ai_session_started': t('AI Session Started'),
      'ai_session_completed': t('AI Session Completed'),
      'workflow_started': t('Workflow Started'),
      'workflow_completed': t('Workflow Completed'),
      'condition': t('Condition'),
      'notification': t('Notification'),
      'ai_session': t('AI Session'),
      'workflow': t('Workflow'),
      'return_data': t('Return Data to Caller'),
      'webhook_received': t('Webhook Received'),
    };
    return labels[block.blockType] || block.blockType;
  };

  // Update block type (for first event block)
  const updateBlockType = (blockId: string, blockType: string) => {
    const newBlocks = flowBlocks.map(b =>
      b.id === blockId ? { ...b, blockType, config: {} } : b
    );
    setFlowBlocks(newBlocks);
  };

  // Helper to get ancestor chain for a block
  const getBlockAncestorChain = (blockId: string): FlowBlock[] => {
    const result: FlowBlock[] = [];
    let current = flowBlocks.find(b => b.id === blockId);
    while (current) {
      result.push(current);
      if (current.parentId) {
        current = flowBlocks.find(b => b.id === current!.parentId);
      } else {
        break;
      }
    }
    return result;
  };

  // Render expandable placeholder hints for a specific block's context
  const renderBlockPlaceholderHints = (block: FlowBlock) => {
    const ancestors = getBlockAncestorChain(block.id);
    const hasChangeDetected = ancestors.some(b => b.blockType === 'change_detected');
    const hasAiSession = ancestors.some(b => b.blockType === 'ai_session' || b.blockType === 'ai_session_completed' || b.blockType === 'ai_session_started');
    const hasWorkflow = ancestors.some(b => b.blockType === 'workflow' || b.blockType === 'workflow_completed' || b.blockType === 'workflow_started');

    return (
      <Disclosure>
        {({ open }) => (
          <div className="mt-3 border-t border-border/50 pt-2">
            <Disclosure.Button className="w-full flex items-center justify-between text-[10px] text-tertiary hover:text-ink">
              <span className="flex items-center gap-1">
                <InformationCircleIcon className="h-3 w-3" />
                {t('Available Placeholders')}
              </span>
              <ChevronDownIcon className={clsx('h-3 w-3 transition-transform', open && 'rotate-180')} />
            </Disclosure.Button>
            <Disclosure.Panel className="mt-2 space-y-2 text-[10px]">
              {/* Always available */}
              <div>
                <p className="text-tertiary mb-1">{t('General:')}</p>
                <div className="flex flex-wrap gap-1">
                  <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{now}}'}</code>
                  <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{now_time}}'}</code>
                  <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{now_date}}'}</code>
                  <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{target_name}}'}</code>
                  <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{target_url}}'}</code>
                  <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{trigger_name}}'}</code>
                </div>
              </div>

              {/* Change detection placeholders */}
              {hasChangeDetected && (
                <div>
                  <p className="text-ink mb-1">{t('From Content Change:')}</p>
                  <div className="flex flex-wrap gap-1">
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{content}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{diff_snippet}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{extracted.price}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{extracted.status}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{extracted.*}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{selector_name}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{change_detected_at}}'}</code>
                  </div>
                </div>
              )}

              {/* AI Session placeholders */}
              {hasAiSession && (
                <div>
                  <p className="text-ink mb-1">{t('From AI Session:')}</p>
                  <div className="flex flex-wrap gap-1">
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{session_id}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{session_name}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{session_status}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{success}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{session_steps_taken}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{session_duration_seconds}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{session_error}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{session_started_at}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{session_completed_at}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{ai_result.*}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{ai_extracted.*}}'}</code>
                  </div>
                </div>
              )}

              {/* Workflow placeholders */}
              {hasWorkflow && (
                <div>
                  <p className="text-ink mb-1">{t('From Workflow:')}</p>
                  <div className="flex flex-wrap gap-1">
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{workflow_id}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{workflow_name}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{workflow_status}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{success}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{workflow_steps_completed}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{workflow_duration_seconds}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{workflow_error}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{workflow_started_at}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{workflow_completed_at}}'}</code>
                    <code className="bg-hover text-ink px-1 py-0.5 rounded">{'{{result.*}}'}</code>
                  </div>
                </div>
              )}

              {/* Chain info */}
              <p className="text-tertiary italic">
                {t('Data from all ancestor blocks flows down to this point.')}
              </p>
            </Disclosure.Panel>
          </div>
        )}
      </Disclosure>
    );
  };

  // Render block content
  const renderBlockContent = (block: FlowBlock) => {
    const isFirstBlock = !block.parentId;

    // Event blocks
    if (block.type === 'event') {
      // First block shows event type selector + filter
      if (isFirstBlock) {
        return (
          <div className="space-y-3">
            {/* Event Type Selector */}
            <div>
              <label className="block text-xs text-secondary mb-2">{t('Event Type')}</label>
              <div className="grid grid-cols-2 gap-2">
                {EVENT_TYPES.map(et => (
                  <button
                    key={et.value}
                    type="button"
                    onClick={() => updateBlockType(block.id, et.value)}
                    className={clsx(
                      'p-2 rounded-lg border text-left transition-all',
                      block.blockType === et.value
                        ? 'border-border bg-hover ring-1 ring-ink/20'
                        : 'border-border bg-hover/50 hover:border-border-strong'
                    )}
                  >
                    <div className="text-xs font-medium text-ink">{t(et.label)}</div>
                  </button>
                ))}
              </div>
            </div>

            {/* Filter based on selected event type */}
            {block.blockType === 'change_detected' && (
              <div className="space-y-3">
                {/* Target selector - shown when trigger is standalone (no pre-set targetId) */}
                {!targetId && (
                  <div>
                    <label className="block text-xs text-secondary mb-1">{t('Target')}</label>
                    <Select<number>
                      value={block.config.target_id || undefined}
                      onChange={async (v) => {
                        const tid = v || null;
                        updateBlockConfig(block.id, { ...block.config, target_id: tid, selector_id: null });
                        // Load selectors for selected target
                        if (tid) {
                          try {
                            const sels = await selectorsApi.listForTarget(tid);
                            setSelectors(sels);
                          } catch { setSelectors([]); }
                        } else {
                          setSelectors([]);
                        }
                      }}
                      placeholder={t('Select a target...')}
                      options={allTargets.map(tgt => ({ value: Number(tgt.id), label: tgt.url }))}
                      className="w-full"
                    />
                  </div>
                )}
                {/* Selector filter */}
                <div>
                  <label className="block text-xs text-secondary mb-1">{t('Filter by selector (optional)')}</label>
                  <Select<number>
                    value={block.config.selector_id || 0}
                    onChange={v => updateBlockConfig(block.id, { ...block.config, selector_id: v === 0 ? null : v })}
                    disabled={selectors.length === 0 && !targetId}
                    placeholder={t('Any selector change')}
                    options={[
                      { value: 0, label: t('Any selector change') },
                      ...selectors.map(s => ({ value: Number(s.id), label: s.name })),
                    ]}
                    className="w-full"
                  />
                  {selectors.length === 0 && !targetId && !block.config.target_id && (
                    <p className="text-[10px] text-tertiary mt-1">{t('Select a target first to see available selectors')}</p>
                  )}
                </div>
              </div>
            )}
            {(block.blockType === 'ai_session_completed' || block.blockType === 'ai_session_started') && (
              <div>
                <label className="block text-xs text-secondary mb-1">{t('Filter by AI session (optional)')}</label>
                <Select<number>
                  value={block.config.ai_session_id || 0}
                  onChange={v => updateBlockConfig(block.id, { ...block.config, ai_session_id: v === 0 ? null : v })}
                  placeholder={t('Any AI session')}
                  options={[
                    { value: 0, label: t('Any AI session') },
                    ...availableSessions.map(s => ({ value: Number(s.id), label: s.name })),
                  ]}
                  className="w-full"
                />
              </div>
            )}
            {(block.blockType === 'workflow_completed' || block.blockType === 'workflow_started') && (
              <div>
                <label className="block text-xs text-secondary mb-1">{t('Filter by workflow (optional)')}</label>
                <Select<number>
                  value={block.config.workflow_id || 0}
                  onChange={v => updateBlockConfig(block.id, { ...block.config, workflow_id: v === 0 ? null : v })}
                  placeholder={t('Any workflow')}
                  options={[
                    { value: 0, label: t('Any workflow') },
                    ...workflows.map(w => ({ value: Number(w.id), label: w.name, icon: w.is_installed ? <LockClosedIcon className="h-3.5 w-3.5 text-tertiary" aria-hidden="true" /> : undefined })),
                  ]}
                  className="w-full"
                />
              </div>
            )}
            {block.blockType === 'webhook_received' && (
              <div className="space-y-3">
                <div className="p-3 bg-hover border border-border rounded-lg">
                  <div className="flex items-center gap-2 mb-2">
                    <LinkIcon className="h-4 w-4 text-secondary" />
                    <span className="text-xs font-medium text-secondary">{t('Webhook Entry Point')}</span>
                  </div>
                  <p className="text-xs text-secondary mb-3">
                    {t('External systems can POST to this URL to trigger this workflow. The webhook payload will be available as variables.')}
                  </p>
                  {block.config.webhook_trigger_token ? (
                    <div className="space-y-2">
                      {/* Normal URL */}
                      <div>
                        <div className="text-[10px] text-tertiary mb-0.5">{t('Fire & forget:')}</div>
                        <div className="flex items-center gap-2">
                          <code className="flex-1 text-xs text-ink bg-surface px-2 py-1.5 rounded truncate">
                            {webhookTriggersApi.getWebhookUrl(block.config.webhook_trigger_token)}
                          </code>
                          <button type="button" onClick={() => copyWebhookUrl(block.config.webhook_trigger_token)}
                            className="p-1.5 hover:bg-hover rounded transition-colors" title={t('Copy URL')}>
                            {copiedWebhookToken === block.config.webhook_trigger_token ? (
                              <ClipboardDocumentCheckIcon className="h-4 w-4 text-ink" />
                            ) : (
                              <ClipboardIcon className="h-4 w-4 text-secondary" />
                            )}
                          </button>
                        </div>
                      </div>
                      {/* Wait URL */}
                      <div>
                        <div className="text-[10px] text-ink mb-0.5">{t('Wait for extracted data:')}</div>
                        <div className="flex items-center gap-2">
                          <code className="flex-1 text-xs text-ink bg-surface border border-border px-2 py-1.5 rounded truncate">
                            {webhookTriggersApi.getWebhookUrl(block.config.webhook_trigger_token)}?wait=true
                          </code>
                          <button type="button" onClick={() => {
                            navigator.clipboard.writeText(webhookTriggersApi.getWebhookUrl(block.config.webhook_trigger_token) + '?wait=true');
                            toast.success(t('Copied wait URL'));
                          }}
                            className="p-1.5 hover:bg-hover rounded transition-colors" title={t('Copy wait URL')}>
                            <ClipboardIcon className="h-4 w-4 text-ink" />
                          </button>
                        </div>
                      </div>
                      <p className="text-xs text-tertiary">{t('Token: {{token}}...', { token: block.config.webhook_trigger_token.substring(0, 8) })}</p>
                    </div>
                  ) : (
                    <div className="space-y-3">
                      <p className="text-xs text-secondary">
                        {t('Webhook URL will be generated when you save the trigger.')}
                      </p>

                      {/* Custom URL path */}
                      <div>
                        <label className="block text-xs text-secondary mb-1">{t('Custom URL path (optional)')}</label>
                        <div className="flex items-center gap-0">
                          <span className="px-2 py-2 bg-hover border border-r-0 border-border rounded-l-lg text-xs text-tertiary whitespace-nowrap">
                            /api/v1/webhooks/
                          </span>
                          <input
                            type="text"
                            value={block.config.custom_path || ''}
                            onChange={e => updateBlockConfig(block.id, { ...block.config, custom_path: e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, '') })}
                            className="flex-1 px-3 py-2 bg-surface border border-border rounded-r-lg text-sm text-ink placeholder-tertiary"
                            placeholder="googlepoptimes"
                          />
                        </div>
                        {block.config.custom_path && (
                          <p className="text-[10px] text-ink mt-1">
                            {t('Clients will call: POST {{url}}', { url: `${window.location.origin}/api/v1/webhooks/${block.config.custom_path}` })}
                          </p>
                        )}
                      </div>

                      {/* Secret */}
                      <div>
                        <label className="block text-xs text-secondary mb-1">{t('Webhook Secret (optional)')}</label>
                        <input
                          type="text"
                          value={block.config.webhook_secret || ''}
                          onChange={e => updateBlockConfig(block.id, { ...block.config, webhook_secret: e.target.value })}
                          className="w-full px-3 py-2 bg-surface border border-border rounded-lg text-sm text-ink placeholder-tertiary"
                          placeholder={t('For HMAC signature verification')}
                        />
                      </div>
                    </div>
                  )}
                </div>
                <div className="text-xs text-tertiary">
                  <p className="font-medium text-secondary mb-1">{t('Available payload variables:')}</p>
                  <code className="block bg-surface p-2 rounded text-secondary">
                    {`{{payload.field_name}}`} - {t('Access webhook JSON payload')}
                  </code>
                </div>
              </div>
            )}
          </div>
        );
      }

      // Non-first event blocks (completion events) - smart auto-linking with status conditions
      if (block.blockType === 'ai_session_completed' || block.blockType === 'ai_session_started') {
        // Find linked AI session block (auto-linked from previous block)
        const linkedBlockId = block.config.linked_to_block;
        const linkedBlock = linkedBlockId ? flowBlocks.find(b => b.id === linkedBlockId) : null;
        const linkedSessionId = linkedBlock?.config?.session_ids?.[0];
        const linkedSession = linkedSessionId ? availableSessions.find(s => s.id === linkedSessionId) : null;

        return (
          <div className="space-y-3">
            {/* Show auto-linked session info */}
            {linkedSession ? (
              <div className="flex items-center gap-2 text-sm text-ink bg-hover px-3 py-2 rounded-lg border border-border">
                <CpuChipIcon className="h-4 w-4" />
                <span>{t('Linked to:')} <strong>{linkedSession.name}</strong></span>
              </div>
            ) : (
              <div className="space-y-2">
                <label className="block text-xs text-secondary">{t('Filter by AI session (optional)')}</label>
                <Select<number>
                  value={block.config.ai_session_id || 0}
                  onChange={v => updateBlockConfig(block.id, { ...block.config, ai_session_id: v === 0 ? null : v })}
                  placeholder={t('Any AI session')}
                  options={[
                    { value: 0, label: t('Any AI session') },
                    ...availableSessions.map(s => ({ value: Number(s.id), label: s.name })),
                  ]}
                  className="w-full"
                />
              </div>
            )}

            {/* Status condition - only for completion events */}
            {block.blockType === 'ai_session_completed' && (
              <div className="space-y-2">
                <label className="block text-xs text-secondary">{t('When status is:')}</label>
                <div className="flex gap-2">
                  {[
                    { value: 'any', label: t('Always'), color: 'bg-active' },
                    { value: 'success', label: t('Success Only'), color: 'bg-green-100' },
                    { value: 'error', label: t('Error Only'), color: 'bg-red-100' },
                  ].map(opt => (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() => updateBlockConfig(block.id, { ...block.config, status_condition: opt.value })}
                      className={clsx(
                        'px-3 py-1.5 rounded-lg text-xs font-medium transition-all',
                        (block.config.status_condition || 'any') === opt.value
                          ? `${opt.color} text-ink ring-2 ring-white/20`
                          : 'bg-hover text-secondary hover:bg-active'
                      )}
                    >
                      {t(opt.label)}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        );
      }
      if (block.blockType === 'workflow_completed' || block.blockType === 'workflow_started') {
        // Find linked workflow block (auto-linked from previous block)
        const linkedBlockId = block.config.linked_to_block;
        const linkedBlock = linkedBlockId ? flowBlocks.find(b => b.id === linkedBlockId) : null;
        const linkedWorkflowId = linkedBlock?.config?.workflow_id;
        const linkedWorkflow = linkedWorkflowId ? workflows.find(w => w.id === linkedWorkflowId) : null;

        return (
          <div className="space-y-3">
            {/* Show auto-linked workflow info */}
            {linkedWorkflow ? (
              <div className="flex items-center gap-2 text-sm text-ink bg-hover px-3 py-2 rounded-lg border border-border">
                <Cog6ToothIcon className="h-4 w-4" />
                <span>{t('Linked to:')} <strong>{linkedWorkflow.name}</strong></span>
              </div>
            ) : (
              <div className="space-y-2">
                <label className="block text-xs text-secondary">{t('Filter by workflow (optional)')}</label>
                <Select<number>
                  value={block.config.workflow_id || 0}
                  onChange={v => updateBlockConfig(block.id, { ...block.config, workflow_id: v === 0 ? null : v })}
                  placeholder={t('Any workflow')}
                  options={[
                    { value: 0, label: t('Any workflow') },
                    ...workflows.map(w => ({ value: Number(w.id), label: w.name, icon: w.is_installed ? <LockClosedIcon className="h-3.5 w-3.5 text-tertiary" aria-hidden="true" /> : undefined })),
                  ]}
                  className="w-full"
                />
              </div>
            )}

            {/* Status condition - only for completion events */}
            {block.blockType === 'workflow_completed' && (
              <div className="space-y-2">
                <label className="block text-xs text-secondary">{t('When status is:')}</label>
                <div className="flex gap-2">
                  {[
                    { value: 'any', label: t('Always'), color: 'bg-active' },
                    { value: 'success', label: t('Success Only'), color: 'bg-green-100' },
                    { value: 'error', label: t('Error Only'), color: 'bg-red-100' },
                  ].map(opt => (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() => updateBlockConfig(block.id, { ...block.config, status_condition: opt.value })}
                      className={clsx(
                        'px-3 py-1.5 rounded-lg text-xs font-medium transition-all',
                        (block.config.status_condition || 'any') === opt.value
                          ? `${opt.color} text-ink ring-2 ring-white/20`
                          : 'bg-hover text-secondary hover:bg-active'
                      )}
                    >
                      {t(opt.label)}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        );
      }
    }

    // Condition block - improved UX
    if (block.type === 'condition') {
      // Get available fields based on what blocks are before this one
      const blockIndex = flowBlocks.findIndex(b => b.id === block.id);
      const blocksBeforeThis = flowBlocks.slice(0, blockIndex);
      const hasChangeDetected = blocksBeforeThis.some(b => b.blockType === 'change_detected');
      const hasAiSession = blocksBeforeThis.some(b => b.blockType === 'ai_session' || b.blockType === 'ai_session_completed');
      const hasWorkflow = blocksBeforeThis.some(b => b.blockType === 'workflow' || b.blockType === 'workflow_completed');

      // Quick condition presets
      const presets = [
        hasChangeDetected && { label: t('Price < $100'), field: 'extracted.price', operator: 'lt', value: '100' },
        hasChangeDetected && { label: t('Contains "in stock"'), field: 'content', operator: 'contains', value: 'in stock' },
        hasChangeDetected && { label: t('Status changed'), field: 'extracted.status', operator: 'changed', value: '' },
        hasAiSession && { label: t('AI succeeded'), field: 'success', operator: 'equals', value: 'true' },
        hasAiSession && { label: t('AI failed'), field: 'success', operator: 'equals', value: 'false' },
        hasAiSession && { label: t('Has error'), field: 'error', operator: 'not_equals', value: '' },
        hasWorkflow && { label: t('Workflow succeeded'), field: 'success', operator: 'equals', value: 'true' },
      ].filter(Boolean) as Array<{ label: string; field: string; operator: string; value: string }>;

      const applyPreset = (preset: typeof presets[0]) => {
        updateBlockConfig(block.id, { field: preset.field, operator: preset.operator, value: preset.value });
      };

      return (
        <div className="space-y-3">
          {/* Quick presets */}
          {presets.length > 0 && !block.config.field && (
            <div>
              <p className="text-xs text-tertiary mb-2">{t('Quick conditions:')}</p>
              <div className="flex flex-wrap gap-1">
                {presets.map((preset, i) => (
                  <button
                    key={i}
                    type="button"
                    onClick={() => applyPreset(preset)}
                    className="px-2 py-1 text-xs bg-hover hover:bg-active text-ink rounded transition-colors"
                  >
                    {t(preset.label)}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Custom condition builder */}
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-sm text-secondary">
              <span>{t('If')}</span>
              <Select
                value={block.config.field || ''}
                onChange={v => updateBlockConfig(block.id, { ...block.config, field: v })}
                size="sm"
                className="flex-1"
                placeholder={t('select a field...')}
                options={[
                  ...(hasChangeDetected ? [{
                    label: t('From Content Change'),
                    options: [
                      { value: 'content', label: t('Page Content') },
                      { value: 'diff_snippet', label: t('What Changed (diff)') },
                      { value: 'extracted.price', label: t('Extracted: Price') },
                      { value: 'extracted.status', label: t('Extracted: Status') },
                      { value: 'extracted.title', label: t('Extracted: Title') },
                      { value: 'extracted.count', label: t('Extracted: Count') },
                    ],
                  }] : []),
                  ...(hasAiSession ? [{
                    label: t('From AI Session'),
                    options: [
                      { value: 'success', label: t('AI Success (true/false)') },
                      { value: 'status', label: t('AI Status') },
                      { value: 'error', label: t('AI Error Message') },
                      { value: 'steps_taken', label: t('Steps Taken') },
                    ],
                  }] : []),
                  ...(hasWorkflow ? [{
                    label: t('From Workflow'),
                    options: [
                      { value: 'success', label: t('Workflow Success (true/false)') },
                      { value: 'status', label: t('Workflow Status') },
                      { value: 'error', label: t('Workflow Error') },
                    ],
                  }] : []),
                  { label: t('Custom'), options: [
                    { value: 'custom', label: t('Custom field path...') },
                  ]},
                ]}
              />
            </div>

            {block.config.field && (
              <div className="flex items-center gap-2 text-sm text-secondary">
                <Select
                  value={block.config.operator || 'contains'}
                  onChange={v => updateBlockConfig(block.id, { ...block.config, operator: v })}
                  size="sm"
                  className="w-36"
                  options={[
                    { label: t('Text'), options: [
                      { value: 'contains', label: t('contains') },
                      { value: 'not_contains', label: t('does not contain') },
                      { value: 'equals', label: t('equals exactly') },
                      { value: 'not_equals', label: t('does not equal') },
                      { value: 'matches', label: t('matches regex') },
                    ]},
                    { label: t('Numbers'), options: [
                      { value: 'gt', label: t('is greater than (>)') },
                      { value: 'gte', label: t('is at least (≥)') },
                      { value: 'lt', label: t('is less than (<)') },
                      { value: 'lte', label: t('is at most (≤)') },
                    ]},
                    { label: t('Special'), options: [
                      { value: 'changed', label: t('has changed') },
                      { value: 'exists', label: t('exists (not empty)') },
                    ]},
                  ]}
                />
                {!['changed', 'exists'].includes(block.config.operator) && (
                  <input
                    type="text"
                    value={block.config.value || ''}
                    onChange={e => updateBlockConfig(block.id, { ...block.config, value: e.target.value })}
                    placeholder={block.config.operator?.includes('gt') || block.config.operator?.includes('lt') ? t('number') : t('value')}
                    className="flex-1 px-2 py-1.5 bg-surface border border-border rounded text-ink"
                  />
                )}
              </div>
            )}

            {block.config.field === 'custom' && (
              <input
                type="text"
                value={block.config.customField || ''}
                onChange={e => updateBlockConfig(block.id, { ...block.config, field: e.target.value })}
                placeholder={t('e.g., extracted.my_field or result.data.value')}
                className="w-full px-2 py-1.5 bg-surface border border-border rounded text-sm text-ink"
              />
            )}
          </div>

          {/* Show current condition summary */}
          {block.config.field && block.config.field !== 'custom' && (
            <div className="text-xs text-tertiary bg-surface/50 px-2 py-1 rounded">
              {t('Condition:')} <span className="text-secondary">{block.config.field}</span>{' '}
              <span className="text-secondary">{block.config.operator || 'contains'}</span>{' '}
              {block.config.value && <span className="text-secondary">"{block.config.value}"</span>}
            </div>
          )}
        </div>
      );
    }

    // Action blocks
    if (block.type === 'action') {
      if (block.blockType === 'notification') {
        const showAdvanced = expandedAdvancedBlocks.has(block.id);
        const setShowAdvanced = (show: boolean) => {
          setExpandedAdvancedBlocks(prev => {
            const newSet = new Set(prev);
            if (show) {
              newSet.add(block.id);
            } else {
              newSet.delete(block.id);
            }
            return newSet;
          });
        };

        // Check all blocks in the flow (for parallel branches)
        const allBlocksInFlow = flowBlocks;
        const hasChangeDetected = allBlocksInFlow.some(b => b.blockType === 'change_detected');
        const hasAiSession = allBlocksInFlow.some(b => b.blockType === 'ai_session' || b.blockType === 'ai_session_completed' || b.blockType === 'ai_session_started');
        const hasWorkflow = allBlocksInFlow.some(b => b.blockType === 'workflow' || b.blockType === 'workflow_completed' || b.blockType === 'workflow_started');

        // Build example placeholders based on context
        const getExamplePlaceholder = () => {
          if (hasAiSession && hasChangeDetected) {
            return t('e.g., AI session {{p1}} for {{p2}}. Original change: {{p3}}. Session took {{p4}}s', { p1: '{{session_status}}', p2: '{{target_name}}', p3: '{{diff_snippet}}', p4: '{{session_duration_seconds}}' });
          }
          if (hasAiSession) {
            return t('e.g., AI session {{p1}} {{p2}}. Took {{p3}} steps in {{p4}}s', { p1: '{{session_name}}', p2: '{{session_status}}', p3: '{{session_steps_taken}}', p4: '{{session_duration_seconds}}' });
          }
          if (hasWorkflow && hasChangeDetected) {
            return t('e.g., Workflow {{p1}} {{p2}}. Triggered by: {{p3}}', { p1: '{{workflow_name}}', p2: '{{workflow_status}}', p3: '{{diff_snippet}}' });
          }
          if (hasWorkflow) {
            return t('e.g., Workflow {{p1}} completed with status: {{p2}}', { p1: '{{workflow_name}}', p2: '{{workflow_status}}' });
          }
          if (hasChangeDetected) {
            return t('e.g., Price changed to {{p1}} on {{p2}} at {{p3}}', { p1: '{{extracted.price}}', p2: '{{target_name}}', p3: '{{now_time}}' });
          }
          return t('e.g., Alert at {{p1}} for {{p2}}', { p1: '{{now_time}}', p2: '{{target_name}}' });
        };

        return (
          <div className="space-y-3">
            {/* Title */}
            <input
              type="text"
              value={block.config.title || ''}
              onChange={e => updateBlockConfig(block.id, { ...block.config, title: e.target.value })}
              placeholder={t('Title (optional): e.g., Alert: Price Drop')}
              className="w-full px-2 py-1.5 bg-surface border border-border rounded text-sm text-ink"
            />
            {/* Message */}
            <textarea
              value={block.config.template || ''}
              onChange={e => updateBlockConfig(block.id, { ...block.config, template: e.target.value })}
              placeholder={t('Message: {{example}}', { example: getExamplePlaceholder() })}
              className="w-full px-2 py-1.5 bg-surface border border-border rounded text-sm text-ink resize-none"
              rows={2}
            />
            {/* Context-aware placeholder hints */}
            <div className="text-[10px] text-tertiary flex flex-wrap gap-x-2 gap-y-1">
              <span className="text-secondary">{t('Placeholders:')}</span>
              <code className="text-ink">{'{{now_time}}'}</code>
              <code className="text-ink">{'{{target_name}}'}</code>
              {hasChangeDetected && (
                <>
                  <code className="text-ink">{'{{extracted.*}}'}</code>
                  <code className="text-ink">{'{{diff_snippet}}'}</code>
                  <code className="text-ink">{'{{change_detected_at}}'}</code>
                </>
              )}
              {hasAiSession && (
                <>
                  <code className="text-ink">{'{{session_status}}'}</code>
                  <code className="text-ink">{'{{session_name}}'}</code>
                  <code className="text-ink">{'{{session_duration_seconds}}'}</code>
                  <code className="text-ink">{'{{session_steps_taken}}'}</code>
                  <code className="text-ink">{'{{session_error}}'}</code>
                  <code className="text-ink">{'{{ai_result.*}}'}</code>
                </>
              )}
              {hasWorkflow && (
                <>
                  <code className="text-ink">{'{{workflow_status}}'}</code>
                  <code className="text-ink">{'{{workflow_name}}'}</code>
                  <code className="text-ink">{'{{workflow_duration_seconds}}'}</code>
                  <code className="text-ink">{'{{workflow_error}}'}</code>
                </>
              )}
            </div>
            {/* Channels */}
            <div>
              <label className="text-xs font-medium text-secondary mb-2 block">{t('Notification Channels')}</label>
              <div className="flex flex-wrap gap-1.5">
                {[
                  { key: 'pushover', color: 'cyan' },
                  { key: 'email', color: 'amber' },
                  { key: 'twilio', color: 'green' },
                  { key: 'whatsapp', color: 'emerald' },
                  { key: 'webhook', color: 'purple' },
                ].map(channel => {
                  const { Icon, label } = channelMeta(channel.key);
                  const isSelected = (block.config.channels || []).includes(channel.key);
                  return (
                    <button
                      key={channel.key}
                      type="button"
                      onClick={() => {
                        const channels = block.config.channels || [];
                        const newChannels = isSelected
                          ? channels.filter((c: string) => c !== channel.key)
                          : [...channels, channel.key];
                        updateBlockConfig(block.id, { ...block.config, channels: newChannels });
                      }}
                      className={clsx(
                        'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border transition text-xs font-medium',
                        isSelected
                          ? clsx(
                              'border-transparent',
                              channel.color === 'cyan' && 'bg-hover text-ink',
                              channel.color === 'amber' && 'bg-hover text-secondary',
                              channel.color === 'green' && 'bg-hover text-ink',
                              channel.color === 'emerald' && 'bg-hover text-ink',
                              channel.color === 'purple' && 'bg-hover text-ink',
                            )
                          : 'border-border bg-hover/50 text-tertiary hover:border-border-strong hover:text-secondary'
                      )}
                    >
                      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
                      <span>{t(label)}</span>
                      {isSelected && <CheckIcon className="h-3 w-3" />}
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Recipients Selection */}
            {(block.config.channels || []).some((c: string) => ['pushover', 'email', 'twilio', 'whatsapp', 'signal'].includes(c)) && (() => {
              const selectedChannels = (block.config.channels || []) as string[];
              const selectedRecipients = (block.config.recipients || []) as string[];
              const channelsWithRecipients = selectedChannels.filter(ch =>
                availableRecipients.some(r => r.provider === ch)
              );
              const totalAvailable = availableRecipients.filter(r => selectedChannels.includes(r.provider)).length;

              return (
                <div className="space-y-2 p-2 bg-surface/30 rounded-lg border border-border">
                  <div className="flex items-center justify-between">
                    <label className="text-xs font-medium text-ink flex items-center gap-1.5">
                      <UserGroupIcon className="h-4 w-4 text-secondary" />
                      {t('Recipients')}
                      {selectedRecipients.length > 0 && (
                        <span className="px-1.5 py-0.5 rounded-full bg-hover text-ink text-[10px]">
                          {t('{{n}} selected', { n: selectedRecipients.length })}
                        </span>
                      )}
                    </label>
                    <div className="flex items-center gap-2">
                      {selectedRecipients.length > 0 && (
                        <button
                          type="button"
                          onClick={() => updateBlockConfig(block.id, { ...block.config, recipients: [] })}
                          className="text-[10px] text-tertiary hover:text-ink"
                        >
                          {t('Clear')}
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => {
                          const matchingRecipients = availableRecipients
                            .filter(r => selectedChannels.includes(r.provider))
                            .map(r => `${r.provider}:${r.id}`);
                          updateBlockConfig(block.id, { ...block.config, recipients: matchingRecipients });
                        }}
                        className="text-[10px] text-ink"
                      >
                        {t('Select All ({{n}})', { n: totalAvailable })}
                      </button>
                    </div>
                  </div>

                  {channelsWithRecipients.length === 0 ? (
                    <p className="text-[10px] text-tertiary italic py-2">
                      {t('No recipients configured for selected channels. Add them in Notifications settings.')}
                    </p>
                  ) : (
                    <div className="space-y-2">
                      {channelsWithRecipients.map((channel: string) => {
                        const channelRecipients = availableRecipients.filter(r => r.provider === channel);
                        const { Icon: ChannelIcon } = channelMeta(channel);
                        const channelLabel = {
                          pushover: { name: t('Pushover'), color: 'cyan' },
                          email: { name: t('Email'), color: 'amber' },
                          twilio: { name: t('SMS'), color: 'green' },
                          whatsapp: { name: t('WhatsApp'), color: 'emerald' },
                          signal: { name: t('Signal'), color: 'blue' },
                        }[channel] || { name: channel, color: 'zinc' };

                        const selectedInChannel = channelRecipients.filter(r =>
                          selectedRecipients.includes(`${r.provider}:${r.id}`)
                        ).length;

                        return (
                          <div key={channel} className="space-y-1">
                            <div className="flex items-center gap-2">
                              <ChannelIcon className="h-3 w-3 text-tertiary" aria-hidden="true" />
                              <span className={clsx(
                                'text-[10px] font-medium',
                                channelLabel.color === 'cyan' && 'text-ink',
                                channelLabel.color === 'amber' && 'text-secondary',
                                channelLabel.color === 'green' && 'text-ink',
                                channelLabel.color === 'emerald' && 'text-ink',
                                channelLabel.color === 'blue' && 'text-ink',
                              )}>
                                {channelLabel.name}
                              </span>
                              <span className="text-[9px] text-tertiary">
                                ({selectedInChannel}/{channelRecipients.length})
                              </span>
                            </div>
                            <div className="flex flex-wrap gap-1 pl-5">
                              {channelRecipients.map(recipient => {
                                const recipientKey = `${recipient.provider}:${recipient.id}`;
                                const isSelected = selectedRecipients.includes(recipientKey);

                                return (
                                  <button
                                    key={recipientKey}
                                    type="button"
                                    onClick={() => {
                                      const newRecipients = isSelected
                                        ? selectedRecipients.filter(r => r !== recipientKey)
                                        : [...selectedRecipients, recipientKey];
                                      updateBlockConfig(block.id, { ...block.config, recipients: newRecipients });
                                    }}
                                    className={clsx(
                                      'flex items-center gap-1.5 px-2 py-1 rounded border transition text-[11px]',
                                      isSelected
                                        ? clsx(
                                            'border-transparent',
                                            channelLabel.color === 'cyan' && 'bg-hover text-ink',
                                            channelLabel.color === 'amber' && 'bg-hover text-secondary',
                                            channelLabel.color === 'green' && 'bg-hover text-ink',
                                            channelLabel.color === 'emerald' && 'bg-hover text-ink',
                                            channelLabel.color === 'blue' && 'bg-hover text-ink',
                                          )
                                        : 'border-border bg-hover/50 text-tertiary hover:border-border-strong hover:text-secondary'
                                    )}
                                  >
                                    {isSelected && <CheckIcon className="h-3 w-3" />}
                                    <span>{recipient.name}</span>
                                  </button>
                                );
                              })}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {selectedRecipients.length === 0 && totalAvailable > 0 && (
                    <p className="text-[10px] text-secondary flex items-center gap-1 pt-1">
                      <ExclamationTriangleIcon className="h-3 w-3" />
                      {t('No recipients selected — will notify all enabled recipients')}
                    </p>
                  )}
                </div>
              );
            })()}

            {/* Advanced options toggle */}
            <button
              type="button"
              onClick={() => setShowAdvanced(!showAdvanced)}
              className="text-xs text-tertiary hover:text-ink flex items-center gap-1"
            >
              <ChevronDownIcon className={clsx('h-3 w-3 transition-transform', showAdvanced && 'rotate-180')} />
              {showAdvanced ? t('Hide advanced options') : t('Show advanced options')}
            </button>

            {showAdvanced && (
              <div className="space-y-3 p-2 bg-surface/50 rounded border border-border">
                {/* Pushover options */}
                {(block.config.channels || []).includes('pushover') && (
                  <div className="grid grid-cols-2 gap-2">
                    <div>
                      <label className="block text-xs text-tertiary mb-1">{t('Priority')}</label>
                      <Select<number>
                        value={block.config.priority ?? 0}
                        onChange={v => updateBlockConfig(block.id, { ...block.config, priority: v })}
                        size="sm"
                        className="w-full"
                        options={[
                          { value: -2, label: t('-2 (Silent)') },
                          { value: -1, label: t('-1 (Quiet)') },
                          { value: 0, label: t('0 (Normal)') },
                          { value: 1, label: t('1 (High)') },
                          { value: 2, label: t('2 (Emergency)') },
                        ]}
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-tertiary mb-1">{t('Sound')}</label>
                      <Select
                        value={block.config.sound || ''}
                        onChange={v => updateBlockConfig(block.id, { ...block.config, sound: v })}
                        size="sm"
                        className="w-full"
                        options={[
                          { value: '', label: t('Default') },
                          { value: 'pushover', label: t('Pushover') },
                          { value: 'cashregister', label: t('Cash Register') },
                          { value: 'magic', label: t('Magic') },
                          { value: 'siren', label: t('Siren') },
                          { value: 'spacealarm', label: t('Space Alarm') },
                          { value: 'alien', label: t('Alien') },
                          { value: 'vibrate', label: t('Vibrate Only') },
                          { value: 'none', label: t('None') },
                        ]}
                      />
                    </div>
                  </div>
                )}

                {/* Email subject */}
                {(block.config.channels || []).includes('email') && (
                  <div>
                    <label className="block text-xs text-tertiary mb-1">{t('Email Subject')}</label>
                    <input
                      type="text"
                      value={block.config.email_subject || ''}
                      onChange={e => updateBlockConfig(block.id, { ...block.config, email_subject: e.target.value })}
                      placeholder={t('e.g., [Writ] {{ph}} changed', { ph: '{{target_name}}' })}
                      className="w-full px-2 py-1 bg-surface border border-border rounded text-xs text-ink"
                    />
                  </div>
                )}

                {/* Webhook URL */}
                {(block.config.channels || []).includes('webhook') && (
                  <div>
                    <label className="block text-xs text-tertiary mb-1">{t('Webhook URL')}</label>
                    <input
                      type="url"
                      value={block.config.webhook_url || ''}
                      onChange={e => updateBlockConfig(block.id, { ...block.config, webhook_url: e.target.value })}
                      placeholder="https://your-webhook-endpoint.com/notify"
                      className="w-full px-2 py-1 bg-surface border border-border rounded text-xs text-ink"
                    />
                  </div>
                )}

                {/* URL attachment for Pushover */}
                <div>
                  <label className="block text-xs text-tertiary mb-1">{t('Link URL (optional)')}</label>
                  <input
                    type="text"
                    value={block.config.url || ''}
                    onChange={e => updateBlockConfig(block.id, { ...block.config, url: e.target.value })}
                    placeholder={t('{{ph}} or custom URL', { ph: '{{target_url}}' })}
                    className="w-full px-2 py-1 bg-surface border border-border rounded text-xs text-ink"
                  />
                </div>
              </div>
            )}

            {/* Block-specific placeholder hints */}
            {renderBlockPlaceholderHints(block)}
          </div>
        );
      }

      if (block.blockType === 'ai_session') {
        // Check all blocks in the flow for context
        const hasChangeDetected = flowBlocks.some(b => b.blockType === 'change_detected');

        return (
          <div className="space-y-3">
            {/* Session Selection */}
            <div>
              <label className="block text-xs font-medium text-secondary mb-1">{t('AI Session')}</label>
              <Select<number>
                value={block.config.session_ids?.[0] || undefined}
                onChange={v => updateBlockConfig(block.id, {
                  ...block.config,
                  session_ids: v ? [v] : []
                })}
                size="sm"
                className="w-full"
                placeholder={t('Select AI session...')}
                options={availableSessions.map(s => ({ value: Number(s.id), label: s.name }))}
              />
            </div>

            {/* User Context */}
            <div>
              <label className="block text-xs font-medium text-secondary mb-1">{t('User Context')}</label>
              <textarea
                value={block.config.user_context || ''}
                onChange={e => updateBlockConfig(block.id, { ...block.config, user_context: e.target.value })}
                placeholder={hasChangeDetected
                  ? t('Context for AI: e.g., Price changed to {{p1}}. Content: {{p2}}', { p1: '{{extracted.price}}', p2: '{{diff_snippet}}' })
                  : t('Context for AI (optional): e.g., Current time is {{ph}}', { ph: '{{now_time}}' })
                }
                className="w-full px-2 py-1.5 bg-surface border border-border rounded text-sm text-ink resize-none"
                rows={2}
              />
              {/* Placeholder hints */}
              <div className="mt-1 text-[10px] text-tertiary flex flex-wrap gap-x-2 gap-y-1">
                <span className="text-secondary">{t('Placeholders:')}</span>
                <code className="text-ink">{'{{target_name}}'}</code>
                <code className="text-ink">{'{{target_url}}'}</code>
                <code className="text-ink">{'{{now_time}}'}</code>
                {hasChangeDetected && (
                  <>
                    <code className="text-ink">{'{{extracted.*}}'}</code>
                    <code className="text-ink">{'{{diff_snippet}}'}</code>
                    <code className="text-ink">{'{{content}}'}</code>
                  </>
                )}
              </div>
            </div>

            {/* Form Data */}
            <div>
              <label className="block text-xs font-medium text-secondary mb-1">{t('Form Data (key=value)')}</label>
              <div className="space-y-1">
                {Object.entries(block.config.form_data || {}).map(([key, value], i) => (
                  <div key={i} className="flex gap-1">
                    <input
                      type="text"
                      value={key}
                      onChange={e => {
                        const newFormData = { ...block.config.form_data };
                        delete newFormData[key];
                        newFormData[e.target.value] = value;
                        updateBlockConfig(block.id, { ...block.config, form_data: newFormData });
                      }}
                      placeholder={t('Field name')}
                      className="flex-1 px-2 py-1 bg-surface border border-border rounded text-xs text-ink"
                    />
                    <input
                      type="text"
                      value={value as string}
                      onChange={e => {
                        const newFormData = { ...block.config.form_data, [key]: e.target.value };
                        updateBlockConfig(block.id, { ...block.config, form_data: newFormData });
                      }}
                      placeholder={t('Value or {{ph}}', { ph: '{{placeholder}}' })}
                      className="flex-1 px-2 py-1 bg-surface border border-border rounded text-xs text-ink"
                    />
                    <button
                      type="button"
                      onClick={() => {
                        const newFormData = { ...block.config.form_data };
                        delete newFormData[key];
                        updateBlockConfig(block.id, { ...block.config, form_data: newFormData });
                      }}
                      className="px-2 text-red-700 hover:text-red-700"
                    >
                      <TrashIcon className="h-3 w-3" />
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  onClick={() => {
                    const newFormData = { ...block.config.form_data, [`field_${Object.keys(block.config.form_data || {}).length + 1}`]: '' };
                    updateBlockConfig(block.id, { ...block.config, form_data: newFormData });
                  }}
                  className="text-xs text-ink flex items-center gap-1"
                >
                  <PlusIcon className="h-3 w-3" /> {t('Add Form Field')}
                </button>
              </div>
            </div>

            {/* Options Row */}
            <div className="flex flex-wrap gap-3">
              {/* Max Steps */}
              <div className="flex-1 min-w-[100px]">
                <label className="block text-xs font-medium text-secondary mb-1">{t('Max Steps')}</label>
                <NumberInput
                  value={block.config.max_steps || 20}
                  onChange={v => updateBlockConfig(block.id, { ...block.config, max_steps: v ?? 20 })}
                  min={1}
                  max={100}
                  size="sm"
                  className="w-full"
                />
              </div>

              {/* Max Actions */}
              <div className="flex-1 min-w-[100px]">
                <label className="block text-xs font-medium text-secondary mb-1">{t('Max Actions')}</label>
                <NumberInput
                  value={block.config.max_actions || 50}
                  onChange={v => updateBlockConfig(block.id, { ...block.config, max_actions: v ?? 50 })}
                  min={1}
                  max={200}
                  size="sm"
                  className="w-full"
                />
              </div>
            </div>

            {/* Toggles */}
            <div className="flex flex-wrap gap-4 pt-2">
              <Checkbox
                checked={block.config.secured || false}
                onChange={e => updateBlockConfig(block.id, { ...block.config, secured: e.target.checked })}
                label={<span className="flex items-center gap-1.5 text-xs text-ink"><LockClosedIcon className="h-3.5 w-3.5 text-tertiary" aria-hidden="true" />{t('Secured Mode')}</span>}
              />
              <Checkbox
                checked={block.config.use_vision !== false}
                onChange={e => updateBlockConfig(block.id, { ...block.config, use_vision: e.target.checked })}
                label={<span className="flex items-center gap-1.5 text-xs text-ink"><EyeIcon className="h-3.5 w-3.5 text-tertiary" aria-hidden="true" />{t('Use Vision')}</span>}
              />
            </div>

            {/* Block-specific placeholder hints */}
            {renderBlockPlaceholderHints(block)}
          </div>
        );
      }

      if (block.blockType === 'workflow') {
        return (
          <div className="space-y-3">
            <Select<number>
              value={block.config.workflow_id || undefined}
              onChange={v => updateBlockConfig(block.id, {
                ...block.config,
                workflow_id: v || null
              })}
              size="sm"
              className="w-full"
              placeholder={t('Select workflow...')}
              options={workflows.map(w => ({ value: Number(w.id), label: w.name, icon: w.is_installed ? <LockClosedIcon className="h-3.5 w-3.5 text-tertiary" aria-hidden="true" /> : undefined }))}
            />

            {/* Block-specific placeholder hints */}
            {renderBlockPlaceholderHints(block)}
          </div>
        );
      }
    }

    return null;
  };

  // Initialize flowBlocks from trigger data when editing
  useEffect(() => {
    if (editingTrigger) {
      // If trigger has blocks (new format), use them directly
      if (editingTrigger.blocks && editingTrigger.blocks.length > 0) {
        setFlowBlocks(editingTrigger.blocks as FlowBlock[]);
        return;
      }

      // Otherwise, reconstruct blocks from legacy format
      const blocks: FlowBlock[] = [];

      // Add event block
      blocks.push({
        id: 'block_event',
        type: 'event',
        blockType: formData.event_type,
        config: {
          selector_id: formData.target_selector_id,
          ai_session_id: formData.ai_session_id,
          workflow_id: formData.workflow_id,
        },
      });

      // Add condition blocks
      formData.conditions.forEach((cond, i) => {
        blocks.push({
          id: `block_cond_${i}`,
          type: 'condition',
          blockType: 'condition',
          config: cond,
        });
      });

      // Add action blocks
      formData.actions.forEach((action, i) => {
        blocks.push({
          id: `block_action_${i}`,
          type: 'action',
          blockType: action.type,
          config: action.config,
        });
      });

      setFlowBlocks(blocks);
    }
  }, [editingTrigger]);

  // Render the trigger form as a visual flow builder
  // Recursive function to render a block and its children
  const renderBlockNode = (block: FlowBlock, depth: number = 0): React.ReactNode => {
    const color = getBlockColor(block);
    const Icon = getBlockIcon(block);
    const label = getBlockLabel(block);
    const isFirst = !block.parentId;

    // Get children of this block
    const children = getChildBlocks(block.id);
    const actionChildren = children.filter(c => c.type === 'action');
    const otherChildren = children.filter(c => c.type !== 'action');
    const hasParallelActions = actionChildren.length > 1;

    return (
      <div key={block.id} className="flex flex-col">
        {/* The Block */}
        <div className={clsx(
          'relative p-4 rounded-xl border transition-all',
          color === 'blue' && 'bg-hover border-border',
          color === 'yellow' && 'bg-hover border-border',
          color === 'purple' && 'bg-hover border-border',
          color === 'green' && 'bg-hover border-border',
          color === 'gray' && 'bg-hover border-border',
        )}>
          {/* Block Header */}
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <div className={clsx(
                'w-7 h-7 rounded-lg flex items-center justify-center',
                color === 'blue' && 'bg-ink',
                color === 'yellow' && 'bg-hover',
                color === 'purple' && 'bg-ink',
                color === 'green' && 'bg-hover',
                color === 'gray' && 'bg-active',
              )}>
                <Icon className="h-4 w-4 text-ink" />
              </div>
              <span className="text-sm font-medium text-ink">{label}</span>
              <span className={clsx(
                'text-xs px-2 py-0.5 rounded-full',
                block.type === 'event' && 'bg-hover text-ink',
                block.type === 'condition' && 'bg-hover text-secondary',
                block.type === 'action' && 'bg-active text-ink',
              )}>
                {block.type}
              </span>
            </div>
            {!isFirst && (
              <button
                type="button"
                onClick={() => removeBlock(block.id)}
                className="p-1 text-tertiary hover:text-red-700 transition-colors"
              >
                <TrashIcon className="h-4 w-4" />
              </button>
            )}
          </div>

          {/* Block Content */}
          {renderBlockContent(block)}
        </div>

        {/* Add Block Button */}
        <div className="relative py-2 flex justify-center">
          {/* Vertical connector line */}
          <div className={clsx(
            'absolute left-1/2 top-0 bottom-0 w-0.5 -translate-x-1/2',
            color === 'blue' && 'bg-hover',
            color === 'yellow' && 'bg-hover',
            color === 'purple' && 'bg-hover',
            color === 'green' && 'bg-hover',
            color === 'gray' && 'bg-active',
          )} />

          <Disclosure>
            {({ open, close }) => (
              <div className="relative z-10">
                <Disclosure.Button className={clsx(
                  'w-8 h-8 rounded-full flex items-center justify-center transition-all',
                  'bg-hover hover:bg-active text-ink',
                  open && 'bg-active ring-2 ring-zinc-500'
                )}>
                  <PlusIcon className="h-5 w-5" />
                </Disclosure.Button>
                <Disclosure.Panel className="absolute left-10 top-0 z-20 w-72 bg-hover rounded-xl border border-border shadow-xl overflow-hidden">
                  {renderAddBlockMenu(block.id, close)}
                </Disclosure.Panel>
              </div>
            )}
          </Disclosure>
        </div>

        {/* Children - render non-action children first (conditions, events) */}
        {otherChildren.length > 0 && (
          <div className="space-y-1">
            {otherChildren.map(child => renderBlockNode(child, depth + 1))}
          </div>
        )}

        {/* Parallel Action Branches - render horizontally */}
        {hasParallelActions && (
          <div className="mt-2">
            {/* Parallel branch header */}
            <div className="flex items-center justify-center gap-2 mb-3">
              <div className="h-px flex-1 bg-gradient-to-r from-transparent via-zinc-600 to-transparent" />
              <div className="flex items-center gap-1 px-3 py-1 bg-hover rounded-full border border-border">
                <ArrowsRightLeftIcon className="h-3 w-3 text-ink" />
                <span className="text-xs text-secondary">{t('Parallel Branches')}</span>
                <span className="text-xs text-tertiary">({t('{{n}} running simultaneously', { n: actionChildren.length })})</span>
              </div>
              <div className="h-px flex-1 bg-gradient-to-r from-transparent via-zinc-600 to-transparent" />
            </div>

            {/* Horizontal grid of parallel branches */}
            <div className={clsx(
              'grid gap-4',
              actionChildren.length === 2 && 'grid-cols-2',
              actionChildren.length === 3 && 'grid-cols-3',
              actionChildren.length >= 4 && 'grid-cols-2 lg:grid-cols-4',
            )}>
              {actionChildren.map((child, branchIdx) => (
                <div key={child.id} className="relative">
                  {/* Branch label */}
                  <div className="flex items-center gap-1 mb-2">
                    <div className={clsx(
                      'w-5 h-5 rounded-full flex items-center justify-center text-xs font-bold',
                      branchIdx === 0 && 'bg-ink text-ink',
                      branchIdx === 1 && 'bg-hover text-ink',
                      branchIdx === 2 && 'bg-ink text-ink',
                      branchIdx >= 3 && 'bg-hover text-ink',
                    )}>
                      {branchIdx + 1}
                    </div>
                    <span className="text-xs text-tertiary">{t('Branch {{n}}', { n: branchIdx + 1 })}</span>
                  </div>
                  {/* Branch content */}
                  <div className={clsx(
                    'p-2 rounded-lg border-l-2',
                    branchIdx === 0 && 'border-border bg-hover',
                    branchIdx === 1 && 'border-border bg-hover',
                    branchIdx === 2 && 'border-border bg-hover',
                    branchIdx >= 3 && 'border-border bg-hover',
                  )}>
                    {renderBlockNode(child, depth + 1)}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Single action child - render normally */}
        {actionChildren.length === 1 && (
          <div className="space-y-1">
            {actionChildren.map(child => renderBlockNode(child, depth + 1))}
          </div>
        )}
      </div>
    );
  };

  // Render the add block menu
  const renderAddBlockMenu = (parentBlockId: string, close: () => void) => {
    const availableBlocks = getAvailableBlocks(parentBlockId);
    const recommendedBlocks = availableBlocks.filter(b => b.priority >= 100);
    const actionBlocks = availableBlocks.filter(b => b.type === 'action');
    const otherBlocks = availableBlocks.filter(b => b.priority < 100 && b.type !== 'action');

    return (
      <>
        {/* Recommended section */}
        {recommendedBlocks.length > 0 && (
          <div className="p-2 bg-hover border-b border-border">
            <div className="text-xs text-ink px-2 py-1 font-medium flex items-center gap-1">
              <SparklesIcon className="h-3 w-3" /> {t('Recommended Next')}
            </div>
            {recommendedBlocks.map((availableBlock, i) => {
              const AvailIcon = availableBlock.icon;
              return (
                <button
                  key={`rec-${i}`}
                  type="button"
                  onClick={() => {
                    addBlock(availableBlock, parentBlockId);
                    close();
                  }}
                  className="w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-left transition-colors bg-hover/50 hover:bg-hover border border-border"
                >
                  <div className={clsx(
                    'w-8 h-8 rounded-lg flex items-center justify-center',
                    availableBlock.color === 'purple' && 'bg-ink',
                    availableBlock.color === 'green' && 'bg-hover',
                    availableBlock.color === 'gray' && 'bg-active',
                  )}>
                    <AvailIcon className="h-4 w-4 text-ink" />
                  </div>
                  <div>
                    <div className="text-sm font-medium text-ink">{t(availableBlock.label)}</div>
                    <div className="text-xs text-secondary">{t(availableBlock.description)}</div>
                  </div>
                </button>
              );
            })}
          </div>
        )}

        {/* Actions section */}
        <div className="p-2">
          <div className="text-xs text-tertiary px-2 py-1">{t('Actions')}</div>
          <div className="space-y-1">
            {actionBlocks.map((availableBlock, i) => {
              const AvailIcon = availableBlock.icon;
              return (
                <button
                  key={`action-${i}`}
                  type="button"
                  onClick={() => {
                    addBlock(availableBlock, parentBlockId);
                    close();
                  }}
                  className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-left transition-colors hover:bg-hover"
                >
                  <AvailIcon className={clsx(
                    'h-4 w-4',
                    availableBlock.color === 'blue' && 'text-ink',
                    availableBlock.color === 'purple' && 'text-ink',
                    availableBlock.color === 'green' && 'text-ink',
                    availableBlock.color === 'gray' && 'text-ink',
                  )} />
                  <div>
                    <div className="text-sm text-ink">{t(availableBlock.label)}</div>
                    <div className="text-xs text-tertiary">{t(availableBlock.description)}</div>
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        {/* Other blocks */}
        {otherBlocks.length > 0 && (
          <div className="p-2 border-t border-border">
            <div className="text-xs text-tertiary px-2 py-1">{t('Other')}</div>
            <div className="space-y-1">
              {otherBlocks.map((availableBlock, i) => {
                const AvailIcon = availableBlock.icon;
                return (
                  <button
                    key={`other-${i}`}
                    type="button"
                    onClick={() => {
                      addBlock(availableBlock, parentBlockId);
                      close();
                    }}
                    className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-left transition-colors hover:bg-hover"
                  >
                    <AvailIcon className={clsx(
                      'h-4 w-4',
                      availableBlock.color === 'yellow' && 'text-secondary',
                      availableBlock.color === 'purple' && 'text-ink',
                      availableBlock.color === 'green' && 'text-ink',
                      availableBlock.color === 'gray' && 'text-ink',
                    )} />
                    <div>
                      <div className="text-sm text-ink">{t(availableBlock.label)}</div>
                      <div className="text-xs text-tertiary">{t(availableBlock.description)}</div>
                    </div>
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </>
    );
  };

  const renderTriggerForm = () => (
    <div className="space-y-6">
      {/* Trigger Name Header */}
      <div className="flex items-center gap-4">
        <div className="flex-1">
          <input
            type="text"
            value={formData.name}
            onChange={e => setFormData(prev => ({ ...prev, name: e.target.value }))}
            className="w-full px-4 py-3 bg-hover border border-border rounded-xl text-lg font-medium text-ink focus:ring-2 focus:ring-ink/20 focus:border-transparent"
            placeholder={t('Trigger name...')}
          />
        </div>
        <div className="flex items-center gap-2">
          <span className="text-sm text-secondary">{t('Enabled')}</span>
          <Switch
            checked={formData.enabled}
            onChange={enabled => setFormData(prev => ({ ...prev, enabled }))}
          />
        </div>
      </div>

      {/* Visual Flow Builder - Tree Structure */}
      <div className="space-y-1">
        {(() => {
          // Find root blocks (no parentId)
          const rootBlocks = flowBlocks.filter(b => !b.parentId);
          return rootBlocks.map(block => renderBlockNode(block, 0));
        })()}
      </div>

      {/* Dynamic placeholder hint based on blocks in the chain */}
      <Disclosure defaultOpen={false}>
        {({ open }) => (
          <div className="bg-hover/50 rounded-lg border border-border/50 overflow-hidden">
            <Disclosure.Button className="w-full p-4 flex items-center justify-between hover:bg-hover/30 transition-colors">
              <div className="flex items-center gap-2">
                <InformationCircleIcon className="h-5 w-5 text-ink" />
                <span className="font-medium text-ink text-sm">{t('Available Placeholders')}</span>
                <span className="text-xs text-tertiary">({open ? t('click to collapse') : t('click to expand')})</span>
              </div>
              <ChevronDownIcon className={clsx('h-5 w-5 text-secondary transition-transform', open && 'rotate-180')} />
            </Disclosure.Button>
            <Disclosure.Panel className="p-4 pt-0 text-xs border-t border-border/50">
              <p className="text-secondary mb-4">{t('Use these in notification messages, AI context, or conditions. All data flows through the entire chain.')}</p>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {/* Always available - Date/Time placeholders */}
                <div className="p-3 bg-surface/50 rounded-lg">
                  <p className="text-ink font-medium mb-2 flex items-center gap-1">
                    <ClockIcon className="h-3.5 w-3.5" /> {t('Date & Time')}
                  </p>
                  <div className="flex flex-wrap gap-1">
                    <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('Current ISO timestamp')}>{'{{now}}'}</code>
                    <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title="YYYY-MM-DD">{'{{now_date}}'}</code>
                    <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title="HH:MM:SS">{'{{now_time}}'}</code>
                    <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title="YYYY-MM-DD HH:MM:SS">{'{{now_datetime}}'}</code>
                    <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('Unix timestamp')}>{'{{now_timestamp}}'}</code>
                  </div>
                </div>

                {/* Target info - always available */}
                <div className="p-3 bg-surface/50 rounded-lg">
                  <p className="text-ink font-medium mb-2 flex items-center gap-1">
                    <Squares2X2Icon className="h-3.5 w-3.5" /> {t('Target Info')}
                  </p>
                  <div className="flex flex-wrap gap-1">
                    <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{target_id}}'}</code>
                    <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{target_name}}'}</code>
                    <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{target_url}}'}</code>
                    <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{trigger_name}}'}</code>
                  </div>
                </div>

                {/* Content Change placeholders */}
                {flowBlocks.some(b => b.blockType === 'change_detected') && (
                  <div className="p-3 bg-hover rounded-lg border border-border">
                    <p className="text-ink font-medium mb-2 flex items-center gap-1">
                      <BoltIcon className="h-3.5 w-3.5" /> {t('Content Change Event')}
                    </p>
                    <div className="space-y-2">
                      <div>
                        <p className="text-tertiary text-[10px] uppercase mb-1">{t('Extracted Data')}</p>
                        <div className="flex flex-wrap gap-1">
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{extracted.price}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{extracted.status}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{extracted.title}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{extracted.*}}'}</code>
                        </div>
                      </div>
                      <div>
                        <p className="text-tertiary text-[10px] uppercase mb-1">{t('Change Details')}</p>
                        <div className="flex flex-wrap gap-1">
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{content}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{diff_snippet}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{selector_name}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{selector_id}}'}</code>
                        </div>
                      </div>
                      <div>
                        <p className="text-tertiary text-[10px] uppercase mb-1">{t('When Change Was Detected')}</p>
                        <div className="flex flex-wrap gap-1">
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('ISO timestamp of when change was detected')}>{'{{change_detected_at}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('Date only (YYYY-MM-DD)')}>{'{{change_detected_date}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('Time only (HH:MM:SS)')}>{'{{change_detected_time}}'}</code>
                        </div>
                      </div>
                    </div>
                  </div>
                )}

                {/* Notification placeholders */}
                {flowBlocks.some(b => b.blockType === 'notification') && (
                  <div className="p-3 bg-hover rounded-lg border border-border">
                    <p className="text-secondary font-medium mb-2 flex items-center gap-1">
                      <BellIcon className="h-3.5 w-3.5" /> {t('Notification Results')}
                    </p>
                    <div className="flex flex-wrap gap-1">
                      <code className="bg-hover text-secondary px-1.5 py-0.5 rounded">{'{{notification_status}}'}</code>
                      <code className="bg-hover text-secondary px-1.5 py-0.5 rounded">{'{{notification_sent}}'}</code>
                      <code className="bg-hover text-secondary px-1.5 py-0.5 rounded">{'{{notification_failed}}'}</code>
                      <code className="bg-hover text-secondary px-1.5 py-0.5 rounded">{'{{notification_message}}'}</code>
                    </div>
                  </div>
                )}

                {/* AI Session placeholders */}
                {flowBlocks.some(b => b.blockType === 'ai_session' || b.blockType === 'ai_session_completed' || b.blockType === 'ai_session_started') && (
                  <div className="p-3 bg-hover rounded-lg border border-border">
                    <p className="text-ink font-medium mb-2 flex items-center gap-1">
                      <CpuChipIcon className="h-3.5 w-3.5" /> {t('AI Session')}
                    </p>
                    <div className="space-y-2">
                      <div>
                        <p className="text-tertiary text-[10px] uppercase mb-1">{t('Session Info')}</p>
                        <div className="flex flex-wrap gap-1">
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{session_id}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{session_name}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{ai_session_id}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{ai_session_name}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{task_id}}'}</code>
                        </div>
                      </div>
                      <div>
                        <p className="text-tertiary text-[10px] uppercase mb-1">{t('Status (after completion)')}</p>
                        <div className="flex flex-wrap gap-1">
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{session_status}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{success}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{status}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('Error message if failed')}>{'{{error}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('Error message if failed')}>{'{{session_error}}'}</code>
                        </div>
                      </div>
                      <div>
                        <p className="text-tertiary text-[10px] uppercase mb-1">{t('Execution Metrics')}</p>
                        <div className="flex flex-wrap gap-1">
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('Number of steps the AI took')}>{'{{session_steps_taken}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('Number of steps')}>{'{{steps_taken}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('Duration in milliseconds')}>{'{{duration_ms}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('Duration in milliseconds')}>{'{{session_duration_ms}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('How long the session ran')}>{'{{session_duration_seconds}}'}</code>
                        </div>
                      </div>
                      <div>
                        <p className="text-tertiary text-[10px] uppercase mb-1">{t('Results & Extracted Data')}</p>
                        <div className="flex flex-wrap gap-1">
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{final_url}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{ai_result.final_url}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{ai_result.extracted_data}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{extracted_*}}'}</code>
                        </div>
                      </div>
                      <div>
                        <p className="text-tertiary text-[10px] uppercase mb-1">{t('Timestamps')}</p>
                        <div className="flex flex-wrap gap-1">
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('ISO timestamp when AI session started')}>{'{{session_started_at}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('ISO timestamp when AI session completed')}>{'{{session_completed_at}}'}</code>
                        </div>
                      </div>
                    </div>
                  </div>
                )}

                {/* Workflow placeholders */}
                {flowBlocks.some(b => b.blockType === 'workflow' || b.blockType === 'workflow_completed' || b.blockType === 'workflow_started') && (
                  <div className="p-3 bg-hover rounded-lg border border-border">
                    <p className="text-ink font-medium mb-2 flex items-center gap-1">
                      <Cog6ToothIcon className="h-3.5 w-3.5" /> {t('Workflow')}
                    </p>
                    <div className="space-y-2">
                      <div>
                        <p className="text-tertiary text-[10px] uppercase mb-1">{t('Workflow Info')}</p>
                        <div className="flex flex-wrap gap-1">
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{workflow_id}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{workflow_name}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{workflow_task_id}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{task_id}}'}</code>
                        </div>
                      </div>
                      <div>
                        <p className="text-tertiary text-[10px] uppercase mb-1">{t('Status (after completion)')}</p>
                        <div className="flex flex-wrap gap-1">
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{workflow_status}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{success}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{status}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('Error message if failed')}>{'{{error}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('Error message if failed')}>{'{{workflow_error}}'}</code>
                        </div>
                      </div>
                      <div>
                        <p className="text-tertiary text-[10px] uppercase mb-1">{t('Execution Metrics')}</p>
                        <div className="flex flex-wrap gap-1">
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('Number of workflow steps completed')}>{'{{workflow_steps_completed}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('Number of steps')}>{'{{steps_completed}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('Duration in milliseconds')}>{'{{duration_ms}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('Duration in seconds')}>{'{{duration_seconds}}'}</code>
                        </div>
                      </div>
                      <div>
                        <p className="text-tertiary text-[10px] uppercase mb-1">{t('Results')}</p>
                        <div className="flex flex-wrap gap-1">
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded">{'{{result.*}}'}</code>
                        </div>
                      </div>
                      <div>
                        <p className="text-tertiary text-[10px] uppercase mb-1">{t('Timestamps')}</p>
                        <div className="flex flex-wrap gap-1">
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('ISO timestamp when workflow started')}>{'{{workflow_started_at}}'}</code>
                          <code className="bg-hover text-ink px-1.5 py-0.5 rounded" title={t('ISO timestamp when workflow completed')}>{'{{workflow_completed_at}}'}</code>
                        </div>
                      </div>
                    </div>
                  </div>
                )}
              </div>

              {/* Branch-scoped placeholders - show when there could be parallel branches */}
              {flowBlocks.filter(b => b.type === 'action').length > 1 && (
                <div className="mt-4 p-3 bg-hover rounded-lg border border-border">
                  <p className="text-secondary font-medium mb-2 flex items-center gap-1">
                    <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7h12m0 0l-4-4m4 4l-4 4m0 6H4m0 0l4 4m-4-4l4-4" />
                    </svg>
                    {t('Parallel Branches')}
                  </p>
                  <p className="text-secondary text-[11px] mb-2">
                    {t('When you have parallel action branches, each branch gets its own context (branch1, branch2, etc.). Use simple placeholders within your branch, or prefix to access other branches.')}
                  </p>
                  <div className="space-y-2">
                    <div>
                      <p className="text-tertiary text-[10px] uppercase mb-1">{t('Within Your Branch (no prefix needed)')}</p>
                      <div className="flex flex-wrap gap-1">
                        <code className="bg-hover text-secondary px-1.5 py-0.5 rounded">{'{{session_name}}'}</code>
                        <code className="bg-hover text-secondary px-1.5 py-0.5 rounded">{'{{session_status}}'}</code>
                        <code className="bg-hover text-secondary px-1.5 py-0.5 rounded">{'{{success}}'}</code>
                      </div>
                    </div>
                    <div>
                      <p className="text-tertiary text-[10px] uppercase mb-1">{t('Access Other Branches')}</p>
                      <div className="flex flex-wrap gap-1">
                        <code className="bg-hover text-secondary px-1.5 py-0.5 rounded">{'{{branch1.session_name}}'}</code>
                        <code className="bg-hover text-secondary px-1.5 py-0.5 rounded">{'{{branch2.session_status}}'}</code>
                        <code className="bg-hover text-secondary px-1.5 py-0.5 rounded">{'{{branch1.success}}'}</code>
                      </div>
                    </div>
                    <div>
                      <p className="text-tertiary text-[10px] uppercase mb-1">{t('Available in Each Branch')}</p>
                      <div className="flex flex-wrap gap-1">
                        <code className="bg-hover text-secondary px-1.5 py-0.5 rounded">{'{{task_id}}'}</code>
                        <code className="bg-hover text-secondary px-1.5 py-0.5 rounded">{'{{ai_session_id}}'}</code>
                        <code className="bg-hover text-secondary px-1.5 py-0.5 rounded">{'{{notification_sent}}'}</code>
                        <code className="bg-hover text-secondary px-1.5 py-0.5 rounded">{'{{workflow_id}}'}</code>
                        <code className="bg-hover text-secondary px-1.5 py-0.5 rounded">{'{{duration_ms}}'}</code>
                        <code className="bg-hover text-secondary px-1.5 py-0.5 rounded">{'{{error}}'}</code>
                      </div>
                    </div>
                  </div>
                </div>
              )}

              <div className="mt-4 p-3 bg-hover rounded-lg border border-border">
                <p className="text-ink font-medium mb-1">{t('Chain Data Flow')}</p>
                <p className="text-tertiary">
                  {t('Data from ALL previous blocks flows through the entire chain. After an AI session completes, you can still use')} <code className="text-ink">{'{{extracted.price}}'}</code> {t('from the original change detection alongside')} <code className="text-ink">{'{{session_status}}'}</code> {t('from the AI result.')}
                  {flowBlocks.filter(b => b.type === 'action').length > 1 && (
                    <span className="block mt-1">
                      {t('For parallel branches, use')} <code className="text-secondary">{'{{branch1.field}}'}</code> {t('or')} <code className="text-secondary">{'{{branch2.field}}'}</code> {t('to access specific branch data.')}
                    </span>
                  )}
                </p>
              </div>
            </Disclosure.Panel>
          </div>
        )}
      </Disclosure>

      {/* Form Actions */}
      <div className="flex justify-between items-center pt-4 border-t border-border">
        <button
          type="button"
          onClick={() => {
            setEditingTrigger(null);
            setIsCreating(false);
            resetForm();
            setFlowBlocks([{ id: 'block_1', type: 'event', blockType: 'change_detected', config: {} }]);
          }}
          className="px-4 py-2 text-secondary hover:text-ink"
        >
          {t('Cancel')}
        </button>
        <button
          type="button"
          onClick={handleSave}
          disabled={saving || !formData.name.trim() || flowBlocks.filter(b => b.type === 'action').length === 0}
          className="px-6 py-2 bg-ink hover:bg-ink/90 disabled:bg-hover disabled:text-tertiary text-ink rounded-lg font-medium flex items-center gap-2"
        >
          {saving && <ArrowPathIcon className="h-4 w-4 animate-spin" />}
          {editingTrigger ? t('Update Trigger') : t('Create Trigger')}
        </button>
      </div>
    </div>
  );

  // Get event type display info
  const getEventTypeInfo = (eventType: string) => {
    const info = EVENT_TYPES.find(e => e.value === eventType);
    const colors: Record<string, string> = {
      'change_detected': 'bg-hover text-ink border-border',
      'webhook_received': 'bg-hover text-secondary border-border',
      'ai_session_started': 'bg-hover text-ink border-border',
      'ai_session_completed': 'bg-hover text-ink border-border',
      'workflow_started': 'bg-hover text-ink border-border',
      'workflow_completed': 'bg-hover text-ink border-border',
    };
    return { label: info?.label || eventType, color: colors[eventType] || 'bg-hover text-ink' };
  };

  // Render a trigger card
  const renderTriggerCard = (trigger: TriggerRule) => {
    const eventTypeInfo = getEventTypeInfo(trigger.event_type);
    return (
    <div key={trigger.id} className="bg-hover/50 rounded-xl border border-border/50 overflow-hidden">
      <div className="p-4">
        <div className="flex items-start justify-between">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <BoltIcon className={clsx('h-5 w-5 flex-shrink-0', trigger.enabled ? 'text-secondary' : 'text-tertiary')} />
              <h4 className="font-semibold text-ink truncate">{trigger.name}</h4>
              {/* Event Type Badge */}
              <span className={clsx('text-xs px-2 py-0.5 rounded-full border', eventTypeInfo.color)}>
                {t(eventTypeInfo.label)}
              </span>
              {trigger.priority !== 0 && (
                <span className="text-xs px-1.5 py-0.5 bg-hover rounded text-secondary">
                  {t('Priority: {{n}}', { n: trigger.priority })}
                </span>
              )}
            </div>
            {trigger.description && (
              <p className="text-sm text-secondary mt-1 line-clamp-1">{trigger.description}</p>
            )}

            {/* Actions Summary */}
            <div className="flex flex-wrap gap-2 mt-3">
              {trigger.actions.map((action, i) => {
                const actionType = ACTION_TYPES.find(t => t.value === action.type);
                const Icon = actionType?.icon || BoltIcon;
                return (
                  <span
                    key={i}
                    className={clsx(
                      'flex items-center gap-1.5 px-2 py-1 rounded-full text-xs font-medium',
                      action.type === 'notification' && 'bg-hover text-ink',
                      action.type === 'ai_session' && 'bg-hover text-ink',
                      action.type === 'workflow' && 'bg-hover text-ink'
                    )}
                  >
                    <Icon className="h-3 w-3" />
                    {actionType?.label && t(actionType.label)}
                    {action.type === 'ai_session' && action.config.session_ids?.length > 0 && (
                      <span className="opacity-75">({action.config.session_ids.length})</span>
                    )}
                  </span>
                );
              })}
            </div>

            {/* Stats */}
            <div className="flex items-center gap-4 mt-3 text-xs text-tertiary">
              <span className="flex items-center gap-1">
                <PlayIcon className="h-3 w-3" />
                {t('{{n}} runs', { n: trigger.trigger_count })}
              </span>
              {trigger.last_triggered_at && (
                <span className="flex items-center gap-1">
                  <ClockIcon className="h-3 w-3" />
                  {new Date(trigger.last_triggered_at).toLocaleString(uiLocale())}
                </span>
              )}
            </div>

            {/* Webhook URL for webhook_received triggers */}
            {trigger.event_type === 'webhook_received' && trigger.webhook_trigger_token && (() => {
              const baseUrl = window.location.origin;
              const customPath = (trigger as any).custom_path;
              const primaryUrl = customPath
                ? `${baseUrl}/api/v1/webhooks/${customPath}`
                : webhookTriggersApi.getWebhookUrl(trigger.webhook_trigger_token);

              return (
                <div className="mt-3 p-2 bg-hover border border-border rounded-lg space-y-1.5">
                  {/* Primary URL */}
                  <div className="flex items-center gap-2">
                    <LinkIcon className="h-4 w-4 text-secondary flex-shrink-0" />
                    <code className="text-xs text-secondary truncate flex-1">{primaryUrl}</code>
                    <button onClick={(e) => { e.stopPropagation(); navigator.clipboard.writeText(primaryUrl); toast.success(t('Copied')); }}
                      className="p-1 hover:bg-hover rounded transition-colors flex-shrink-0" title={t('Copy URL')}>
                      <ClipboardIcon className="h-4 w-4 text-secondary" />
                    </button>
                  </div>
                  <p className="text-[10px] text-tertiary">
                    {customPath ? t('Token: {{token}}', { token: webhookTriggersApi.getWebhookUrl(trigger.webhook_trigger_token) }) : t('Add a return step in the workflow to auto-return extracted data')}
                  </p>
                </div>
              );
            })()}
          </div>

          {/* Actions */}
          <div className="flex items-center gap-1 ml-4">
            <Switch
              checked={trigger.enabled}
              onChange={() => handleToggle(trigger)}
            />

            <button onClick={() => handleTest(trigger.id)} className="p-2 text-secondary hover:text-ink rounded-lg hover:bg-hover" title={t('Test')}>
              <BeakerIcon className="h-4 w-4" />
            </button>

            <button
              onClick={() => {
                setEditingTrigger(trigger);
                setIsCreating(false);
                populateForm(trigger);
              }}
              className="p-2 text-secondary hover:text-ink rounded-lg hover:bg-hover"
              title={t('Edit')}
            >
              <PencilIcon className="h-4 w-4" />
            </button>

            <button onClick={() => setDeleteTargetId(trigger.id)} className="p-2 text-secondary hover:text-red-700 rounded-lg hover:bg-hover" title={t('Delete')}>
              <TrashIcon className="h-4 w-4" />
            </button>

            <button onClick={() => toggleExpanded(trigger.id)} className="p-2 text-secondary hover:text-ink rounded-lg hover:bg-hover" title={t('History')}>
              {expandedTriggerId === trigger.id ? <ChevronUpIcon className="h-4 w-4" /> : <ChevronDownIcon className="h-4 w-4" />}
            </button>
          </div>
        </div>
      </div>

      {/* Execution History */}
      {expandedTriggerId === trigger.id && (
        <div className="px-4 pb-4 border-t border-border/50">
          <h5 className="text-sm font-medium text-secondary mt-3 mb-2">{t('Recent Executions')}</h5>
          {executions[trigger.id]?.length ? (
            <div className="space-y-2">
              {executions[trigger.id].map(exec => (
                <div key={exec.id} className="flex items-center justify-between text-xs bg-hover rounded-lg p-2">
                  <div className="flex items-center gap-2">
                    {exec.status === 'completed' ? (
                      <CheckCircleIcon className="h-4 w-4 text-green-700" />
                    ) : exec.status === 'failed' ? (
                      <XCircleIcon className="h-4 w-4 text-red-700" />
                    ) : (
                      <ArrowPathIcon className="h-4 w-4 text-ink animate-spin" />
                    )}
                    <span className={clsx(
                      'font-medium',
                      exec.status === 'completed' && 'text-green-700',
                      exec.status === 'failed' && 'text-red-700',
                      exec.status === 'pending' && 'text-ink'
                    )}>
                      {exec.status}
                    </span>
                    {exec.error_message && (
                      <span className="text-red-700 truncate max-w-xs">{exec.error_message}</span>
                    )}
                  </div>
                  <span className="text-tertiary">
                    {exec.triggered_at && new Date(exec.triggered_at).toLocaleString(uiLocale())}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-tertiary text-sm py-2">{t('No executions yet')}</p>
          )}
        </div>
      )}
    </div>
  );
  };

  // Embedded mode: render inline without Dialog, auto-create mode
  if (embedded) {
    return (
      <div className="h-full overflow-y-auto">
        <div className="p-6">
          {loading ? (
            <div className="flex flex-col items-center justify-center py-16">
              <ArrowPathIcon className="h-8 w-8 text-tertiary animate-spin" />
              <p className="mt-3 text-secondary text-sm">{t('Loading...')}</p>
            </div>
          ) : (
            renderTriggerForm()
          )}
        </div>
      </div>
    );
  }

  return (
    <>
    <Transition appear show={isOpen} as={Fragment}>
      <Dialog as="div" className="relative z-50" onClose={onClose}>
        <Transition.Child
          as={Fragment}
          enter="ease-out duration-300"
          enterFrom="opacity-0"
          enterTo="opacity-100"
          leave="ease-in duration-200"
          leaveFrom="opacity-100"
          leaveTo="opacity-0"
        >
          <div className="fixed inset-0 bg-black/20 backdrop-blur-sm" />
        </Transition.Child>

        <div className="fixed inset-0 overflow-y-auto">
          <div className="flex min-h-full items-center justify-center p-4">
            <Transition.Child
              as={Fragment}
              enter="ease-out duration-300"
              enterFrom="opacity-0 scale-95"
              enterTo="opacity-100 scale-100"
              leave="ease-in duration-200"
              leaveFrom="opacity-100 scale-100"
              leaveTo="opacity-0 scale-95"
            >
              <Dialog.Panel className="w-full max-w-6xl max-h-[90vh] transform overflow-hidden rounded-2xl bg-surface shadow-2xl transition-all flex flex-col">
                {/* Header */}
                <div className="px-6 py-4 border-b border-border bg-surface/80 backdrop-blur sticky top-0 z-10">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <div className="p-2 bg-hover rounded-xl">
                        <BoltIcon className="h-6 w-6 text-secondary" />
                      </div>
                      <div>
                        <Dialog.Title className="text-xl font-semibold text-ink">
                          {isCreating ? t('Create Trigger') : editingTrigger ? t('Edit Trigger') : t('Unified Triggers')}
                        </Dialog.Title>
                        <p className="text-sm text-secondary truncate max-w-lg">{targetUrl}</p>
                      </div>
                    </div>
                    <button onClick={onClose} className="p-2 hover:bg-hover rounded-lg transition-colors">
                      <XMarkIcon className="h-6 w-6 text-secondary" />
                    </button>
                  </div>
                </div>

                {/* Content */}
                <div className="p-6 max-h-[75vh] overflow-y-auto">
                  {loading ? (
                    <div className="flex flex-col items-center justify-center py-16">
                      <ArrowPathIcon className="h-10 w-10 text-tertiary animate-spin" />
                      <p className="mt-4 text-secondary">{t('Loading triggers...')}</p>
                    </div>
                  ) : isCreating || editingTrigger ? (
                    renderTriggerForm()
                  ) : (
                    <div className="space-y-6">
                      {triggers.length === 0 ? (
                        <div className="text-center py-16">
                          <BoltIcon className="h-16 w-16 text-ink mx-auto mb-4" />
                          <h3 className="text-lg font-medium text-ink">{t('No triggers configured')}</h3>
                          <p className="text-tertiary mt-2 max-w-sm mx-auto">
                            {t('Create triggers to automate notifications, AI sessions, and workflows when events occur.')}
                          </p>
                          <button
                            onClick={() => { setIsCreating(true); resetForm(); }}
                            className="mt-6 px-6 py-3 bg-ink hover:bg-ink/90 text-ink rounded-xl font-medium inline-flex items-center gap-2"
                          >
                            <PlusIcon className="h-5 w-5" />
                            {t('Create First Trigger')}
                          </button>
                        </div>
                      ) : (
                        <>
                          {/* Visual Flow Overview */}
                          <div className="p-4 bg-hover/50 rounded-xl border border-border/50">
                            <h4 className="text-sm font-medium text-ink mb-3 flex items-center gap-2">
                              <Squares2X2Icon className="h-4 w-4" />
                              {t('Trigger Flow Overview')}
                            </h4>
                            <div className="space-y-2">
                              {/* Group triggers by event type */}
                              {['change_detected', 'webhook_received', 'ai_session_completed', 'workflow_completed'].map(eventType => {
                                const eventTriggers = triggers.filter(t => t.event_type === eventType);
                                if (eventTriggers.length === 0) return null;
                                const eventInfo = getEventTypeInfo(eventType);
                                return (
                                  <div key={eventType} className="flex items-center gap-2 flex-wrap">
                                    <span className={clsx('text-xs px-2 py-1 rounded-full border', eventInfo.color)}>
                                      {t(eventInfo.label)}
                                    </span>
                                    <span className="text-tertiary">→</span>
                                    {eventTriggers.map((trg, i) => (
                                      <div key={trg.id} className="flex items-center gap-1">
                                        {i > 0 && <span className="text-tertiary mx-1">|</span>}
                                        <span className="text-xs text-secondary bg-hover/50 px-2 py-1 rounded">
                                          {trg.name}
                                        </span>
                                        <span className="text-tertiary">→</span>
                                        {trg.actions.map((a, ai) => {
                                          const actionType = ACTION_TYPES.find(at => at.value === a.type);
                                          return (
                                            <span
                                              key={ai}
                                              className={clsx(
                                                'text-xs px-2 py-1 rounded',
                                                a.type === 'notification' && 'bg-hover text-ink',
                                                a.type === 'ai_session' && 'bg-hover text-ink',
                                                a.type === 'workflow' && 'bg-hover text-ink'
                                              )}
                                            >
                                              {actionType?.label && t(actionType.label)}
                                            </span>
                                          );
                                        })}
                                      </div>
                                    ))}
                                  </div>
                                );
                              })}
                            </div>

                            {/* Helpful hints */}
                            <div className="mt-4 pt-3 border-t border-border/50">
                              <p className="flex items-start gap-1.5 text-xs text-tertiary">
                                <LightBulbIcon className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                                <span><strong>{t('Tip:')}</strong> {t('Chain triggers to create automation workflows. For example:')}</span>
                              </p>
                              <div className="mt-2 flex flex-wrap gap-2 text-xs">
                                <span className="bg-hover text-ink px-2 py-1 rounded">{t('Content Change')}</span>
                                <span className="text-tertiary">→</span>
                                <span className="bg-hover text-ink px-2 py-1 rounded">{t('AI Session')}</span>
                                <span className="text-tertiary">→</span>
                                <span className="bg-hover text-ink px-2 py-1 rounded">{t('Notification')}</span>
                              </div>
                            </div>
                          </div>

                          {/* Trigger Cards by Event Type */}
                          {['change_detected', 'webhook_received', 'ai_session_started', 'ai_session_completed', 'workflow_started', 'workflow_completed'].map(eventType => {
                            const eventTriggers = triggers.filter(t => t.event_type === eventType);
                            if (eventTriggers.length === 0) return null;
                            const eventInfo = getEventTypeInfo(eventType);
                            return (
                              <div key={eventType} className="space-y-3">
                                <h4 className={clsx(
                                  'text-sm font-medium flex items-center gap-2',
                                  eventType === 'change_detected' && 'text-ink',
                                  eventType === 'webhook_received' && 'text-secondary',
                                  eventType.startsWith('ai_session') && 'text-ink',
                                  eventType.startsWith('workflow') && 'text-ink'
                                )}>
                                  <span className={clsx('w-2 h-2 rounded-full', eventInfo.color.split(' ')[0])} />
                                  {t('{{label}} Triggers ({{n}})', { label: t(eventInfo.label), n: eventTriggers.length })}
                                </h4>
                                {eventTriggers.map(renderTriggerCard)}
                              </div>
                            );
                          })}

                          <button
                            onClick={() => { setIsCreating(true); resetForm(); }}
                            className="w-full py-4 border-2 border-dashed border-border rounded-xl text-secondary hover:text-ink hover:border-ink/30 flex items-center justify-center gap-2 transition-colors"
                          >
                            <PlusIcon className="h-5 w-5" />
                            {t('Add Trigger')}
                          </button>
                        </>
                      )}
                    </div>
                  )}
                </div>
              </Dialog.Panel>
            </Transition.Child>
          </div>
        </div>
      </Dialog>
    </Transition>

    <ConfirmDialog
      isOpen={deleteTargetId != null}
      onClose={() => setDeleteTargetId(null)}
      onConfirm={confirmDelete}
      title={t('Delete trigger')}
      message={t('Delete this trigger? Automations attached to it will stop firing. This cannot be undone.')}
      confirmText={t('Delete trigger')}
      variant="danger"
      isLoading={deleting}
    />
    </>
  );
};

export default UnifiedTriggersModal;

/**
 * Inline variant of UnifiedTriggersModal for embedding in wizard steps.
 * Renders as a div instead of a dialog - the modal opens/closes are handled
 * by the parent controlling `visible`.
 */
export interface TriggerConfigInlineProps {
  visible: boolean;
  targetId: string;
  targetUrl: string;
  workflowId?: number;
  defaultEventType?: string;
  onTriggerCreated?: () => void;
}

export const TriggerConfigInline: React.FC<TriggerConfigInlineProps> = ({
  visible,
  targetId,
  targetUrl,
  workflowId,
  defaultEventType,
  onTriggerCreated,
}) => {
  if (!visible) return null;

  return (
    <div className="bg-surface rounded-xl border border-border/50 overflow-hidden">
      <UnifiedTriggersModalInner
        targetId={targetId}
        targetUrl={targetUrl}
        workflowId={workflowId}
        defaultEventType={defaultEventType}
        onTriggerCreated={onTriggerCreated}
      />
    </div>
  );
};

/**
 * Inner content of the triggers modal, without Dialog wrapper.
 * Used by TriggerConfigInline for embedding.
 */
const UnifiedTriggersModalInner: React.FC<{
  targetId: string;
  targetUrl: string;
  workflowId?: number;
  defaultEventType?: string;
  onTriggerCreated?: () => void;
}> = ({ targetId, targetUrl, workflowId, defaultEventType, onTriggerCreated: _onTriggerCreated }) => {
  // This component re-renders the essential trigger management UI inline.
  // For the wizard, we embed the full UnifiedTriggersModal as a controlled modal
  // but expose it with a simplified API via TriggerConfigInline.
  // The inline version shows a summary + "Manage Triggers" button that opens the modal.

  const { t } = useTranslation();
  const [showModal, setShowModal] = React.useState(false);
  const [triggerCount, setTriggerCount] = React.useState(0);

  React.useEffect(() => {
    const loadCount = async () => {
      try {
        if (workflowId) {
          const all = await triggersApi.listAll({ workflow_id: workflowId });
          setTriggerCount(all.length);
        } else if (targetId) {
          const triggers = await triggersApi.listForTarget(parseInt(targetId));
          setTriggerCount(triggers.length);
        }
      } catch {
        // ignore
      }
    };
    loadCount();
  }, [targetId, workflowId]);

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-hover rounded-xl">
            <BoltIcon className="h-5 w-5 text-secondary" />
          </div>
          <div>
            <h3 className="text-base font-semibold text-ink">{t('Triggers')}</h3>
            <p className="text-sm text-secondary">
              {triggerCount > 0 ? (triggerCount === 1 ? t('1 trigger configured') : t('{{n}} triggers configured', { n: triggerCount })) : t('No triggers yet')}
            </p>
          </div>
        </div>
        <button
          onClick={() => setShowModal(true)}
          className="px-4 py-2 bg-ink hover:bg-ink/90 text-ink rounded-lg text-sm font-medium inline-flex items-center gap-2 transition-colors"
        >
          <PlusIcon className="h-4 w-4" />
          {triggerCount > 0 ? t('Manage Triggers') : t('Add Trigger')}
        </button>
      </div>

      {triggerCount === 0 && (
        <div className="text-center py-8 text-tertiary text-sm">
          <BoltIcon className="h-10 w-10 mx-auto mb-3 text-ink" />
          <p>{t('Create triggers to automate actions when events occur.')}</p>
          <p className="text-xs mt-1 text-tertiary">{t('Notifications, AI sessions, workflow chains, and more.')}</p>
        </div>
      )}

      <UnifiedTriggersModal
        isOpen={showModal}
        onClose={() => { setShowModal(false); }}
        targetId={targetId}
        targetUrl={targetUrl}
        workflowId={workflowId}
        defaultEventType={defaultEventType}
      />
    </div>
  );
};
