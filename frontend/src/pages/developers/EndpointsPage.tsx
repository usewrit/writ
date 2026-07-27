import React, { useState } from 'react';
import { useTranslation, Trans } from 'react-i18next';
import { Link, useNavigate } from 'react-router-dom';
import { useRequireAuth } from '../../hooks/useAuth';
import { useDocumentTitle } from '../../hooks/useDocumentTitle';
import { useQuery } from '../../hooks/useQuery';
import { Q } from '../../stores/queryKeys';
import { formatRelativeTime } from '../../utils/format';
import { statusStyle } from '../../utils/statusStyle';
import { docsUrl, DOCS_LINK_PROPS } from '../../utils/docs';
import { mcpOverviewApi, McpOverview, McpOverviewServer } from '../../api/mcp';
import {
  listInboundWebhooks,
  WebhookTriggerWithAutomation,
} from '../../api/endpointsRegistry';
import { webhookTriggersApi } from '../../api/endpoints';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import {
  CircleStackIcon,
  ServerStackIcon,
  BoltIcon,
  CodeBracketIcon,
  KeyIcon,
  ClipboardDocumentIcon,
  CheckIcon,
  EyeIcon,
  EyeSlashIcon,
  PlusIcon,
  ArrowPathIcon,
  ExclamationTriangleIcon,
  WrenchScrewdriverIcon,
  ArrowTopRightOnSquareIcon,
} from '@heroicons/react/24/outline';

// ────────────────────────────────────────────────────────────────────────────
// Shared little bits
// ────────────────────────────────────────────────────────────────────────────

type SectionId = 'rest' | 'mcp' | 'webhooks';

const SECTIONS: { id: SectionId; label: string; icon: typeof CircleStackIcon }[] = [
  { id: 'rest', label: 'REST', icon: CircleStackIcon },
  { id: 'mcp', label: 'MCP tools', icon: ServerStackIcon },
  { id: 'webhooks', label: 'Incoming webhooks', icon: BoltIcon },
];

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

const SectionLoading: React.FC<{ label: string }> = ({ label }) => (
  <p className="text-[13px] text-tertiary py-3">{label}</p>
);

const SectionError: React.FC<{ message: string | null; onRetry: () => void }> = ({ message, onRetry }) => {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col @pair/stage:flex-row @pair/stage:items-center @pair/stage:justify-between gap-3 rounded-xl border border-ink/20 bg-surface px-4 py-3.5 shadow-sm">
      <div className="flex items-start gap-3 min-w-0">
        <ExclamationTriangleIcon className="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
        <div className="min-w-0">
          <p className="text-[13px] font-medium text-ink">{t("Couldn't load this section")}</p>
          {message && <p className="text-xs text-secondary mt-0.5 break-all">{message}</p>}
        </div>
      </div>
      <button
        onClick={onRetry}
        className="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 bg-accent-strong text-accent-on text-[12px] font-medium rounded-lg hover:bg-accent-strong/90 transition-colors"
      >
        <ArrowPathIcon className="w-3.5 h-3.5" />
        {t('Retry')}
      </button>
    </div>
  );
};

// Compact empty state — rendered INSIDE the caller's bordered card (no own border).
const EmptyState: React.FC<{
  icon: typeof CircleStackIcon;
  title: string;
  body: React.ReactNode;
  action?: React.ReactNode;
}> = ({ icon: Icon, title, body, action }) => (
  <div className="flex flex-col @pair/stage:flex-row @pair/stage:items-center @pair/stage:justify-between gap-3 px-4 py-3.5">
    <div className="flex items-start gap-3 min-w-0">
      <Icon className="w-5 h-5 text-tertiary shrink-0 mt-0.5" />
      <div className="min-w-0">
        <p className="text-[13px] font-medium text-ink">{title}</p>
        <p className="text-xs text-secondary mt-0.5 leading-relaxed">{body}</p>
      </div>
    </div>
    {action && <div className="shrink-0">{action}</div>}
  </div>
);

const SectionHeading: React.FC<{ title: string; meta?: string; description?: string; right?: React.ReactNode }> = ({ title, meta, description, right }) => (
  <div className="flex items-end justify-between gap-3 mb-3">
    <div className="min-w-0">
      <h2 className="text-base font-semibold text-ink tracking-tight flex items-baseline gap-2">
        {title}
        {meta && <span className="text-xs text-tertiary font-normal tabular-nums">{meta}</span>}
      </h2>
      {description && <p className="text-xs text-secondary mt-0.5 leading-relaxed">{description}</p>}
    </div>
    {right && <div className="shrink-0">{right}</div>}
  </div>
);

// ────────────────────────────────────────────────────────────────────────────
// REST section — how to call a workflow over HTTP (self-host serves /api/v1).
// Caller auth keys live in the dedicated API Keys tab (single source for keys).
// ────────────────────────────────────────────────────────────────────────────

const RestSection: React.FC = () => {
  const { t } = useTranslation();
  const { copied, copy } = useCopy();
  const base = `${window.location.origin}/api/v1`;

  return (
    <div className="space-y-6">
      <div>
        <SectionHeading
          title={t('REST access')}
          description={t('Every workflow is callable over HTTP at the coordinator’s own origin. Authenticate with an API key from the API Keys tab.')}
        />
        <div className="bg-surface border border-ink/20 rounded-xl p-4 space-y-4 shadow-sm">
          <div>
            <div className="text-[12px] font-medium text-secondary mb-1">{t('Run a workflow')}</div>
            <div className="flex items-center gap-2">
              <code className="flex-1 text-[12px] font-mono text-ink bg-canvas border border-border rounded-lg px-3 py-2 truncate">
                POST {base}/workflows/{'{workflow_id}'}/run
              </code>
              <button
                onClick={() => copy(`${base}/workflows/{workflow_id}/run`, 'rest-run')}
                className="p-2 text-tertiary hover:text-ink rounded-lg hover:bg-chrome transition-colors shrink-0"
                title={t('Copy')}
              >
                {copied === 'rest-run' ? <CheckIcon className="w-4 h-4 text-emerald-500" /> : <ClipboardDocumentIcon className="w-4 h-4" />}
              </button>
            </div>
          </div>
          <div className="flex items-start gap-2 text-[12px] text-secondary">
            <KeyIcon className="w-4 h-4 text-tertiary shrink-0 mt-0.5" />
            <span>
              <Trans i18nKey="Pass your key as <0>Authorization: Bearer wlk_…</0>. Mint and scope keys in the API Keys tab.">
                Pass your key as <code className="font-mono text-ink">Authorization: Bearer wlk_…</code>. Mint and scope keys in the API Keys tab.
              </Trans>
            </span>
          </div>
          <Link
            to="/workflows"
            className="inline-flex items-center gap-1.5 text-[12px] text-ink underline decoration-zinc-300 hover:decoration-zinc-500 transition-colors"
          >
            {t('Open a workflow to see its exact call snippet')}
            <ArrowTopRightOnSquareIcon className="w-3 h-3" />
          </Link>
        </div>
      </div>
    </div>
  );
};

// ────────────────────────────────────────────────────────────────────────────
// MCP section — workflows exposed as MCP tools (read-only, /api/mcp/overview)
// ────────────────────────────────────────────────────────────────────────────

const McpSection: React.FC = () => {
  const { t } = useTranslation();
  const { copied, copy } = useCopy();
  const { data, loading, error, refresh } = useQuery<McpOverview>(
    Q.key('registry-mcp-overview'),
    mcpOverviewApi.getOverview,
  );

  const servers = data?.servers || [];
  const initialLoading = loading && !data;
  const loadFailed = !data && !loading && Boolean(error);

  if (initialLoading) return <SectionLoading label={t('Loading MCP tools…')} />;
  if (loadFailed) return <SectionError message={error} onRetry={refresh} />;

  if (servers.length === 0) {
    return (
      <div className="bg-surface border border-ink/20 rounded-xl shadow-sm">
        <EmptyState
          icon={ServerStackIcon}
          title={t('No MCP tools exposed')}
          body={
            <Trans>
              Expose a workflow as an MCP tool so Claude Code, Claude Desktop, or any MCP client can
              call it with a <code className="bg-zinc-100 px-1 py-0.5 rounded text-[10px]">wt_*</code> API
              key. Use the <span className="text-secondary font-medium">Publish / Call this</span> panel on a workflow.
            </Trans>
          }
          action={
            <Link
              to="/workflows"
              className="inline-flex items-center gap-1.5 px-4 py-2 bg-accent-strong text-accent-on text-sm font-medium rounded-lg hover:bg-accent-strong/90 transition-colors"
            >
              {t('Go to Workflows')}
            </Link>
          }
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {servers.map((server: McpOverviewServer) => (
        <div key={server.id}>
          <SectionHeading
            title={server.name}
            meta={
              server.tools.length === 1
                ? t('{{count}} tool', { count: server.tools.length })
                : t('{{count}} tools', { count: server.tools.length })
            }
          />
          {/* Connection + auth */}
          <div className="bg-surface border border-ink/20 rounded-xl overflow-hidden shadow-sm">
            <div className="px-4 py-3 border-b border-border space-y-2.5">
              <div className="flex items-center gap-2 p-2.5 bg-hover border border-border rounded-lg">
                <ServerStackIcon className="w-4 h-4 text-tertiary shrink-0" />
                <span className="text-[11px] text-tertiary shrink-0">{t('Server URL')}</span>
                <code className="flex-1 text-[11px] font-mono text-ink truncate">{server.connection_url}</code>
                {!server.enabled && (
                  <span className={clsx('text-[10px] px-1.5 py-0.5 rounded-full font-medium shrink-0', statusStyle('disabled').pill)}>
                    {t('disabled')}
                  </span>
                )}
                <button
                  onClick={() => copy(server.connection_url, `mcp-url-${server.id}`)}
                  className="flex items-center gap-1.5 px-2 py-1 text-[11px] text-secondary hover:text-ink transition-colors shrink-0"
                >
                  {copied === `mcp-url-${server.id}` ? <CheckIcon className="w-3.5 h-3.5" /> : <ClipboardDocumentIcon className="w-3.5 h-3.5" />}
                  {copied === `mcp-url-${server.id}` ? t('Copied') : t('Copy')}
                </button>
              </div>
              <div className="flex items-start gap-2">
                <KeyIcon className="w-4 h-4 text-tertiary shrink-0 mt-0.5" />
                <p className="text-[11px] text-tertiary leading-relaxed">{server.auth.instructions}</p>
              </div>
            </div>

            {/* Tools table — 3 columns (name/description/workflow) need @rail/stage of
                room to read as a table; below that the rows stack (see below). */}
            <div className="hidden @rail/stage:grid grid-cols-12 gap-3 px-4 py-2 border-b border-border">
              <span className="col-span-3 text-[10px] font-semibold text-tertiary uppercase tracking-wider">{t('Tool')}</span>
              <span className="col-span-6 text-[10px] font-semibold text-tertiary uppercase tracking-wider">{t('Description')}</span>
              <span className="col-span-3 text-[10px] font-semibold text-tertiary uppercase tracking-wider">{t('Workflow')}</span>
            </div>
            {server.tools.length === 0 ? (
              <div className="flex items-center gap-2 px-4 py-3">
                <WrenchScrewdriverIcon className="w-4 h-4 text-tertiary shrink-0" />
                <p className="text-[11px] text-tertiary">
                  {t("No tools exposed yet — add workflows to this server from a workflow's Publish panel.")}
                </p>
              </div>
            ) : (
              <div className="divide-y divide-border">
                {server.tools.map(tool => (
                  <div
                    key={tool.workflow_id}
                    className="grid grid-cols-1 @rail/stage:grid-cols-12 gap-1 @rail/stage:gap-3 px-4 py-3 hover:bg-chrome transition-colors"
                  >
                    <div className="@rail/stage:col-span-3 min-w-0 flex items-center">
                      <span className="text-[11px] font-mono text-ink bg-zinc-100 px-1.5 py-0.5 rounded truncate">
                        {tool.tool_name}
                      </span>
                    </div>
                    <div className="@rail/stage:col-span-6 min-w-0 flex items-center">
                      <p className="text-[12px] text-secondary truncate">{tool.description || '—'}</p>
                    </div>
                    <div className="@rail/stage:col-span-3 min-w-0 flex items-center">
                      {tool.workflow_name ? (
                        <Link
                          to={`/workflows/${tool.workflow_id}`}
                          className="flex items-center gap-1 text-[12px] text-ink underline decoration-zinc-300 hover:decoration-zinc-500 truncate transition-colors"
                          title={t("Open the workflow's Publish panel")}
                        >
                          {tool.workflow_name}
                          <ArrowTopRightOnSquareIcon className="w-3 h-3 shrink-0" />
                        </Link>
                      ) : (
                        <span className="text-[12px] text-tertiary">{t('Workflow unavailable')}</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      ))}
      <p className="text-[11px] text-tertiary leading-relaxed">
        {t("MCP tools are configured on each workflow's Publish panel. Open a tool's workflow to change which functions are exposed, the tool name, or auth.")}
      </p>
    </div>
  );
};

// ────────────────────────────────────────────────────────────────────────────
// Webhooks section — incoming webhook_received endpoints (read-only)
// ────────────────────────────────────────────────────────────────────────────

const WebhookRow: React.FC<{ item: WebhookTriggerWithAutomation }> = ({ item }) => {
  const { trigger, automationId, automationName } = item;
  const { t } = useTranslation();
  const { copied, copy } = useCopy();
  const [revealed, setRevealed] = useState(false);

  const url = webhookTriggersApi.getWebhookUrl(trigger.token);
  const maskedToken = trigger.token.length > 8
    ? `${trigger.token.slice(0, 4)}${'•'.repeat(Math.max(trigger.token.length - 8, 4))}${trigger.token.slice(-4)}`
    : '••••••••';

  return (
    <div className="px-4 py-3 hover:bg-chrome transition-colors">
      <div className="flex items-center gap-3">
        <span
          className={clsx(
            'w-2 h-2 rounded-full shrink-0',
            statusStyle(trigger.enabled ? 'enabled' : 'disabled').dot,
          )}
          title={trigger.enabled ? t('Enabled') : t('Disabled')}
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <p className="text-[13px] font-medium text-ink truncate">{trigger.name}</p>
            {!trigger.enabled && (
              <span className={clsx('text-[10px] px-1.5 py-0.5 rounded-full font-medium shrink-0', statusStyle('disabled').pill)}>
                {t('disabled')}
              </span>
            )}
            {trigger.has_secret && (
              <span
                className="flex items-center gap-1 text-[10px] text-tertiary shrink-0"
                title={t('Each request is verified with a signature, so only your system can trigger it')}
              >
                <CheckIcon className="w-3 h-3" /> {t('signature verified')}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2 mt-0.5 text-[11px] text-tertiary">
            <span className="tabular-nums">{t('{{count}} deliveries', { count: trigger.trigger_count ?? 0 })}</span>
            {trigger.last_triggered_at && (
              <span>· {t('last {{time}}', { time: formatRelativeTime(trigger.last_triggered_at) })}</span>
            )}
            <span title={t('What the automation does when this webhook fires')}>· {t('runs')} <code className="font-mono">{trigger.action}</code></span>
          </div>
        </div>
        {automationId ? (
          <Link
            to={`/automations/${automationId}`}
            className="flex items-center gap-1 text-[12px] text-ink underline decoration-zinc-300 hover:decoration-zinc-500 transition-colors shrink-0"
            title={t('Open the automation that this webhook triggers')}
          >
            {automationName || t('Automation #{{id}}', { id: automationId })}
            <ArrowTopRightOnSquareIcon className="w-3 h-3" />
          </Link>
        ) : (
          <span className="text-[11px] text-tertiary shrink-0">{t('No automation linked')}</span>
        )}
      </div>

      {/* URL + token reveal */}
      <div className="mt-2.5 space-y-2 pl-5">
        <div className="flex items-center gap-2 p-2 bg-hover border border-border rounded-lg">
          <span className="text-[10px] text-tertiary shrink-0 uppercase tracking-wider">{t('URL')}</span>
          <code className="flex-1 text-[11px] font-mono text-ink truncate">{url}</code>
          <button
            onClick={() => copy(url, `url-${trigger.id}`)}
            className="flex items-center gap-1.5 px-2 py-0.5 text-[11px] text-secondary hover:text-ink transition-colors shrink-0"
          >
            {copied === `url-${trigger.id}` ? <CheckIcon className="w-3.5 h-3.5" /> : <ClipboardDocumentIcon className="w-3.5 h-3.5" />}
            {copied === `url-${trigger.id}` ? t('Copied') : t('Copy')}
          </button>
        </div>
        <div className="flex items-center gap-2 p-2 bg-hover border border-border rounded-lg">
          <span className="text-[10px] text-tertiary shrink-0 uppercase tracking-wider">{t('Token')}</span>
          <code className="flex-1 text-[11px] font-mono text-ink truncate">
            {revealed ? trigger.token : maskedToken}
          </code>
          <button
            onClick={() => setRevealed(v => !v)}
            className="flex items-center gap-1.5 px-2 py-0.5 text-[11px] text-secondary hover:text-ink transition-colors shrink-0"
            title={revealed ? t('Hide token') : t('Reveal token')}
          >
            {revealed ? <EyeSlashIcon className="w-3.5 h-3.5" /> : <EyeIcon className="w-3.5 h-3.5" />}
            {revealed ? t('Hide') : t('Reveal')}
          </button>
          <button
            onClick={() => copy(trigger.token, `tok-${trigger.id}`)}
            className="flex items-center gap-1.5 px-2 py-0.5 text-[11px] text-secondary hover:text-ink transition-colors shrink-0"
          >
            {copied === `tok-${trigger.id}` ? <CheckIcon className="w-3.5 h-3.5" /> : <ClipboardDocumentIcon className="w-3.5 h-3.5" />}
            {copied === `tok-${trigger.id}` ? t('Copied') : t('Copy')}
          </button>
        </div>
        {trigger.has_secret && (
          <p className="text-[11px] text-tertiary leading-relaxed">
            {t('Each request is signed with a shared secret so this endpoint only accepts calls from your system. The secret is set in the automation builder and is never shown here.')}
          </p>
        )}
      </div>
    </div>
  );
};

const WebhooksSection: React.FC = () => {
  const { t } = useTranslation();
  const { data, loading, error, refresh } = useQuery<WebhookTriggerWithAutomation[]>(
    Q.key('registry-inbound-webhooks'),
    listInboundWebhooks,
    { pollInterval: 30000 },
  );

  const items = data || [];
  const initialLoading = loading && !data;
  const loadFailed = !data && !loading && Boolean(error);

  if (initialLoading) return <SectionLoading label={t('Loading incoming webhooks…')} />;
  if (loadFailed) return <SectionError message={error} onRetry={refresh} />;

  if (items.length === 0) {
    return (
      <div className="bg-surface border border-ink/20 rounded-xl shadow-sm">
        <EmptyState
          icon={BoltIcon}
          title={t('No incoming webhooks')}
          body={
            <Trans>
              An incoming webhook is a <code className="bg-zinc-100 px-1 py-0.5 rounded text-[10px]">Webhook received</code> trigger
              block in an automation. When an external system POSTs to its URL, the automation runs. Add
              a <span className="text-secondary font-medium">Webhook received</span> trigger in the automation builder to create one.
            </Trans>
          }
          action={
            <Link
              to="/automations/new"
              className="inline-flex items-center gap-1.5 px-4 py-2 bg-accent-strong text-accent-on text-sm font-medium rounded-lg hover:bg-accent-strong/90 transition-colors"
            >
              <PlusIcon className="w-4 h-4" />
              {t('New automation')}
            </Link>
          }
        />
      </div>
    );
  }

  return (
    <div>
      <SectionHeading
        title={t('Incoming webhooks')}
        meta={`${items.length}`}
        description={t('External systems POST to these URLs to trigger an automation. Configured in the automation builder.')}
      />
      <div className="bg-surface border border-ink/20 rounded-xl overflow-hidden divide-y divide-border shadow-sm">
        {items.map(item => (
          <WebhookRow key={item.trigger.id} item={item} />
        ))}
      </div>
      <p className="text-[11px] text-tertiary mt-2 leading-relaxed">
        {t("Webhooks are configured in the automation builder (URL, secret, payload mapping). Per-delivery logs aren't retained — the count above reflects total deliveries. To send a test call, POST any JSON body to the URL above.")}
      </p>
    </div>
  );
};

// ────────────────────────────────────────────────────────────────────────────
// Page shell
// ────────────────────────────────────────────────────────────────────────────

export const EndpointsPage: React.FC = () => {
  const { t } = useTranslation();
  useRequireAuth();
  useDocumentTitle(t('Endpoints'));
  const navigate = useNavigate();
  const [section, setSection] = useState<SectionId>('rest');

  return (
    <>
      <div className="flex flex-col h-full">
        {/* Toolbar */}
        <div className="flex items-center gap-3 h-12 px-4 sm:px-6 border-b border-border shrink-0">
          <CircleStackIcon className="w-4 h-4 text-tertiary shrink-0" />
          <span className="text-[13px] font-semibold text-ink shrink-0">{t('Endpoints')}</span>

          {/* Segmented control — needs @pair/stage of room beside the title before the
              mobile stacked control below takes over. */}
          <div data-tour="endpoints-sections" className="hidden @pair/stage:flex items-center gap-0.5 ml-2">
            {SECTIONS.map(s => (
              <button
                key={s.id}
                onClick={() => setSection(s.id)}
                className={clsx(
                  'flex items-center gap-1.5 px-2.5 py-1 text-[12px] font-medium rounded-md transition-colors',
                  section === s.id ? 'bg-surface text-ink shadow-sm' : 'text-tertiary hover:text-secondary hover:bg-surface/60',
                )}
              >
                <s.icon className="w-3.5 h-3.5" />
                {t(s.label)}
              </button>
            ))}
          </div>

          <div className="flex-1" />

          <button
            onClick={() => navigate('/workflows')}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-accent-strong text-accent-on text-[12px] font-medium rounded-lg hover:bg-accent-strong/90 transition-colors shrink-0"
          >
            <PlusIcon className="w-3.5 h-3.5" />
            {t('Publish a workflow')}
          </button>
        </div>

        {/* Mobile segmented control */}
        <div className="@pair/stage:hidden flex items-center gap-0.5 px-4 py-2 border-b border-border">
          {SECTIONS.map(s => (
            <button
              key={s.id}
              onClick={() => setSection(s.id)}
              className={clsx(
                'flex-1 flex items-center justify-center gap-1.5 px-2 py-1.5 text-[12px] font-medium rounded-md transition-colors',
                section === s.id ? 'bg-surface text-ink shadow-sm' : 'text-tertiary',
              )}
            >
              <s.icon className="w-3.5 h-3.5" />
              {t(s.label)}
            </button>
          ))}
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
            {/* Main + a real sidebar (how-it-works + quick links) — both need to stay
                comfortable, so this waits for @split/stage rather than pair. */}
            <div className="grid grid-cols-1 @split/stage:grid-cols-4 gap-x-8 gap-y-8 items-start">
              {/* MAIN — the published surface for this section */}
              <div className="@split/stage:col-span-3 min-w-0">
                {section === 'rest' && <RestSection />}
                {section === 'mcp' && <McpSection />}
                {section === 'webhooks' && <WebhooksSection />}
              </div>

              {/* SIDEBAR — how this surface works + quick links */}
              <aside className="@split/stage:col-span-1 min-w-0 space-y-8">
                <div>
                  <h2 className="text-[13px] font-semibold text-ink mb-2">
                    {section === 'rest' && t('How REST endpoints work')}
                    {section === 'mcp' && t('How MCP tools work')}
                    {section === 'webhooks' && t('How webhooks work')}
                  </h2>
                  <div className="rounded-xl border border-ink/20 bg-surface p-4 text-[12px] text-secondary leading-relaxed shadow-sm">
                    {section === 'rest' && t('The ways other systems can call your workflows over HTTP — the endpoints you publish and the branded domains you serve them under. Keys to authenticate calls live in the API Keys tab.')}
                    {section === 'mcp' && t('Workflows you have exposed as tools for AI assistants (Claude, and any MCP client) to call directly with a wt_* API key.')}
                    {section === 'webhooks' && t('URLs that an outside system can POST to in order to kick off one of your automations.')}
                  </div>
                </div>

                <div>
                  <h2 className="text-[13px] font-semibold text-ink mb-2">{t('Quick links')}</h2>
                  <div className="rounded-xl border border-ink/20 bg-surface divide-y divide-border overflow-hidden shadow-sm">
                    <button onClick={() => navigate('/workflows')} className="w-full flex items-center gap-2.5 px-4 py-2.5 text-left hover:bg-chrome transition-colors">
                      <PlusIcon className="w-4 h-4 text-tertiary shrink-0" />
                      <span className="text-[12px] text-ink">{t('Publish a workflow')}</span>
                    </button>
                    <button onClick={() => navigate('/developers')} className="w-full flex items-center gap-2.5 px-4 py-2.5 text-left hover:bg-chrome transition-colors">
                      <KeyIcon className="w-4 h-4 text-tertiary shrink-0" />
                      <span className="text-[12px] text-ink">{t('Manage API keys')}</span>
                    </button>
                    <a href={docsUrl('api')} {...DOCS_LINK_PROPS} className="w-full flex items-center gap-2.5 px-4 py-2.5 text-left hover:bg-chrome transition-colors">
                      <CodeBracketIcon className="w-4 h-4 text-tertiary shrink-0" />
                      <span className="text-[12px] text-ink">{t('Developer docs')}</span>
                      <ArrowTopRightOnSquareIcon className="w-3.5 h-3.5 text-tertiary shrink-0 ml-auto" />
                    </a>
                  </div>
                </div>
              </aside>
            </div>
          </div>
        </div>
      </div>
    </>
  );
};

export default EndpointsPage;
