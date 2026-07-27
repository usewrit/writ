// Single source of truth for status presentation across the app.
//
// Design intent (see design memory): the product is monochrome by personality,
// but STATUS is the one place we allow a restrained semantic color — and only
// here. The eye should be drawn to the thing that needs attention (failure),
// not to the non-event (success). So failure is the loudest mark, success is a
// calm green, in-progress pulses amber, and terminal-but-uneventful states stay
// neutral gray. Mirrors the palette in StatusBadge.tsx so shared and inline
// badges look identical.

export type StatusTone = 'positive' | 'negative' | 'progress' | 'neutral' | 'muted';

export interface StatusStyle {
  tone: StatusTone;
  /** Background class for a small status dot. */
  dot: string;
  /** Combined bg + text classes for a soft-fill status pill. */
  pill: string;
  /** Text-only color class (for inline labels without a fill). */
  text: string;
  /** Whether this state is "live" and should pulse. */
  live: boolean;
}

const TONE_STYLES: Record<StatusTone, Omit<StatusStyle, 'tone' | 'live'>> = {
  positive: { dot: 'bg-green-500',  pill: 'bg-green-50 text-green-700',   text: 'text-green-700' },
  negative: { dot: 'bg-red-500',    pill: 'bg-red-50 text-red-700',       text: 'text-red-700' },
  progress: { dot: 'bg-amber-500',  pill: 'bg-amber-50 text-amber-700',   text: 'text-amber-700' },
  neutral:  { dot: 'bg-zinc-300',   pill: 'bg-zinc-100 text-zinc-600',    text: 'text-zinc-600' },
  muted:    { dot: 'bg-zinc-200',   pill: 'bg-canvas text-zinc-400',     text: 'text-zinc-400' },
};

// Normalize any status-ish string from runs / checks / workflows / agents to a tone.
const TONE_BY_STATUS: Record<string, StatusTone> = {
  // positive
  success: 'positive', ok: 'positive', online: 'positive', active: 'positive',
  enabled: 'positive', passing: 'positive', healthy: 'positive', live: 'positive',
  completed: 'positive', connected: 'positive', verified: 'positive',
  // negative
  failed: 'negative', error: 'negative', offline: 'negative', down: 'negative',
  revoked: 'negative', suspended: 'negative', broken: 'negative', failing: 'negative',
  // in progress / needs attention
  running: 'progress', pending: 'progress', queued: 'progress', degraded: 'progress',
  warning: 'progress', starting: 'progress', retrying: 'progress', changed: 'progress',
  // neutral terminal
  cancelled: 'neutral', canceled: 'neutral', idle: 'neutral', inactive: 'neutral',
  paused: 'neutral', disabled: 'neutral', draft: 'neutral', never: 'neutral',
  // de-emphasized
  skipped: 'muted', unknown: 'muted', '': 'muted',
};

const LIVE_TONES = new Set<StatusTone>(['progress']);

export function statusTone(status: string | null | undefined): StatusTone {
  return TONE_BY_STATUS[(status || '').toLowerCase()] ?? 'neutral';
}

export function statusStyle(status: string | null | undefined): StatusStyle {
  const tone = statusTone(status);
  return { tone, ...TONE_STYLES[tone], live: LIVE_TONES.has(tone) };
}
