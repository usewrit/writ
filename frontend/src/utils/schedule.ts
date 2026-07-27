// Structured schedule recurrence — shared TS helpers for all 3 frontends.
// Copy into each app's `src/utils/schedule.ts` (adapt import of i18n if you localize labels).
// Mirrors backend services/schedule_recurrence.py. UI-side we only need validate + label +
// an approximate next-run preview; the authoritative next-run is computed server/daemon side.

export type ScheduleKind = 'interval' | 'daily' | 'weekly';

export interface ScheduleValue {
  kind: ScheduleKind;
  intervalMs: number; // interval kind
  time: string; // 'HH:MM' (daily/weekly)
  days: number[]; // 1..7 ISO, 1=Mon (weekly)
  tz: string; // IANA; default local
}

export const WEEKDAYS: ReadonlyArray<{ iso: number; label: string }> = [
  { iso: 1, label: 'Mon' },
  { iso: 2, label: 'Tue' },
  { iso: 3, label: 'Wed' },
  { iso: 4, label: 'Thu' },
  { iso: 5, label: 'Fri' },
  { iso: 6, label: 'Sat' },
  { iso: 7, label: 'Sun' },
];

const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

export function localTz(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

export function defaultSchedule(intervalMs = 3_600_000): ScheduleValue {
  return { kind: 'interval', intervalMs, time: '12:00', days: [], tz: localTz() };
}

/** Build a ScheduleValue from persisted API fields (snake or camel tolerated). */
export function scheduleFromApi(src: any, fallbackIntervalMs = 3_600_000): ScheduleValue {
  const kind: ScheduleKind = src?.schedule_kind ?? src?.scheduleKind ?? 'interval';
  const intervalMs =
    Number(src?.schedule_interval_ms ?? src?.scheduleIntervalMs ?? src?.check_period_ms ?? src?.checkPeriodMs ?? fallbackIntervalMs) ||
    fallbackIntervalMs;
  const time = src?.schedule_time ?? src?.scheduleTime ?? '12:00';
  const daysRaw = src?.schedule_days ?? src?.scheduleDays ?? [];
  const days = Array.isArray(daysRaw) ? daysRaw.map(Number).filter((n) => n >= 1 && n <= 7) : [];
  const tz = src?.schedule_tz ?? src?.scheduleTz ?? localTz();
  return { kind: kind === 'daily' || kind === 'weekly' ? kind : 'interval', intervalMs, time, days, tz };
}

/** Snake_case payload fields to send for workflows/monitors. Interval kind sends nulls for the rest. */
export function scheduleToPayload(s: ScheduleValue): {
  schedule_kind: ScheduleKind;
  schedule_time: string | null;
  schedule_days: number[] | null;
  schedule_tz: string | null;
} {
  if (s.kind === 'interval') {
    return { schedule_kind: 'interval', schedule_time: null, schedule_days: null, schedule_tz: null };
  }
  return {
    schedule_kind: s.kind,
    schedule_time: s.time,
    schedule_days: s.kind === 'weekly' ? [...s.days].sort((a, b) => a - b) : null,
    schedule_tz: s.tz || localTz(),
  };
}

/** Automation `scheduled` block config shape (mode/time/days/tz). */
export function scheduleToBlockConfig(s: ScheduleValue): Record<string, any> {
  if (s.kind === 'interval') return { mode: 'interval', interval_ms: Math.max(1, Math.floor(s.intervalMs)) };
  return {
    mode: s.kind,
    time: s.time,
    ...(s.kind === 'weekly' ? { days: [...s.days].sort((a, b) => a - b) } : {}),
    tz: s.tz || localTz(),
  };
}

export function scheduleFromBlockConfig(cfg: any): ScheduleValue {
  const mode: ScheduleKind = cfg?.mode === 'daily' || cfg?.mode === 'weekly' ? cfg.mode : 'interval';
  const intervalMs = Number(cfg?.interval_ms ?? (cfg?.interval_minutes ? cfg.interval_minutes * 60000 : 3_600_000)) || 3_600_000;
  const daysRaw = Array.isArray(cfg?.days) ? cfg.days.map(Number).filter((n: number) => n >= 1 && n <= 7) : [];
  return { kind: mode, intervalMs, time: cfg?.time ?? '12:00', days: daysRaw, tz: cfg?.tz ?? localTz() };
}

/** Returns an error string if invalid, else null. UI disables submit while this is non-null. */
export function scheduleError(s: ScheduleValue): string | null {
  if (s.kind === 'interval') return s.intervalMs > 0 ? null : 'Pick an interval';
  if (!TIME_RE.test(s.time)) return 'Enter a valid time (HH:MM)';
  if (s.kind === 'weekly' && s.days.length === 0) return 'Pick at least one day';
  return null;
}

/** Short human summary, e.g. "Every 15 min", "Daily at 12:00", "Wed, Fri at 13:00". */
export function scheduleLabel(s: ScheduleValue): string {
  if (s.kind === 'interval') {
    const mins = Math.round(s.intervalMs / 60000);
    if (mins < 60) return `Every ${mins} min`;
    const hours = mins / 60;
    if (hours < 24) return Number.isInteger(hours) ? `Every ${hours}h` : `Every ${hours.toFixed(1)}h`;
    return `Every ${Math.round(hours / 24)}d`;
  }
  if (s.kind === 'daily') return `Daily at ${s.time}`;
  const labels = s.days
    .slice()
    .sort((a, b) => a - b)
    .map((d) => WEEKDAYS.find((w) => w.iso === d)?.label ?? '?')
    .join(', ');
  return `${labels || '—'} at ${s.time}`;
}

/** Approximate next fire (local Date) for a preview line. Uses the browser's local tz. */
export function previewNextRun(s: ScheduleValue, now = new Date()): Date | null {
  if (s.kind === 'interval') return new Date(now.getTime() + s.intervalMs);
  const m = TIME_RE.exec(s.time);
  if (!m) return null;
  const [hh, mm] = s.time.split(':').map(Number);
  if (s.kind === 'daily') {
    const cand = new Date(now);
    cand.setHours(hh, mm, 0, 0);
    if (cand <= now) cand.setDate(cand.getDate() + 1);
    return cand;
  }
  const allowed = new Set(s.days);
  for (let offset = 0; offset <= 7; offset++) {
    const cand = new Date(now);
    cand.setDate(cand.getDate() + offset);
    cand.setHours(hh, mm, 0, 0);
    const iso = cand.getDay() === 0 ? 7 : cand.getDay(); // JS 0=Sun -> ISO 7
    if (allowed.has(iso) && cand > now) return cand;
  }
  return null;
}
