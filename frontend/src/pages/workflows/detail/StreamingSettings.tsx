import React from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import { automationApi } from '../../../api/endpoints';
import { NumberInput as NumberInputPrimitive, Switch as UiSwitch } from '../../../components/ui';
import { Section } from './Section';

/**
 * The four leaf controls below live at MODULE scope, not inside
 * StreamingSettings.
 *
 * They used to be declared in the component body, which meant every render
 * produced brand-new component *types*. React compares types by identity, so
 * it cannot see those as the same component — it unmounts the entire subtree
 * and mounts a fresh one on each render. For `TextInput` that is not a
 * performance footnote: the DOM node is replaced mid-keystroke, so the field
 * loses focus after every character typed. (`react-hooks/static-components`.)
 *
 * They need `markDirty`, which is parent state, so it arrives through a
 * context rather than as a prop — that keeps all ~40 call sites unchanged
 * while the components themselves stay stable across renders.
 */
const MarkDirtyContext = React.createContext<() => void>(() => {});

const Toggle: React.FC<{ checked: boolean; onChange: (v: boolean) => void }> = ({ checked, onChange }) => {
  const markDirty = React.useContext(MarkDirtyContext);
  return <UiSwitch checked={checked} size="sm" onChange={(next) => { onChange(next); markDirty(); }} />;
};

const SettingRow: React.FC<{ label: string; desc?: string; children: React.ReactNode }> = ({ label, desc, children }) => (
  <div className="flex items-center justify-between py-2.5 gap-4">
    <div className="min-w-0">
      <div className="text-[13px] text-ink">{label}</div>
      {desc && <div className="text-[11px] text-tertiary mt-0.5">{desc}</div>}
    </div>
    <div className="shrink-0">{children}</div>
  </div>
);

const TextInput: React.FC<{ value: string; onChange: (v: string) => void; placeholder?: string; className?: string }> = ({ value, onChange, placeholder, className }) => {
  const markDirty = React.useContext(MarkDirtyContext);
  return (
    <input
      type="text" value={value} placeholder={placeholder}
      onChange={e => { onChange(e.target.value); markDirty(); }}
      className={clsx('px-2.5 py-1 text-[12px] font-mono bg-hover border border-border rounded-lg text-ink focus:outline-none focus:ring-1 focus:ring-zinc-300', className || 'w-36')}
    />
  );
};

const NumberInput: React.FC<{ value: number; onChange: (v: number) => void; min?: number; max?: number; suffix?: string }> = ({ value, onChange, min, max, suffix }) => {
  const markDirty = React.useContext(MarkDirtyContext);
  return (
    <NumberInputPrimitive
      value={value}
      min={min}
      max={max}
      size="sm"
      hideSteppers
      suffix={suffix}
      onChange={v => { onChange(v ?? 0); markDirty(); }}
      wrapperClassName={suffix ? 'w-32' : 'w-20'}
      className="font-mono text-right"
    />
  );
};

/**
 * Streaming workflow configuration, grouped into three explained cards:
 * OpenAI-compatible model / Conversations / Session. One shared Save bar.
 */
export const StreamingSettings: React.FC<{ workflowId: number; workflow: any; streamingConfig: any; onUpdate: () => void }> = ({ workflowId, workflow, streamingConfig, onUpdate }) => {
  const { t } = useTranslation();
  const [saving, setSaving] = React.useState(false);
  const [dirty, setDirty] = React.useState(false);

  // Local state for all settings
  const [multiConv, setMultiConv] = React.useState<boolean>(streamingConfig.multi_conversation ?? false);
  const [contextMode, setContextMode] = React.useState<string>(streamingConfig.context_mode || 'shared');
  const [maxSessions, setMaxSessions] = React.useState<number>(streamingConfig.max_sessions ?? 1);
  const [maxThreads, setMaxThreads] = React.useState<number>(streamingConfig.max_concurrent_threads ?? 3);
  const [maxDuration, setMaxDuration] = React.useState<number>(streamingConfig.max_duration_seconds ?? 3600);
  const [sessionPersistence, setSessionPersistence] = React.useState<boolean>(workflow.session_persistence ?? false);
  const [sessionTtl, setSessionTtl] = React.useState<number>(workflow.session_ttl_seconds ?? 0);
  const [headless, setHeadless] = React.useState<boolean>(workflow.headless ?? true);
  const [distributeIps, setDistributeIps] = React.useState<boolean>(streamingConfig.distribute_ips ?? false);
  const [oaiEnabled, setOaiEnabled] = React.useState<boolean>(streamingConfig.openai_compat?.enabled ?? false);
  const [oaiModel, setOaiModel] = React.useState<string>(streamingConfig.openai_compat?.model?.name || streamingConfig.openai_compat?.model_name || 'streaming');
  const [oaiHandler, setOaiHandler] = React.useState<string>(streamingConfig.openai_compat?.default_handler || 'chat');
  const [oaiResponseField, setOaiResponseField] = React.useState<string>(streamingConfig.openai_compat?.response_field || 'content');
  const [modelDesc, setModelDesc] = React.useState<string>(streamingConfig.openai_compat?.model?.description || '');
  const [modelVersion, setModelVersion] = React.useState<string>(streamingConfig.openai_compat?.model?.version || '1.0');
  const [capVision, setCapVision] = React.useState<boolean>(streamingConfig.openai_compat?.model?.capabilities?.vision ?? false);
  const [capTools, setCapTools] = React.useState<boolean>(streamingConfig.openai_compat?.model?.capabilities?.tools ?? true);
  const [capStreaming, setCapStreaming] = React.useState<boolean>(streamingConfig.openai_compat?.model?.capabilities?.streaming ?? true);
  const [capFileUpload, setCapFileUpload] = React.useState<boolean>(streamingConfig.openai_compat?.model?.capabilities?.file_upload ?? false);
  const [contextWindow, setContextWindow] = React.useState<number>(streamingConfig.openai_compat?.model?.limits?.context_window ?? 128000);
  const [maxOutputTokens, setMaxOutputTokens] = React.useState<number>(streamingConfig.openai_compat?.model?.limits?.max_output_tokens ?? 8192);

  const markDirty = React.useCallback(() => setDirty(true), []);

  const handleSave = async () => {
    setSaving(true);
    try {
      const newStreamingConfig = {
        ...streamingConfig,
        max_sessions: maxSessions,
        distribute_ips: distributeIps,
        multi_conversation: multiConv,
        context_mode: contextMode,
        max_concurrent_threads: maxThreads,
        max_duration_seconds: maxDuration,
        openai_compat: {
          enabled: oaiEnabled,
          default_handler: oaiHandler,
          response_field: oaiResponseField,
          model: {
            name: oaiModel,
            description: modelDesc,
            version: modelVersion,
            capabilities: {
              vision: capVision,
              tools: capTools,
              streaming: capStreaming,
              file_upload: capFileUpload,
            },
            limits: {
              context_window: contextWindow,
              max_output_tokens: maxOutputTokens,
            },
          },
        },
      };
      await automationApi.updateWorkflow(workflowId, {
        streaming_config: newStreamingConfig,
        session_persistence: sessionPersistence,
        session_ttl_seconds: sessionTtl || null,
        headless,
      } as any);
      toast.success(t('Settings saved'));
      setDirty(false);
      onUpdate();
    } catch { toast.error(t('Failed to save settings')); }
    finally { setSaving(false); }
  };

  return (
    <MarkDirtyContext.Provider value={markDirty}>
    <div className="space-y-6">
      {/* Single sticky save bar shared by all three cards */}
      {dirty && (
        <div className="sticky top-0 z-10 px-3 py-2 bg-surface/95 backdrop-blur border border-ink/20 shadow-sm rounded-xl flex items-center justify-between">
          <span className="text-[12px] text-secondary">{t('Unsaved settings changes')}</span>
          <div className="flex items-center gap-2">
            <button onClick={() => { setDirty(false); onUpdate(); }} className="px-2.5 py-1 text-[11px] text-secondary hover:text-ink rounded-lg hover:bg-chrome transition-colors">
              {t('Discard')}
            </button>
            <button onClick={handleSave} disabled={saving} className="px-2.5 py-1 text-[11px] font-semibold shadow-sm bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 transition-all disabled:opacity-50">
              {saving ? t('Saving...') : t('Save')}
            </button>
          </div>
        </div>
      )}

      {/* OpenAI-compatible model */}
      <Section
        title={t('OpenAI-compatible model')}
        description={t('Expose the live session as a drop-in OpenAI chat completions API — endpoints and snippets live in the Connect tab.')}
      >
        <div className="bg-surface border border-ink/20 shadow-sm rounded-xl overflow-hidden px-4 py-1">
          <SettingRow label={t('OpenAI-compatible endpoint')} desc={t('Serve /v1/chat/completions and /v1/models for this workflow')}>
            <Toggle checked={oaiEnabled} onChange={setOaiEnabled} />
          </SettingRow>
          {oaiEnabled && (
            <div className="pb-3 space-y-0 pl-0">
              {/* Handler settings */}
              <div className="pb-2 mb-1 border-b border-border">
                <p className="text-[10px] font-semibold text-tertiary uppercase tracking-wider mt-2 mb-1">{t('Handler')}</p>
                <SettingRow label={t('Default handler')} desc={t('Handler invoked for each chat message')}>
                  <TextInput value={oaiHandler} onChange={setOaiHandler} placeholder="chat" />
                </SettingRow>
                <SettingRow label={t('Response field')} desc={t('Field to extract from handler response')}>
                  <TextInput value={oaiResponseField} onChange={setOaiResponseField} placeholder="content" />
                </SettingRow>
              </div>

              {/* Model identity */}
              <div className="pb-2 mb-1 border-b border-border">
                <p className="text-[10px] font-semibold text-tertiary uppercase tracking-wider mt-2 mb-1">{t('Model Card')}</p>
                <SettingRow label={t('Model name')} desc={t('Model ID returned by /v1/models')}>
                  <TextInput value={oaiModel} onChange={setOaiModel} placeholder="streaming" />
                </SettingRow>
                <SettingRow label={t('Description')} desc={t('Human-readable model description')}>
                  <TextInput value={modelDesc} onChange={setModelDesc} placeholder={t('A streaming model')} className="w-52" />
                </SettingRow>
                <SettingRow label={t('Version')} desc={t('Model version string')}>
                  <TextInput value={modelVersion} onChange={setModelVersion} placeholder="1.0" className="w-20" />
                </SettingRow>
              </div>

              {/* Capabilities */}
              <div className="pb-2 mb-1 border-b border-border">
                <p className="text-[10px] font-semibold text-tertiary uppercase tracking-wider mt-2 mb-1">{t('Capabilities')}</p>
                <SettingRow label={t('Vision')} desc={t('Model accepts image inputs')}>
                  <Toggle checked={capVision} onChange={setCapVision} />
                </SettingRow>
                <SettingRow label={t('Tool calling')} desc={t('Model supports function/tool calls')}>
                  <Toggle checked={capTools} onChange={setCapTools} />
                </SettingRow>
                <SettingRow label={t('Streaming')} desc={t('Model supports streaming responses')}>
                  <Toggle checked={capStreaming} onChange={setCapStreaming} />
                </SettingRow>
                <SettingRow label={t('File upload')} desc={t('Model accepts file attachments')}>
                  <Toggle checked={capFileUpload} onChange={setCapFileUpload} />
                </SettingRow>
              </div>

              {/* Limits */}
              <div className="pb-1">
                <p className="text-[10px] font-semibold text-tertiary uppercase tracking-wider mt-2 mb-1">{t('Limits')}</p>
                <SettingRow label={t('Context window')} desc={t('Maximum input tokens')}>
                  <NumberInput value={contextWindow} onChange={setContextWindow} min={1024} max={2000000} suffix={t('tokens')} />
                </SettingRow>
                <SettingRow label={t('Max output tokens')} desc={t('Maximum tokens per response')}>
                  <NumberInput value={maxOutputTokens} onChange={setMaxOutputTokens} min={256} max={128000} suffix={t('tokens')} />
                </SettingRow>
              </div>
            </div>
          )}
        </div>
      </Section>

      {/* Conversations */}
      <Section
        title={t('Conversations')}
        description={t('How incoming chat threads map onto browser tabs in the session.')}
      >
        <div className="bg-surface border border-ink/20 shadow-sm rounded-xl overflow-hidden px-4 py-1">
          <SettingRow
            label={t('Multiple conversations')}
            desc={t('Run separate conversations in their own tabs. When off, every message reuses the same tab.')}
          >
            <Toggle checked={multiConv} onChange={setMultiConv} />
          </SettingRow>
          {multiConv && (
            <>
              <SettingRow label={t('Context mode')} desc={t('How new conversations share browser state')}>
                <div className="flex gap-1">
                  {(['shared', 'isolated'] as const).map(mode => (
                    <button
                      key={mode}
                      onClick={() => { setContextMode(mode); markDirty(); }}
                      className={clsx(
                        'px-3 py-1 text-[11px] rounded-lg border transition-all',
                        contextMode === mode
                          ? 'bg-hover text-ink border-border'
                          : 'bg-surface text-secondary border-border hover:border-zinc-300',
                      )}
                    >
                      {mode === 'shared' ? t('Shared') : t('Isolated')}
                    </button>
                  ))}
                </div>
              </SettingRow>
              <div className="pb-2 -mt-1">
                <p className="text-[11px] text-tertiary leading-relaxed">
                  {contextMode === 'shared'
                    ? t('Conversations open new tabs in the same browser — shared cookies, login state, and session data.')
                    : t('Each conversation gets a fresh browser context — independent cookies and state. Login cookies are copied from the main session.')}
                </p>
              </div>
              {/* Floor only — how many tabs the operator's own machine can carry is
                  their call, not ours. */}
              <SettingRow label={t('Max concurrent conversations')} desc={t('Maximum parallel browser tabs/contexts (extras reuse an existing tab)')}>
                <NumberInput value={maxThreads} onChange={setMaxThreads} min={1} />
              </SettingRow>
            </>
          )}
        </div>
      </Section>

      {/* Session */}
      <Section
        title={t('Session')}
        description={t('Browser lifecycle: parallelism, timeouts, and whether login state survives between sessions.')}
      >
        <div className="bg-surface border border-ink/20 shadow-sm rounded-xl overflow-hidden px-4 py-1">
          <SettingRow label={t('Max agents')} desc={t('Run on multiple agents in parallel — API calls auto-route with conversation affinity')}>
            <NumberInput value={maxSessions} onChange={setMaxSessions} min={1} max={10} />
          </SettingRow>
          {maxSessions > 1 && (
            <>
              <div className="pb-2 -mt-1">
                <p className="text-[11px] text-tertiary leading-relaxed">
                  {t('Start up to {{n}} sessions across agents. Each conversation sticks to its assigned session. New conversations are distributed round-robin.', { n: maxSessions })}
                </p>
              </div>
              <SettingRow label={t('IP distribution')} desc={t('Each session on a different agent for distinct IPs')}>
                <Toggle checked={distributeIps} onChange={setDistributeIps} />
              </SettingRow>
              {distributeIps && (
                <div className="pb-2 -mt-1">
                  <p className="text-[11px] text-tertiary leading-relaxed">
                    {t('Sessions are spread across agents — each gets a different IP. Requires at least {{n}} connected agents.', { n: maxSessions })}
                  </p>
                </div>
              )}
              {!distributeIps && maxSessions > 1 && (
                <div className="pb-2 -mt-1">
                  <p className="text-[11px] text-tertiary leading-relaxed">
                    {t('Sessions are packed on the same agent until full, then overflow to the next.')}
                  </p>
                </div>
              )}
            </>
          )}
          <SettingRow label={t('Max session duration')} desc={t('Hard timeout for the streaming session')}>
            <NumberInput value={maxDuration} onChange={setMaxDuration} min={60} max={86400} suffix={t('sec')} />
          </SettingRow>
          <SettingRow label={t('Persist login state')} desc={t('Save cookies/localStorage between sessions')}>
            <Toggle checked={sessionPersistence} onChange={setSessionPersistence} />
          </SettingRow>
          {sessionPersistence && (
            <SettingRow label={t('Session TTL')} desc={t('Override cookie expiry (0 = use cookie expiry)')}>
              <NumberInput value={sessionTtl} onChange={setSessionTtl} min={0} max={604800} suffix={t('sec')} />
            </SettingRow>
          )}
          <SettingRow label={t('Headless browser')} desc={t('Run without visible browser window')}>
            <Toggle checked={headless} onChange={setHeadless} />
          </SettingRow>
        </div>
      </Section>
    </div>
    </MarkDirtyContext.Provider>
  );
};
