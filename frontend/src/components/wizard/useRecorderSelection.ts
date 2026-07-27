/**
 * useRecorderSelection — the live-preview element/zone/area picking layer, shared by RecorderPreview
 * and BrowserRecorder so both speak the SAME coordinate math + WS picker protocol.
 *
 * The host owns the WebSocket + canvas + screencast loop; this hook adds selection ON TOP:
 *  - click mode: click → `get_element_info {x,y}` → `element_info` frame → onElementClick(selector)
 *  - area mode:  drag  → `get_elements_in_region {x,y,width,height}` → `elements_in_region` → onElementsFound
 *  - zone mode:  drag  → a viewport region (no WS call) → onZoneDrawn (visual/screenshot checks)
 *
 * Coordinate mapping accounts for letterboxing: the daemon streams JPEG frames at the agent's real
 * viewport (1280×800 Python / 1920×1080 Rust), object-contain-scaled into the canvas. The host keeps
 * `frameSizeRef` updated from each decoded bitmap; the hook maps client↔viewport↔display through it.
 *
 * The host wires three things: spread `canvasProps` onto the <canvas>, render the overlays from the
 * returned state (zones via `toDisplayRect`, `flash`, `pickError`, and the drawing overlay with
 * `drawingHandlers`), and call `consumeMessage(data)` for each JSON WS frame (it returns true when it
 * handled a picker response). On a transient recorder `error` while connected, call `showPickError`.
 */
import { useCallback, useEffect, useRef, useState, type MutableRefObject, type RefObject } from 'react';
import i18n from '../../i18n';

export interface ElementInfo {
  selector: string;
  tag: string;
  text: string;
  ariaLabel?: string;
  rect?: { x: number; y: number; w: number; h: number };
  /** How many elements the selector matches on the page (1 = unique). */
  matchCount?: number;
}

export interface SelectionRegion {
  x: number;
  y: number;
  width: number;
  height: number;
  // Page scroll when the zone was drawn (x/y are viewport-relative). Lets the
  // monitor re-scroll to the same spot before clipping a below-the-fold zone.
  scroll_x?: number;
  scroll_y?: number;
}

export type SelectionMode = 'click' | 'zone' | 'area';

interface DrawRect {
  startX: number;
  startY: number;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface DisplayRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

interface UseRecorderSelectionArgs {
  wsRef: RefObject<WebSocket | null>;
  canvasRef: RefObject<HTMLCanvasElement | null>;
  containerRef: RefObject<HTMLElement | null>;
  frameSizeRef: MutableRefObject<{ w: number; h: number }>;
  /** True when a live recorder session is connected (selection requires it). */
  connected: boolean;
  /** `null` disables selection entirely (normal recording). */
  mode: SelectionMode | null;
  onElementClick?: (info: ElementInfo) => void;
  onElementsFound?: (infos: ElementInfo[]) => void;
  onZoneDrawn?: (region: SelectionRegion) => void;
}

export interface UseRecorderSelectionResult {
  /** Spread onto the <canvas> (click picking + crosshair cursor in click mode). */
  canvasProps: {
    onClick: (e: React.MouseEvent) => void;
    'data-selecting': boolean;
  };
  /** Whether to show the crosshair cursor on the canvas (click mode). */
  clickCursor: boolean;
  /** Handlers for the zone/area drawing overlay (render it only when `drawingActive`). */
  drawingHandlers: {
    onMouseDown: (e: React.MouseEvent) => void;
    onMouseMove: (e: React.MouseEvent) => void;
    onMouseUp: () => void;
  };
  /** True when the zone/area drawing overlay should be mounted. */
  drawingActive: boolean;
  /** The in-progress drag rectangle (container-relative display px), or null. */
  drawing: DrawRect | null;
  /** Brief highlight over the element just picked (container-relative display px), or null. */
  flash: DisplayRect | null;
  /** Transient picker error (shown as a banner; never tears down the session). */
  pickError: string;
  /** Map a viewport region to container-relative display px (for drawing persistent zones). */
  toDisplayRect: (region: SelectionRegion) => DisplayRect | null;
  /** Feed each JSON WS frame; returns true when it consumed a picker response. */
  consumeMessage: (data: any) => boolean;
  /** Show a transient picker-error banner (call from the host's `error` case while connected). */
  showPickError: (msg: string) => void;
}

export function useRecorderSelection(args: UseRecorderSelectionArgs): UseRecorderSelectionResult {
  const { wsRef, canvasRef, containerRef, frameSizeRef, connected, mode } = args;

  const [drawing, setDrawing] = useState<DrawRect | null>(null);
  const [flash, setFlash] = useState<DisplayRect | null>(null);
  const [pickError, setPickError] = useState('');
  const flashTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pickErrorTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Live page scroll, fed by the recorder's `scroll_position` frames (see
  // consumeMessage). A zone is stamped with this at drop time so the monitor can
  // re-scroll before clipping — a viewport-relative clip otherwise grabs the
  // top-of-page region for anything drawn below the fold.
  const scrollRef = useRef<{ x: number; y: number }>({ x: 0, y: 0 });

  // The picker closures are created once; read live props through refs so a mode switch / re-render
  // never leaves a handler acting on stale callbacks or mode.
  const modeRef = useRef(mode);
  const connectedRef = useRef(connected);
  const onElementClickRef = useRef(args.onElementClick);
  const onElementsFoundRef = useRef(args.onElementsFound);
  const onZoneDrawnRef = useRef(args.onZoneDrawn);
  // The mirrors are refreshed AFTER commit, never during render — writing a ref
  // in the render path is a render-time mutation React may tear on. Every reader
  // below is a DOM event handler or a WS message callback, all of which run
  // post-commit, so they still see the values from the latest render.
  useEffect(() => {
    modeRef.current = mode;
    connectedRef.current = connected;
    onElementClickRef.current = args.onElementClick;
    onElementsFoundRef.current = args.onElementsFound;
    onZoneDrawnRef.current = args.onZoneDrawn;
  });

  // ── coordinate mapping (letterboxed object-contain frame) ──
  const contentBox = useCallback(() => {
    const cvs = canvasRef.current;
    if (!cvs) return null;
    const rect = cvs.getBoundingClientRect();
    const { w: vw, h: vh } = frameSizeRef.current;
    if (!vw || !vh || rect.width === 0 || rect.height === 0) return null;
    const scale = Math.min(rect.width / vw, rect.height / vh);
    return {
      left: rect.left + (rect.width - vw * scale) / 2,
      top: rect.top + (rect.height - vh * scale) / 2,
      width: vw * scale,
      height: vh * scale,
      scale,
    };
  }, [canvasRef, frameSizeRef]);

  const toViewport = useCallback(
    (clientX: number, clientY: number): { x: number; y: number } => {
      const box = contentBox();
      const { w: vw, h: vh } = frameSizeRef.current;
      if (!box) return { x: 0, y: 0 };
      const x = Math.round((clientX - box.left) / box.scale);
      const y = Math.round((clientY - box.top) / box.scale);
      const clamp = (v: number, max: number) => Math.max(0, Math.min(max, v));
      return { x: clamp(x, vw - 1), y: clamp(y, vh - 1) };
    },
    [contentBox, frameSizeRef],
  );

  const toDisplayRect = useCallback(
    (region: SelectionRegion): DisplayRect | null => {
      const box = contentBox();
      const cont = containerRef.current?.getBoundingClientRect();
      if (!box || !cont) return null;
      return {
        left: box.left - cont.left + region.x * box.scale,
        top: box.top - cont.top + region.y * box.scale,
        width: region.width * box.scale,
        height: region.height * box.scale,
      };
    },
    [contentBox, containerRef],
  );

  const flashRect = useCallback(
    (rect?: { x: number; y: number; w: number; h: number }) => {
      if (!rect) return;
      const disp = toDisplayRect({ x: rect.x, y: rect.y, width: rect.w, height: rect.h });
      if (!disp) return;
      setFlash(disp);
      if (flashTimerRef.current) clearTimeout(flashTimerRef.current);
      flashTimerRef.current = setTimeout(() => setFlash(null), 1200);
    },
    [toDisplayRect],
  );

  const showPickError = useCallback((msg: string) => {
    setPickError(msg);
    if (pickErrorTimerRef.current) clearTimeout(pickErrorTimerRef.current);
    pickErrorTimerRef.current = setTimeout(() => setPickError(''), 4000);
  }, []);

  // ── click picking ──
  const onCanvasClick = useCallback(
    (e: React.MouseEvent) => {
      if (!connectedRef.current || !wsRef.current) return;
      if (modeRef.current !== 'click') return;
      const { x, y } = toViewport(e.clientX, e.clientY);
      try {
        wsRef.current.send(JSON.stringify({ type: 'action', action: 'get_element_info', x, y }));
      } catch {
        /* socket closed mid-pick */
      }
    },
    [wsRef, toViewport],
  );

  // ── zone/area drawing ──
  const onMouseDown = useCallback((e: React.MouseEvent) => {
    if (modeRef.current !== 'zone' && modeRef.current !== 'area') return;
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    setDrawing({ startX: x, startY: y, x, y, w: 0, h: 0 });
  }, []);

  const onMouseMove = useCallback((e: React.MouseEvent) => {
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const cx = e.clientX - rect.left;
    const cy = e.clientY - rect.top;
    setDrawing((prev) =>
      prev
        ? {
            ...prev,
            x: Math.min(prev.startX, cx),
            y: Math.min(prev.startY, cy),
            w: Math.abs(cx - prev.startX),
            h: Math.abs(cy - prev.startY),
          }
        : prev,
    );
  }, []);

  const onMouseUp = useCallback(() => {
    setDrawing((cur) => {
      if (!cur || cur.w < 10 || cur.h < 10) return null;
      const cont = containerRef.current?.getBoundingClientRect();
      const box = contentBox();
      if (!cont || !box) return null;
      // Drawing coords are container-relative; map through the letterboxed frame to viewport coords.
      const vx = Math.round((cur.x - (box.left - cont.left)) / box.scale);
      const vy = Math.round((cur.y - (box.top - cont.top)) / box.scale);
      const vw = Math.round(cur.w / box.scale);
      const vh = Math.round(cur.h / box.scale);
      const { w: fw, h: fh } = frameSizeRef.current;
      const clamp = (v: number, max: number) => Math.max(0, Math.min(max, v));
      const region: SelectionRegion = {
        x: clamp(vx, fw),
        y: clamp(vy, fh),
        width: clamp(vw, fw - clamp(vx, fw)),
        height: clamp(vh, fh - clamp(vy, fh)),
        scroll_x: scrollRef.current.x,
        scroll_y: scrollRef.current.y,
      };
      if (modeRef.current === 'area') {
        if (wsRef.current && connectedRef.current) {
          try {
            wsRef.current.send(JSON.stringify({ type: 'action', action: 'get_elements_in_region', ...region }));
          } catch {
            /* socket closed */
          }
        }
      } else {
        onZoneDrawnRef.current?.(region);
      }
      return null;
    });
  }, [containerRef, contentBox, frameSizeRef, wsRef]);

  // ── WS picker responses ──
  const consumeMessage = useCallback(
    (data: any): boolean => {
      switch (data?.type) {
        case 'element_info':
          if (data.selector) {
            flashRect(data.rect);
            onElementClickRef.current?.({
              selector: data.selector,
              tag: data.tag,
              text: data.text,
              ariaLabel: data.ariaLabel,
              rect: data.rect,
              matchCount: data.matchCount,
            });
          } else if (data.error) {
            showPickError(data.error);
          }
          return true;
        case 'elements_in_region':
          if (Array.isArray(data.elements) && data.elements.length > 0) {
            onElementsFoundRef.current?.(data.elements as ElementInfo[]);
          } else {
            showPickError(data.error || i18n.t('No elements found in that area'));
          }
          return true;
        case 'scroll_position':
          // Recorder reports the live window scroll after each scroll action.
          scrollRef.current = {
            x: Math.round(Number(data.scrollX)) || 0,
            y: Math.round(Number(data.scrollY)) || 0,
          };
          return true;
        case 'navigation':
          // New document → scroll resets to the top. Don't consume it; the host
          // still handles navigation for its own UI.
          scrollRef.current = { x: 0, y: 0 };
          return false;
        default:
          return false;
      }
    },
    [flashRect, showPickError],
  );

  useEffect(() => {
    return () => {
      if (flashTimerRef.current) clearTimeout(flashTimerRef.current);
      if (pickErrorTimerRef.current) clearTimeout(pickErrorTimerRef.current);
    };
  }, []);

  return {
    canvasProps: { onClick: onCanvasClick, 'data-selecting': mode != null },
    clickCursor: mode === 'click',
    drawingHandlers: { onMouseDown, onMouseMove, onMouseUp },
    drawingActive: connected && (mode === 'zone' || mode === 'area'),
    drawing,
    flash,
    pickError,
    toDisplayRect,
    consumeMessage,
    showPickError,
  };
}
