import { useEffect } from 'react';
import { getAccessToken } from '../utils/auth';
import { useLiveRuns } from '../stores/liveRuns';
import { invalidateRunFeed } from '../api/endpoints';

/**
 * Subscribes to the backend's per-tenant run-events SSE stream (`GET
 * /api/runs/events`). On every event it makes the whole app reflect the change
 * instantly:
 *   - bumps the live-runs epoch, so {@link useActivity} refetches now, and
 *   - marks the run feed stale, so every mounted runs query background-revalidates.
 *
 * Uses `fetch` (not `EventSource`) so it can attach the bearer token. Reconnects
 * with capped backoff and drops a stale stream when the tab regains focus. The
 * stream is a pure accelerator: the frontend still polls while work is live, so a
 * dropped connection or a missed (e.g. cross-worker) event only costs one poll
 * interval, never correctness. Mount this ONCE, high in the tree (the app shell).
 */
export function useRunEventStream() {
  useEffect(() => {
    let stopped = false;
    let controller: AbortController | null = null;
    let wakeTimer: ReturnType<typeof setTimeout> | null = null;

    const onEvent = () => {
      useLiveRuns.getState().signal();
      try {
        invalidateRunFeed();
      } catch {
        /* invalidation is best-effort */
      }
    };

    const wait = (ms: number) =>
      new Promise<void>((resolve) => {
        wakeTimer = setTimeout(resolve, ms);
      });

    const loop = async () => {
      let retry = 0;
      while (!stopped) {
        const token = getAccessToken();
        if (document.hidden || !token) {
          // Not visible or not authenticated yet — idle briefly, then re-check.
          await wait(1500);
          continue;
        }
        controller = new AbortController();
        try {
          const resp = await fetch('/api/runs/events', {
            headers: { Authorization: `Bearer ${token}`, Accept: 'text/event-stream' },
            signal: controller.signal,
            cache: 'no-store',
          });
          if (!resp.ok || !resp.body) throw new Error(`sse ${resp.status}`);
          retry = 0; // healthy connection resets backoff
          const reader = resp.body.getReader();
          const decoder = new TextDecoder();
          let buffer = '';
          while (!stopped) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            // SSE frames are separated by a blank line. A frame with a `data:`
            // line is a run-changed nudge; comment frames (`: ping`) are ignored.
            let idx: number;
            while ((idx = buffer.indexOf('\n\n')) >= 0) {
              const frame = buffer.slice(0, idx);
              buffer = buffer.slice(idx + 2);
              if (frame.split('\n').some((l) => l.startsWith('data:'))) onEvent();
            }
          }
        } catch {
          /* aborted or network error — fall through to backoff + reconnect */
        }
        if (stopped) break;
        await wait(Math.min(15000, 1000 * 2 ** retry++));
      }
    };

    void loop();

    // Returning to the tab: abort the (likely-stale) stream so the loop reconnects.
    const onVisible = () => {
      if (!document.hidden) controller?.abort();
    };
    document.addEventListener('visibilitychange', onVisible);

    return () => {
      stopped = true;
      controller?.abort();
      if (wakeTimer) clearTimeout(wakeTimer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, []);
}

export default useRunEventStream;
