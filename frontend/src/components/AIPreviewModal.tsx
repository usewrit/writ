import React, { useState, useEffect, useRef } from 'react';
import { Dialog, Transition } from '@headlessui/react';
import { useTranslation } from 'react-i18next';
import {
  XMarkIcon,
  EyeIcon,
  SignalIcon,
  SignalSlashIcon,
} from '@heroicons/react/24/outline';
import toast from 'react-hot-toast';

interface AIPreviewModalProps {
  isOpen: boolean;
  onClose: () => void;
  sessionId: string;  // The recorder/streaming session id (session_key or ai-{id}) to spectate
  sessionName?: string;
}

type ConnectionState = 'disconnected' | 'connecting' | 'connected' | 'error';

/**
 * Live spectate preview — connects to the spectate WebSocket at
 * `/ws/spectate/{sessionId}` (proxied to the owning recorder/agent) and draws the
 * binary screencast frames (`[4B BE url_len][url][jpeg]`, the same wire format as
 * `/ws/record`). Read-only.
 *
 * NOTE: frames arrive as BINARY, not JSON. An older revision of this modal gated
 * the connection on a legacy `recorder_url` localStorage key (never set) and only
 * handled JSON `screenshot` frames — so it errored before connecting and, even
 * connected, drew nothing. Both are fixed here to match the desktop preview.
 */
export const AIPreviewModal: React.FC<AIPreviewModalProps> = ({
  isOpen,
  onClose,
  sessionId,
  sessionName,
}) => {
  const { t } = useTranslation();
  const [connectionState, setConnectionState] = useState<ConnectionState>('disconnected');
  const [currentUrl, setCurrentUrl] = useState<string>('');

  const wsRef = useRef<WebSocket | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const pingIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastFrameUrlRef = useRef<string>('');
  // Timestamp of the last frame received from the agent (screenshot OR pong).
  // Used to detect a HALF-OPEN socket: TCP still looks OPEN but nothing is
  // arriving because the recorder/agent died. Without this the modal shows a
  // frozen screenshot under a "connected" badge indefinitely.
  const lastRxRef = useRef<number>(0);

  const connectToSession = () => {
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProtocol}//${window.location.host}/ws/spectate/${encodeURIComponent(sessionId)}`;

    // Supersede any existing socket, detaching its handlers so its async
    // onclose/onerror can't fire after the new socket is assigned and clobber
    // the live ref/state or clear the new socket's ping interval (sessionId
    // change / StrictMode double-mount race).
    if (wsRef.current) {
      const stale = wsRef.current;
      stale.onopen = stale.onmessage = stale.onerror = stale.onclose = null;
      try { stale.close(); } catch { /* noop */ }
      wsRef.current = null;
    }

    try {
      const ws = new WebSocket(wsUrl);
      ws.binaryType = 'arraybuffer';
      wsRef.current = ws;

      ws.onopen = () => {
        setConnectionState('connected');
        lastRxRef.current = Date.now();

        // Ping keepalive AND half-open detection. If nothing has arrived (no
        // screenshot, no pong) for well over a ping cycle, the stream is dead
        // even though the socket reports OPEN — close it so the UI stops showing
        // a frozen "connected" preview instead of pinging into the void forever.
        pingIntervalRef.current = setInterval(() => {
          if (ws.readyState !== WebSocket.OPEN) return;
          if (lastRxRef.current && Date.now() - lastRxRef.current > 45000) {
            setConnectionState('error');
            toast.error(t('Live preview stalled — connection lost'));
            try { ws.close(); } catch { /* noop */ }
            return;
          }
          ws.send(JSON.stringify({ type: 'ping' }));
        }, 30000);
      };

      ws.onmessage = (event) => {
        lastRxRef.current = Date.now();

        // Binary = a screencast frame: [4B BE url_len][url][jpeg].
        if (event.data instanceof ArrayBuffer) {
          const buf = event.data as ArrayBuffer;
          if (buf.byteLength < 4) return;
          const dv = new DataView(buf);
          const urlLen = dv.getUint32(0);
          if (urlLen > 0 && urlLen < 2048 && 4 + urlLen <= buf.byteLength) {
            const frameUrl = new TextDecoder().decode(new Uint8Array(buf, 4, urlLen));
            if (frameUrl && frameUrl !== lastFrameUrlRef.current) {
              lastFrameUrlRef.current = frameUrl;
              setCurrentUrl(frameUrl);
            }
          }
          const jpeg = buf.slice(4 + urlLen);
          const canvas = canvasRef.current;
          if (canvas && typeof createImageBitmap !== 'undefined') {
            createImageBitmap(new Blob([jpeg], { type: 'image/jpeg' }))
              .then((bitmap) => {
                const ctx = canvas.getContext('2d');
                if (!ctx) return;
                if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
                  canvas.width = bitmap.width;
                  canvas.height = bitmap.height;
                }
                ctx.drawImage(bitmap, 0, 0);
                bitmap.close();
              })
              .catch(() => { /* stale frame */ });
          }
          return;
        }

        // Text = a JSON control frame.
        let data: any;
        try { data = JSON.parse(event.data); } catch { return; }
        switch (data.type) {
          case 'spectate_started':
            if (data.url) setCurrentUrl(data.url);
            break;

          // Some paths may still send base64 JSON frames — draw them too.
          case 'screenshot':
            if (data.url) setCurrentUrl(data.url);
            if (data.data) {
              const canvas = canvasRef.current;
              if (canvas && typeof createImageBitmap !== 'undefined') {
                fetch(`data:image/jpeg;base64,${data.data}`)
                  .then((r) => r.blob())
                  .then((blob) => createImageBitmap(blob))
                  .then((bitmap) => {
                    const ctx = canvas.getContext('2d');
                    if (!ctx) return;
                    if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
                      canvas.width = bitmap.width;
                      canvas.height = bitmap.height;
                    }
                    ctx.drawImage(bitmap, 0, 0);
                    bitmap.close();
                  })
                  .catch(() => { /* noop */ });
              }
            }
            break;

          case 'error':
            toast.error(data.message || t('Connection error'));
            setConnectionState('error');
            break;

          case 'spectate_ended':
            // Agent-reported terminal state: the page behind the stream is gone
            // (browser died or the session ended). Without this the frames just
            // stop and the modal keeps waiting on a socket that will never paint.
            toast.error(
              data.reason === 'browser_closed'
                ? t('The browser session ended or failed — there is nothing left to stream.')
                : t('The session has ended.'),
            );
            setConnectionState('error');
            break;

          case 'pong':
          default:
            break;
        }
      };

      ws.onerror = () => {
        if (wsRef.current !== ws) return;
        setConnectionState('error');
        toast.error(t('Connection error'));
      };

      ws.onclose = () => {
        // A superseded socket's late close must not flip state to
        // 'disconnected' or clear the new socket's ping interval.
        if (wsRef.current !== ws) return;
        setConnectionState((s) => (s === 'error' ? s : 'disconnected'));
        if (pingIntervalRef.current) {
          clearInterval(pingIntervalRef.current);
          pingIntervalRef.current = null;
        }
      };

    } catch {
      // A `new WebSocket()` throw is the same class of event as `onerror`, and
      // the browser delivers those asynchronously. Reporting it in the same tick
      // would overwrite the 'connecting' state before it ever painted, and a
      // setState reachable synchronously from the effect that opened the
      // connection costs an extra render pass
      // (`react-hooks/set-state-in-effect`).
      queueMicrotask(() => {
        setConnectionState('error');
        toast.error(t('Failed to connect'));
      });
    }
  };

  const disconnect = () => {
    if (pingIntervalRef.current) {
      clearInterval(pingIntervalRef.current);
      pingIntervalRef.current = null;
    }
    if (wsRef.current) {
      const ws = wsRef.current;
      ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null;
      try { ws.close(); } catch { /* noop */ }
      wsRef.current = null;
    }
    setConnectionState('disconnected');
    lastFrameUrlRef.current = '';
  };

  // Opening the modal (or pointing it at another session) shows "connecting"
  // immediately. Done in render off that edge rather than at the top of
  // connectToSession: a setState reachable synchronously from an effect body only
  // lands after paint, so the badge would show the previous session's state for a
  // frame and cost an extra render (`react-hooks/set-state-in-effect`).
  const connectKey = isOpen && sessionId ? sessionId : null;
  const [connectingFor, setConnectingFor] = useState<string | null>(null);
  if (connectingFor !== connectKey) {
    setConnectingFor(connectKey);
    setConnectionState(connectKey ? 'connecting' : 'disconnected');
  }

  // Connect to the spectate WebSocket when the modal opens. Declared AFTER
  // connectToSession/disconnect, not before them: reaching backwards past a
  // `const` is a temporal-dead-zone access, and the effect would keep whichever
  // binding existed when it ran rather than the current one
  // (`react-hooks/immutability`).
  useEffect(() => {
    if (isOpen && sessionId) {
      connectToSession();
    }
    return () => {
      disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen, sessionId]);

  const handleClose = () => {
    disconnect();
    onClose();
  };

  return (
    <Transition show={isOpen} as={React.Fragment}>
      <Dialog as="div" className="relative z-50" onClose={handleClose}>
        <Transition.Child
          as={React.Fragment}
          enter="ease-out duration-300"
          enterFrom="opacity-0"
          enterTo="opacity-100"
          leave="ease-in duration-200"
          leaveFrom="opacity-100"
          leaveTo="opacity-0"
        >
          <div className="fixed inset-0 bg-black/60" />
        </Transition.Child>

        <div className="fixed inset-0 overflow-y-auto">
          <div className="flex min-h-full items-center justify-center p-4">
            <Transition.Child
              as={React.Fragment}
              enter="ease-out duration-300"
              enterFrom="opacity-0 scale-95"
              enterTo="opacity-100 scale-100"
              leave="ease-in duration-200"
              leaveFrom="opacity-100 scale-100"
              leaveTo="opacity-0 scale-95"
            >
              <Dialog.Panel className="w-full max-w-5xl transform overflow-hidden bg-surface border border-border rounded-xl shadow-xl transition-all">
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-border">
                  <div className="flex items-center gap-3 min-w-0">
                    <EyeIcon className="h-6 w-6 text-tertiary shrink-0" />
                    <div className="min-w-0">
                      <Dialog.Title className="text-lg font-semibold text-ink">
                        {sessionName ? t('Live Preview - {{name}}', { name: sessionName }) : t('Live Preview')}
                      </Dialog.Title>
                      <p className="text-[13px] text-secondary truncate max-w-md">
                        {currentUrl
                          || (connectionState === 'connecting' ? t('Connecting…')
                            : connectionState === 'connected' ? t('Waiting for the first frame…')
                            : connectionState === 'error' ? t('Not connected')
                            : t('Disconnected'))}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-3 shrink-0">
                    {/* Connection status */}
                    <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${
                      connectionState === 'connected' ? 'bg-green-50 text-green-700' :
                      connectionState === 'error' ? 'bg-red-50 text-red-700' :
                      'bg-hover text-secondary'
                    }`}>
                      {connectionState === 'connected' ? (
                        <>
                          <SignalIcon className="h-3.5 w-3.5 animate-pulse" />
                          {t('Live')}
                        </>
                      ) : connectionState === 'connecting' ? (
                        <>
                          <SignalIcon className="h-3.5 w-3.5 animate-pulse" />
                          {t('Connecting...')}
                        </>
                      ) : (
                        <>
                          <SignalSlashIcon className="h-3.5 w-3.5" />
                          {t('Disconnected')}
                        </>
                      )}
                    </span>
                    <button
                      onClick={handleClose}
                      className="p-1.5 text-tertiary hover:text-ink hover:bg-hover rounded-lg transition-colors duration-150"
                    >
                      <XMarkIcon className="h-5 w-5" />
                    </button>
                  </div>
                </div>

                {/* Browser viewport */}
                <div className="relative bg-ink flex items-center justify-center" style={{ minHeight: '500px' }}>
                  {connectionState === 'connected' ? (
                    <canvas
                      ref={canvasRef}
                      className="max-w-full max-h-[70vh] rounded-lg"
                      style={{ imageRendering: 'auto' }}
                    />
                  ) : connectionState === 'connecting' ? (
                    <div className="flex flex-col items-center gap-4 text-zinc-400">
                      <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-white" />
                      <p>{t('Connecting to session...')}</p>
                    </div>
                  ) : connectionState === 'error' ? (
                    <div className="flex flex-col items-center gap-3 text-zinc-400 text-center">
                      <SignalSlashIcon className="h-10 w-10 text-red-500" />
                      <p>{t('Failed to connect to session')}</p>
                      <p className="text-xs text-secondary">{t('The session may have ended')}</p>
                      <button
                        onClick={connectToSession}
                        className="mt-1 px-4 py-2 bg-surface hover:bg-active rounded-lg text-ink text-sm font-medium transition-colors"
                      >
                        {t('Retry Connection')}
                      </button>
                    </div>
                  ) : (
                    <div className="flex flex-col items-center gap-3 text-tertiary">
                      <EyeIcon className="h-12 w-12 opacity-50" />
                      <p>{t('Waiting for preview...')}</p>
                    </div>
                  )}
                </div>

                {/* Footer */}
                <div className="flex items-center justify-between px-6 py-3.5 border-t border-border bg-canvas rounded-b-xl">
                  <p className="text-[13px] text-secondary">
                    {t('Read-only preview - You are watching an AI session')}
                  </p>
                  <button
                    onClick={handleClose}
                    className="px-4 py-2 bg-hover hover:bg-border text-ink rounded-lg text-sm font-medium transition-colors"
                  >
                    {t('Close')}
                  </button>
                </div>
              </Dialog.Panel>
            </Transition.Child>
          </div>
        </div>
      </Dialog>
    </Transition>
  );
};

export default AIPreviewModal;
