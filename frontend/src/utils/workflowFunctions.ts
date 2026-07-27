/**
 * Normalize BrowserRecorder step-group "segments" into the canonical workflow
 * `functions` payload (what lands in AutomationWorkflow.functions and drives the
 * Connect tab, MCP tools-list, Managed-API and output-manifest).
 *
 * Recorder segment shape:  { name, segment_type: 'extract'|'action', step_indices, depends_on, extract_outputs }
 * Canonical function shape: { name, type: 'extraction'|'steps', description, step_indices, step_range, depends_on, input_variables, output_fields }
 *
 * Single source of truth so every recorder entry point (the unified create
 * wizard, the template wizard, flow blocks) persists groupings identically.
 */

export interface RecorderSegment {
  name: string;
  segment_type: string;
  step_indices?: number[];
  depends_on?: string[];
  extract_outputs?: string[];
}

export interface WorkflowFunctionPayload {
  name: string;
  type: 'steps' | 'extraction';
  description: string;
  step_indices: number[];
  step_range: [number, number];
  depends_on: string[];
  input_variables: unknown[];
  output_fields: Array<{ name: string; type: string; description: string }>;
}

export function segmentsToFunctions(
  segments?: RecorderSegment[] | null,
): WorkflowFunctionPayload[] {
  return (segments || [])
    // Only real step-group functions. `streaming_config` segments configure the
    // streaming session (handled by StreamingWorkflowPanel), not callable units.
    .filter(
      (seg) =>
        seg &&
        !!seg.name &&
        seg.segment_type !== 'streaming_config' &&
        Array.isArray(seg.step_indices),
    )
    .map((seg) => {
      const idx = seg.step_indices || [];
      return {
        name: seg.name,
        type: seg.segment_type === 'extract' ? 'extraction' : 'steps',
        description: '',
        step_indices: idx,
        // Contiguous range for display / range-based execution (min..max+1).
        step_range: (idx.length ? [Math.min(...idx), Math.max(...idx) + 1] : [0, 0]) as [number, number],
        depends_on: seg.depends_on || [],
        input_variables: [],
        output_fields: (seg.extract_outputs || []).map((o) => ({
          name: o,
          type: 'string',
          description: '',
        })),
      };
    });
}
