import React, { useState, useRef, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams, Link } from 'react-router-dom';
import { useRequireAuth } from '../../hooks/useAuth';
import toast from 'react-hot-toast';
import { useQuery } from '../../hooks/useQuery';
import { Q } from '../../stores/queryKeys';
import { streamingApi, automationApi } from '../../api/endpoints';
import { getAccessToken, primeAccessToken, refreshAccessToken } from '../../utils/auth';
import { statusStyle } from '../../utils/statusStyle';
import { formatRelativeTime, uiLocale } from '../../utils/format';
import { UsageMeter } from '../../components/ui/UsageMeter';
import { SectionHead } from '../../components/common/SectionHead';
import { PersonaPicker } from '../../components/workflows/PersonaPicker';
import {
  StopCircleIcon,
  PaperAirplaneIcon,
  ArrowPathIcon,
  ClipboardDocumentIcon,
  CheckIcon,
  SignalIcon,
  CodeBracketIcon,
  ArrowLeftIcon,
  ArrowTopRightOnSquareIcon,
  ChevronDownIcon,
  ChevronRightIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';

// Friendly labels for the event names the runtime emits. Unknown names fall
// back to their snake_case → Title Case form.
const EVENT_LABELS: Record<string, string> = {
  message: 'Message',
  response: 'Response',
  command: 'Command',
  command_result: 'Command result',
  handler_invoked: 'Handler invoked',
  handler_result: 'Handler result',
  navigation: 'Navigation',
  page_loaded: 'Page loaded',
  error: 'Error',
  session_started: 'Session started',
  session_ended: 'Session ended',
  streaming_session_ended: 'Session ended',
  chat: 'Chat',
  chat_chunk: 'Chat chunk',
  log: 'Log',
};

const humanizeEvent = (name: string): string =>
  EVENT_LABELS[name] || name.replace(/[_-]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase());

// First few scalar fields of an event payload, for a one-line preview.
const summarizePayload = (data: any): string => {
  if (data == null) return '';
  if (typeof data !== 'object') return String(data).slice(0, 120);
  const parts: string[] = [];
  for (const [k, v] of Object.entries(data)) {
    if (v == null || typeof v === 'object') continue;
    parts.push(`${k}: ${String(v).slice(0, 60)}`);
    if (parts.length >= 3) break;
  }
  return parts.join(' · ');
};

// Memoized so each event row stringifies its payload once (on mount) instead of
// re-running JSON.stringify for every row on every parent render. Events are
// append-only with stable object identities, so existing rows never re-render.
const EventRow = React.memo<{ evt: any }>(({ evt }) => {
  const [open, setOpen] = useState(false);
  const name = evt.event_name || evt.type || 'event';
  const summary = summarizePayload(evt.data);
  const hasDetail = evt.data != null && (typeof evt.data !== 'object' || Object.keys(evt.data).length > 0);

  return (
    <div>
      <button
        onClick={() => hasDetail && setOpen(o => !o)}
        className={clsx('w-full flex items-center gap-2 px-3 py-2 text-left', hasDetail && 'hover:bg-chrome transition-colors')}
      >
        {hasDetail ? (
          <ChevronRightIcon className={clsx('w-3 h-3 text-tertiary shrink-0 transition-transform', open && 'rotate-90')} />
        ) : (
          <span className="w-3 shrink-0" />
        )}
        <span className="text-[12px] font-medium text-ink shrink-0">{humanizeEvent(name)}</span>
        {summary && <span className="text-[11px] text-tertiary font-mono truncate flex-1">{summary}</span>}
        <span className="text-[10px] text-tertiary shrink-0 ml-auto">
          {new Date(evt.timestamp * 1000).toLocaleTimeString(uiLocale())}
        </span>
      </button>
      {open && hasDetail && (
        <pre className="px-3 pb-2.5 pt-0 text-[11px] font-mono text-secondary whitespace-pre-wrap break-all max-h-64 overflow-y-auto">
          {JSON.stringify(evt.data, null, 2)}
        </pre>
      )}
    </div>
  );
});

export const StreamingSessionPage: React.FC = () => {
  useRequireAuth();
  const { t } = useTranslation();
  const { sessionKey } = useParams<{ sessionKey: string }>();

  const [events, setEvents] = useState<any[]>([]);
  const [selectedHandler, setSelectedHandler] = useState('');
  const [inputData, setInputData] = useState('{}');
  const [response, setResponse] = useState<any>(null);
  const [invoking, setInvoking] = useState(false);
  const [copied, setCopied] = useState('');
  const [endpointsOpen, setEndpointsOpen] = useState(false);
  const eventsEndRef = useRef<HTMLDivElement>(null);
  // Buffer SSE messages and flush on a timer so a burst of events causes a
  // handful of re-renders per second instead of one render per message.
  const eventBufferRef = useRef<any[]>([]);

  // Set from the session status below (see `wantedPoll`) — that reconciliation
  // runs during the same render, so any seed here would never reach the query.
  const [pollInterval, setPollInterval] = useState<number | undefined>(undefined);
  // True only after the SSE event stream has been down past a grace window, so a
  // normal EventSource reconnect blip doesn't flicker the indicator. Drives the
  // honest "the live view isn't live" hint; the 3s status poll remains the
  // authoritative liveness source and reconciles a session that actually stopped.
  const [streamStale, setStreamStale] = useState(false);
  const streamStaleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { data: session, error, loading, refresh } = useQuery(
    Q.key(`streaming-session-${sessionKey}`),
    () => streamingApi.getSession(sessionKey!),
    { pollInterval },
  );

  // The workflow behind this session — its `default_persona_id` is the login
  // identity the session signs in as. Fetched lazily once the session resolves
  // (its `workflow_id` is the anchor); the persona picker below links/unlinks it.
  const workflowId = session?.workflow_id;
  const { data: workflow, refresh: refreshWorkflow } = useQuery(
    Q.workflow(workflowId ?? 0),
    () => automationApi.getWorkflow(workflowId!),
    { enabled: workflowId != null },
  );

  // Link/unlink the persona on the WORKFLOW (the durable owner of the identity).
  // The Python agent resolves `default_persona_id` at session start — restoring
  // the persona's cookies and answering 2FA — so a change applies the next time
  // the session (re)starts, not to an already-running tab.
  const handlePersonaChange = useCallback(async (pid: number | null) => {
    if (workflowId == null) return;
    try {
      await automationApi.updateWorkflow(workflowId, { default_persona_id: pid });
      toast.success(pid ? t('Persona linked — used when the session next starts') : t('Persona unlinked'));
      refreshWorkflow();
    } catch {
      toast.error(t('Failed to update persona'));
    }
  }, [workflowId, refreshWorkflow, t]);

  // Adjust poll speed based on session status. The cadence depends on the very
  // payload the query returns, so it can't be computed before the call above —
  // it's reconciled DURING RENDER (React's adjust-state-from-props escape hatch)
  // instead of from an effect, which would cost a second render pass per poll.
  const wantedPoll = session?.status === 'running' ? 3000 : undefined;
  if (pollInterval !== wantedPoll) setPollInterval(wantedPoll);

  // The handler actually in play: the user's pick, falling back to the first one
  // the session exposes. Derived rather than seeded into state by an effect, so
  // the first handler is selected on the very frame the session payload lands.
  const activeHandler = selectedHandler || session?.handlers?.[0]?.name || '';

  // SSE events subscription.
  //
  // We deliberately do NOT use the browser EventSource API here: it cannot set an
  // Authorization header, but our access token is memory-only and the backend's
  // /events endpoint authenticates via `Bearer` (the httpOnly refresh cookie alone
  // does NOT satisfy get_auth_context). An EventSource therefore always 401s and
  // no live event ever arrives. We instead read the SSE stream over fetch(), which
  // carries the same bearer token as every other request — no token in the URL —
  // and we own the reconnect loop that EventSource used to give us for free.
  useEffect(() => {
    const clearStaleTimer = () => {
      if (streamStaleTimerRef.current) {
        clearTimeout(streamStaleTimerRef.current);
        streamStaleTimerRef.current = null;
      }
    };

    // Nothing to stream. The stale flag is cleared by this effect's CLEANUP (which
    // runs before every re-run, including the one that lands here), so it must not
    // be reset again from the effect body — that write is redundant and would
    // cascade an extra render on every status change.
    if (!sessionKey || session?.status !== 'running') {
      clearStaleTimer();
      return;
    }

    const ac = new AbortController();
    let closed = false;
    let seq = 0;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const markLive = () => {
      clearStaleTimer();
      setStreamStale(false);
    };

    // A transient drop must NOT flip the indicator immediately. Only mark the view
    // stale if the stream stays down past the grace window; the 3s status poll
    // independently reconciles a session that actually stopped.
    const markMaybeStale = () => {
      if (!streamStaleTimerRef.current) {
        streamStaleTimerRef.current = setTimeout(() => {
          streamStaleTimerRef.current = null;
          setStreamStale(true);
        }, 6000);
      }
    };

    const emit = (data: string) => {
      markLive();
      try {
        const event = JSON.parse(data);
        // Push to buffer; the flush interval moves it into state in batches.
        eventBufferRef.current.push({ ...event, _id: `${Date.now()}-${seq++}` });
      } catch {}
    };

    // One connection attempt. Resolves when the stream ends cleanly; throws on a
    // transport/auth error so the loop below decides whether to reconnect.
    const connectOnce = async () => {
      const open = (t: string | null) =>
        fetch(`/api/streaming/sessions/${sessionKey}/events`, {
          headers: {
            Accept: 'text/event-stream',
            ...(t ? { Authorization: `Bearer ${t}` } : {}),
          },
          credentials: 'include',
          signal: ac.signal,
        });

      let token = getAccessToken() ?? (await primeAccessToken());
      let resp = await open(token);
      // The access token can expire mid-session; this raw fetch bypasses the axios
      // 401-refresh interceptor, so mirror it once here.
      if (resp.status === 401) {
        token = await refreshAccessToken();
        if (!token) throw new Error('unauthorized');
        resp = await open(token);
      }
      if (!resp.ok || !resp.body) throw new Error(`sse ${resp.status}`);

      markLive();
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      // SSE frames are separated by a blank line; we emit one JSON object per
      // frame (joining its `data:` lines, ignoring comments/heartbeats).
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let sep: number;
        while ((sep = buf.indexOf('\n\n')) !== -1) {
          const frame = buf.slice(0, sep);
          buf = buf.slice(sep + 2);
          const payload = frame
            .split('\n')
            .filter((l) => l.startsWith('data:'))
            .map((l) => l.slice(5).replace(/^ /, ''))
            .join('\n');
          if (payload) emit(payload);
        }
      }
    };

    // Reconnect loop with capped backoff. Abort (cleanup) ends it silently.
    void (async () => {
      let backoff = 1000;
      while (!closed) {
        try {
          await connectOnce();
          backoff = 1000; // clean end (upstream closed) → reconnect promptly
        } catch (err: any) {
          if (closed || ac.signal.aborted) return;
          if (err?.message === 'unauthorized') {
            // No valid session — stop; the next API call routes to /login.
            markMaybeStale();
            return;
          }
        }
        if (closed) return;
        markMaybeStale();
        await new Promise<void>((r) => { reconnectTimer = setTimeout(r, backoff); });
        backoff = Math.min(backoff * 2, 10000);
      }
    })();

    const flush = setInterval(() => {
      if (eventBufferRef.current.length === 0) return;
      const batch = eventBufferRef.current;
      eventBufferRef.current = [];
      setEvents(prev => [...prev, ...batch].slice(-200));
    }, 400);

    return () => {
      closed = true;
      ac.abort();
      if (reconnectTimer) clearTimeout(reconnectTimer);
      clearInterval(flush);
      eventBufferRef.current = [];
      clearStaleTimer();
      setStreamStale(false);
    };
  }, [sessionKey, session?.status]);

  // Auto-scroll events
  useEffect(() => {
    eventsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [events]);

  const handleInvoke = useCallback(async () => {
    if (!sessionKey || !activeHandler) return;
    setInvoking(true);
    setResponse(null);
    try {
      let data = {};
      try { data = JSON.parse(inputData); } catch { data = { message: inputData }; }
      const result = await streamingApi.invokeHandler(sessionKey, activeHandler, data);
      setResponse(result);
    } catch (err: any) {
      setResponse({ error: err.response?.data?.detail || err.message });
    } finally {
      setInvoking(false);
    }
  }, [sessionKey, activeHandler, inputData]);

  const handleEnd = async () => {
    if (!sessionKey) return;
    await streamingApi.endSession(sessionKey);
    refresh();
  };

  const copyToClipboard = (text: string, label: string) => {
    navigator.clipboard.writeText(text);
    setCopied(label);
    setTimeout(() => setCopied(''), 2000);
  };

  if (!session) {
    if (!loading && error) {
      return (
        <div className="flex flex-col items-center justify-center min-h-[50vh] text-center px-6">
          <div className="w-12 h-12 rounded-2xl bg-zinc-100 border border-zinc-200 flex items-center justify-center mb-4">
            <SignalIcon className="h-6 w-6 text-zinc-400" />
          </div>
          <p className="text-sm font-medium text-ink">{t("Couldn't load this session")}</p>
          <p className="text-xs text-secondary mt-1 max-w-sm">{error}</p>
          <div className="mt-4 flex items-center gap-2">
            <button
              onClick={() => refresh()}
              className="px-4 py-2 bg-accent-strong text-accent-on text-sm font-semibold shadow-sm rounded-lg hover:bg-accent-strong/90 transition-colors"
            >
              {t('Retry')}
            </button>
            <Link
              to="/streaming"
              className="px-4 py-2 text-sm font-medium text-secondary border border-zinc-200 rounded-lg hover:bg-chrome hover:text-ink transition-colors"
            >
              {t('Back to Streaming')}
            </Link>
          </div>
        </div>
      );
    }
    return (
      <div className="flex flex-col items-center justify-center min-h-[50vh] text-center">
        <div className="w-2 h-2 rounded-full bg-zinc-300 animate-pulse mb-3" />
        <p className="text-xs text-tertiary">{t('Loading session…')}</p>
      </div>
    );
  }

  const isRunning = session.status === 'running';
  const isQueued = session.status === 'queued';

  // Session duration vs its hard cap — the one real "X / Y" limit on this page.
  // Elapsed is derived from start→(end | now); polling keeps a running session's
  // bar moving without an explicit elapsed field on the payload.
  const startedMs = session.started_at ? new Date(session.started_at).getTime() : 0;
  const endMs = session.ended_at ? new Date(session.ended_at).getTime() : Date.now();
  // Queued = not started running yet; keep the timer at 0 even if the backend
  // stamped started_at at enqueue time, so it doesn't look like it's already running.
  const elapsedSeconds = (startedMs && !isQueued) ? Math.max(0, Math.round((endMs - startedMs) / 1000)) : 0;
  const fmtDuration = (s: number) => {
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
  };

  const apiBase = `${window.location.origin}/api/streaming/sessions/${sessionKey}`;
  const handlerHint = (session.handlers || []).find(h => h.name === activeHandler);
  const placeholderInputs = handlerHint?.input_variables?.length
    ? `{${handlerHint.input_variables.map(v => `"${v}": "..."`).join(', ')}}`
    : '{"message": "Hello"}';

  const endpoints = [
    { label: t('Send command'), method: 'POST', path: `${apiBase}/command` },
    { label: t('Invoke handler'), method: 'POST', path: `${apiBase}/invoke/{handler}` },
    { label: t('Events (SSE)'), method: 'GET', path: `${apiBase}/events` },
    { label: t('WebSocket'), method: 'WS', path: `${apiBase}/ws` },
    { label: t('End session'), method: 'POST', path: `${apiBase}/end` },
    { label: t('Completions'), method: 'POST', path: `${apiBase}/v1/chat/completions` },
    { label: t('Models'), method: 'GET', path: `${apiBase}/v1/models` },
  ];

  return (
    <>
      <div className="flex flex-col h-full">
        {/* Toolbar */}
        <div className="flex items-center gap-3 h-12 px-4 sm:px-6 bg-chrome chrome-topbar border-b border-border shrink-0">
          <Link to="/streaming" className="p-1 text-tertiary hover:text-ink transition-colors">
            <ArrowLeftIcon className="w-3.5 h-3.5" />
          </Link>
          <SignalIcon className="w-4 h-4 text-tertiary shrink-0" />
          <span className="text-[13px] font-semibold text-ink truncate">{session.target_url}</span>
          {(() => {
            const s = statusStyle(session.status);
            return (
              <span className="relative flex w-2 h-2 shrink-0">
                {s.live && <span className={clsx('absolute inline-flex w-full h-full rounded-full opacity-60 animate-status-pulse', s.dot)} />}
                <span className={clsx('relative inline-flex w-2 h-2 rounded-full', s.dot)} />
              </span>
            );
          })()}
          <span className="hidden @pair/stage:inline text-[11px] text-tertiary">
            <span className="capitalize">{session.status}</span> &middot; {t('{{n}} handlers', { n: session.handlers?.length || 0 })}
            {session.events_emitted > 0 && <> &middot; {t('{{n}} events', { n: session.events_emitted })}</>}
          </span>
          {/* The status dot pulses "live" off the polled status; if the event
              stream has stayed dropped past the grace window, the view is no
              longer live even when the row still says running. Say so plainly. */}
          {isRunning && streamStale && (
            <span className="flex items-center gap-1 text-[11px] text-amber-600 shrink-0">
              <span className="w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse" />
              {t('Reconnecting…')}
            </span>
          )}

          <div className="flex-1" />

          {session.workflow_id != null && (
            <Link
              to={`/workflows/${session.workflow_id}?tab=connect`}
              className="hidden @pair/stage:flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium text-secondary border border-zinc-200 rounded-lg hover:bg-chrome hover:text-ink transition-colors shrink-0"
            >
              <ArrowTopRightOnSquareIcon className="w-3.5 h-3.5" />
              {t('Workflow')}
            </Link>
          )}

          {(isRunning || isQueued) && (
            <button
              onClick={handleEnd}
              className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium text-secondary border border-zinc-200 rounded-lg hover:bg-chrome hover:text-ink transition-colors shrink-0"
            >
              <StopCircleIcon className="w-4 h-4" />
              {isQueued ? t('Cancel') : t('End Session')}
            </button>
          )}
        </div>

        {/* Main content: 2-column working console (events log · control panel).
            @split/stage, not @pair — each pane is a dense JSON console + form,
            unusable at pair's ~250px-per-column width. */}
        <div className="flex-1 grid grid-cols-1 @split/stage:grid-cols-2 gap-4 min-h-0 p-4 sm:p-6">
          {/* Left: Events log */}
          <div className="flex flex-col bg-surface rounded-xl border border-ink/20 shadow-sm overflow-hidden">
            <div className="px-4 py-3 border-b border-border flex items-center justify-between">
              <div>
                <h2 className="text-base font-semibold text-ink tracking-tight">{t('Events')}</h2>
                <p className="text-[11px] text-tertiary mt-0.5">{t('Live stream from the session — click a row to expand its payload.')}</p>
              </div>
              <button
                onClick={() => setEvents([])}
                className="text-xs text-secondary hover:text-ink transition-colors"
              >
                {t('Clear')}
              </button>
            </div>
            <div className="flex-1 overflow-y-auto">
              {events.length === 0 ? (
                <p className="text-tertiary text-center py-8 text-xs">{t('No events yet')}</p>
              ) : (
                <div className="divide-y divide-border">
                  {events.map((evt, i) => (
                    <EventRow key={evt._id || i} evt={evt} />
                  ))}
                </div>
              )}
              <div ref={eventsEndRef} />
            </div>
          </div>

          {/* Right: ONE control panel, hairline-separated sections (not 3 cards) */}
          <div className="bg-surface rounded-xl border border-ink/20 shadow-sm overflow-y-auto flex flex-col min-h-0">
            {/* Session details — facts not surfaced in the toolbar (no dup) */}
            <div className="p-4">
              <SectionHead title={t('Session details')} description={t('Where the browser is and how much it has done.')} />
              {session.max_duration_seconds > 0 && (
                <div className="mb-3">
                  <UsageMeter
                    label={t('Time used')}
                    used={elapsedSeconds}
                    limit={session.max_duration_seconds}
                    format={fmtDuration}
                    note={isRunning ? t('Ends automatically at the limit') : undefined}
                  />
                </div>
              )}
              <dl className="divide-y divide-border border-t border-border">
                {session.current_url && (
                  <div className="flex items-start justify-between gap-3 py-2 min-w-0">
                    <dt className="text-[12px] text-tertiary shrink-0">{t('Current page')}</dt>
                    <dd className="text-[12px] text-ink font-mono truncate text-right min-w-0">{session.current_url}</dd>
                  </div>
                )}
                <div className="flex items-center justify-between gap-3 py-2">
                  <dt className="text-[12px] text-tertiary">{t('Commands received')}</dt>
                  <dd className="text-[12px] text-ink tabular-nums">{session.commands_received ?? 0}</dd>
                </div>
                {session.started_at && (
                  <div className="flex items-center justify-between gap-3 py-2">
                    <dt className="text-[12px] text-tertiary">{t('Started')}</dt>
                    <dd className="text-[12px] text-ink truncate">{formatRelativeTime(session.started_at)}</dd>
                  </div>
                )}
              </dl>
            </div>

            {/* Sign-in persona — the login identity this session runs as. Linked on
                the workflow; the agent applies it (cookies + 2FA) at session start. */}
            {workflow && (
              <div className="p-4 border-t border-border space-y-3">
                <SectionHead
                  title={t('Sign-in persona')}
                  description={t('The login identity this session signs in as — its saved cookies are restored and its 2FA answered automatically when the session starts.')}
                />
                <PersonaPicker
                  value={workflow.default_persona_id ?? null}
                  domain={(() => { try { return workflow.entry_url ? new URL(workflow.entry_url).hostname : undefined; } catch { return undefined; } })()}
                  onChange={handlePersonaChange}
                  allowClear
                />
                {isRunning && (
                  <p className="text-[11px] text-amber-600">
                    {t('A live session keeps the persona it launched with — end and relaunch to apply a change now.')}
                  </p>
                )}
              </div>
            )}

            {/* Invoke a handler */}
            <div className="p-4 border-t border-border space-y-3">
              <SectionHead title={t('Invoke a handler')} description={t('A handler is a callable action on this session. Pick one, send it JSON input, and see what it returns.')} />

              {(session.handlers || []).length === 0 ? (
                <p className="text-[12px] text-tertiary">{t('This session exposes no handlers.')}</p>
              ) : (
                <div className="flex flex-wrap gap-2">
                  {(session.handlers || []).map(h => (
                    <button
                      key={h.name}
                      onClick={() => setSelectedHandler(h.name)}
                      className={clsx(
                        'px-3 py-1.5 text-xs font-mono rounded-lg border transition-colors',
                        activeHandler === h.name
                          ? 'border-ink bg-ink text-white'
                          : 'border-border text-secondary hover:text-ink',
                      )}
                    >
                      {h.name}
                    </button>
                  ))}
                </div>
              )}

              {handlerHint && (
                <p className="text-[11px] text-tertiary">
                  {handlerHint.input_variables?.length
                    ? t('Expects: {{fields}}', { fields: handlerHint.input_variables.join(', ') })
                    : t('Takes no specific input — send {} or a free-text message.')}
                </p>
              )}

              <textarea
                value={inputData}
                onChange={(e) => setInputData(e.target.value)}
                rows={3}
                placeholder={placeholderInputs}
                className="w-full px-3 py-2 text-sm font-mono bg-hover border border-border rounded-lg text-ink resize-y"
                spellCheck={false}
              />

              <button
                onClick={handleInvoke}
                disabled={!isRunning || invoking || !activeHandler}
                className="flex items-center gap-2 px-4 py-2 bg-accent-strong text-accent-on text-sm font-semibold shadow-sm rounded-lg hover:bg-accent-strong/90 disabled:opacity-40"
              >
                {invoking ? (
                  <ArrowPathIcon className="w-4 h-4 animate-spin" />
                ) : (
                  <PaperAirplaneIcon className="w-4 h-4" />
                )}
                {t('Invoke')}
              </button>

              {response && (
                <div className="p-3 bg-hover rounded-lg border border-border">
                  <p className="text-xs font-medium text-secondary mb-1">{t('Response')}</p>
                  <pre className="text-xs font-mono text-ink whitespace-pre-wrap break-all">
                    {JSON.stringify(response, null, 2)}
                  </pre>
                </div>
              )}
            </div>

            {/* Call this session from code (collapsible) */}
            <div className="border-t border-border">
              <button
                onClick={() => setEndpointsOpen(o => !o)}
                className="w-full flex items-center gap-2 px-4 py-3 text-left hover:bg-chrome transition-colors"
              >
                <CodeBracketIcon className="w-4 h-4 text-tertiary shrink-0" />
                <div className="flex-1 min-w-0">
                  <span className="text-sm font-medium text-ink">{t('Call this session from code')}</span>
                  <p className="text-[11px] text-tertiary">{t('HTTP endpoints unique to this session — drive it from your own app or scripts.')}</p>
                </div>
                <ChevronDownIcon className={clsx('w-4 h-4 text-tertiary shrink-0 transition-transform', endpointsOpen && 'rotate-180')} />
              </button>
              {endpointsOpen && (
                <div className="px-4 pb-3 space-y-1.5">
                  {endpoints.map(ep => (
                    <div key={ep.label} className="flex items-center gap-2 py-1">
                      <span className="text-[10px] font-mono text-ink bg-hover px-1.5 py-0.5 rounded w-10 text-center shrink-0">{ep.method}</span>
                      <span className="text-[11px] text-tertiary w-24 shrink-0">{t(ep.label)}</span>
                      <code className="flex-1 truncate text-secondary text-[11px] font-mono">{ep.path}</code>
                      <button onClick={() => copyToClipboard(ep.path, ep.label)} className="p-1 text-secondary hover:text-ink shrink-0">
                        {copied === ep.label ? <CheckIcon className="w-3 h-3 text-ink" /> : <ClipboardDocumentIcon className="w-3 h-3" />}
                      </button>
                    </div>
                  ))}
                  <p className="text-[11px] text-tertiary pt-1">
                    {t('Auth:')} <code className="bg-hover px-1 py-0.5 rounded text-[10px]">Authorization: Bearer YOUR_API_KEY</code>.
                    {session.workflow_id != null && (
                      <> {t('Full snippets & publishing live in the')}{' '}
                        <Link to={`/workflows/${session.workflow_id}?tab=connect`} className="text-ink underline">{t('Connect tab')}</Link>.</>
                    )}
                  </p>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </>
  );
};
