import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useRequireAuth } from '../../hooks/useAuth';
import { useDocumentTitle } from '../../hooks/useDocumentTitle';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '../../hooks/useQuery';
import { Q } from '../../stores/queryKeys';
import { streamingApi, automationApi } from '../../api/endpoints';
import { RowsSkeleton } from '../../components/library/shelf';
import { StreamingSession } from '../../types/api';
import { statusStyle } from '../../utils/statusStyle';
import { SectionHead } from '../../components/common/SectionHead';
import { formatRelativeTime, formatDate, cleanTitle, uiLocale } from '../../utils/format';
import toast from 'react-hot-toast';
import {
  PlusIcon,
  SignalIcon,
  StopCircleIcon,
  PlayIcon,
  CursorArrowRaysIcon,
  ExclamationTriangleIcon,
  BoltIcon,
  ClockIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';

const hostOf = (url?: string): string | undefined => {
  if (!url) return undefined;
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return url; }
};

export const StreamingListPage: React.FC = () => {
  useRequireAuth();
  const { t } = useTranslation();
  useDocumentTitle(t('Streaming'));
  const navigate = useNavigate();
  const [filter, setFilter] = useState<string>('');
  const [starting, setStarting] = useState<number | null>(null);
  const [endedPage, setEndedPage] = useState(0);
  const ENDED_PAGE_SIZE = 10;

  // "Now" for the still-running durations below. Reading `Date.now()` during
  // render is impure — the clock moves without React knowing, so two renders of
  // one commit can disagree (`react-hooks/purity`). Refreshed on the same 5s
  // cadence as the session poll, so the numbers advance exactly as before.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 5000);
    return () => window.clearInterval(id);
  }, []);

  // Streaming sessions (active/ended) — key includes the status filter so
  // switching tabs refetches immediately instead of waiting on the poll
  const { data: sessions, loading: sessionsLoading, error: sessionsError, refresh: refreshSessions } = useQuery<StreamingSession[]>(
    Q.streamingSessions(filter || undefined),
    () => streamingApi.listSessions(filter || undefined),
    { pollInterval: 5000 },
  );

  // Streaming workflows (templates)
  const { data: workflows, loading: workflowsLoading, error: workflowsError } = useQuery(
    Q.workflows(),
    () => automationApi.listWorkflows(),
  );

  const sessionList = sessions || [];
  const streamingWorkflows = (workflows || []).filter((w: any) => w.workflow_type === 'streaming');
  const activeStatuses = new Set(['running', 'starting', 'queued', 'ending']);
  // "Live" = a browser is actually up (consuming a concurrent slot). A queued
  // session is only waiting for a slot — it isn't running, so it must not
  // inflate the live count or fire the uptime timer.
  const liveStatuses = new Set(['running', 'starting', 'ending']);
  const activeSessions = sessionList.filter(s => activeStatuses.has(s.status));
  const endedSessions = sessionList.filter(s => !activeStatuses.has(s.status));
  const liveSessions = sessionList.filter(s => liveStatuses.has(s.status));
  const activeCount = activeSessions.length;
  const liveCount = liveSessions.length;

  // Live elapsed (start→end | now) for the "how long has this been up" column.
  const fmtDuration = (s: number) => {
    if (s <= 0) return '—';
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h > 0) return `${h}h ${m}m`;
    return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
  };
  const sessionDurationSecs = (session: StreamingSession): number => {
    // A queued session hasn't started running — don't tick a timer for it even
    // if the backend stamped started_at at enqueue time.
    if (!session.started_at || session.status === 'queued') return 0;
    const startMs = new Date(session.started_at).getTime();
    const endMs = session.ended_at ? new Date(session.ended_at).getTime() : now;
    return Math.max(0, Math.round((endMs - startMs) / 1000));
  };
  const sessionDuration = (session: StreamingSession): string => {
    const secs = sessionDurationSecs(session);
    return secs > 0 ? fmtDuration(secs) : '—';
  };

  // Live-usage rollup for the overview strip ("see live running sessions, usage").
  const totalEvents = sessionList.reduce((sum, s) => sum + (s.events_emitted || 0), 0);
  const liveUptimeSecs = liveSessions.reduce((sum, s) => sum + sessionDurationSecs(s), 0);
  // Self-host has no plan concurrency cap surfaced to the UI.
  const concurrentLimit: number | undefined = undefined;

  // Ended-sessions pagination window.
  const pagedEnded = endedSessions.slice(endedPage * ENDED_PAGE_SIZE, endedPage * ENDED_PAGE_SIZE + ENDED_PAGE_SIZE);

  const handleEnd = async (sessionKey: string) => {
    try {
      await streamingApi.endSession(sessionKey);
      toast.success(t('Session ended'));
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('Failed to end session'));
    } finally {
      refreshSessions();
    }
  };

  const handleStart = async (workflowId: number, entryUrl?: string) => {
    if (!entryUrl) {
      toast.error(t('Workflow has no entry URL — edit it first'));
      return;
    }
    setStarting(workflowId);
    try {
      await streamingApi.startSession({
        workflow_id: workflowId,
        target_url: entryUrl,
      });
      toast.success(t('Streaming session started'));
      refreshSessions();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('Failed to start session'));
    } finally {
      setStarting(null);
    }
  };

  // One scannable row per session. Used for both the Active and Ended groups so
  // they stay visually identical apart from which group they're filed under.
  const renderSessionRow = (session: StreamingSession) => {
    const s = statusStyle(session.status);
    const isActive = activeStatuses.has(session.status);
    const timeRef = (session.ended_at || session.last_activity_at || session.started_at) as string | undefined;
    return (
      <div
        key={session.session_key}
        onClick={() => navigate(`/streaming/${session.session_key}`)}
        className="flex items-center gap-3 py-3 hover:bg-chrome cursor-pointer transition-colors"
      >
        <span className="relative flex w-2 h-2 shrink-0">
          {s.live && <span className={clsx('absolute inline-flex w-full h-full rounded-full opacity-60 animate-status-pulse', s.dot)} />}
          <span className={clsx('relative inline-flex w-2 h-2 rounded-full', s.dot)} />
        </span>
        <div className="flex-1 min-w-0">
          <p className="text-[13px] font-medium text-ink truncate">{session.target_url}</p>
          <div className="flex items-center gap-2 mt-0.5">
            <span className={clsx('text-[10px] px-1.5 py-0.5 rounded font-medium capitalize', s.pill)}>
              {session.status}
            </span>
            <span className="text-[11px] text-tertiary">
              {t('{{n}} handlers', { n: session.handlers?.length || 0 })}
              {session.events_emitted > 0 && <> · {t('{{n}} events', { n: session.events_emitted })}</>}
            </span>
          </div>
        </div>
        {/* Time + duration — the "when / how long" the wall-of-cards buried */}
        <div className="hidden @pair/stage:flex flex-col items-end shrink-0 mr-1">
          {timeRef && (
            <span className="text-[11px] text-secondary tabular-nums" title={formatDate(timeRef)}>
              {isActive ? t('Started {{ago}}', { ago: formatRelativeTime(timeRef) }) : formatRelativeTime(timeRef)}
            </span>
          )}
          <span className="text-[11px] text-tertiary tabular-nums">{sessionDuration(session)}</span>
        </div>
        <button
          onClick={(e) => { e.stopPropagation(); navigate(`/streaming/${session.session_key}`); }}
          className="px-2.5 py-1 text-[11px] font-medium text-secondary border border-zinc-200 rounded-lg hover:bg-chrome hover:text-ink transition-colors shrink-0"
        >
          {t('Open')}
        </button>
        {session.status === 'running' && (
          <button
            onClick={(e) => { e.stopPropagation(); handleEnd(session.session_key); }}
            className="p-1.5 text-tertiary hover:text-ink transition-colors shrink-0"
            title={t('End session')}
          >
            <StopCircleIcon className="w-4 h-4" />
          </button>
        )}
      </div>
    );
  };

  const hasContent = streamingWorkflows.length > 0 || sessionList.length > 0;
  const initialLoading = (sessionsLoading || workflowsLoading) && !hasContent;
  const loadFailed = !hasContent && !initialLoading && Boolean(sessionsError || workflowsError);

  return (
    <>
      <div className="flex flex-col h-full">
        {/* Toolbar */}
        <div className="flex items-center gap-3 h-12 px-4 sm:px-6 bg-chrome chrome-topbar border-b border-border shrink-0">
          <SignalIcon className="w-4 h-4 text-tertiary shrink-0" />
          <span className="text-[13px] font-semibold text-ink shrink-0">{t('Streaming')}</span>
          <span className="hidden @pair/stage:inline text-[11px] text-tertiary ml-1">
            {streamingWorkflows.length === 1 ? t('{{n}} workflow', { n: streamingWorkflows.length }) : t('{{n}} workflows', { n: streamingWorkflows.length })} · {t('{{n}} active', { n: activeCount })}
          </span>

          <div className="flex-1" />

          {sessionList.length > 0 && (
            <div className="hidden @pair/stage:flex items-center gap-0.5">
              {['', 'running', 'ended', 'failed'].map(s => (
                <button
                  key={s}
                  onClick={() => setFilter(s)}
                  className={clsx(
                    'px-2.5 py-1 text-[12px] font-medium rounded-md transition-colors capitalize',
                    filter === s ? 'bg-zinc-100 text-ink' : 'text-tertiary hover:text-secondary',
                  )}
                >
                  {s || t('All')}
                </button>
              ))}
            </div>
          )}

          {hasContent && (
            <button
              data-tour="streaming-new"
              onClick={() => navigate('/workflows/new')}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-accent-strong text-accent-on text-[12px] font-semibold shadow-sm rounded-lg hover:bg-accent-strong/90 transition-colors shrink-0"
            >
              <PlusIcon className="w-3.5 h-3.5" />
              {t('New')}
            </button>
          )}
        </div>

        {/* Content */}
        <div className="flex-1 relative overflow-auto">
          <div
            className="absolute inset-0 pointer-events-none"
            style={{
              backgroundImage: 'radial-gradient(circle, #d4d4d8 1px, transparent 1px)',
              backgroundSize: '24px 24px',
              opacity: 0.12,
            }}
          />
          <div className="relative z-10 py-4 px-4 sm:px-6 space-y-6">

            {/* Loading state */}
            {initialLoading && <RowsSkeleton rows={6} label={t('Loading sessions…')} />}

            {/* Error state */}
            {loadFailed && (
              <div className="flex flex-col items-center justify-center min-h-[50vh] text-center">
                <div className="w-12 h-12 rounded-2xl bg-zinc-100 border border-zinc-200 flex items-center justify-center mb-4">
                  <ExclamationTriangleIcon className="h-6 w-6 text-zinc-400" />
                </div>
                <p className="text-sm font-medium text-ink">{t("Couldn't load streaming sessions")}</p>
                <p className="text-xs text-secondary mt-1">{sessionsError || workflowsError}</p>
                <button
                  onClick={() => refreshSessions()}
                  className="mt-4 px-4 py-2 bg-accent-strong text-accent-on text-sm font-semibold shadow-sm rounded-lg hover:bg-accent-strong/90 transition-colors"
                >
                  {t('Retry')}
                </button>
              </div>
            )}

            {/* Empty state */}
            {!hasContent && !initialLoading && !loadFailed && (
              <div className="flex flex-col items-center justify-center min-h-[50vh] text-center">
                <div className="w-12 h-12 rounded-2xl bg-zinc-100 border border-zinc-200 flex items-center justify-center mb-4">
                  <SignalIcon className="h-6 w-6 text-zinc-400" />
                </div>
                <p className="text-sm font-medium text-ink">{t('No streaming workflows yet')}</p>
                <p className="text-xs text-secondary mt-1">{t('Create a streaming workflow to keep a browser session alive with callable handlers')}</p>
                <button
                  onClick={() => navigate('/workflows/new')}
                  className="mt-4 px-4 py-2 bg-accent-strong text-accent-on text-sm font-semibold shadow-sm rounded-lg hover:bg-accent-strong/90 transition-colors"
                >
                  {t('New Streaming Workflow')}
                </button>
              </div>
            )}

            {/* Live usage — see what's running and how it draws on your plan */}
            {hasContent && (
              <div className="bg-surface rounded-xl border border-ink/20 shadow-sm grid grid-cols-2 @split/stage:grid-cols-4 divide-x divide-y @split/stage:divide-y-0 divide-border">
                <div className="p-5">
                  <span className="text-[11px] uppercase tracking-wide text-tertiary font-medium inline-flex items-center gap-1.5">
                    <SignalIcon className="w-3.5 h-3.5" />
                    {t('Live now')}
                  </span>
                  <p className={clsx('text-xl font-semibold mt-1.5 tabular-nums', liveCount > 0 ? 'text-emerald-600' : 'text-ink')}>
                    {liveCount}
                    {concurrentLimit != null && concurrentLimit > 0 && (
                      <span className="text-sm text-tertiary font-normal"> / {concurrentLimit}</span>
                    )}
                  </p>
                  <p className="text-[11px] text-tertiary mt-0.5">
                    {concurrentLimit != null && concurrentLimit > 0 ? t('of your concurrent browsers') : t('browsers running now')}
                  </p>
                </div>
                <div className="p-5">
                  <span className="text-[11px] uppercase tracking-wide text-tertiary font-medium inline-flex items-center gap-1.5">
                    <BoltIcon className="w-3.5 h-3.5" />
                    {t('Events streamed')}
                  </span>
                  <p className="text-xl font-semibold text-ink mt-1.5 tabular-nums">{totalEvents.toLocaleString(uiLocale())}</p>
                  <p className="text-[11px] text-tertiary mt-0.5">{t('emitted across sessions')}</p>
                </div>
                <div className="p-5">
                  <span className="text-[11px] uppercase tracking-wide text-tertiary font-medium inline-flex items-center gap-1.5">
                    <ClockIcon className="w-3.5 h-3.5" />
                    {t('Live uptime')}
                  </span>
                  <p className="text-xl font-semibold text-ink mt-1.5 tabular-nums">{liveCount > 0 ? fmtDuration(liveUptimeSecs) : '—'}</p>
                  <p className="text-[11px] text-tertiary mt-0.5">{t('across active sessions')}</p>
                </div>
                <div className="p-5">
                  <span className="text-[11px] uppercase tracking-wide text-tertiary font-medium inline-flex items-center gap-1.5">
                    <CursorArrowRaysIcon className="w-3.5 h-3.5" />
                    {t('Streaming workflows')}
                  </span>
                  <p className="text-xl font-semibold text-ink mt-1.5 tabular-nums">{streamingWorkflows.length}</p>
                  <p className="text-[11px] text-tertiary mt-0.5">{t('ready to launch')}</p>
                </div>
              </div>
            )}

            {/* Dashboard: live sessions + launcher are the PRINCIPAL surface (main);
                ended run history is informational (sidebar). */}
            {hasContent && (
              <div className="grid grid-cols-1 @split/stage:grid-cols-4 gap-x-8 gap-y-8 items-start">
                {/* MAIN — the streaming sessions: what's live + what you can launch */}
                <div className="@split/stage:col-span-3 min-w-0 space-y-8">
                  {/* Active sessions — live now, the hero of this page */}
                  {activeSessions.length > 0 && (
                    <section>
                      <SectionHead
                        title={t('Active sessions')}
                        description={t('Live browsers running now. Open one to invoke its handlers, or end it to free the slot.')}
                        right={(
                          <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-emerald-600">
                            <span className="relative flex w-2 h-2">
                              <span className="absolute inline-flex w-full h-full rounded-full bg-emerald-500 opacity-60 animate-status-pulse" />
                              <span className="relative inline-flex w-2 h-2 rounded-full bg-emerald-500" />
                            </span>
                            {t('{{n}} running', { n: liveCount })}
                          </span>
                        )}
                      />
                      <div className="border-y border-border divide-y divide-border">
                        {activeSessions.map((session) => renderSessionRow(session))}
                      </div>
                    </section>
                  )}

                  {/* Start a live session — the launcher (principal action) */}
                  {streamingWorkflows.length > 0 && (
                    <section>
                      <SectionHead
                        title={t('Start a live session')}
                        description={t('Launch one of your streaming workflows to open a warm browser with callable handlers.')}
                      />
                      <div className="border-y border-border divide-y divide-border">
                        {streamingWorkflows.map((w: any) => (
                          <div key={w.id} className="flex items-center gap-3 py-3">
                            <CursorArrowRaysIcon className="w-4 h-4 text-tertiary shrink-0" />
                            <div className="flex-1 min-w-0">
                              <p className="text-[13px] font-medium text-ink truncate">{cleanTitle(w.name, t('Untitled'))}</p>
                              <div className="flex items-center gap-2 mt-0.5 text-[11px] text-tertiary">
                                {w.entry_url && <span className="truncate max-w-[320px] font-mono">{w.entry_url}</span>}
                                {w.steps?.length > 0 && <span>· {t('{{n}} setup steps', { n: w.steps.length })}</span>}
                                {w.streaming_config?.advanced_script?.enabled && <span>· {t('Custom script')}</span>}
                              </div>
                            </div>
                            <button
                              onClick={() => handleStart(w.id, w.entry_url)}
                              disabled={starting === w.id}
                              className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-semibold shadow-sm bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 disabled:opacity-50 transition-colors shrink-0"
                            >
                              <PlayIcon className="w-3.5 h-3.5" />
                              {starting === w.id ? t('Starting…') : t('Start session')}
                            </button>
                            <button
                              onClick={() => navigate(`/workflows/${w.id}`)}
                              className="text-[11px] text-tertiary hover:text-ink transition-colors shrink-0"
                            >
                              {t('Edit')}
                            </button>
                          </div>
                        ))}
                      </div>
                    </section>
                  )}

                  {activeSessions.length === 0 && streamingWorkflows.length === 0 && (
                    <p className="text-[13px] text-tertiary py-2">
                      {t('No streaming workflows yet. Create one to open a live session.')}
                    </p>
                  )}
                </div>

                {/* SIDEBAR — run history (informational) + how it works */}
                <aside className="@split/stage:col-span-1 min-w-0 space-y-8">
                  {endedSessions.length > 0 && (
                    <section>
                      <SectionHead
                        title={t('Recent sessions')}
                        right={<span className="text-[11px] text-tertiary tabular-nums">{t('{{n}} total', { n: endedSessions.length })}</span>}
                      />
                      <div className="border-y border-border divide-y divide-border">
                        {pagedEnded.map((session) => {
                          const s = statusStyle(session.status);
                          const timeRef = (session.ended_at || session.last_activity_at || session.started_at) as string | undefined;
                          return (
                            <button
                              key={session.session_key}
                              onClick={() => navigate(`/streaming/${session.session_key}`)}
                              className="w-full flex items-center gap-2 py-2 text-left hover:bg-chrome transition-colors"
                            >
                              <span className={clsx('w-1.5 h-1.5 rounded-full shrink-0', s.dot)} />
                              <span className="text-[12px] text-ink truncate flex-1 min-w-0">{hostOf(session.target_url) || session.target_url}</span>
                              <span className="text-[11px] text-tertiary tabular-nums shrink-0">{timeRef ? formatRelativeTime(timeRef) : ''}</span>
                            </button>
                          );
                        })}
                      </div>
                      {endedSessions.length > ENDED_PAGE_SIZE && (
                        <div className="flex items-center justify-between pt-2.5">
                          <span className="text-[11px] text-tertiary tabular-nums">
                            {t('{{from}}–{{to}} of {{total}}', {
                              from: endedPage * ENDED_PAGE_SIZE + 1,
                              to: Math.min((endedPage + 1) * ENDED_PAGE_SIZE, endedSessions.length),
                              total: endedSessions.length,
                            })}
                          </span>
                          <div className="flex items-center gap-1.5">
                            <button
                              onClick={() => setEndedPage((p) => Math.max(0, p - 1))}
                              disabled={endedPage === 0}
                              className="px-2 py-0.5 text-[11px] font-medium text-secondary border border-border rounded-md hover:text-ink disabled:opacity-40 disabled:pointer-events-none transition-colors"
                            >
                              {t('Prev')}
                            </button>
                            <button
                              onClick={() => setEndedPage((p) => p + 1)}
                              disabled={(endedPage + 1) * ENDED_PAGE_SIZE >= endedSessions.length}
                              className="px-2 py-0.5 text-[11px] font-medium text-secondary border border-border rounded-md hover:text-ink disabled:opacity-40 disabled:pointer-events-none transition-colors"
                            >
                              {t('Next')}
                            </button>
                          </div>
                        </div>
                      )}
                    </section>
                  )}

                  {/* How streaming works */}
                  <section>
                    <SectionHead title={t('How streaming works')} />
                    <div className="rounded-xl border border-ink/20 bg-surface p-4 text-[12px] text-secondary leading-relaxed space-y-2 shadow-sm">
                      <p>{t('A streaming session keeps one browser logged in and warm so you can call its handlers on demand — a live, callable version of a workflow.')}</p>
                      <p className="text-tertiary">{t('Open a running session to invoke handlers and watch events stream in. Each live session uses one of your concurrent browsers.')}</p>
                    </div>
                  </section>
                </aside>
              </div>
            )}

          </div>
        </div>
      </div>
    </>
  );
};
