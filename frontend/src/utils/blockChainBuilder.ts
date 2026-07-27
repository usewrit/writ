export interface FlowBlock {
  id: string;
  type: 'event' | 'condition' | 'action';
  blockType: string;
  config: any;
  parentId?: string;
}

function genId(): string {
  return `block_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
}

export function buildNotifyOnChange(targetId: number, config: {
  channels: string[];
  recipients: string[];
  title?: string;
  template?: string;
}): { blocks: FlowBlock[]; payload: any } {
  const eventId = genId();
  const notifyId = genId();

  const blocks: FlowBlock[] = [
    { id: eventId, type: 'event', blockType: 'change_detected', config: { target_id: targetId } },
    { id: notifyId, type: 'action', blockType: 'notification', config: { channels: config.channels, recipients: config.recipients, title: config.title || '', template: config.template || '' }, parentId: eventId },
  ];

  return {
    blocks,
    payload: {
      event_type: 'change_detected',
      target_id: targetId,
      actions: [{ type: 'notification', config: blocks[1].config }],
      blocks,
    },
  };
}

export function buildWorkflowOnChange(targetId: number, workflowId: number, workflowName: string): { blocks: FlowBlock[]; payload: any } {
  const eventId = genId();
  const workflowActionId = genId();
  const waitId = genId();
  const notifyId = genId();

  const blocks: FlowBlock[] = [
    { id: eventId, type: 'event', blockType: 'change_detected', config: { target_id: targetId } },
    { id: workflowActionId, type: 'action', blockType: 'workflow', config: { workflow_id: workflowId }, parentId: eventId },
    { id: waitId, type: 'event', blockType: 'workflow_completed', config: { workflow_id: workflowId, linked_to_block: workflowActionId }, parentId: workflowActionId },
    { id: notifyId, type: 'action', blockType: 'notification', config: { channels: ['pushover'], recipients: [], title: `"${workflowName}" completed`, template: 'Status: {{workflow_status}}' }, parentId: waitId },
  ];

  return {
    blocks,
    payload: {
      event_type: 'change_detected',
      target_id: targetId,
      actions: blocks.filter(b => b.type === 'action').map(b => ({ type: b.blockType, config: b.config })),
      blocks,
    },
  };
}

export function buildAISessionOnChange(targetId: number, sessionId: number, sessionName: string): { blocks: FlowBlock[]; payload: any } {
  const eventId = genId();
  const aiActionId = genId();
  const waitId = genId();
  const notifyId = genId();

  const blocks: FlowBlock[] = [
    { id: eventId, type: 'event', blockType: 'change_detected', config: { target_id: targetId } },
    { id: aiActionId, type: 'action', blockType: 'ai_session', config: { session_ids: [sessionId] }, parentId: eventId },
    { id: waitId, type: 'event', blockType: 'ai_session_completed', config: { ai_session_id: sessionId, linked_to_block: aiActionId }, parentId: aiActionId },
    { id: notifyId, type: 'action', blockType: 'notification', config: { channels: ['pushover'], recipients: [], title: `"${sessionName}" completed`, template: 'Status: {{session_status}}' }, parentId: waitId },
  ];

  return {
    blocks,
    payload: {
      event_type: 'change_detected',
      target_id: targetId,
      actions: blocks.filter(b => b.type === 'action').map(b => ({ type: b.blockType, config: b.config })),
      blocks,
    },
  };
}

export function buildExposeAsAPI(workflowId: number, webhookTriggerId: number, webhookToken: string): { blocks: FlowBlock[]; payload: any } {
  const eventId = genId();
  const workflowActionId = genId();
  const waitId = genId();
  const returnId = genId();

  const blocks: FlowBlock[] = [
    { id: eventId, type: 'event', blockType: 'webhook_received', config: { webhook_trigger_id: webhookTriggerId, webhook_trigger_token: webhookToken } },
    { id: workflowActionId, type: 'action', blockType: 'workflow', config: { workflow_id: workflowId }, parentId: eventId },
    { id: waitId, type: 'event', blockType: 'workflow_completed', config: { workflow_id: workflowId, linked_to_block: workflowActionId }, parentId: workflowActionId },
    { id: returnId, type: 'action', blockType: 'return_data', config: {}, parentId: waitId },
  ];

  return {
    blocks,
    payload: {
      event_type: 'webhook_received',
      webhook_trigger_id: webhookTriggerId,
      workflow_id: workflowId,
      actions: blocks.filter(b => b.type === 'action').map(b => ({ type: b.blockType, config: b.config })),
      blocks,
    },
  };
}

export function buildExposeAsMcp(workflowId: number, _toolName: string): { blocks: FlowBlock[]; payload: any } {
  const eventId = genId();
  const workflowActionId = genId();
  const waitId = genId();
  const returnId = genId();

  const blocks: FlowBlock[] = [
    { id: eventId, type: 'event', blockType: 'webhook_received', config: {} },
    { id: workflowActionId, type: 'action', blockType: 'workflow', config: { workflow_id: workflowId }, parentId: eventId },
    { id: waitId, type: 'event', blockType: 'workflow_completed', config: { workflow_id: workflowId, linked_to_block: workflowActionId }, parentId: workflowActionId },
    { id: returnId, type: 'action', blockType: 'return_data', config: {}, parentId: waitId },
  ];

  return {
    blocks,
    payload: {
      event_type: 'webhook_received',
      workflow_id: workflowId,
      actions: blocks.filter(b => b.type === 'action').map(b => ({ type: b.blockType, config: b.config })),
      blocks,
    },
  };
}

export function buildNotifyOnWorkflowComplete(workflowId: number, config: {
  channels: string[];
  recipients: string[];
}): { blocks: FlowBlock[]; payload: any } {
  const eventId = genId();
  const notifyId = genId();

  const blocks: FlowBlock[] = [
    { id: eventId, type: 'event', blockType: 'workflow_completed', config: { workflow_id: workflowId } },
    { id: notifyId, type: 'action', blockType: 'notification', config: { channels: config.channels, recipients: config.recipients, title: 'Workflow completed', template: 'Status: {{workflow_status}}' }, parentId: eventId },
  ];

  return {
    blocks,
    payload: {
      event_type: 'workflow_completed',
      workflow_id: workflowId,
      actions: [{ type: 'notification', config: blocks[1].config }],
      blocks,
    },
  };
}

export function buildWorkflowOnChangeFromWorkflow(targetId: number, workflowId: number, workflowName: string): { blocks: FlowBlock[]; payload: any } {
  return buildWorkflowOnChange(targetId, workflowId, workflowName);
}
