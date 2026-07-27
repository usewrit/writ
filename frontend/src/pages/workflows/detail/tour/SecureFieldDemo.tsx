import React from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import {
  PlusIcon,
  TrashIcon,
  LockClosedIcon,
  LockOpenIcon,
  ShieldCheckIcon,
  ArrowPathIcon,
  CheckCircleIcon,
} from '@heroicons/react/24/outline';

/**
 * A self-playing simulation of adding one input field and locking it.
 *
 * It is a REPLICA, not the real editor: the tour must be able to show what
 * happens when you lock a secret without writing a field into someone's actual
 * workflow (and without a half-typed `api_key` surviving the tour). Every visual
 * — the two inputs, the lock toggle, the masked value, the delete button —
 * mirrors `components/workflows/SecureDataFields` so the user recognises the
 * real control the moment the tour ends.
 *
 * The whole animation is DERIVED from one frame counter, so replay is a reset
 * and `prefers-reduced-motion` is "jump the counter to the end" — there is no
 * timeline of side effects to unwind.
 */

const TICK_MS = 60;

const NAME = 'api_key';
const VALUE = 'sk-live-7Q2f9Xd';

// Beats, in ticks. Named so the derivations below read as a storyboard.
const ROW_IN = 8;
const NAME_FROM = 10;
const NAME_TO = NAME_FROM + NAME.length;       // 17
const VALUE_FROM = NAME_TO + 2;                // 19
const VALUE_TO = VALUE_FROM + VALUE.length;    // 34
const REACH_LOCK = VALUE_TO + 1;               // 35
const LOCKED = VALUE_TO + 6;                   // 40
const SAVED = LOCKED + 10;                     // 50
const END = SAVED + 6;                         // 56

const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n));

export const SecureFieldDemo: React.FC = () => {
  const { t } = useTranslation();
  const reduce =
    typeof window !== 'undefined' &&
    (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false);

  const [rawTick, setRawTick] = React.useState(0);
  const [runId, setRunId] = React.useState(0);
  // Reduced motion is "jump the counter to the end", derived rather than written:
  // the effect below then has nothing to do, and a user turning the preference on
  // mid-play lands on the finished frame without a state write of its own.
  const tick = reduce ? END : rawTick;
  // Replay rewinds the counter in the click handler (see the button below), so the
  // interval effect only ever has to own the timer.
  const replay = () => { setRawTick(0); setRunId((n) => n + 1); };

  React.useEffect(() => {
    if (reduce) return;
    const id = window.setInterval(() => {
      setRawTick((v) => {
        if (v >= END) { window.clearInterval(id); return v; }
        return v + 1;
      });
    }, TICK_MS);
    return () => window.clearInterval(id);
  }, [runId, reduce]);

  const rowIn = tick >= ROW_IN;
  const nameText = NAME.slice(0, clamp(tick - NAME_FROM, 0, NAME.length));
  const valueChars = clamp(tick - VALUE_FROM, 0, VALUE.length);
  const valueText = VALUE.slice(0, valueChars);
  const locked = tick >= LOCKED;
  const saved = tick >= SAVED;
  const done = tick >= END;

  const typingName = tick >= NAME_FROM && tick < NAME_TO;
  const typingValue = tick >= VALUE_FROM && tick < VALUE_TO;

  // ── Ghost pointer ────────────────────────────────────────────────────────
  const stageRef = React.useRef<HTMLDivElement>(null);
  const addRef = React.useRef<HTMLSpanElement>(null);
  const keyRef = React.useRef<HTMLSpanElement>(null);
  const valueRef = React.useRef<HTMLSpanElement>(null);
  const lockRef = React.useRef<HTMLSpanElement>(null);

  const targetName: 'add' | 'key' | 'value' | 'lock' =
    tick < ROW_IN + 1 ? 'add' : tick < VALUE_FROM ? 'key' : tick < REACH_LOCK ? 'value' : 'lock';
  const clicking = (tick >= ROW_IN - 2 && tick < ROW_IN) || (tick >= LOCKED - 3 && tick < LOCKED);

  const [cursor, setCursor] = React.useState<{ x: number; y: number } | null>(null);
  React.useLayoutEffect(() => {
    const refs = { add: addRef, key: keyRef, value: valueRef, lock: lockRef };
    const stage = stageRef.current;
    const el = refs[targetName].current;
    if (!stage || !el) return;
    const s = stage.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    setCursor({ x: r.left - s.left + r.width / 2, y: r.top - s.top + r.height * 0.62 });
  }, [targetName, rowIn]);

  const caption = !rowIn
    ? t('Most workflows need a value or two — a search term, an account number, an API key.')
    : typingName || tick < VALUE_FROM
      ? t('Name it. Steps and AI prompts refer to the value by this name.')
      : typingValue || tick < REACH_LOCK
        ? t('Then the value. As plain text, anyone who can open this workflow can read it.')
        : !locked
          ? t('This one is a credential. Click the lock.')
          : !saved
            ? t('Locked: encrypted before it is stored, hidden from the AI that drives the page, and never returned by the API.')
            : t('Saved. Each run decrypts it inside the runner and types it into the page — it never comes back to your browser.');

  return (
    <div>
      <div
        ref={stageRef}
        className="relative rounded-lg border border-border bg-canvas/60 p-2.5 select-none"
        aria-hidden
      >
        {/* Header — mirrors SecureDataFields' label + add row */}
        <div className="flex items-center justify-between mb-2">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-tertiary">
            {t('Input data')}
          </span>
          <span
            ref={addRef}
            className={clsx(
              'flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-medium rounded-md transition-colors',
              !rowIn ? 'text-ink bg-hover' : 'text-tertiary',
            )}
          >
            <PlusIcon className="w-3 h-3" /> {t('Add Field')}
          </span>
        </div>

        {/* The field row */}
        <div
          className="flex gap-1.5 items-center transition-all duration-200"
          style={{
            opacity: rowIn ? 1 : 0,
            transform: rowIn ? 'translateY(0)' : 'translateY(-4px)',
          }}
        >
          <span
            ref={keyRef}
            className={clsx(
              'w-1/3 px-2 py-1.5 text-[11px] border rounded-lg bg-surface font-mono truncate',
              locked ? 'border-border-strong text-ink' : 'border-border text-ink',
            )}
          >
            {nameText || <span className="text-tertiary font-sans">{t('Field name')}</span>}
            {typingName && <Caret />}
          </span>

          <span
            ref={valueRef}
            className={clsx(
              'flex-1 flex items-center gap-1 px-2 py-1.5 text-[11px] border rounded-lg bg-surface font-mono truncate',
              locked ? 'border-border-strong text-secondary' : 'border-border text-ink',
            )}
          >
            {locked ? (
              <>
                <ShieldCheckIcon className="w-3 h-3 shrink-0 text-ink" />
                <span className="tracking-[0.18em]">{'•'.repeat(10)}</span>
              </>
            ) : valueText ? (
              valueText
            ) : (
              <span className="text-tertiary font-sans">{t('Value')}</span>
            )}
            {typingValue && <Caret />}
          </span>

          {/* Lock toggle — the whole point of the demo */}
          <span
            ref={lockRef}
            className={clsx(
              'relative grid place-items-center w-6 h-6 rounded-lg transition-colors duration-200',
              locked ? 'bg-ink text-surface' : 'text-tertiary',
              !locked && tick >= REACH_LOCK && 'bg-hover text-ink',
            )}
          >
            {locked ? <LockClosedIcon className="w-3.5 h-3.5" /> : <LockOpenIcon className="w-3.5 h-3.5" />}
            {clicking && !reduce && (
              <span className="absolute inset-0 rounded-lg ring-2 ring-ink/40 animate-status-pulse" />
            )}
          </span>

          <span className="grid place-items-center w-6 h-6 text-tertiary">
            <TrashIcon className="w-3.5 h-3.5" />
          </span>
        </div>

        {/* Save confirmation — the real card shows Discard/Save while dirty */}
        <div className="flex items-center justify-end h-5 mt-1.5">
          {saved && (
            <span className="flex items-center gap-1 text-[10px] font-medium text-ink animate-fade-in">
              <CheckCircleIcon className="w-3 h-3" />
              {t('Input data saved')}
            </span>
          )}
        </div>

        {/* Ghost pointer */}
        {cursor && !reduce && !done && (
          <span
            className="absolute pointer-events-none"
            style={{
              top: 0,
              left: 0,
              transform: `translate(${cursor.x}px, ${cursor.y}px) scale(${clicking ? 0.86 : 1})`,
              transition: 'transform 340ms var(--ease-out)',
            }}
          >
            <svg viewBox="0 0 16 18" className="w-3.5 h-4 drop-shadow-sm" fill="currentColor">
              <path
                className="text-ink"
                fill="currentColor"
                d="M1 1l12 7-5 1.2L5.6 15z"
                stroke="white"
                strokeWidth="1.2"
                strokeLinejoin="round"
              />
            </svg>
          </span>
        )}
      </div>

      {/* The caption block is height-reserved for its LONGEST line: the tour card
          is positioned from a measured height, so a caption that grew a line
          mid-animation would shove the whole card up the screen. */}
      <div className="flex items-start gap-2 mt-2 min-h-[4.4em]">
        <p className="flex-1 text-[11px] leading-relaxed text-secondary">{caption}</p>
        {done && !reduce && (
          <button
            type="button"
            onClick={replay}
            className="flex items-center gap-1 shrink-0 px-2 py-1 text-[11px] font-medium text-secondary hover:text-ink border border-border rounded-lg hover:bg-hover transition-colors"
          >
            <ArrowPathIcon className="w-3 h-3" />
            {t('Replay')}
          </button>
        )}
      </div>
    </div>
  );
};

/** Blinking text caret — sells the "someone is typing this" beat. */
const Caret: React.FC = () => (
  <span className="inline-block w-px h-3 align-middle bg-ink ml-px animate-status-pulse" />
);

export default SecureFieldDemo;
