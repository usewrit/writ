import React, { useState, useRef, useEffect, useCallback } from 'react';
import {
  XMarkIcon,
  PaperAirplaneIcon,
  ClipboardDocumentCheckIcon,
  PlayIcon,
  ArrowPathIcon,
} from '@heroicons/react/24/outline';
import { ScribeMark } from './brand/ScribeMark';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';

interface Message {
  role: 'user' | 'assistant';
  content: string;
  script?: string;
}

interface AIScriptAssistantProps {
  open: boolean;
  onClose: () => void;
  getScreenshot: () => string | null;
  pageUrl: string;
  handlerNames?: string[];
  /** The current advanced script, so the assistant can SEE and EDIT it (not regenerate blind). */
  currentScript?: string;
  onApplyScript: (script: string) => void;
  onGeneratingChange?: (generating: boolean) => void;
  onTestScript?: (script: string) => Promise<{ success: boolean; result?: any; error?: string }>;
  /** Run a JS script in the browser page and return the result */
  evaluateInPage?: (script: string) => Promise<any>;
}

function extractCodeBlock(text: string): string | null {
  const match = text.match(/```(?:javascript|js)?\s*\n([\s\S]*?)```/);
  return match ? match[1].trim() : null;
}

function stripCodeBlocks(text: string): string {
  return text.replace(/```(?:javascript|js)?\s*\n[\s\S]*?```/g, '').trim();
}

export const AIScriptAssistant: React.FC<AIScriptAssistantProps> = ({
  open,
  onClose,
  getScreenshot,
  pageUrl,
  handlerNames: _handlerNames = [],
  currentScript = '',
  onApplyScript,
  onGeneratingChange,
  onTestScript,
  evaluateInPage,
}) => {
  const { t } = useTranslation();
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [testingIdx, setTestingIdx] = useState<number | null>(null);
  const [testResult, setTestResult] = useState<{ success: boolean; result?: any; error?: string } | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 200);
  }, [open]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, loading]);

  // Notify parent of generating state
  useEffect(() => {
    onGeneratingChange?.(loading);
  }, [loading, onGeneratingChange]);

  const sendMessage = useCallback(async () => {
    if (!input.trim() || loading) return;
    const instruction = input.trim();
    setInput('');
    setTestResult(null);

    setMessages(prev => [...prev, { role: 'user', content: instruction }]);
    setLoading(true);

    const screenshotB64 = getScreenshot();
    if (!screenshotB64) {
      setMessages(prev => [...prev, { role: 'assistant', content: t('Could not capture screenshot. Make sure the browser is connected.') }]);
      setLoading(false);
      return;
    }

    try {
      // Extract comprehensive DOM context for the AI
      let domContext = '';
      if (evaluateInPage) {
        try {
          const dom = await evaluateInPage(`(() => {
            // 1. Interactive elements with full attributes
            const interactive = [];
            document.querySelectorAll('input, textarea, select, button, a[href], [role="button"], [role="link"], [role="textbox"], [role="combobox"], [role="listbox"], [role="search"], [contenteditable="true"]').forEach((el, i) => {
              const tag = el.tagName.toLowerCase();
              const rect = el.getBoundingClientRect();
              if (rect.width === 0 && rect.height === 0) return; // skip hidden
              const attrs = {};
              for (const a of el.attributes) {
                if (['style','class','d','viewBox','xmlns','fill','stroke'].includes(a.name)) continue;
                attrs[a.name] = a.value.substring(0, 100);
              }
              const cls = el.className && typeof el.className === 'string' ? el.className.split(' ').filter(c => c && !c.match(/^[a-z]{1,3}$/)).slice(0, 5).join(' ') : '';
              interactive.push({
                i, tag,
                id: el.id || undefined,
                type: el.getAttribute('type') || undefined,
                name: el.getAttribute('name') || undefined,
                role: el.getAttribute('role') || undefined,
                placeholder: el.getAttribute('placeholder') || undefined,
                ariaLabel: el.getAttribute('aria-label') || undefined,
                text: (tag === 'button' || tag === 'a') ? el.textContent?.trim().substring(0, 60) : undefined,
                value: el.value ? el.value.substring(0, 40) : undefined,
                cls: cls || undefined,
                disabled: el.disabled || undefined,
                visible: rect.width > 0,
              });
            });

            // 2. Key structural containers with IDs, roles, or data attributes
            const containers = [];
            document.querySelectorAll('[id], [role="main"], [role="navigation"], [role="dialog"], [role="list"], [role="feed"], [role="log"], [role="region"], [data-testid], [data-message-id], main, nav, aside, article, section, footer, header').forEach((el, i) => {
              if (i > 40) return;
              const tag = el.tagName.toLowerCase();
              const rect = el.getBoundingClientRect();
              if (rect.width === 0 && rect.height === 0) return;
              const childCount = el.children.length;
              const textLen = (el.textContent || '').length;
              containers.push({
                tag,
                id: el.id || undefined,
                role: el.getAttribute('role') || undefined,
                dataTestid: el.getAttribute('data-testid') || undefined,
                cls: typeof el.className === 'string' ? el.className.split(' ').filter(c => c.length > 3).slice(0, 4).join(' ') : undefined,
                children: childCount,
                textLen,
                rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
              });
            });

            // 3. Headings for page structure
            const headings = [...document.querySelectorAll('h1,h2,h3,h4')].slice(0, 15).map(h => ({
              level: h.tagName,
              text: h.textContent?.trim().substring(0, 80),
            }));

            // 4. Visible text blocks (paragraphs, divs with direct text)
            const textBlocks = [];
            document.querySelectorAll('p, [role="status"], [role="alert"], .message, .chat-message, [data-message-id]').forEach((el, i) => {
              if (i > 20) return;
              const text = el.textContent?.trim();
              if (!text || text.length < 5) return;
              textBlocks.push({
                tag: el.tagName.toLowerCase(),
                cls: typeof el.className === 'string' ? el.className.split(' ').filter(c => c.length > 3).slice(0, 3).join(' ') : undefined,
                text: text.substring(0, 120),
                id: el.id || undefined,
              });
            });

            // 5. Iframes
            const iframes = [...document.querySelectorAll('iframe')].map(f => ({
              src: f.src?.substring(0, 100),
              id: f.id || undefined,
              name: f.name || undefined,
            }));

            return {
              title: document.title,
              url: location.href,
              interactive: interactive.slice(0, 50),
              containers: containers.slice(0, 30),
              headings,
              textBlocks: textBlocks.slice(0, 15),
              iframes: iframes.slice(0, 5),
            };
          })()`);
          if (dom) {
            const parts = ['PAGE: ' + dom.title + ' (' + dom.url + ')'];

            if (dom.headings?.length) {
              parts.push('\\nHEADINGS:');
              dom.headings.forEach((h: any) => parts.push('  ' + h.level + ': ' + h.text));
            }

            if (dom.interactive?.length) {
              parts.push('\\nINTERACTIVE ELEMENTS (use these selectors):');
              dom.interactive.forEach((el: any) => {
                const selParts = [];
                if (el.id) selParts.push('sel=#' + el.id);
                else if (el.name) selParts.push('sel=' + el.tag + '[name="' + el.name + '"]');
                else if (el.ariaLabel) selParts.push('sel=[aria-label="' + el.ariaLabel + '"]');
                else if (el.placeholder) selParts.push('sel=' + el.tag + '[placeholder="' + el.placeholder + '"]');
                else if (el.dataTestid) selParts.push('sel=[data-testid="' + el.dataTestid + '"]');
                const desc = [el.tag, el.type && 'type=' + el.type, el.role && 'role=' + el.role, ...selParts, el.placeholder && 'placeholder="' + el.placeholder + '"', el.ariaLabel && 'aria-label="' + el.ariaLabel + '"', el.text && 'text="' + el.text + '"', el.value && 'val="' + el.value + '"', el.disabled && 'DISABLED', el.cls && 'class="' + el.cls + '"'].filter(Boolean).join(' ');
                parts.push('  [' + el.i + '] ' + desc);
              });
            }

            if (dom.containers?.length) {
              parts.push('\\nKEY CONTAINERS (for observing/extracting):');
              dom.containers.forEach((c: any) => {
                const desc = [c.tag, c.id && 'id=' + c.id, c.role && 'role=' + c.role, c.dataTestid && 'data-testid=' + c.dataTestid, c.children + ' children', c.textLen + ' chars', c.cls && 'class="' + c.cls + '"'].filter(Boolean).join(' ');
                parts.push('  ' + desc + ' [' + c.rect.w + 'x' + c.rect.h + ' at ' + c.rect.x + ',' + c.rect.y + ']');
              });
            }

            if (dom.textBlocks?.length) {
              parts.push('\\nVISIBLE TEXT:');
              dom.textBlocks.forEach((t: any) => parts.push('  ' + (t.id ? '#' + t.id + ' ' : '') + (t.cls ? '.' + t.cls.split(' ')[0] + ' ' : '') + '"' + t.text + '"'));
            }

            if (dom.iframes?.length) {
              parts.push('\\nIFRAMES:');
              dom.iframes.forEach((f: any) => parts.push('  ' + (f.id || f.name || 'unnamed') + ': ' + f.src));
            }

            domContext = parts.join('\\n');
          }
        } catch (e) {
          console.warn('[AIScriptAssistant] DOM extraction failed:', e);
        }
      }

      const { default: client } = await import('../api/client');
      const history = messages.slice(-8).map(m => ({
        role: m.role,
        content: m.role === 'assistant' && m.script
          ? `${m.content}\n\n\`\`\`javascript\n${m.script}\n\`\`\``
          : m.content,
      }));

      const resp = await client.post('/ai-assist/chat', {
        screenshot_b64: screenshotB64,
        page_url: pageUrl,
        instruction,
        conversation: history,
        context: 'streaming_script',
        page_dom: domContext || undefined,
        // Send the current script so the assistant EDITS it (returns a full rewrite)
        // rather than regenerating from scratch.
        advanced_script: currentScript || undefined,
      }, { timeout: 120000 });

      const data = resp.data;
      const rawText = data.message || '';
      const script = extractCodeBlock(rawText);
      const explanation = script ? stripCodeBlocks(rawText) : rawText;

      setMessages(prev => [...prev, {
        role: 'assistant',
        content: explanation || (script ? t('Here\'s the script:') : rawText),
        script: script || undefined,
      }]);

      // Auto-apply script to target
      if (script) {
        onApplyScript(script);
      }

      if (data.credits_used) {
        toast(
          data.credits_used === 1
            ? t('Used 1 credit')
            : t('Used {{n}} credits', { n: data.credits_used }),
          { icon: '✨', duration: 2000 },
        );
      }
    } catch (err: any) {
      const status = err?.response?.status;
      const detail = err?.response?.data?.detail || err.message || t('AI request failed');
      let errorMsg = t('Error: {{detail}}', { detail });
      if (err.code === 'ECONNABORTED' || detail.includes('timeout')) {
        errorMsg = t('Request timed out. The AI service may be slow — try again.');
      } else if (status === 502 || status === 503) {
        errorMsg = t('AI service is temporarily unavailable. Check that the AI gateway is running.');
      } else if (status === 402) {
        errorMsg = t('Insufficient funds. Add funds to use AI features.');
      } else if (status === 429) {
        errorMsg = t('Rate limit reached. Wait a moment and try again.');
      } else if (!err.response) {
        errorMsg = t('Could not reach the server. Check your connection.');
      }
      setMessages(prev => [...prev, { role: 'assistant', content: errorMsg }]);
    } finally {
      setLoading(false);
    }
  }, [input, loading, messages, getScreenshot, pageUrl, currentScript, onApplyScript, t]);

  const handleTest = useCallback(async (script: string, idx: number) => {
    if (!onTestScript) return;
    setTestingIdx(idx);
    setTestResult(null);
    try {
      const result = await onTestScript(script);
      setTestResult(result);
    } catch (err: any) {
      setTestResult({ success: false, error: err.message || t('Test failed') });
    } finally {
      setTestingIdx(null);
    }
  }, [onTestScript, t]);

  if (!open) return null;

  return (
    <div className="flex flex-col h-full bg-zinc-900 border-l border-zinc-700/50">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 h-9 border-b border-zinc-700/50 shrink-0">
        <ScribeMark className="w-4 h-4 shrink-0" />
        <span className="text-[12px] font-medium text-zinc-200 flex-1">{t('AI Script Assistant')}</span>
        <button onClick={onClose} className="p-0.5 text-zinc-500 hover:text-zinc-300 transition-colors">
          <XMarkIcon className="w-3.5 h-3.5" />
        </button>
      </div>

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 py-2 space-y-2 min-h-0">
        {messages.length === 0 && (
          <div className="flex flex-col items-center justify-center py-6 text-center">
            <ScribeMark className="w-7 h-7 mb-2" />
            <p className="text-[11px] text-zinc-400">{t('Describe what your script should do.')}</p>
            <p className="text-[10px] text-zinc-600 mt-1 max-w-[220px]">{t('AI sees the page, generates a handler script, and auto-applies it. Keep chatting to iterate.')}</p>
          </div>
        )}

        {messages.map((msg, i) => (
          <div key={i} className={clsx('text-xs', msg.role === 'user' ? 'text-right' : '')}>
            <div className={clsx(
              'inline-block max-w-full rounded-lg px-2.5 py-1.5 text-left',
              msg.role === 'user'
                ? 'bg-zinc-700 text-zinc-100'
                : 'bg-zinc-800 text-zinc-300',
            )}>
              <p className="whitespace-pre-wrap break-words text-[11px] leading-relaxed">{msg.content}</p>
            </div>

            {msg.script && (
              <div className="mt-1.5 text-left">
                <pre className="px-3 py-2 bg-black/40 text-zinc-200 text-[10px] font-mono rounded-lg overflow-x-auto leading-relaxed max-h-36 overflow-y-auto border border-zinc-700/50">
                  {msg.script}
                </pre>
                <div className="flex gap-1.5 mt-1">
                  <button
                    onClick={() => { onApplyScript(msg.script!); toast.success(t('Script applied')); }}
                    className="flex items-center gap-1 px-2 py-0.5 text-[10px] font-medium text-zinc-300 bg-zinc-800 rounded hover:bg-zinc-700 transition-colors"
                  >
                    <ClipboardDocumentCheckIcon className="w-3 h-3" />
                    {t('Apply')}
                  </button>
                  {onTestScript && (
                    <button
                      onClick={() => handleTest(msg.script!, i)}
                      disabled={testingIdx === i}
                      className="flex items-center gap-1 px-2 py-0.5 text-[10px] font-medium text-zinc-300 bg-zinc-800 rounded hover:bg-zinc-700 transition-colors disabled:opacity-50"
                    >
                      {testingIdx === i ? (
                        <><ArrowPathIcon className="w-3 h-3 animate-spin" /> {t('Testing...')}</>
                      ) : (
                        <><PlayIcon className="w-3 h-3" /> {t('Test')}</>
                      )}
                    </button>
                  )}
                </div>
              </div>
            )}
          </div>
        ))}

        {testResult && (() => {
          const r = testResult.result;
          const hasResponse = r?.response != null;
          const hasEmitted = r?.emitted?.length > 0;
          const hasActions = r?.actions?.length > 0;
          return (
            <div className={clsx(
              'px-2.5 py-2 rounded-lg text-[10px]',
              testResult.success ? 'bg-green-900/30 border border-green-700/50 text-green-400' : 'bg-red-900/30 border border-red-700/50 text-red-400',
            )}>
              <span className="font-semibold font-mono">{testResult.success ? t('Pass') : t('Failed')}</span>
              {r?.message && <span className="ml-1.5 opacity-70">— {r.message}</span>}
              {r?.testedAction && <p className="mt-0.5 opacity-50">{t('Tested:')} {r.testedAction}({r.testData ? JSON.stringify(r.testData) : ''})</p>}
              {testResult.error && <p className="mt-1 font-mono">{testResult.error}</p>}

              {/* Playwright actions executed */}
              {hasActions && (
                <div className="mt-1.5">
                  <span className="text-[9px] uppercase tracking-wider opacity-60">{t('Playwright actions:')}</span>
                  {r.actions.map((a: any, i: number) => (
                    <div key={i} className={clsx('mt-0.5 font-mono', a.ok ? 'text-green-300' : 'text-red-300')}>
                      {a.ok ? '✓' : '✗'} {a.fn}({a.args?.map((x: any) => typeof x === 'string' ? `"${x}"` : x).join(', ')})
                      {a.error && <span className="text-red-400 ml-1">— {a.error}</span>}
                    </div>
                  ))}
                </div>
              )}

              {hasResponse && (
                <div className="mt-1.5">
                  <span className="text-[9px] uppercase tracking-wider opacity-60">{t('Response to caller:')}</span>
                  <pre className="mt-0.5 whitespace-pre-wrap break-all font-mono text-zinc-300">{JSON.stringify(r.response.data, null, 2)}</pre>
                </div>
              )}
              {!hasResponse && testResult.success && r?.ok && !hasActions && (
                <p className="mt-1 opacity-60">{t("Handler ran but didn't call ps.respond()")}</p>
              )}
              {hasEmitted && (
                <div className="mt-1.5">
                  <span className="text-[9px] uppercase tracking-wider opacity-60">{t('Emitted events:')}</span>
                  {r.emitted.map((e: any, i: number) => (
                    <pre key={i} className="mt-0.5 whitespace-pre-wrap break-all font-mono text-zinc-300">{e.event}: {JSON.stringify(e.data)}</pre>
                  ))}
                </div>
              )}
            </div>
          );
        })()}

        {loading && (
          <div className="flex items-center gap-1.5 text-[10px] text-zinc-500 py-1">
            <ArrowPathIcon className="w-3 h-3 animate-spin" />
            {t('AI is thinking...')}
          </div>
        )}
      </div>

      {/* Input */}
      <div className="px-2 pb-2 pt-1 border-t border-zinc-700/50 shrink-0">
        <div className="flex gap-1">
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); e.stopPropagation(); sendMessage(); } }}
            placeholder={messages.length === 0 ? t('e.g. "Extract table rows as JSON on request"') : t('Ask for changes...')}
            disabled={loading}
            className="flex-1 px-2.5 py-1.5 bg-zinc-800 border border-zinc-700 rounded text-[11px] text-zinc-200 placeholder:text-zinc-600 focus:ring-1 focus:ring-zinc-400 focus:outline-none disabled:opacity-50"
          />
          <button
            onClick={sendMessage}
            disabled={loading || !input.trim()}
            className="px-2 py-1.5 bg-zinc-100 hover:bg-white disabled:bg-zinc-800 disabled:text-zinc-600 text-zinc-900 rounded transition"
          >
            <PaperAirplaneIcon className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </div>
  );
};
