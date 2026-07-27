import React, { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import {
  SparklesIcon,
  ServerStackIcon,
  KeyIcon,
  CommandLineIcon,
  ClipboardDocumentIcon,
  CheckIcon,
  ArrowPathIcon,
  ExclamationTriangleIcon,
  CodeBracketIcon,
  WrenchScrewdriverIcon,
  ChevronRightIcon,
  CubeIcon,
} from '@heroicons/react/24/outline';
import { useRequireAuth } from '../../hooks/useAuth';
import { useDocumentTitle } from '../../hooks/useDocumentTitle';
import { useQuery } from '../../hooks/useQuery';
import { Q } from '../../stores/queryKeys';
import { mcpConnectApi, McpConnectInfo } from '../../api/mcp';

// ─────────────────────────────────────────────────────────────────────────────
// Connect — attach an AI assistant (Claude Code, Claude Desktop, Cursor, or any
// MCP client) to THIS self-hosted coordinator. It surfaces the same operate
// tools the desktop app exposes over MCP: run saved workflows, read their data,
// schedule them, expose them as REST, and crawl. Snippets come from the
// coordinator's own GET /api/mcp/connect-info; the API key is a placeholder the
// operator swaps for one they mint in Developers → API Keys.
// ─────────────────────────────────────────────────────────────────────────────

const useCopy = () => {
  const { t } = useTranslation();
  const [copied, setCopied] = useState<string | null>(null);
  const copy = (text: string, key: string) => {
    navigator.clipboard.writeText(text);
    setCopied(key);
    toast.success(t('Copied'));
    setTimeout(() => setCopied(c => (c === key ? null : c)), 2000);
  };
  return { copied, copy };
};

// A labelled code block with a copy button in its header bar.
const CodeBlock: React.FC<{
  label: string;
  code: string;
  copyKey: string;
  copied: string | null;
  onCopy: (text: string, key: string) => void;
  multiline?: boolean;
}> = ({ label, code, copyKey, copied, onCopy, multiline }) => {
  const { t } = useTranslation();
  const isCopied = copied === copyKey;
  return (
    <div className="rounded-xl border border-border bg-canvas overflow-hidden">
      <div className="flex items-center justify-between px-3 py-1.5 border-b border-border bg-hover">
        <span className="text-[10px] font-semibold text-tertiary uppercase tracking-wider">{label}</span>
        <button
          onClick={() => onCopy(code, copyKey)}
          className="flex items-center gap-1.5 px-2 py-0.5 text-[11px] text-secondary hover:text-ink transition-colors"
        >
          {isCopied ? <CheckIcon className="w-3.5 h-3.5 text-emerald-500" /> : <ClipboardDocumentIcon className="w-3.5 h-3.5" />}
          {isCopied ? t('Copied') : t('Copy')}
        </button>
      </div>
      <pre
        className={clsx(
          'text-[12px] font-mono text-ink px-3 py-2.5 overflow-x-auto',
          multiline ? 'whitespace-pre leading-relaxed' : 'whitespace-pre',
        )}
      >
        <code>{code}</code>
      </pre>
    </div>
  );
};

type ClientId = 'claude_code' | 'claude_desktop' | 'cursor';

const CLIENTS: { id: ClientId; label: string }[] = [
  { id: 'claude_code', label: 'Claude Code' },
  { id: 'claude_desktop', label: 'Claude Desktop' },
  { id: 'cursor', label: 'Cursor' },
];

const ConnectClaudeCard: React.FC<{ info: McpConnectInfo }> = ({ info }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { copied, copy } = useCopy();
  const [client, setClient] = useState<ClientId>('claude_code');
  const [showHttp, setShowHttp] = useState(false);
  const pkg = info.node_connector.package;

  const snippet = useMemo(() => {
    switch (client) {
      case 'claude_code':
        return { label: t('Terminal — run once'), code: info.node_connector.claude_code, multiline: false };
      case 'claude_desktop':
        return { label: 'claude_desktop_config.json', code: JSON.stringify(info.node_connector.claude_desktop, null, 2), multiline: true };
      case 'cursor':
        return { label: '~/.cursor/mcp.json', code: JSON.stringify(info.node_connector.cursor, null, 2), multiline: true };
    }
  }, [client, info, t]);

  const httpCode = JSON.stringify(info.streamable_http, null, 2);

  return (
    <div className="bg-surface border border-ink/20 rounded-xl shadow-sm overflow-hidden">
      {/* Header */}
      <div className="flex items-start gap-3 px-4 py-4 border-b border-border">
        <div className="w-9 h-9 rounded-lg bg-ink text-white flex items-center justify-center shrink-0">
          <SparklesIcon className="w-5 h-5" />
        </div>
        <div className="min-w-0">
          <h2 className="text-base font-semibold text-ink tracking-tight">{t('Connect Claude')}</h2>
          <p className="text-[12px] text-secondary mt-0.5 leading-relaxed">
            {t('Attach Claude (or any MCP client) to this coordinator. Your saved workflows become tools it can run, read, schedule, expose, and build — locally, at zero AI-token cost.')}
          </p>
        </div>
      </div>

      {/* Step 1 — key */}
      <div className="px-4 py-3.5 border-b border-border">
        <div className="flex items-start gap-2.5">
          <span className="mt-0.5 w-5 h-5 rounded-full bg-hover border border-border text-[11px] font-semibold text-secondary flex items-center justify-center shrink-0">1</span>
          <div className="min-w-0 flex-1">
            <p className="text-[13px] font-medium text-ink">{t('Get an API key')}</p>
            <p className="text-[12px] text-secondary mt-0.5 leading-relaxed">{info.auth.instructions}</p>
            <button
              onClick={() => navigate('/developers')}
              className="mt-2 inline-flex items-center gap-1.5 px-3 py-1.5 bg-accent-strong text-accent-on text-[12px] font-medium rounded-lg hover:bg-accent-strong/90 transition-colors"
            >
              <KeyIcon className="w-3.5 h-3.5" />
              {t('Create an API key')}
            </button>
          </div>
        </div>
      </div>

      {/* Step 2 — the writ-mcp Node connector (primary path) */}
      <div className="px-4 py-3.5">
        <div className="flex items-start gap-2.5">
          <span className="mt-0.5 w-5 h-5 rounded-full bg-hover border border-border text-[11px] font-semibold text-secondary flex items-center justify-center shrink-0">2</span>
          <div className="min-w-0 flex-1 space-y-3">
            <div>
              <div className="flex items-center gap-2 flex-wrap">
                <p className="text-[13px] font-medium text-ink flex items-center gap-1.5">
                  <CubeIcon className="w-4 h-4 text-tertiary" />
                  {t('Add the')} <code className="font-mono text-ink bg-canvas border border-border rounded px-1 py-0.5 text-[11px]">{pkg}</code> {t('connector')}
                </p>
                <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-ink/10 text-ink font-semibold uppercase tracking-wide">{t('Recommended')}</span>
              </div>
              <p className="text-[12px] text-secondary mt-1 leading-relaxed">
                {t('A tiny zero-dependency Node bridge (Node 18+).')}{' '}
                <code className="font-mono text-secondary">npx -y {pkg}</code>{' '}
                {t('fetches it — or run the copy bundled with your install at')}{' '}
                <code className="font-mono text-secondary">connectors/writ-mcp</code>.{' '}
                {t('Pick your client and paste; replace')}{' '}
                <code className="font-mono text-ink bg-canvas border border-border rounded px-1 py-0.5 text-[11px]">&lt;YOUR_API_KEY&gt;</code>{' '}
                {t('with your key.')}
              </p>
            </div>

            {/* Client segmented control (all via the Node connector) */}
            <div className="flex items-center gap-0.5 flex-wrap">
              {CLIENTS.map(c => (
                <button
                  key={c.id}
                  onClick={() => setClient(c.id)}
                  className={clsx(
                    'flex items-center gap-1.5 px-2.5 py-1 text-[12px] font-medium rounded-md transition-colors',
                    client === c.id ? 'bg-canvas text-ink shadow-sm border border-border' : 'text-tertiary hover:text-secondary hover:bg-canvas/60',
                  )}
                >
                  <CommandLineIcon className="w-3.5 h-3.5" />
                  {c.label}
                </button>
              ))}
            </div>

            {snippet && (
              <CodeBlock
                label={snippet.label}
                code={snippet.code}
                copyKey={`snippet-${client}`}
                copied={copied}
                onCopy={copy}
                multiline={snippet.multiline}
              />
            )}

            {/* Endpoint line */}
            <div className="flex items-center gap-2 p-2 bg-hover border border-border rounded-lg">
              <ServerStackIcon className="w-4 h-4 text-tertiary shrink-0" />
              <span className="text-[11px] text-tertiary shrink-0">{t('Endpoint')}</span>
              <code className="flex-1 text-[11px] font-mono text-ink truncate">{info.endpoint}</code>
              <button
                onClick={() => copy(info.endpoint, 'endpoint')}
                className="flex items-center gap-1.5 px-2 py-0.5 text-[11px] text-secondary hover:text-ink transition-colors shrink-0"
              >
                {copied === 'endpoint' ? <CheckIcon className="w-3.5 h-3.5" /> : <ClipboardDocumentIcon className="w-3.5 h-3.5" />}
                {copied === 'endpoint' ? t('Copied') : t('Copy')}
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Secondary — Streamable-HTTP, for clients that don't run Node */}
      <div className="px-4 py-3 border-t border-border bg-canvas/40">
        <button
          onClick={() => setShowHttp(v => !v)}
          className="flex items-center gap-2 text-[12px] font-medium text-secondary hover:text-ink transition-colors"
        >
          <ChevronRightIcon className={clsx('w-3.5 h-3.5 transition-transform', showHttp && 'rotate-90')} />
          <ServerStackIcon className="w-3.5 h-3.5 text-tertiary" />
          {t('Not using Node? Connect a Streamable-HTTP client directly')}
        </button>
        {showHttp && (
          <div className="mt-2.5 pl-5 space-y-2">
            <CodeBlock
              label={t('Streamable HTTP (no connector)')}
              code={httpCode}
              copyKey="snippet-http"
              copied={copied}
              onCopy={copy}
              multiline
            />
            <p className="text-[11px] text-tertiary leading-relaxed">
              {t('Point any Streamable-HTTP MCP client straight at the endpoint above with an')}{' '}
              <code className="font-mono text-secondary">Authorization: Bearer &lt;YOUR_API_KEY&gt;</code> {t('header — no Node needed.')}
            </p>
          </div>
        )}
      </div>
    </div>
  );
};

const ToolsCard: React.FC<{ tools: string[] }> = ({ tools }) => {
  const { t } = useTranslation();
  if (!tools.length) return null;
  return (
    <div>
      <h2 className="text-[13px] font-semibold text-ink mb-2 flex items-center gap-1.5">
        <WrenchScrewdriverIcon className="w-4 h-4 text-tertiary" />
        {t('Tools it gets')}
      </h2>
      <div className="rounded-xl border border-ink/20 bg-surface p-3 shadow-sm">
        <div className="flex flex-wrap gap-1.5">
          {tools.map(name => (
            <span key={name} className="text-[11px] font-mono text-ink bg-canvas border border-border rounded px-1.5 py-0.5">
              {name}
            </span>
          ))}
        </div>
        <p className="text-[11px] text-tertiary mt-2.5 leading-relaxed">
          {t('Plus a run tool per saved workflow. Recording new workflows happens in the app — these tools drive the ones you already have.')}
        </p>
      </div>
    </div>
  );
};

export const ConnectPage: React.FC = () => {
  const { t } = useTranslation();
  useRequireAuth();
  useDocumentTitle(t('Connect'));
  const navigate = useNavigate();

  const { data, loading, error, refresh } = useQuery<McpConnectInfo>(
    Q.key('mcp-connect-info'),
    mcpConnectApi.getConnectInfo,
  );

  const initialLoading = loading && !data;
  const loadFailed = !data && !loading && Boolean(error);

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center gap-3 h-12 px-4 sm:px-6 border-b border-border shrink-0">
        <SparklesIcon className="w-4 h-4 text-tertiary shrink-0" />
        <span className="text-[13px] font-semibold text-ink shrink-0">{t('Connect')}</span>
        <span className="hidden @pair/stage:inline text-[12px] text-tertiary truncate">
          {t('Use your workflows from Claude & other AI assistants')}
        </span>
        <div className="flex-1" />
        <button
          onClick={() => navigate('/developers')}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-accent-strong text-accent-on text-[12px] font-medium rounded-lg hover:bg-accent-strong/90 transition-colors shrink-0"
        >
          <KeyIcon className="w-3.5 h-3.5" />
          {t('API keys')}
        </button>
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
        <div className="relative z-10 py-6 px-4 sm:px-6 w-full">
          {initialLoading ? (
            <p className="text-[13px] text-tertiary py-3">{t('Loading connection details…')}</p>
          ) : loadFailed ? (
            <div className="flex flex-col @pair/stage:flex-row @pair/stage:items-center @pair/stage:justify-between gap-3 rounded-xl border border-ink/20 bg-surface px-4 py-3.5 shadow-sm max-w-2xl">
              <div className="flex items-start gap-3 min-w-0">
                <ExclamationTriangleIcon className="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
                <div className="min-w-0">
                  <p className="text-[13px] font-medium text-ink">{t("Couldn't load connection details")}</p>
                  {error && <p className="text-xs text-secondary mt-0.5 break-all">{error}</p>}
                </div>
              </div>
              <button
                onClick={refresh}
                className="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 bg-accent-strong text-accent-on text-[12px] font-medium rounded-lg hover:bg-accent-strong/90 transition-colors"
              >
                <ArrowPathIcon className="w-3.5 h-3.5" />
                {t('Retry')}
              </button>
            </div>
          ) : data ? (
            <div className="grid grid-cols-1 @split/stage:grid-cols-4 gap-x-8 gap-y-8 items-start">
              {/* MAIN */}
              <div className="@split/stage:col-span-3 min-w-0 space-y-6">
                <ConnectClaudeCard info={data} />
                <ToolsCard tools={data.tools} />
              </div>

              {/* SIDEBAR */}
              <aside className="@split/stage:col-span-1 min-w-0 space-y-8">
                <div>
                  <h2 className="text-[13px] font-semibold text-ink mb-2">{t('How it works')}</h2>
                  <div className="rounded-xl border border-ink/20 bg-surface p-4 text-[12px] text-secondary leading-relaxed shadow-sm">
                    {t('Your coordinator speaks the Model Context Protocol. A connected assistant sees your saved workflows as callable tools and runs them locally — no browsing, no scraping code, no AI-token cost to replay.')}
                  </div>
                </div>

                {/* Coexistence with the official Writ desktop app. */}
                {data.coexists_with_desktop && (
                  <div>
                    <h2 className="text-[13px] font-semibold text-ink mb-2">{t('Also use the Writ app?')}</h2>
                    <div className="rounded-xl border border-ink/20 bg-surface p-4 text-[12px] text-secondary leading-relaxed shadow-sm">
                      <p>
                        {t('This server registers as')}{' '}
                        <code className="font-mono text-ink bg-canvas border border-border rounded px-1 py-0.5 text-[11px]">{data.server_name}</code>{' '}
                        {t('so it runs alongside the Writ desktop app (which registers as')}{' '}
                        <code className="font-mono text-ink bg-canvas border border-border rounded px-1 py-0.5 text-[11px]">writ</code>
                        {t('). Both can be connected at once — the assistant builds/records in the app, and runs the coordinator’s shared workflows here.')}
                      </p>
                    </div>
                  </div>
                )}
                <div>
                  <h2 className="text-[13px] font-semibold text-ink mb-2">{t('Quick links')}</h2>
                  <div className="rounded-xl border border-ink/20 bg-surface divide-y divide-border overflow-hidden shadow-sm">
                    <button onClick={() => navigate('/developers')} className="w-full flex items-center gap-2.5 px-4 py-2.5 text-left hover:bg-chrome transition-colors">
                      <KeyIcon className="w-4 h-4 text-tertiary shrink-0" />
                      <span className="text-[12px] text-ink">{t('Manage API keys')}</span>
                    </button>
                    <button onClick={() => navigate('/developers?tab=endpoints')} className="w-full flex items-center gap-2.5 px-4 py-2.5 text-left hover:bg-chrome transition-colors">
                      <ServerStackIcon className="w-4 h-4 text-tertiary shrink-0" />
                      <span className="text-[12px] text-ink">{t('Endpoints & MCP tools')}</span>
                    </button>
                    <button onClick={() => navigate('/workflows')} className="w-full flex items-center gap-2.5 px-4 py-2.5 text-left hover:bg-chrome transition-colors">
                      <CodeBracketIcon className="w-4 h-4 text-tertiary shrink-0" />
                      <span className="text-[12px] text-ink">{t('Your workflows')}</span>
                    </button>
                  </div>
                </div>
              </aside>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
};

export default ConnectPage;
