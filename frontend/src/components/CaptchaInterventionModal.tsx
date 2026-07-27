import React, { useState, useEffect, useRef, useCallback, Fragment } from 'react';
import { Dialog, Transition } from '@headlessui/react';
import { useTranslation } from 'react-i18next';
import { ShieldExclamationIcon, CheckCircleIcon } from '@heroicons/react/24/outline';

interface Props {
  open: boolean;
  onClose: () => void;
  recorderUrl: string;
  recorderSessionId: string;
}

/**
 * Modal that shows a live browser view for manual captcha solving.
 * Connects to the recorder's /ws/interact/{sessionId} endpoint,
 * receives screenshot frames, and allows clicking to solve the captcha.
 */
export function CaptchaInterventionModal({ open, onClose, recorderUrl, recorderSessionId }: Props) {
  const { t } = useTranslation();
  const wsRef = useRef<WebSocket | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const imageRef = useRef<HTMLImageElement | null>(null);
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState<'connecting' | 'connected' | 'solved'>('connecting');

  // Declared ABOVE the socket effect that calls it: reaching backwards past a
  // `const` is a temporal-dead-zone access, and the effect would capture whatever
  // binding existed when it ran rather than the current one
  // (`react-hooks/immutability`).
  const drawScreenshot = useCallback((src: string) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    if (!imageRef.current) {
      imageRef.current = new Image();
    }

    imageRef.current.onload = () => {
      canvas.width = imageRef.current!.width;
      canvas.height = imageRef.current!.height;
      ctx.drawImage(imageRef.current!, 0, 0);
    };
    imageRef.current.src = src;
  }, []);

  // Connect to recorder WebSocket
  useEffect(() => {
    if (!open || !recorderUrl || !recorderSessionId) return;

    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProtocol}//${window.location.host}/api/recorder`;

    const ws = new WebSocket(`${wsUrl}/ws/interact/${recorderSessionId}`);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      setStatus('connected');
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'screenshot' && data.data) {
          drawScreenshot(`data:image/jpeg;base64,${data.data}`);
        } else if (data.type === 'captcha_ack') {
          setStatus('solved');
        }
      } catch {
        // ignore parse errors
      }
    };

    // Guard against a superseded socket's late close/error (open or
    // recorderSessionId change / StrictMode double-mount): without this the old
    // socket's async onclose would flip `connected` to false on the live one.
    ws.onclose = () => { if (wsRef.current !== ws) return; setConnected(false); };
    ws.onerror = () => { if (wsRef.current !== ws) return; setConnected(false); };

    // Keep alive ping
    const pingInterval = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping' }));
      }
    }, 5000);

    return () => {
      clearInterval(pingInterval);
      // Detach handlers before closing so the async onclose/onerror can't fire
      // after a subsequent effect run assigns a new socket to wsRef.current.
      ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
        ws.close();
      }
      if (wsRef.current === ws) wsRef.current = null;
    };
  }, [open, recorderUrl, recorderSessionId, drawScreenshot]);

  const handleCanvasClick = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const ws = wsRef.current;
    const canvas = canvasRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN || !canvas) return;

    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;

    const x = Math.round((e.clientX - rect.left) * scaleX);
    const y = Math.round((e.clientY - rect.top) * scaleY);

    ws.send(JSON.stringify({ type: 'action', action: 'click', x, y }));
  }, []);

  const handleSolved = useCallback(() => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'captcha_solved' }));
    }
    setStatus('solved');
    setTimeout(onClose, 1500);
  }, [onClose]);

  return (
    <Transition appear show={open} as={Fragment}>
      <Dialog as="div" className="relative z-50" onClose={() => {}}>
        <Transition.Child
          as={Fragment}
          enter="ease-out duration-200" enterFrom="opacity-0" enterTo="opacity-100"
          leave="ease-in duration-150" leaveFrom="opacity-100" leaveTo="opacity-0"
        >
          <div className="fixed inset-0 bg-black/60" />
        </Transition.Child>

        <div className="fixed inset-0 overflow-y-auto">
          <div className="flex min-h-full items-center justify-center p-4">
            <Transition.Child
              as={Fragment}
              enter="ease-out duration-200" enterFrom="opacity-0 scale-95" enterTo="opacity-100 scale-100"
              leave="ease-in duration-150" leaveFrom="opacity-100 scale-100" leaveTo="opacity-0 scale-95"
            >
              <Dialog.Panel className="w-full max-w-4xl bg-surface border border-border rounded-xl shadow-xl">
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-border">
                  <div className="flex items-center gap-3">
                    <ShieldExclamationIcon className="h-6 w-6 text-amber-500" />
                    <div>
                      <Dialog.Title className="text-lg font-semibold text-ink">
                        {t('Captcha Detected')}
                      </Dialog.Title>
                      <p className="text-sm text-secondary">
                        {t('Please solve the captcha in the browser view below, then click "Done"')}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium ${
                      status === 'solved' ? 'bg-green-100 text-green-700' :
                      connected ? 'bg-ink text-white' :
                      'bg-hover text-secondary'
                    }`}>
                      <span className={`w-2 h-2 rounded-full ${
                        status === 'solved' ? 'bg-green-500' :
                        connected ? 'bg-current animate-pulse' :
                        'bg-gray-400'
                      }`} />
                      {status === 'solved' ? t('Solved') : connected ? t('Live') : t('Connecting...')}
                    </span>
                  </div>
                </div>

                {/* Browser view */}
                <div className="p-4 bg-gray-900 flex items-center justify-center" style={{ minHeight: 500 }}>
                  {connected ? (
                    <canvas
                      ref={canvasRef}
                      onClick={handleCanvasClick}
                      className="max-w-full max-h-[60vh] rounded cursor-pointer"
                      style={{ imageRendering: 'auto' }}
                    />
                  ) : (
                    <div className="text-zinc-400 text-center">
                      <ShieldExclamationIcon className="h-12 w-12 mx-auto mb-3 opacity-50" />
                      <p>{t('Connecting to browser...')}</p>
                      <p className="text-xs mt-1 text-zinc-500">{t('This only takes a moment.')}</p>
                    </div>
                  )}
                </div>

                {/* Footer */}
                <div className="flex items-center justify-between px-6 py-4 border-t border-border bg-canvas rounded-b-xl">
                  <p className="text-sm text-secondary">
                    {t('Click directly on the captcha in the view above to interact with it')}
                  </p>
                  <button
                    onClick={handleSolved}
                    className="flex items-center gap-2 px-5 py-2.5 bg-green-600 hover:bg-green-700 text-white rounded-lg font-medium transition-colors"
                  >
                    <CheckCircleIcon className="h-5 w-5" />
                    {status === 'solved' ? t('Solved!') : t('Done - Captcha Solved')}
                  </button>
                </div>
              </Dialog.Panel>
            </Transition.Child>
          </div>
        </div>
      </Dialog>
    </Transition>
  );
}
