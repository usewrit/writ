import {
  CursorArrowRaysIcon,
  SparklesIcon,
  EyeIcon,
  BoltIcon,
  ClockIcon,
  CommandLineIcon,
  SignalIcon,
} from '@heroicons/react/24/outline';

export const TYPE_META: Record<string, { label: string; icon: any }> = {
  pre_check: { label: 'Pre-Check', icon: EyeIcon },
  on_change: { label: 'On Change', icon: BoltIcon },
  ai_navigate: { label: 'AI Navigate', icon: SparklesIcon },
  recorded: { label: 'Recorded', icon: CursorArrowRaysIcon },
  scheduled: { label: 'Scheduled', icon: ClockIcon },
  api_recorded: { label: 'API', icon: CommandLineIcon },
  streaming: { label: 'Streaming', icon: SignalIcon },
};

// Restrained semantic status color (matches utils/statusStyle.ts + RunsPage):
// failure shouts red, success is calm green, in-progress pulses amber.
export const STATUS_DOT: Record<string, string> = {
  success: 'bg-green-500',
  completed: 'bg-green-500',
  failed: 'bg-red-500',
  running: 'bg-amber-500 animate-status-pulse',
  pending: 'bg-amber-500 animate-status-pulse',
  cancelled: 'bg-zinc-300',
  skipped: 'bg-zinc-200',
};

/** Uppercase section header + optional one-line explanation — the scannability
 * primitive every detail-page section shares. */
export const sectionHeaderClass = 'text-xs font-semibold text-secondary uppercase tracking-wider';
