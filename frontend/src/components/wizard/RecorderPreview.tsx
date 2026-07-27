/**
 * RecorderPreview — Live browser preview via Playwright recorder WebSocket.
 *
 * Dispatches a real recorder session so it runs alongside other parallel sessions.
 * Streams screenshots to a canvas; element selection works like the record window:
 *  - click mode: click → `get_element_info` action → selector + flash highlight
 *  - area mode: drag → `get_elements_in_region` action → one selector per element
 *  - zone mode: drag → viewport region for visual (screenshot) checks
 *  - mouse wheel scrolls the live page so below-the-fold elements are reachable
 */
import React, { useRef, useState, useCallback, useEffect, useImperativeHandle, forwardRef } from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { ArrowPathIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { getAccessToken } from '../../utils/auth';

/**
 * Mint a single-use, short-lived WebSocket auth ticket so the long-lived JWT does
 * not have to ride in the WS query string (where it leaks into proxy/access logs).
 * The access token is sent in the Authorization header (never in a URL). Returns
 * null on any failure so the caller can fall back to the legacy ?token= path.
 */
async function mintWsTicket(): Promise<string | null> {
  const token = getAccessToken();
  if (!token) return null;
  try {
    const resp = await fetch('/api/ws-ticket', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    });
    if (!resp.ok) return null;
    const data = await resp.json();
    return typeof data?.ticket === 'string' ? data.ticket : null;
  } catch {
    return null;
  }
}

export interface ElementInfo {
  selector: string;
  tag: string;
  text: string;
  ariaLabel?: string;
  rect?: { x: number; y: number; w: number; h: number };
  /** How many elements the selector matches on the page (1 = unique). */
  matchCount?: number;
}

interface RecorderPreviewProps {
  url: string;
  /** Called when user clicks an element — returns selector + element info */
  onElementClick?: (info: ElementInfo) => void;
  /** Called when user draws a zone (mousedown→drag→mouseup) in zone mode */
  onZoneDrawn?: (region: { x: number; y: number; width: number; height: number; scroll_x?: number; scroll_y?: number }) => void;
  /** Called in area mode with every element found inside the dragged rect */
  onElementsFound?: (infos: ElementInfo[]) => void;
  /** Selection mode: 'click' selects elements, 'zone' draws regions, 'area' multi-selects */
  mode: 'click' | 'zone' | 'area';
  /** Whether to auto-connect on mount when URL exists */
  autoConnect?: boolean;
  /** Already-defined visual zones to draw persistently over the live frame. */
  zones?: Array<{ id: string; region: { x: number; y: number; width: number; height: number; scroll_x?: number; scroll_y?: number } }>;
  /** Zone id to emphasize (e.g. hovered in the sidebar). */
  highlightZoneId?: string | null;
}

// Default recorder viewport (Python recorder.py recording context). The live
// frame size is read from the streamed screenshot instead of trusting this —
// the Rust agent records at 1920x1080, so a hardcoded space mapped clicks to
// the wrong page coordinates (picker selected nothing / the wrong element).
const VIEWPORT_W = 1280;
const VIEWPORT_H = 800;

export interface RecorderPreviewHandle {
  /** Get current canvas frame as base64 JPEG */
  getScreenshot: () => string | null;
  /** Request DOM extraction from recorder (returns via promise) */
  getDOM: () => Promise<string | null>;
  /** Run a JS expression in the page and resolve its value (for selector validation). */
  evaluate: (script: string) => Promise<any>;
}

export const RecorderPreview = forwardRef<RecorderPreviewHandle, RecorderPreviewProps>(({
  url,
  onElementClick,
  onZoneDrawn,
  onElementsFound,
  mode,
  autoConnect = true,
  zones = [],
  highlightZoneId = null,
}, ref) => {
  const { t } = useTranslation();
  const wsRef = useRef<WebSocket | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const lastUrlRef = useRef('');
  const domResolveRef = useRef<((dom: string | null) => void) | null>(null);
  const evalResolveRef = useRef<((value: any) => void) | null>(null);

  const [state, setState] = useState<'idle' | 'connecting' | 'connected' | 'error'>('idle');
  const [errorMsg, setErrorMsg] = useState('');
  const [, setSessionId] = useState<string | null>(null);

  // Brief highlight box drawn over the element that was just selected.
  const [flash, setFlash] = useState<{ left: number; top: number; width: number; height: number } | null>(null);
  const flashTimerRef = useRef<any>(null);

  // Actual viewport coordinate space, read from the streamed screenshot's
  // dimensions (device_scale_factor is 1.0 on both agents, so screenshot
  // pixels == page CSS pixels == the space document.elementFromPoint uses).
  // Defaults to the Python recorder size until the first frame arrives.
  const frameSizeRef = useRef<{ w: number; h: number }>({ w: VIEWPORT_W, h: VIEWPORT_H });

  // Live page scroll offset, reported by the recorder after each scroll action.
  // A zone drawn while scrolled is captured with this offset so the monitor can
  // re-scroll to the same spot before clipping (viewport-relative clip otherwise
  // grabs the top-of-page region). Ref drives the drop-time capture; the state
  // mirror re-positions the persistent-zone overlays as the page scrolls.
  const scrollRef = useRef<{ x: number; y: number }>({ x: 0, y: 0 });
  const [scroll, setScroll] = useState<{ x: number; y: number }>({ x: 0, y: 0 });

  // Transient picker error (e.g. inspection timed out). Shown as a small banner
  // WITHOUT tearing down the connected session — otherwise these errors set
  // errorMsg but nothing renders them, so a failed pick looked like a no-op.
  const [pickError, setPickError] = useState('');
  const pickErrorTimerRef = useRef<any>(null);

  // The WS onmessage closure is created once per connection — read live props
  // through refs so mode switches / re-renders don't leave it acting on stale
  // callbacks (previously, each element click saw the FIRST render's state).
  const modeRef = useRef(mode);
  const onElementClickRef = useRef(onElementClick);
  const onElementsFoundRef = useRef(onElementsFound);
  const onZoneDrawnRef = useRef(onZoneDrawn);
  const stateRef = useRef(state);
  modeRef.current = mode;
  onElementClickRef.current = onElementClick;
  onElementsFoundRef.current = onElementsFound;
  onZoneDrawnRef.current = onZoneDrawn;
  stateRef.current = state;

  // The canvas renders the 1280x800 frame with object-contain, so the drawn
  // image may be letterboxed inside the canvas element. All coordinate mapping
  // goes through this box (display position + scale of the actual frame).
  const contentBox = useCallback(() => {
    const cvs = canvasRef.current;
    if (!cvs) return null;
    const rect = cvs.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;
    const { w: vw, h: vh } = frameSizeRef.current;
    const scale = Math.min(rect.width / vw, rect.height / vh);
    const w = vw * scale;
    const h = vh * scale;
    return {
      left: rect.left + (rect.width - w) / 2,
      top: rect.top + (rect.height - h) / 2,
      width: w,
      height: h,
      scale,
    };
  }, []);

  // Scale from display (client) coords to recorder viewport coords
  const toViewport = useCallback((clientX: number, clientY: number) => {
    const box = contentBox();
    if (!box) return { x: 0, y: 0 };
    const { w: vw, h: vh } = frameSizeRef.current;
    const x = Math.round((clientX - box.left) / box.scale);
    const y = Math.round((clientY - box.top) / box.scale);
    return {
      x: Math.max(0, Math.min(vw - 1, x)),
      y: Math.max(0, Math.min(vh - 1, y)),
    };
  }, [contentBox]);

  // Map a viewport rect (recorder coords) to a display rect inside the container.
  const toDisplayRect = useCallback((r: { x: number; y: number; w: number; h: number }) => {
    const box = contentBox();
    const cont = containerRef.current?.getBoundingClientRect();
    if (!box || !cont) return null;
    return {
      left: box.left - cont.left + r.x * box.scale,
      top: box.top - cont.top + r.y * box.scale,
      width: r.w * box.scale,
      height: r.h * box.scale,
    };
  }, [contentBox]);

  const flashRect = useCallback((rect?: { x: number; y: number; w: number; h: number }) => {
    if (!rect) return;
    const disp = toDisplayRect(rect);
    if (!disp) return;
    setFlash(disp);
    if (flashTimerRef.current) clearTimeout(flashTimerRef.current);
    flashTimerRef.current = setTimeout(() => setFlash(null), 1200);
  }, [toDisplayRect]);

  // Expose methods to parent via ref
  useImperativeHandle(ref, () => ({
    getScreenshot: () => {
      const cvs = canvasRef.current;
      if (!cvs) return null;
      return cvs.toDataURL('image/jpeg', 0.8).split(',')[1]; // base64 without prefix
    },
    getDOM: () => {
      return new Promise<string | null>((resolve) => {
        if (!wsRef.current || state !== 'connected') { resolve(null); return; }
        domResolveRef.current = resolve;
        wsRef.current.send(JSON.stringify({ type: 'action', action: 'get_dom' }));
        // Timeout after 10s (the agent caps extraction at 8s)
        setTimeout(() => { if (domResolveRef.current) { domResolveRef.current(null); domResolveRef.current = null; } }, 10000);
      });
    },
    evaluate: (script: string) => {
      return new Promise<any>((resolve) => {
        if (!wsRef.current || state !== 'connected') { resolve(null); return; }
        evalResolveRef.current = resolve;
        wsRef.current.send(JSON.stringify({ type: 'action', action: 'evaluate_js', script }));
        setTimeout(() => { if (evalResolveRef.current) { evalResolveRef.current(null); evalResolveRef.current = null; } }, 5000);
      });
    },
  }), [state]);

  // Zone drawing state
  const [drawing, setDrawing] = useState<{ startX: number; startY: number; x: number; y: number; w: number; h: number } | null>(null);

  const connect = useCallback(async () => {
    if (!url) return;
    // Disconnect existing session if reconnecting. Detach handlers first so the
    // old socket's async onclose/onerror can't fire and clobber the new socket's
    // ref/state (the picker-dead race).
    if (wsRef.current) {
      const stale = wsRef.current;
      stale.onopen = stale.onmessage = stale.onerror = stale.onclose = null;
      try { stale.close(); } catch {}
      wsRef.current = null;
    }
    setState('connecting');
    setErrorMsg('');

    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // The browser can't set an Authorization header on a WebSocket, so the gateway
    // authenticates the frontend via a query-string credential. Prefer a SINGLE-USE,
    // short-lived ticket (minted over an authenticated HTTP request — Bearer in the
    // header, never logged in a URL) so the long-lived JWT no longer leaks into
    // proxy/access logs. The ws-gateway redeems ?ticket= for the bound JWT and
    // verifies it. The ?token= fallback is used ONLY when the ticket mint fails
    // (store down / not yet deployed) so the preview never hard-breaks; on the
    // normal path the JWT is NOT placed in the URL at all.
    const _tok = getAccessToken();
    const _ticket = await mintWsTicket();
    const params = new URLSearchParams();
    if (_ticket) {
      params.set('ticket', _ticket);
    } else if (_tok) {
      // Fallback: ticketing unavailable — fall back to the legacy ?token= path.
      params.set('token', _tok);
    }
    const _qs = params.toString();
    const wsUrl = `${wsProtocol}//${window.location.host}/ws/record${_qs ? `?${_qs}` : ''}`;

    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      ws.binaryType = 'arraybuffer';

      ws.onopen = () => {
        ws.send(JSON.stringify({
          type: 'start',
          url,
          options: { record_wait_steps: false },
        }));
      };

      ws.onmessage = (event) => {
        // Binary = screenshot frame (JPEG with URL prefix)
        if (event.data instanceof ArrayBuffer) {
          const buf = event.data as ArrayBuffer;
          if (buf.byteLength < 4) return;
          const dv = new DataView(buf);
          const urlLen = dv.getUint32(0);
          const jpegData = buf.slice(4 + urlLen);
          const cvs = canvasRef.current;
          if (cvs && typeof createImageBitmap !== 'undefined') {
            const blob = new Blob([jpegData], { type: 'image/jpeg' });
            createImageBitmap(blob).then((bitmap) => {
              const ctx = cvs.getContext('2d');
              if (!ctx) return;
              if (cvs.width !== bitmap.width || cvs.height !== bitmap.height) {
                cvs.width = bitmap.width;
                cvs.height = bitmap.height;
              }
              // Track the real frame size so click/zone coordinates map into the
              // agent's actual viewport (1280x800 Python vs 1920x1080 Rust).
              if (bitmap.width > 0 && bitmap.height > 0) {
                frameSizeRef.current = { w: bitmap.width, h: bitmap.height };
              }
              ctx.drawImage(bitmap, 0, 0);
              bitmap.close();
            }).catch(() => {});
          }
          return;
        }

        // Text = JSON message
        try {
          const data = JSON.parse(event.data);
          switch (data.type) {
            case 'started':
              setSessionId(data.sessionId);
              setState('connected');
              setErrorMsg('');
              scrollRef.current = { x: 0, y: 0 };
              setScroll({ x: 0, y: 0 });
              break;
            case 'scroll_position': {
              // Recorder reports the live window scroll after each scroll action.
              const sx = Math.round(Number(data.scrollX)) || 0;
              const sy = Math.round(Number(data.scrollY)) || 0;
              scrollRef.current = { x: sx, y: sy };
              setScroll({ x: sx, y: sy });
              break;
            }
            case 'navigation':
              // New document → scroll resets to the top.
              scrollRef.current = { x: 0, y: 0 };
              setScroll({ x: 0, y: 0 });
              break;
            case 'element_info':
              flashRect(data.rect);
              onElementClickRef.current?.({
                selector: data.selector,
                tag: data.tag,
                text: data.text,
                ariaLabel: data.ariaLabel,
                rect: data.rect,
                matchCount: data.matchCount,
              });
              break;
            case 'elements_in_region':
              if (Array.isArray(data.elements) && data.elements.length > 0) {
                onElementsFoundRef.current?.(data.elements as ElementInfo[]);
              }
              break;
            case 'step_recorded':
              // In click mode, we use the recorded step's selector
              if (data.step?.selector && modeRef.current === 'click') {
                onElementClickRef.current?.({
                  selector: data.step.selector,
                  tag: data.step.options?.tag || '',
                  text: data.step.options?.text || data.step.description || '',
                  ariaLabel: data.step.options?.ariaLabel || '',
                });
              }
              break;
            case 'dom_content':
              // Response to get_dom request
              if (domResolveRef.current) {
                domResolveRef.current(data.html || data.content || null);
                domResolveRef.current = null;
              }
              break;
            case 'eval_result':
              // Response to evaluate_js request (selector validation)
              if (evalResolveRef.current) {
                evalResolveRef.current(data.error ? null : data.result);
                evalResolveRef.current = null;
              }
              break;
            case 'error':
              // While connected (e.g. a picker action failed/timed out), show a
              // transient banner instead of swallowing it — tearing the session
              // down for a single failed inspection would be worse. Only a
              // pre-connect failure should drive the full error state.
              if (stateRef.current === 'connected') {
                setPickError(data.message || t('Recorder error'));
                if (pickErrorTimerRef.current) clearTimeout(pickErrorTimerRef.current);
                pickErrorTimerRef.current = setTimeout(() => setPickError(''), 4000);
              } else {
                setErrorMsg(data.message || t('Recorder error'));
              }
              break;
          }
        } catch {}
      };

      ws.onerror = () => {
        // Ignore events from a socket that's already been superseded by a newer
        // connect() (StrictMode double-mount, URL change). Otherwise a stale
        // socket's late error would tear down the live session.
        if (wsRef.current !== ws) return;
        setState('error');
        setErrorMsg(t('Connection to recorder failed'));
      };

      ws.onclose = () => {
        // A previous connection's close fires AFTER the new socket is assigned to
        // wsRef.current. Without this guard it would null out the live socket's
        // ref — leaving state='connected' but wsRef.current=null, so every click
        // silently early-returns (the element picker appeared dead).
        if (wsRef.current !== ws) return;
        if (stateRef.current !== 'error') setState('idle');
        wsRef.current = null;
        setSessionId(null);
      };
    } catch (e: any) {
      setState('error');
      setErrorMsg(e.message || t('Failed to connect'));
    }
  }, [url, flashRect, t]);

  const disconnect = useCallback(() => {
    if (wsRef.current) {
      try {
        wsRef.current.send(JSON.stringify({ type: 'stop' }));
        wsRef.current.close();
      } catch {}
      wsRef.current = null;
    }
    setState('idle');
    setSessionId(null);
  }, []);

  // Auto-connect when URL exists (on mount or URL change)
  useEffect(() => {
    if (autoConnect && url && url.startsWith('http') && url !== lastUrlRef.current) {
      lastUrlRef.current = url;
      connect();
    }
  }, [url, autoConnect, state, connect]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      disconnect();
      if (flashTimerRef.current) clearTimeout(flashTimerRef.current);
      if (pickErrorTimerRef.current) clearTimeout(pickErrorTimerRef.current);
    };
  }, []);

  // Forward mouse-wheel to the live page (accumulated + throttled) so users
  // can scroll to elements below the fold — same as the record window.
  // Attached manually because React's delegated wheel listeners are passive
  // (preventDefault would be ignored).
  const wheelAccumRef = useRef({ dx: 0, dy: 0, x: 0, y: 0, timer: null as any });
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (stateRef.current !== 'connected' || !wsRef.current) return;
      e.preventDefault();
      const { x, y } = toViewport(e.clientX, e.clientY);
      const acc = wheelAccumRef.current;
      acc.dx += e.deltaX;
      acc.dy += e.deltaY;
      acc.x = x;
      acc.y = y;
      if (!acc.timer) {
        acc.timer = setTimeout(() => {
          acc.timer = null;
          const { dx, dy, x: ax, y: ay } = acc;
          acc.dx = 0;
          acc.dy = 0;
          if ((Math.abs(dx) < 1 && Math.abs(dy) < 1) || !wsRef.current) return;
          wsRef.current.send(JSON.stringify({
            type: 'action',
            action: 'scroll',
            deltaX: Math.round(dx),
            deltaY: Math.round(dy),
            x: ax,
            y: ay,
          }));
        }, 80);
      }
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => {
      el.removeEventListener('wheel', onWheel);
      if (wheelAccumRef.current.timer) clearTimeout(wheelAccumRef.current.timer);
    };
  }, [toViewport]);

  const handleCanvasClick = useCallback((e: React.MouseEvent) => {
    if (state !== 'connected' || !wsRef.current) return;
    if (mode !== 'click') return;

    const { x, y } = toViewport(e.clientX, e.clientY);

    // Send get_element_info to get selector without navigating
    wsRef.current.send(JSON.stringify({
      type: 'action',
      action: 'get_element_info',
      x,
      y,
    }));
  }, [state, mode, toViewport]);

  // Zone/area drawing handlers
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if (mode !== 'zone' && mode !== 'area') return;
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    setDrawing({ startX: x, startY: y, x, y, w: 0, h: 0 });
  }, [mode]);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!drawing) return;
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const cx = e.clientX - rect.left;
    const cy = e.clientY - rect.top;
    setDrawing({
      ...drawing,
      x: Math.min(drawing.startX, cx),
      y: Math.min(drawing.startY, cy),
      w: Math.abs(cx - drawing.startX),
      h: Math.abs(cy - drawing.startY),
    });
  }, [drawing]);

  const handleMouseUp = useCallback(() => {
    if (!drawing || drawing.w < 10 || drawing.h < 10) {
      setDrawing(null);
      return;
    }

    const cont = containerRef.current?.getBoundingClientRect();
    const box = contentBox();
    if (!cont || !box) { setDrawing(null); return; }

    // Drawing coords are container-relative; map through the letterboxed
    // frame position to recorder viewport coords.
    const vx = Math.round((drawing.x - (box.left - cont.left)) / box.scale);
    const vy = Math.round((drawing.y - (box.top - cont.top)) / box.scale);
    const vw = Math.round(drawing.w / box.scale);
    const vh = Math.round(drawing.h / box.scale);
    const { w: fw, h: fh } = frameSizeRef.current;
    const clamp = (v: number, max: number) => Math.max(0, Math.min(max, v));
    const region = {
      x: clamp(vx, fw),
      y: clamp(vy, fh),
      width: clamp(vw, fw - clamp(vx, fw)),
      height: clamp(vh, fh - clamp(vy, fh)),
      // Stamp the scroll the page is at right now so the monitor re-scrolls here.
      scroll_x: scrollRef.current.x,
      scroll_y: scrollRef.current.y,
    };

    if (mode === 'area') {
      // Ask the live page for every element inside the rect — the reply
      // arrives as an `elements_in_region` frame → onElementsFound.
      if (wsRef.current && state === 'connected') {
        wsRef.current.send(JSON.stringify({
          type: 'action',
          action: 'get_elements_in_region',
          ...region,
        }));
      }
    } else {
      onZoneDrawnRef.current?.(region);
    }
    setDrawing(null);
  }, [drawing, mode, state, contentBox]);

  return (
    <div ref={containerRef} className="relative w-full h-full bg-canvas">
      {/* Canvas — live screenshot stream */}
      <canvas
        ref={canvasRef}
        className={clsx(
          'w-full h-full object-contain',
          state === 'connected' ? 'block' : 'hidden',
          mode === 'click' && 'cursor-crosshair',
        )}
        onClick={handleCanvasClick}
      />

      {/* Persistent visual zones already added — drawn over the live frame so
          the user sees what's watched; the hovered one is emphasized. */}
      {state === 'connected' && zones.map((z) => {
        // A zone stored at scroll (z.scroll) sits at a different viewport spot
        // once the live page scrolls — shift it by (stored − current) scroll so
        // the overlay tracks the content it watches.
        const dx = (z.region.scroll_x ?? 0) - scroll.x;
        const dy = (z.region.scroll_y ?? 0) - scroll.y;
        const d = toDisplayRect({ x: z.region.x + dx, y: z.region.y + dy, w: z.region.width, h: z.region.height });
        if (!d) return null;
        const hot = z.id === highlightZoneId;
        return (
          <div
            key={z.id}
            className={clsx(
              'absolute rounded-[2px] pointer-events-none z-20 transition-all',
              hot ? 'border-2 border-ink bg-ink/15' : 'border border-ink/40 bg-ink/[0.04]',
            )}
            style={{ left: d.left, top: d.top, width: d.width, height: d.height }}
          />
        );
      })}

      {/* Selected-element flash highlight */}
      {flash && (
        <div
          className="absolute border-2 border-ink bg-ink/10 rounded pointer-events-none z-20"
          style={{ left: flash.left, top: flash.top, width: flash.width, height: flash.height }}
        />
      )}

      {/* Transient picker error (does not tear down the live session) */}
      {state === 'connected' && pickError && (
        <div className="absolute bottom-3 left-1/2 -translate-x-1/2 z-30 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-ink text-white text-[11px] shadow-lg pointer-events-none">
          <ExclamationTriangleIcon className="w-3.5 h-3.5" />
          {pickError}
        </div>
      )}

      {/* Zone/area drawing overlay */}
      {state === 'connected' && (mode === 'zone' || mode === 'area') && (
        <div
          className="absolute inset-0 cursor-crosshair z-10"
          onMouseDown={handleMouseDown}
          onMouseMove={handleMouseMove}
          onMouseUp={handleMouseUp}
        >
          {drawing && drawing.w > 0 && (
            <div
              className={clsx(
                'absolute rounded pointer-events-none border-2 border-ink bg-ink/10',
                mode === 'area' && 'border-dashed',
              )}
              style={{ left: drawing.x, top: drawing.y, width: drawing.w, height: drawing.h }}
            />
          )}
        </div>
      )}

      {/* Loading */}
      {state === 'connecting' && (
        <div className="absolute inset-0 flex items-center justify-center bg-canvas">
          <div className="text-center">
            <ArrowPathIcon className="w-6 h-6 text-ink animate-spin mx-auto mb-2" />
            <p className="text-sm text-secondary">{t('Launching browser session...')}</p>
          </div>
        </div>
      )}

      {/* Idle — no session */}
      {state === 'idle' && (
        <div className="absolute inset-0 flex items-center justify-center">
          <div
            className="absolute inset-0 pointer-events-none"
            style={{
              backgroundImage: 'radial-gradient(circle, #D6D7DC 1px, transparent 1px)',
              backgroundSize: '24px 24px',
              opacity: 0.3,
            }}
          />
          <div className="relative text-center">
            {url ? (
              <p className="text-xs text-secondary">{t('Loading preview...')}</p>
            ) : (
              <p className="text-sm text-secondary">{t('Enter a URL above')}</p>
            )}
          </div>
        </div>
      )}

      {/* Error */}
      {state === 'error' && (
        <div className="absolute inset-0 flex items-center justify-center">
          <div className="text-center max-w-sm">
            <ExclamationTriangleIcon className="w-6 h-6 text-tertiary mx-auto mb-2" />
            <p className="text-sm text-secondary">{errorMsg}</p>
            <button onClick={() => { setState('idle'); connect(); }} className="mt-2 text-xs text-ink underline">{t('Retry')}</button>
          </div>
        </div>
      )}
    </div>
  );
});
