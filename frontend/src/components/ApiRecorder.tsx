import React, { useState, useEffect, useRef, useCallback, Fragment } from 'react';
import { Dialog, Transition } from '@headlessui/react';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import {
  XMarkIcon,
  PlayIcon,
  StopIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  BoltIcon,
  ShieldCheckIcon,
  TagIcon,
  ArrowPathIcon,
} from '@heroicons/react/24/outline';
import { Checkbox } from './ui';

interface CapturedRequest {
  id: string;
  timestamp: number;
  method: string;
  url: string;
  headers: Record<string, string>;
  body: any;
  content_type: string;
  response?: {
    status: number;
    status_text?: string;
    headers: Record<string, string>;
    body: any;
    content_type?: string;
  };
  // User-defined function metadata
  function_name?: string;
  function_label?: string;
  is_auth?: boolean;
}

interface ApiFunctionDef {
  label: string;
  is_auth: boolean;
  order: number;
  request: {
    method: string;
    url: string;
    headers: Record<string, string>;
    body_template: Record<string, any>;
  };
  response_sample?: {
    status: number;
    content_type?: string;
    body: any;
  } | null;
  response_extractions: Record<string, string>;
  parameters: string[];
  secrets: string[];
}

interface ApiRecorderProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (functions: Record<string, ApiFunctionDef>, name: string, description: string, customPathPrefix: string) => void;
}

const getRecorderWsUrl = (): string | null => {
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${wsProtocol}//${window.location.host}/ws/record`;
};

export const ApiRecorder: React.FC<ApiRecorderProps> = ({
  isOpen,
  onClose,
  onSave,
}) => {
  const { t } = useTranslation();
  const [recording, setRecording] = useState(false);
  const [startUrl, setStartUrl] = useState('');
  const [workflowName, setWorkflowName] = useState('');
  const [description, setDescription] = useState('');
  const [customPathPrefix, setCustomPathPrefix] = useState('');
  const [capturedRequests, setCapturedRequests] = useState<CapturedRequest[]>([]);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [screenshot, setScreenshot] = useState<string | null>(null);
  const [labelingId, setLabelingId] = useState<string | null>(null);
  const [labelForm, setLabelForm] = useState({ name: '', label: '', is_auth: false });
  const [phase, setPhase] = useState<'setup' | 'recording' | 'review'>('setup');
  const [starting, setStarting] = useState(false);
  const [saving, setSaving] = useState(false);
  // Track parameterized fields per request: {requestId: {fieldPath: paramName}}
  const [parameterized, setParameterized] = useState<Record<string, Record<string, string>>>({});
  // Track response extractions per request: {requestId: {extractName: jsonPath}}
  const [extractions, setExtractions] = useState<Record<string, Record<string, string>>>({});

  const wsRef = useRef<WebSocket | null>(null);

  // Connect WebSocket
  const connectWs = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    // Tear down any non-open leftover socket (connecting/closing) with its
    // handlers detached, so its async onclose can't null out the new socket's
    // ref once it's assigned below (reconnect / double-mount race).
    if (wsRef.current) {
      const stale = wsRef.current;
      stale.onmessage = stale.onerror = stale.onclose = null;
      try { stale.close(); } catch {}
      wsRef.current = null;
    }

    const wsUrl = getRecorderWsUrl();
    if (!wsUrl) {
      toast.error(t('Recorder not configured. Set the recorder URL in Settings > Recorder.'));
      return;
    }

    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.type === 'api_record_started') {
        setRecording(true);
        setPhase('recording');
        toast.success(t('API recording started'));
      } else if (data.type === 'api_request_captured') {
        setCapturedRequests(prev => [...prev, data.request]);
      } else if (data.type === 'api_response_captured') {
        setCapturedRequests(prev =>
          prev.map(r => r.id === data.request_id ? { ...r, response: data.response } : r)
        );
      } else if (data.type === 'api_record_stopped') {
        setRecording(false);
        setPhase('review');
        toast.success(t('Recording stopped - {{n}} requests captured', { n: data.request_count }));
      } else if (data.type === 'screenshot') {
        setScreenshot(`data:image/jpeg;base64,${data.data}`);
      } else if (data.type === 'error') {
        toast.error(data.message);
      }
    };

    ws.onerror = () => {
      if (wsRef.current !== ws) return;
      toast.error(t('WebSocket connection failed'));
    };
    ws.onclose = () => {
      // A superseded socket's late close must not null the live ref.
      if (wsRef.current !== ws) return;
      wsRef.current = null;
    };
  }, [t]);

  // Cleanup on close. Written as the OPEN effect's teardown rather than a
  // "when closed, reset" effect body: it runs at exactly the same moment (the
  // isOpen true→false transition) without a synchronous setState cascade, and it
  // also tears the socket down if the recorder unmounts while still open.
  useEffect(() => {
    if (!isOpen) return;
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      setRecording(false);
      setPhase('setup');
      setCapturedRequests([]);
      setScreenshot(null);
      setParameterized({});
      setExtractions({});
    };
  }, [isOpen]);

  const startRecording = () => {
    if (starting) return;
    if (!startUrl) { toast.error(t('Enter a URL')); return; }
    setStarting(true);
    connectWs();
    // Wait for connection then send start
    const checkAndSend = () => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: 'start_api_record', url: startUrl }));
        setStarting(false);
      } else {
        setTimeout(checkAndSend, 100);
      }
    };
    setTimeout(checkAndSend, 200);
  };

  const stopRecording = () => {
    wsRef.current?.send(JSON.stringify({ type: 'stop_api_record' }));
  };

  // Label a request as a function
  const applyLabel = (reqId: string) => {
    setCapturedRequests(prev =>
      prev.map(r => r.id === reqId ? {
        ...r,
        function_name: labelForm.name,
        function_label: labelForm.label || labelForm.name,
        is_auth: labelForm.is_auth,
      } : r)
    );
    setLabelingId(null);
    setLabelForm({ name: '', label: '', is_auth: false });
  };

  // Toggle a body field as parameterized
  const toggleParam = (reqId: string, fieldPath: string) => {
    setParameterized(prev => {
      const reqParams = { ...(prev[reqId] || {}) };
      if (reqParams[fieldPath]) {
        delete reqParams[fieldPath];
      } else {
        // Default param name = field path's last segment
        reqParams[fieldPath] = fieldPath.split('.').pop() || fieldPath;
      }
      return { ...prev, [reqId]: reqParams };
    });
  };

  // Add response extraction
  const toggleExtraction = (reqId: string, fieldPath: string) => {
    setExtractions(prev => {
      const reqExtractions = { ...(prev[reqId] || {}) };
      const name = fieldPath.split('.').pop() || fieldPath;
      if (reqExtractions[name]) {
        delete reqExtractions[name];
      } else {
        reqExtractions[name] = fieldPath.startsWith('$.') ? fieldPath : `$.${fieldPath}`;
      }
      return { ...prev, [reqId]: reqExtractions };
    });
  };

  // Build functions from labeled requests and save
  const handleSave = () => {
    if (saving) return;
    if (!workflowName.trim()) { toast.error(t('Workflow name required')); return; }

    const labeledRequests = capturedRequests.filter(r => r.function_name);
    if (labeledRequests.length === 0) {
      toast.error(t('Label at least one request as a function'));
      return;
    }
    setSaving(true);

    const functions: Record<string, ApiFunctionDef> = {};
    labeledRequests.forEach((req, idx) => {
      const reqParams = parameterized[req.id] || {};
      const reqExtractions = extractions[req.id] || {};

      // Build body template: replace parameterized values with {{paramName}}
      let bodyTemplate = req.body;
      if (typeof bodyTemplate === 'object' && bodyTemplate !== null) {
        bodyTemplate = JSON.parse(JSON.stringify(bodyTemplate)); // deep clone
        for (const [fieldPath, paramName] of Object.entries(reqParams)) {
          setNestedValue(bodyTemplate, fieldPath, `{{${paramName}}}`);
        }
      }

      functions[req.function_name!] = {
        label: req.function_label || req.function_name!,
        is_auth: req.is_auth || false,
        order: idx,
        request: {
          method: req.method,
          url: req.url,
          headers: req.headers,
          body_template: bodyTemplate || {},
        },
        response_sample: req.response ? {
          status: req.response.status,
          content_type: req.response.content_type || req.response.headers?.['content-type'],
          body: req.response.body,
        } : null,
        response_extractions: reqExtractions,
        parameters: Object.values(reqParams),
        secrets: [],
      };
    });

    const prefix = customPathPrefix.trim() || workflowName.toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9_-]/g, '');
    onSave(functions, workflowName.trim(), description.trim(), prefix);
    setSaving(false);
  };

  // Render JSON body with clickable fields for parameterization
  const renderJsonBody = (obj: any, reqId: string, prefix: string = '', isResponse: boolean = false) => {
    if (typeof obj !== 'object' || obj === null) {
      return <span className="text-secondary">{JSON.stringify(obj)}</span>;
    }

    const reqParams = parameterized[reqId] || {};
    const reqExtractions = extractions[reqId] || {};

    return (
      <div className="pl-3 border-l border-border space-y-0.5">
        {Object.entries(obj).map(([key, value]) => {
          const path = prefix ? `${prefix}.${key}` : key;
          const isParam = !!reqParams[path];
          const extractName = Object.entries(reqExtractions).find(([, p]) => p === `$.${path}` || p === path)?.[0];
          const isLeaf = typeof value !== 'object' || value === null;

          return (
            <div key={path} className="text-xs font-mono">
              <span className="text-tertiary">{key}</span>
              <span className="text-tertiary">: </span>
              {isLeaf ? (
                <span className="inline-flex items-center gap-1">
                  <span className={clsx(
                    'px-1 py-0.5 rounded cursor-pointer transition',
                    isParam ? 'bg-amber-200 text-amber-800 font-bold' :
                    extractName ? 'bg-green-200 text-green-800 font-bold' :
                    'hover:bg-hover'
                  )}
                    onClick={() => isResponse ? toggleExtraction(reqId, path) : toggleParam(reqId, path)}
                    title={isResponse ? (extractName ? t('Extract as: {{name}}', { name: extractName }) : t('Click to extract')) : (isParam ? t('Parameter: {{token}}', { token: `{{${reqParams[path]}}}` }) : t('Click to parameterize'))}
                  >
                    {isParam ? `{{${reqParams[path]}}}` : extractName ? `[extract: ${extractName}]` : JSON.stringify(value)}
                  </span>
                </span>
              ) : (
                renderJsonBody(value, reqId, path, isResponse)
              )}
            </div>
          );
        })}
      </div>
    );
  };

  const labeledCount = capturedRequests.filter(r => r.function_name).length;

  return (
    <Transition appear show={isOpen} as={Fragment}>
      <Dialog as="div" className="relative z-50" onClose={onClose}>
        <Transition.Child as={Fragment} enter="ease-out duration-300" enterFrom="opacity-0" enterTo="opacity-100" leave="ease-in duration-200" leaveFrom="opacity-100" leaveTo="opacity-0">
          <div className="fixed inset-0 bg-black/80 backdrop-blur-sm" />
        </Transition.Child>

        <div className="fixed inset-0 overflow-y-auto">
          <div className="flex min-h-full items-center justify-center p-4">
            <Transition.Child as={Fragment} enter="ease-out duration-300" enterFrom="opacity-0 scale-95" enterTo="opacity-100 scale-100" leave="ease-in duration-200" leaveFrom="opacity-100 scale-100" leaveTo="opacity-0 scale-95">
              <Dialog.Panel className="w-full max-w-7xl max-h-[90vh] transform overflow-hidden rounded-2xl bg-zinc-900 shadow-2xl transition-all flex flex-col">
                {/* Header */}
                <div className="px-6 py-4 border-b border-zinc-800 flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <BoltIcon className="h-6 w-6 text-zinc-300" />
                    <div>
                      <Dialog.Title className="text-lg font-semibold text-white">{t('API Recorder')}</Dialog.Title>
                      <p className="text-xs text-zinc-400">
                        {phase === 'setup' && t('Record HTTP API calls from any web application')}
                        {phase === 'recording' && t('Recording... {{n}} requests captured', { n: capturedRequests.length })}
                        {phase === 'review' && t('{{n}} requests captured, {{labeled}} labeled as functions', { n: capturedRequests.length, labeled: labeledCount })}
                      </p>
                    </div>
                  </div>
                  <button onClick={onClose} className="p-2 hover:bg-zinc-800 rounded-lg">
                    <XMarkIcon className="h-5 w-5 text-zinc-400" />
                  </button>
                </div>

                {/* Content */}
                <div className="flex-1 overflow-hidden flex">
                  {phase === 'setup' ? (
                    /* Setup Phase */
                    <div className="flex-1 p-8 flex items-center justify-center">
                      <div className="max-w-md w-full space-y-4">
                        <div>
                          <label className="block text-sm font-medium text-zinc-300 mb-1">{t('Target URL')}</label>
                          <input type="url" value={startUrl} onChange={(e) => setStartUrl(e.target.value)}
                            placeholder="https://your-app.com/login"
                            className="w-full px-4 py-3 bg-zinc-800 border border-zinc-700 rounded-xl text-white placeholder-zinc-500 focus:ring-2 focus:ring-zinc-500 outline-none" />
                          <p className="text-xs text-zinc-500 mt-1">{t('Navigate this app while we capture all API calls in the background')}</p>
                        </div>
                        <button onClick={startRecording} disabled={starting}
                          className="w-full px-4 py-3 bg-white hover:bg-zinc-200 text-zinc-900 rounded-xl font-medium flex items-center justify-center gap-2 disabled:opacity-50">
                          <PlayIcon className="h-5 w-5" /> {starting ? t('Starting...') : t('Start API Recording')}
                        </button>
                      </div>
                    </div>
                  ) : (
                    /* Recording / Review Phase */
                    <div className="flex-1 flex overflow-hidden">
                      {/* Left: Browser Preview */}
                      <div className="w-1/2 border-r border-zinc-800 flex flex-col">
                        <div className="p-3 border-b border-zinc-800 flex items-center justify-between">
                          <span className="text-xs text-zinc-400">{t('Browser Preview')}</span>
                          {recording && (
                            <button onClick={stopRecording}
                              className="px-3 py-1.5 bg-red-600 hover:bg-red-700 text-white rounded-lg text-xs font-medium flex items-center gap-1.5">
                              <StopIcon className="h-3.5 w-3.5" /> {t('Stop Recording')}
                            </button>
                          )}
                        </div>
                        <div className="flex-1 bg-black flex items-center justify-center overflow-hidden">
                          {screenshot ? (
                            <img src={screenshot} alt={t('Browser')} className="max-w-full max-h-full object-contain" />
                          ) : (
                            <p className="text-zinc-600 text-sm">{t('Waiting for browser...')}</p>
                          )}
                        </div>
                      </div>

                      {/* Right: Captured Requests */}
                      <div className="w-1/2 flex flex-col overflow-hidden">
                        {/* Review header with save controls */}
                        {phase === 'review' && (
                          <div className="p-3 border-b border-zinc-800 space-y-2">
                            <div className="grid grid-cols-2 gap-2">
                              <input type="text" value={workflowName} onChange={(e) => setWorkflowName(e.target.value)}
                                placeholder={t('Workflow name')} className="px-3 py-1.5 bg-zinc-800 border border-zinc-700 rounded-lg text-sm text-white placeholder-zinc-500 outline-none" />
                              <input type="text" value={customPathPrefix} onChange={(e) => setCustomPathPrefix(e.target.value)}
                                placeholder={t('URL prefix (e.g. myapp)')} className="px-3 py-1.5 bg-zinc-800 border border-zinc-700 rounded-lg text-sm text-white placeholder-zinc-500 outline-none" />
                            </div>
                            <input type="text" value={description} onChange={(e) => setDescription(e.target.value)}
                              placeholder={t('Description (optional)')} className="w-full px-3 py-1.5 bg-zinc-800 border border-zinc-700 rounded-lg text-sm text-white placeholder-zinc-500 outline-none" />
                            <button onClick={handleSave} disabled={labeledCount === 0 || saving}
                              className="w-full px-3 py-2 bg-white hover:bg-zinc-200 disabled:bg-zinc-700 disabled:text-zinc-500 text-zinc-900 rounded-lg text-sm font-medium">
                              {saving
                                ? t('Saving...')
                                : labeledCount === 1
                                  ? t('Save Workflow (1 function)')
                                  : t('Save Workflow ({{n}} functions)', { n: labeledCount })}
                            </button>
                          </div>
                        )}

                        <div className="p-3 border-b border-zinc-800">
                          <span className="text-xs text-zinc-400 font-medium">
                            {t('Captured Requests ({{n}})', { n: capturedRequests.length })}
                          </span>
                          <p className="text-[10px] text-secondary mt-0.5">{t('Click request body fields to parameterize, response fields to extract')}</p>
                        </div>

                        <div className="flex-1 overflow-y-auto divide-y divide-zinc-800/50">
                          {capturedRequests.length === 0 ? (
                            <div className="p-8 text-center text-secondary text-sm">
                              <ArrowPathIcon className="h-8 w-8 mx-auto mb-2 animate-spin" />
                              {t('Navigate the app to capture API calls...')}
                            </div>
                          ) : (
                            capturedRequests.map((req) => {
                              const isExpanded = expandedId === req.id;
                              const statusColor = req.response
                                ? req.response.status < 300 ? 'text-green-400' : req.response.status < 400 ? 'text-amber-400' : 'text-red-400'
                                : 'text-zinc-500';

                              return (
                                <div key={req.id} className={clsx(
                                  'transition',
                                  req.function_name ? 'bg-zinc-800/40 border-l-2 border-zinc-400' : ''
                                )}>
                                  {/* Request summary row */}
                                  <div className="px-3 py-2 flex items-center gap-2 cursor-pointer hover:bg-zinc-800/50"
                                    onClick={() => setExpandedId(isExpanded ? null : req.id)}>
                                    <span className={clsx('text-[10px] font-mono font-bold px-1.5 py-0.5 rounded',
                                      req.method === 'DELETE' ? 'bg-red-900/30 text-red-400' :
                                      'bg-zinc-800 text-zinc-400'
                                    )}>
                                      {req.method}
                                    </span>
                                    <span className={clsx('text-[10px] font-mono', statusColor)}>
                                      {req.response?.status || '...'}
                                    </span>
                                    <span className="text-xs text-zinc-300 truncate flex-1 font-mono">
                                      {new URL(req.url).pathname}
                                    </span>
                                    {req.function_name && (
                                      <span className="text-[10px] px-1.5 py-0.5 bg-zinc-700 text-zinc-200 rounded font-medium">
                                        {req.is_auth && <ShieldCheckIcon className="h-3 w-3 inline mr-0.5" />}
                                        {req.function_name}
                                      </span>
                                    )}
                                    {isExpanded ? <ChevronUpIcon className="h-3.5 w-3.5 text-zinc-500" /> : <ChevronDownIcon className="h-3.5 w-3.5 text-zinc-500" />}
                                  </div>

                                  {/* Expanded detail */}
                                  {isExpanded && (
                                    <div className="px-3 pb-3 space-y-2">
                                      {/* Label as function */}
                                      <div className="flex items-center gap-2">
                                        {labelingId === req.id ? (
                                          <div className="flex-1 flex items-center gap-1.5">
                                            <input type="text" value={labelForm.name} onChange={(e) => setLabelForm({ ...labelForm, name: e.target.value.replace(/[^a-zA-Z0-9_]/g, '') })}
                                              placeholder={t('functionName')} className="flex-1 px-2 py-1 bg-zinc-800 border border-zinc-600 rounded text-xs text-white outline-none font-mono" />
                                            <Checkbox
                                              checked={labelForm.is_auth}
                                              onChange={(e) => setLabelForm({ ...labelForm, is_auth: e.target.checked })}
                                              label={t('Auth')}
                                              size="sm"
                                            />

                                            <button onClick={() => labelForm.name && applyLabel(req.id)}
                                              className="px-2 py-1 bg-white text-zinc-900 rounded text-[10px] font-medium">{t('Save')}</button>
                                            <button onClick={() => setLabelingId(null)}
                                              className="px-2 py-1 bg-zinc-700 text-zinc-300 rounded text-[10px]">{t('Cancel')}</button>
                                          </div>
                                        ) : (
                                          <button onClick={() => { setLabelingId(req.id); setLabelForm({ name: req.function_name || '', label: req.function_label || '', is_auth: req.is_auth || false }); }}
                                            className="px-2 py-1 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded text-[10px] flex items-center gap-1">
                                            <TagIcon className="h-3 w-3" />
                                            {req.function_name ? t('Rename Function') : t('Label as Function')}
                                          </button>
                                        )}
                                      </div>

                                      {/* URL */}
                                      <div className="text-[10px] text-zinc-500">
                                        <span className="font-mono text-zinc-400 break-all">{req.url}</span>
                                      </div>

                                      {/* Request → Response mapped view */}
                                      <div className="grid grid-cols-2 gap-2">
                                        {/* Request side */}
                                        <div>
                                          <div className="flex items-center gap-1.5 mb-1">
                                            <span className="text-[10px] font-bold text-tertiary">{t('REQUEST')}</span>
                                            <span className="text-[10px] text-secondary">{t('(click values to parameterize)')}</span>
                                          </div>
                                          {req.headers?.['content-type'] && (
                                            <p className="text-[9px] text-secondary font-mono mb-1">{req.headers['content-type']}</p>
                                          )}
                                          <div className="bg-ink/50 rounded-lg p-2 max-h-52 overflow-y-auto border border-zinc-700/50">
                                            {req.body ? (
                                              typeof req.body === 'object'
                                                ? renderJsonBody(req.body, req.id, '', false)
                                                : <pre className="text-xs text-tertiary whitespace-pre-wrap">{req.body}</pre>
                                            ) : (
                                              <span className="text-[10px] text-secondary italic">{t('No body')}</span>
                                            )}
                                          </div>
                                        </div>

                                        {/* Response side */}
                                        <div>
                                          <div className="flex items-center gap-1.5 mb-1">
                                            <span className="text-[10px] font-bold text-tertiary">{t('RESPONSE')}</span>
                                            {req.response ? (
                                              <span className={clsx('text-[10px] font-mono font-bold px-1 rounded',
                                                req.response.status < 300 ? 'bg-green-900/30 text-green-400' :
                                                req.response.status < 400 ? 'bg-amber-900/30 text-amber-400' :
                                                'bg-red-900/30 text-red-400'
                                              )}>
                                                {req.response.status} {req.response.status_text || ''}
                                              </span>
                                            ) : (
                                              <span className="text-[10px] text-secondary animate-pulse">{t('waiting...')}</span>
                                            )}
                                            <span className="text-[10px] text-secondary">{t('(click values to extract)')}</span>
                                          </div>
                                          {req.response?.content_type && (
                                            <p className="text-[9px] text-secondary font-mono mb-1">{req.response.content_type}</p>
                                          )}
                                          <div className="bg-ink/50 rounded-lg p-2 max-h-52 overflow-y-auto border border-zinc-700/50">
                                            {req.response?.body ? (
                                              typeof req.response.body === 'object'
                                                ? renderJsonBody(req.response.body, req.id, '', true)
                                                : <pre className="text-xs text-tertiary whitespace-pre-wrap">{String(req.response.body).substring(0, 2000)}</pre>
                                            ) : (
                                              <span className="text-[10px] text-secondary italic">{req.response ? t('Empty response') : t('No response yet')}</span>
                                            )}
                                          </div>
                                        </div>
                                      </div>
                                    </div>
                                  )}
                                </div>
                              );
                            })
                          )}
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              </Dialog.Panel>
            </Transition.Child>
          </div>
        </div>
      </Dialog>
    </Transition>
  );
};

// Helper: set nested value in object by dot-separated path
function setNestedValue(obj: any, path: string, value: any) {
  const parts = path.split('.');
  let current = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    if (current[parts[i]] === undefined) return;
    current = current[parts[i]];
  }
  current[parts[parts.length - 1]] = value;
}
