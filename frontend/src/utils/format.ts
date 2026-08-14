import { formatDistanceToNow } from 'date-fns';
import { fr } from 'date-fns/locale/fr';
import { es } from 'date-fns/locale/es';
import i18n, { activeLanguage } from '../i18n';

// Timestamps are the single most repeated piece of text in the UI, so they have to
// follow the interface language rather than the build's. `formatDate` used a fixed
// 'MMM dd, yyyy HH:mm:ss' pattern and `formatRelativeTime` used date-fns' default
// (en-US) locale, which left every date and every "3 hours ago" in English no matter
// what the owner picked.
const DATE_FNS_LOCALES = { fr, es } as const;

/** date-fns locale for the active UI language (undefined = the en-US built-in). */
const dateFnsLocale = () =>
  DATE_FNS_LOCALES[activeLanguage() as keyof typeof DATE_FNS_LOCALES];

/**
 * BCP-47 tag for `Intl` / `toLocale*`, from the active UI language.
 *
 * Pass this explicitly wherever you call `toLocaleString`/`toLocaleDateString`:
 * their default (and an explicit `undefined`) is the BROWSER's locale, which is a
 * different thing from the language the owner picked in Settings — an en-US browser
 * set to French would otherwise keep rendering English dates and 1,234.5 numbers.
 */
export const uiLocale = (): string => activeLanguage();
const intlLocale = uiLocale;

// Intl handles the parts a fixed pattern cannot: month names, and the field ORDER
// ("Jul 25, 2026" vs "25 juil. 2026"). hourCycle is pinned to h23 so the clock stays
// 24-hour and fixed-width everywhere — that matches what this function already
// rendered in English, and fr/es use 24-hour natively anyway.
const dateTimeFormatters = new Map<string, Intl.DateTimeFormat>();
const dateTimeFormatter = (locale: string): Intl.DateTimeFormat => {
  let f = dateTimeFormatters.get(locale);
  if (!f) {
    f = new Intl.DateTimeFormat(locale, {
      dateStyle: 'medium',
      timeStyle: 'medium',
      hourCycle: 'h23',
    });
    dateTimeFormatters.set(locale, f);
  }
  return f;
};

export const formatDate = (date: string | Date): string => {
  if (!date) return i18n.t('N/A');
  const d = new Date(date);
  if (isNaN(d.getTime())) return i18n.t('Invalid date');
  return dateTimeFormatter(intlLocale()).format(d);
};

export const formatRelativeTime = (date: string | Date): string => {
  if (!date) return i18n.t('N/A');
  const d = new Date(date);
  if (isNaN(d.getTime())) return i18n.t('Invalid date');
  return formatDistanceToNow(d, { addSuffix: true, locale: dateFnsLocale() });
};

/**
 * Precise, ticking elapsed magnitude since `since` ("45s", "3m 05s", "2h 03m",
 * "1d 04h") — the live metric for the monitor hero. Language-neutral units.
 */
export const formatElapsed = (since: string | Date): string => {
  if (!since) return '0s';
  const start = new Date(since);
  if (isNaN(start.getTime())) return '0s';
  let s = Math.max(0, Math.floor((Date.now() - start.getTime()) / 1000));
  const d = Math.floor(s / 86400);
  s -= d * 86400;
  const h = Math.floor(s / 3600);
  s -= h * 3600;
  const m = Math.floor(s / 60);
  s -= m * 60;
  if (d > 0) return `${d}d ${String(h).padStart(2, '0')}h`;
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${s}s`;
};

/** Compact single-unit elapsed magnitude ("45s", "3m", "2h", "1d"). */
export const formatElapsedShort = (since: string | Date): string => {
  if (!since) return '0s';
  const start = new Date(since);
  if (isNaN(start.getTime())) return '0s';
  const s = Math.max(0, Math.floor((Date.now() - start.getTime()) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
};

// Grouping and decimal separators differ per language (1,234.5 / 1 234,5 / 1.234,5),
// so this follows the interface language rather than the hardcoded 'en-US' it used to.
export const formatNumber = (num: number, decimals = 0): string => {
  return num.toLocaleString(intlLocale(), {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
};

export const formatLatency = (ms: number): string => {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
};

export const formatBytes = (bytes: number): string => {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`;
};

/**
 * Render a PROXY-CREDIT balance (measured in bytes) as GB. Proxy credit is earned
 * GB-for-GB by sharing your connection (IP relay) and spent on your own residential-
 * proxy runs, so 1 GB = 1e9 bytes to match the relay's daily-byte-cap convention.
 * This is NOT withdrawable cash — never format it with a currency symbol.
 */
export const formatGb = (bytes: number): string => {
  const gb = (bytes || 0) / 1_000_000_000;
  if (gb === 0) return '0 GB';
  if (gb < 0.01) return '<0.01 GB';
  return `${parseFloat(gb.toFixed(2))} GB`;
};

export const truncate = (str: string, length: number): string => {
  if (str.length <= length) return str;
  return `${str.substring(0, length)}...`;
};

/**
 * Clean a user/AI-authored title for display. AI-authored workflows are stored
 * with machine prefixes ("AI: …", "AI-generated workflow Goal: …") that read as
 * unfinished in lists. This strips those prefixes and sentence-cases the result
 * for display only — the stored name is untouched (rename still uses the raw
 * value). Width is handled by CSS truncation, so long names stay intact here.
 */
export const cleanTitle = (name?: string | null, fallback = i18n.t('Untitled')): string => {
  if (!name) return fallback;
  let s = name.trim();
  s = s.replace(/^AI[-\s]?generated\s+workflow\s*(goal)?\s*:\s*/i, '');
  s = s.replace(/^AI\b[-\s]*(navigate|workflow)?\s*:\s*/i, '');
  s = s.replace(/^(goal|task|prompt)\s*:\s*/i, '');
  s = s.trim();
  if (!s) return fallback;
  return s.charAt(0).toUpperCase() + s.slice(1);
};

export const copyToClipboard = async (text: string): Promise<boolean> => {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (err) {
    console.error('Failed to copy:', err);
    return false;
  }
};

export const downloadBlob = (blob: Blob, filename: string): void => {
  const url = window.URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  window.URL.revokeObjectURL(url);
  document.body.removeChild(a);
};
