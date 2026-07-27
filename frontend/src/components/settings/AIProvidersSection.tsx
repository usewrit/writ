import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import { Checkbox, NumberInput, Select } from '../ui';
import { SectionHead } from '../common/SectionHead';
import { apiErrorMessage } from '../../api/client';
import {
  listAIProviders,
  createAIProvider,
  updateAIProvider,
  deleteAIProvider,
  testAIProvider,
  getAILimits,
  updateAILimits,
  type AIProvider,
  type AILimits,
  type AILimitsResponse,
} from '../../api/settings';

// Provider set — union of every LLM lane the coordinator's gateway understands.
const PROVIDER_OPTIONS: { value: string; label: string }[] = [
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'openai', label: 'OpenAI (Chat Completions)' },
  { value: 'openai_responses', label: 'OpenAI (Responses API)' },
  { value: 'openrouter', label: 'OpenRouter' },
  { value: 'local', label: 'Local LLM (Ollama / OpenAI-compat)' },
];

// ─────────────────────────────────────────────────────────────────────────────
// AI provider list + BYO-key form. Keys follow the write-only contract: never
// returned (only a mask), and on edit an empty API-key field leaves the stored
// key unchanged.
// ─────────────────────────────────────────────────────────────────────────────
const ProvidersPanel: React.FC = () => {
  const { t } = useTranslation();
  const [providers, setProviders] = useState<AIProvider[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState<number | null>(null);

  const [form, setForm] = useState({
    provider: 'anthropic',
    api_key: '',
    base_url: '',
    model: '',
    is_active: true,
    priority: 0,
  });

  const load = () => {
    listAIProviders()
      .then(setProviders)
      .catch(() => toast.error(t('Failed to load providers')))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, []);

  const resetForm = () => {
    setForm({ provider: 'anthropic', api_key: '', base_url: '', model: '', is_active: true, priority: 0 });
    setEditingId(null);
    setShowForm(false);
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      // Empty api_key on edit = keep the current key (write-only 3-way).
      const payload = {
        provider: form.provider,
        base_url: form.base_url || undefined,
        model: form.model || undefined,
        is_active: form.is_active,
        priority: form.priority,
        ...(form.api_key ? { api_key: form.api_key } : {}),
      };
      if (editingId) {
        await updateAIProvider(editingId, payload);
        toast.success(t('Provider updated'));
      } else {
        await createAIProvider(payload);
        toast.success(t('Provider added'));
      }
      resetForm();
      load();
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to save')));
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: number) => {
    if (!window.confirm(t('Remove this AI provider?'))) return;
    try {
      await deleteAIProvider(id);
      toast.success(t('Provider removed'));
      load();
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Failed to delete')));
    }
  };

  const handleTest = async (id: number) => {
    setTesting(id);
    try {
      const r = await testAIProvider(id);
      if (r.status === 'ok') {
        toast.success(t('Connected — {{model}}: "{{response}}"', { model: r.model, response: r.response }));
      } else {
        toast.error(t('Test failed: {{error}}', { error: r.error }));
      }
    } catch (e) {
      toast.error(apiErrorMessage(e, t('Test failed')));
    } finally {
      setTesting(null);
    }
  };

  const startEdit = (p: AIProvider) => {
    setForm({
      provider: p.provider,
      api_key: '',
      base_url: p.base_url || '',
      model: p.model || '',
      is_active: p.is_active,
      priority: p.priority,
    });
    setEditingId(p.id);
    setShowForm(true);
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-base font-semibold text-ink tracking-tight">{t('Providers')}</h3>
          <p className="text-xs text-tertiary mt-0.5">
            {t('Bring your own keys. Lower priority = preferred; the gateway falls back down the list.')}
          </p>
        </div>
        {!showForm && (
          <button
            onClick={() => {
              resetForm();
              setShowForm(true);
            }}
            className="px-3 py-1.5 text-xs font-medium text-accent-on bg-accent-strong rounded-lg hover:bg-accent-strong/90 transition-colors"
          >
            {t('Add provider')}
          </button>
        )}
      </div>

      {loading ? (
        <div className="text-sm text-secondary">{t('Loading...')}</div>
      ) : (
        <>
          {providers.length === 0 && !showForm && (
            <div className="text-center py-8 text-sm text-secondary border border-dashed border-border rounded-xl">
              {t('No AI providers configured. Add one to enable AI features.')}
            </div>
          )}

          <div className="space-y-2">
            {providers.map((p) => (
              <div
                key={p.id}
                className="flex items-center gap-3 px-4 py-3 border-t border-border"
              >
                <div className={clsx('w-2 h-2 rounded-full', p.is_active ? 'bg-ink' : 'bg-zinc-300')} />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-ink capitalize">{p.provider}</span>
                    <span className="text-[10px] text-tertiary bg-canvas px-1.5 py-0.5 rounded">P{p.priority}</span>
                    {p.model && <span className="text-[10px] text-tertiary truncate">{p.model}</span>}
                  </div>
                  {p.api_key_masked && (
                    <span className="text-[10px] text-tertiary font-mono">{p.api_key_masked}</span>
                  )}
                </div>
                <div className="flex items-center gap-1.5">
                  <button
                    onClick={() => handleTest(p.id)}
                    disabled={testing === p.id}
                    className="px-2 py-1 text-[10px] font-medium text-ink bg-canvas border border-border rounded-lg hover:bg-chrome transition-colors"
                  >
                    {testing === p.id ? t('Testing...') : t('Test')}
                  </button>
                  <button
                    onClick={() => startEdit(p)}
                    className="px-2 py-1 text-[10px] font-medium text-ink bg-canvas border border-border rounded-lg hover:bg-chrome transition-colors"
                  >
                    {t('Edit')}
                  </button>
                  <button
                    onClick={() => handleDelete(p.id)}
                    className="px-2 py-1 text-[10px] font-medium text-ink bg-canvas border border-border rounded-lg hover:bg-chrome transition-colors"
                  >
                    {t('Remove')}
                  </button>
                </div>
              </div>
            ))}
          </div>

          {showForm && (
            <div className="border border-border rounded-xl p-4 space-y-3 bg-canvas">
              <h4 className="text-xs font-semibold text-ink">
                {editingId ? t('Edit provider') : t('Add provider')}
              </h4>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-[10px] font-medium text-secondary mb-1 block">{t('Provider')}</label>
                  <Select
                    value={form.provider}
                    onChange={(v) => setForm((f) => ({ ...f, provider: String(v) }))}
                    options={PROVIDER_OPTIONS.map((o) => ({ ...o, label: t(o.label) }))}
                  />
                </div>
                <div>
                  <label className="text-[10px] font-medium text-secondary mb-1 block">{t('Priority')}</label>
                  <NumberInput value={form.priority} onChange={(v) => setForm((f) => ({ ...f, priority: v ?? 0 }))} />
                </div>
              </div>

              <div>
                <label className="text-[10px] font-medium text-secondary mb-1 block">
                  {t('API Key')} {editingId && t('(leave blank to keep current)')}
                </label>
                <input
                  type="password"
                  value={form.api_key}
                  onChange={(e) => setForm((f) => ({ ...f, api_key: e.target.value }))}
                  placeholder="sk-..."
                  className="w-full px-3 py-2 text-sm border border-border rounded-lg font-mono bg-surface text-ink"
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-[10px] font-medium text-secondary mb-1 block">{t('Base URL (optional)')}</label>
                  <input
                    value={form.base_url}
                    onChange={(e) => setForm((f) => ({ ...f, base_url: e.target.value }))}
                    placeholder={form.provider === 'local' ? 'http://localhost:11434' : 'https://api.openai.com/v1'}
                    className="w-full px-3 py-2 text-sm border border-border rounded-lg bg-surface text-ink"
                  />
                </div>
                <div>
                  <label className="text-[10px] font-medium text-secondary mb-1 block">
                    {t('Model override (optional)')}
                  </label>
                  <input
                    value={form.model}
                    onChange={(e) => setForm((f) => ({ ...f, model: e.target.value }))}
                    placeholder="claude-sonnet-4-20250514"
                    className="w-full px-3 py-2 text-sm border border-border rounded-lg bg-surface text-ink"
                  />
                </div>
              </div>

              <Checkbox
                checked={form.is_active}
                onChange={(e) => setForm((f) => ({ ...f, is_active: e.target.checked }))}
                label={t('Active')}
              />

              <div className="flex gap-2 pt-1">
                <button
                  onClick={handleSave}
                  disabled={saving}
                  className="px-4 py-2 text-sm font-medium text-accent-on bg-accent-strong rounded-lg hover:bg-accent-strong/90 transition-colors disabled:opacity-60"
                >
                  {saving ? t('Saving...') : editingId ? t('Update') : t('Add')}
                </button>
                <button
                  onClick={resetForm}
                  className="px-4 py-2 text-sm font-medium text-ink bg-surface border border-border rounded-lg hover:bg-chrome transition-colors"
                >
                  {t('Cancel')}
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
};

// ─────────────────────────────────────────────────────────────────────────────
// Output-token ceilings — per-bucket caps that only LOWER usage. Blank = use the
// built-in default. Saved on blur / Enter.
// ─────────────────────────────────────────────────────────────────────────────
type Bucket = keyof AILimits;
const BUCKET_ORDER: Bucket[] = ['assist', 'agent', 'optimize', 'repair'];

const LimitsPanel: React.FC = () => {
  const { t } = useTranslation();
  const [data, setData] = useState<AILimitsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [values, setValues] = useState<Record<Bucket, number | null>>({
    assist: null,
    agent: null,
    optimize: null,
    repair: null,
  });

  // Fetch + apply. Writes ONLY from the promise continuations, so the mount
  // effect below can call it without a synchronous setState — the state already
  // starts out loading. `load` adds the explicit reset the Retry button needs.
  const fetchLimits = () =>
    getAILimits()
      .then((res) => {
        setData(res);
        setError(false);
        setValues({
          assist: res.limits.assist ?? null,
          agent: res.limits.agent ?? null,
          optimize: res.limits.optimize ?? null,
          repair: res.limits.repair ?? null,
        });
      })
      .catch(() => setError(true))
      .finally(() => setLoading(false));

  const load = () => {
    setLoading(true);
    setError(false);
    void fetchLimits();
  };

  useEffect(() => {
    void fetchLimits();
  }, []);

  const BUCKETS: Record<Bucket, { label: string; help: string }> = {
    assist: { label: t('Assist'), help: t('Chat, extract, find selectors') },
    agent: { label: t('Agent & build'), help: t('Build extractor, AI agent loop, streaming script, automations') },
    optimize: { label: t('Optimize'), help: t('Optimize workflow') },
    repair: { label: t('AI repair'), help: t('AI auto-repair (streaming script / function repair)') },
  };

  const save = (bucket: Bucket, raw: number | null) => {
    if (!data) return;
    const next: number | null = raw != null && Number.isFinite(raw) && raw > 0 ? raw : null;
    const current = data.limits[bucket];
    if (next === current) return;
    updateAILimits({ [bucket]: next })
      .then((res) => {
        setData(res);
        setValues((v) => ({ ...v, [bucket]: res.limits[bucket] ?? null }));
        toast.success(t('AI limits updated'));
      })
      .catch(() => {
        toast.error(t('Failed to update AI limits'));
        setValues((v) => ({ ...v, [bucket]: current ?? null }));
      });
  };

  if (loading) return <div className="text-sm text-secondary">{t('Loading...')}</div>;
  if (error || !data) {
    return (
      <div className="text-sm text-secondary">
        {t('Failed to load AI limits.')}{' '}
        <button onClick={load} className="text-ink underline hover:no-underline">
          {t('Retry')}
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {BUCKET_ORDER.map((bucket) => {
        const def = data.defaults[bucket];
        return (
          <div key={bucket}>
            <label className="text-xs text-secondary block mb-1">
              {BUCKETS[bucket].label} — {BUCKETS[bucket].help}
            </label>
            <NumberInput
              size="sm"
              min={0}
              step={1}
              value={values[bucket]}
              placeholder={def != null ? String(def) : undefined}
              onChange={(n) => setValues((v) => ({ ...v, [bucket]: n }))}
              onBlur={() => save(bucket, values[bucket])}
              onKeyDown={(e) => {
                if (e.key === 'Enter') save(bucket, values[bucket]);
              }}
            />
            <p className="text-xs text-tertiary mt-1">
              {t('Built-in default: {{n}} tokens — blank = no extra cap', { n: def ?? 0 })}
            </p>
          </div>
        );
      })}
    </div>
  );
};

export const AIProvidersSection: React.FC = () => {
  const { t } = useTranslation();
  return (
    <div className="space-y-8">
      <section>
        <SectionHead
          title={t('AI Providers')}
          description={t('Configure the LLM providers that power AI features. Your keys are sealed and never leave the coordinator.')}
        />
        <div className="mt-3 border-t border-border pt-4">
          <ProvidersPanel />
        </div>
      </section>

      <section>
        <SectionHead
          title={t('Output-token limits')}
          description={t('Cap AI output tokens per feature to control cost. A cap only lowers usage — blank keeps the built-in default.')}
        />
        <div className="mt-3 border-t border-border pt-4">
          <LimitsPanel />
        </div>
      </section>
    </div>
  );
};

export default AIProvidersSection;
