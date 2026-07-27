import {
  BoltIcon,
  BellIcon,
  CpuChipIcon,
  Cog6ToothIcon,
  CheckCircleIcon,
  FunnelIcon,
  LinkIcon,
  EyeIcon,
  ArrowsRightLeftIcon,
  ClockIcon,
  SignalIcon,
  SignalSlashIcon,
  ArrowPathIcon,
  ArrowPathRoundedSquareIcon,
  TableCellsIcon,
  DocumentArrowUpIcon,
} from '@heroicons/react/24/outline';
import { ArrowUturnLeftIcon } from '@heroicons/react/24/outline';
import i18n from '../../i18n';

export type BlockType = 'event' | 'condition' | 'action';

export interface FlowBlock {
  id: string;
  type: BlockType;
  blockType: string;
  config: any;
  parentId?: string;
  children?: string[];
}

export interface BlockMeta {
  type: BlockType;
  blockType: string;
  label: string;
  icon: any;
  color: string;
  description: string;
  priority: number;
}

export const EVENT_TYPES = [
  { value: 'change_detected', label: 'Content Change', description: 'When target/selector content changes' },
  { value: 'webhook_received', label: 'Webhook Received', description: 'When external webhook calls this target' },
  { value: 'ai_session_started', label: 'AI Session Started', description: 'When an autonomous AI goal session starts working' },
  { value: 'ai_session_completed', label: 'AI Session Completed', description: 'When an autonomous AI goal session finishes (success or error)' },
  { value: 'workflow_started', label: 'Workflow Started', description: 'When a workflow begins execution' },
  { value: 'workflow_completed', label: 'Workflow Completed', description: 'When a workflow finishes (success or error)' },
  { value: 'monitor_down', label: 'Monitor Down', description: 'When a monitored page becomes unreachable' },
  { value: 'monitor_stale', label: 'Monitor Stale', description: 'When a monitor stops updating on schedule' },
  { value: 'monitor_recovered', label: 'Monitor Recovered', description: 'When a monitor returns to healthy' },
];

export const SOURCE_TYPES = [
  {
    id: 'content_monitor',
    blockType: 'change_detected',
    label: 'Content Monitor',
    description: 'Watch a page for changes',
    icon: EyeIcon,
  },
  {
    id: 'webhook',
    blockType: 'webhook_received',
    label: 'Incoming Webhook',
    description: 'Receive external HTTP calls',
    icon: ArrowsRightLeftIcon,
  },
  {
    id: 'workflow_completed',
    blockType: 'workflow_completed',
    label: 'Workflow Completed',
    description: 'When a workflow finishes',
    icon: CheckCircleIcon,
  },
  {
    id: 'scheduled',
    blockType: 'scheduled',
    label: 'Schedule',
    description: 'Run on a time interval',
    icon: ClockIcon,
  },
  {
    id: 'streaming_session_started',
    blockType: 'streaming_session_started',
    label: 'Streaming session',
    description: 'When a live session starts or ends',
    icon: SignalIcon,
  },
  {
    id: 'data_extracted',
    blockType: 'data_extracted',
    label: 'Data Extracted',
    description: 'When a workflow produces new rows',
    icon: TableCellsIcon,
  },
  {
    id: 'file_uploaded',
    blockType: 'file_uploaded',
    label: 'File Received',
    description: 'When a file is uploaded or arrives',
    icon: DocumentArrowUpIcon,
  },
  {
    id: 'ai_session_completed',
    blockType: 'ai_session_completed',
    label: 'AI Session Completed',
    description: 'When an AI session finishes',
    icon: CheckCircleIcon,
  },
];

// A soft semantic tint per block family — kept subtle (light bg + soft border).
// The same concept shares a hue across its source and action forms (e.g. an AI
// trigger and "Run AI Agent" are both violet). Shared by the canvas nodes and
// the add-block menu so the color association is consistent.
export function blockColorToken(blockType: string, type?: BlockType): string {
  switch (blockType) {
    case 'change_detected': return 'sky';        // content monitor
    case 'monitor_down': return 'rose';          // monitor health — unreachable
    case 'monitor_stale': return 'orange';       // monitor health — not updating
    case 'monitor_recovered': return 'emerald';  // monitor health — back to healthy
    case 'webhook_received': return 'cyan';      // incoming webhook
    case 'ai_session_started':
    case 'ai_session_completed':
    case 'ai_session': return 'violet';          // AI
    case 'workflow_started':
    case 'workflow_completed':
    case 'workflow': return 'green';             // workflow
    case 'scheduled': return 'orange';           // time trigger
    case 'streaming_session_started':
    case 'streaming_session_ended': return 'purple'; // streaming
    case 'data_extracted':
    case 'save_data_to_file':
    case 'query_and_export':
    case 'append_to_data': return 'emerald';     // data
    case 'file_uploaded':
    case 'send_file': return 'orange';           // files
    case 'condition': return 'amber';            // branch / condition
    case 'for_each': return 'amber';             // loop / control flow
    case 'notification': return 'blue';          // notify
    case 'return_data': return 'teal';           // output
  }
  if (type === 'event') return 'sky';
  if (type === 'condition') return 'amber';
  return 'zinc';
}

export function getBlockColor(block: FlowBlock): string {
  return blockColorToken(block.blockType, block.type);
}

export function getBlockIcon(block: FlowBlock) {
  const map: Record<string, any> = {
    change_detected: BoltIcon,
    monitor_down: SignalSlashIcon,
    monitor_stale: ClockIcon,
    monitor_recovered: ArrowPathIcon,
    webhook_received: LinkIcon,
    ai_session_started: CpuChipIcon,
    ai_session_completed: CheckCircleIcon,
    workflow_started: Cog6ToothIcon,
    workflow_completed: CheckCircleIcon,
    scheduled: ClockIcon,
    streaming_session_started: SignalIcon,
    streaming_session_ended: SignalIcon,
    data_extracted: TableCellsIcon,
    file_uploaded: DocumentArrowUpIcon,
    condition: FunnelIcon,
    for_each: ArrowPathRoundedSquareIcon,
    notification: BellIcon,
    ai_session: CpuChipIcon,
    workflow: Cog6ToothIcon,
    return_data: ArrowUturnLeftIcon,
    save_data_to_file: TableCellsIcon,
    query_and_export: TableCellsIcon,
    append_to_data: TableCellsIcon,
    send_file: DocumentArrowUpIcon,
    start_streaming_session: SignalIcon,
    stop_streaming_session: SignalIcon,
  };
  return map[block.blockType] || BoltIcon;
}

export function getBlockLabel(block: FlowBlock, isSource = false): string {
  if (isSource) {
    const sourceLabels: Record<string, string> = {
      change_detected: i18n.t('Content Monitor'),
      monitor_down: i18n.t('Monitor Down'),
      monitor_stale: i18n.t('Monitor Stale'),
      monitor_recovered: i18n.t('Monitor Recovered'),
      webhook_received: i18n.t('Incoming Webhook'),
      workflow_started: i18n.t('Workflow Started'),
      workflow_completed: block.config?.status_condition === 'error' ? i18n.t('Workflow Error') : i18n.t('Workflow Completed'),
      scheduled: i18n.t('Schedule'),
      streaming_session_started: i18n.t('Streaming Session Started'),
      streaming_session_ended: i18n.t('Streaming Session Ended'),
      data_extracted: i18n.t('Data Extracted'),
      file_uploaded: i18n.t('File Received'),
      ai_session_started: i18n.t('AI Session Started'),
      ai_session_completed: block.config?.status_condition === 'error' ? i18n.t('AI Session Error') : i18n.t('AI Session Completed'),
    };
    if (sourceLabels[block.blockType]) return sourceLabels[block.blockType];
  }
  const labels: Record<string, string> = {
    change_detected: i18n.t('Content Change'),
    monitor_down: i18n.t('Monitor Down'),
    monitor_stale: i18n.t('Monitor Stale'),
    monitor_recovered: i18n.t('Monitor Recovered'),
    webhook_received: i18n.t('Webhook Received'),
    ai_session_started: i18n.t('AI Session Started'),
    ai_session_completed: i18n.t('Wait for AI Session'),
    workflow_started: i18n.t('Workflow Started'),
    workflow_completed: i18n.t('Wait for Workflow'),
    scheduled: i18n.t('Schedule'),
    streaming_session_started: i18n.t('Streaming Session Started'),
    streaming_session_ended: i18n.t('Streaming Session Ended'),
    data_extracted: i18n.t('Data Extracted'),
    file_uploaded: i18n.t('File Received'),
    condition: i18n.t('Condition'),
    for_each: i18n.t('For Each'),
    notification: i18n.t('Send Notification'),
    ai_session: i18n.t('Run AI Agent'),
    workflow: i18n.t('Run Workflow'),
    return_data: i18n.t('Return Data'),
    save_data_to_file: i18n.t('Save Data to File'),
    query_and_export: i18n.t('Query & Export Data'),
    append_to_data: i18n.t('Append to Data'),
    send_file: i18n.t('Send File'),
    start_streaming_session: i18n.t('Start Streaming Session'),
    stop_streaming_session: i18n.t('Stop Streaming Session'),
  };
  return labels[block.blockType] || block.blockType;
}

export interface TriggerRule {
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
  blocks?: FlowBlock[];
  last_triggered_at?: string;
  trigger_count: number;
  created_at?: string;
  updated_at?: string;
}

export interface AISession {
  id: number;
  name: string;
  goal: string;
  mode: string;
  status: string;
  entry_url?: string;
  outputs?: { key: string; label?: string }[];
}

export interface TargetSelector {
  id: number;
  name: string;
  selector: string;
  enabled: boolean;
  content_type?: string;
}

export interface Workflow {
  id: number;
  name: string;
  workflow_type: string;
  steps?: any[];
  is_active: boolean;
  outputs?: { key: string; label?: string }[];
}

export interface Recipient {
  id: number;
  provider: string;
  name: string;
  identifier_preview: string;
  enabled: boolean;
}

export interface TargetInfo {
  id: number;
  url: string;
  check_type: string;
}
