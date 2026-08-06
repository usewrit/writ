import React from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import { ClipboardDocumentIcon, CheckIcon } from '@heroicons/react/24/outline';
import { Modal } from '../ui/Modal';
import { apiKeysApi } from '../../api/endpoints';
import {
  crawlApi,
  FRESHNESS_PRESETS,
  type CrawlDefinition,
  type CrawlView,
} from '../../api/crawl';

/**
 * "Call this crawl" — turn a crawl into a callable API, the way a workflow's
 * Connect tab does.
 *
 * A crawl row is one RUN, so it has no stable handle to hand out. This modal
 * SAVES the crawl's settings as a definition (stable slug), then shows the two
 * things that make it usable from outside: the endpoint, and a minted key.
 *
 * The freshness control is the point of the surface, not a detail. `max_age` is
 * what turns "run this crawl again" into "give me this crawl's data, and only
 * re-crawl if what you have is older than N" — so it leads the snippets and the
 * copy explains the tradeoff in money/time terms rather than cache jargon.
 */
export interface CrawlApiModalProps {
  isOpen: boolean;
  onClose: () => void;
  /** The crawl whose settings become the saved, callable definition. */
  crawl: CrawlView;
  /** An already-saved definition for this crawl, when the caller knows of one. */
  existing?: CrawlDefinition | null;
  /** Notify the parent so it can re-render its "callable" affordance. */
  onSaved?: (definition: CrawlDefinition) => void;
}

/** The scopes a key needs to call a saved crawl and read what it collected. */
const CRAWL_KEY_SCOPES = ['crawl:read', 'crawl:execute', 'workflows:read', 'datasets:read'];

const KEY_PLACEHOLDER = 'wt_YOUR_KEY';

export const CrawlApiModal: React.FC<CrawlApiModalProps> = ({
  isOpen,
  onClose,
  crawl,
  existing,
  onSaved,
}) => {
  const { t } = useTranslation();

  // Only what THIS modal saved lives in state. The parent's `existing` is
  // derived, not mirrored: it may still have been loading when the modal opened,
  // and copying a late-arriving prop into state through an effect means an extra
  // render pass showing the wrong panel — plus a stale value the moment the
  // parent refetches. `saved` wins when present because a save is newer than
  // anything the parent knew.
  const [saved, setSaved] = React.useState<CrawlDefinition | null>(null);
  const definition = saved ?? existing ?? null;
  const [saving, setSaving] = React.useState(false);
  const [maxAge, setMaxAge] = React.useState<number>(86400);
  const [mintedKey, setMintedKey] = React.useState<string | null>(null);
  const [minting, setMinting] = React.useState(false);

  const origin = typeof window !== 'undefined' ? window.location.origin : 'https://your-instance.com';
  const apiBase = `${origin}/api/crawl/definitions`;
  const slug = definition?.slug ?? 'your-saved-crawl';

  const copyText = (text: string) => {
    navigator.clipboard.writeText(text);
    toast.success(t('Copied'));
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      // from_crawl_id, not a client-side config: the crawl's status view does not
      // echo every knob it ran with (politeness, shard sizing, path filters), so
      // rebuilding the config here would silently substitute defaults and save a
      // crawl that behaves differently from this one. The server reads the row.
      const created = await crawlApi.saveDefinition({
        name: crawl.name || crawl.seed_url,
        default_max_age_seconds: maxAge > 0 ? maxAge : null,
        from_crawl_id: crawl.id,
      });
      setSaved(created);
      onSaved?.(created);
      toast.success(t('This crawl is now callable by API'));
    } catch {
      toast.error(t('Could not make this crawl callable'));
    } finally {
      setSaving(false);
    }
  };

  const handleMintKey = async () => {
    setMinting(true);
    try {
      const res: any = await apiKeysApi.create({
        label: definition?.name ? `${definition.name} — API` : t('Crawl API'),
        scopes: CRAWL_KEY_SCOPES,
      });
      const secret = res?.api_key || res?.data?.api_key || res?.key;
      if (!secret) throw new Error('no key in response');
      setMintedKey(secret);
      toast.success(t('API key created — copy it now, it won’t be shown again'));
    } catch {
      toast.error(t('Could not create an API key'));
    } finally {
      setMinting(false);
    }
  };

  const runSnippet =
    `curl -X POST "${apiBase}/${slug}/run" \\\n` +
    `  -H "Authorization: Bearer ${mintedKey ?? KEY_PLACEHOLDER}" \\\n` +
    `  -H "Content-Type: application/json" \\\n` +
    `  -d '{"max_age": ${maxAge}}'`;

  const dataSnippet =
    `curl "${apiBase}/${slug}/data?limit=50" \\\n` +
    `  -H "Authorization: Bearer ${mintedKey ?? KEY_PLACEHOLDER}"`;

  const freshSnippet =
    `curl -X POST "${apiBase}/${slug}/run" \\\n` +
    `  -H "Authorization: Bearer ${mintedKey ?? KEY_PLACEHOLDER}" \\\n` +
    `  -H "Cache-Control: no-cache"`;

  const chosenPreset = FRESHNESS_PRESETS.find((p) => p.seconds === maxAge);

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={t('Call this crawl')}
      subtitle={t('Turn this crawl into an API you can call with a key — and reuse its data instead of re-crawling.')}
      size="lg"
    >
      <div className="space-y-4">
        {/* 1 — freshness. First, because it is the decision that changes what the
            call costs, and every snippet below reflects it live. */}
        <div>
          <span className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wider text-tertiary">
            {t('How fresh does the data need to be?')}
          </span>
          <div className="flex flex-wrap gap-1.5">
            {FRESHNESS_PRESETS.map((preset) => (
              <button
                key={preset.seconds}
                type="button"
                onClick={() => setMaxAge(preset.seconds)}
                className={`rounded-lg border px-2.5 py-1 text-[12px] font-medium transition-colors ${
                  maxAge === preset.seconds
                    ? 'border-ink bg-ink text-white'
                    : 'border-border text-secondary hover:border-ink hover:text-ink'
                }`}
              >
                {t(preset.label)}
              </button>
            ))}
          </div>
          <p className="mt-1.5 text-[11px] leading-relaxed text-tertiary">
            {maxAge > 0
              ? t('If this crawl already finished within that window, the call returns the pages it collected — instantly, with nothing crawled and nothing metered. Older than that, it re-crawls with these exact settings.')
              : t('Every call re-crawls the site with these exact settings. Pick a window above to let callers reuse recent data instead.')}
          </p>
        </div>

        {/* 2 — the callable endpoint (or the button that creates one) */}
        {definition ? (
          <div>
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-[11px] font-semibold uppercase tracking-wider text-tertiary">
                {t('Run it')}
              </span>
              <button
                onClick={() => copyText(runSnippet)}
                className="inline-flex items-center gap-1 text-[11px] font-medium text-tertiary transition-colors hover:text-ink"
              >
                <ClipboardDocumentIcon className="h-3.5 w-3.5" />
                {t('Copy')}
              </button>
            </div>
            <pre className="overflow-x-auto rounded-lg bg-ink px-4 py-3 font-mono text-[11px] leading-relaxed text-green-400">
              {runSnippet}
            </pre>
            <p className="mt-1 text-[11px] text-tertiary">
              {chosenPreset && maxAge > 0
                ? t('Returns collected data when the last crawl is newer than {{window}}; otherwise starts a crawl and answers with a crawl id to poll.', { window: t(chosenPreset.label) })
                : t('Starts a crawl and answers with a crawl id to poll — a whole-site crawl takes longer than one HTTP call.')}
            </p>
          </div>
        ) : (
          <div className="rounded-lg border border-border bg-hover/40 px-3 py-3">
            <p className="text-[12px] leading-relaxed text-secondary">
              {t('Save this crawl’s settings to get a permanent endpoint. The saved crawl keeps its own history, which is what lets a later call reuse the data instead of crawling again.')}
            </p>
            <button
              onClick={handleSave}
              disabled={saving}
              className="mt-2 rounded-lg bg-ink px-3 py-1.5 text-[12px] font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              {saving ? t('Saving…') : t('Make this crawl callable')}
            </button>
          </div>
        )}

        {/* 3 — the two other calls worth knowing, once it is callable */}
        {definition && (
          <div className="space-y-3">
            <div>
              <div className="mb-1.5 flex items-center justify-between">
                <span className="text-[11px] font-semibold uppercase tracking-wider text-tertiary">
                  {t('Just read what it already collected')}
                </span>
                <button
                  onClick={() => copyText(dataSnippet)}
                  className="inline-flex items-center gap-1 text-[11px] font-medium text-tertiary transition-colors hover:text-ink"
                >
                  <ClipboardDocumentIcon className="h-3.5 w-3.5" />
                  {t('Copy')}
                </button>
              </div>
              <pre className="overflow-x-auto rounded-lg bg-ink px-4 py-3 font-mono text-[11px] leading-relaxed text-green-400">
                {dataSnippet}
              </pre>
              <p className="mt-1 text-[11px] text-tertiary">
                {t('Never crawls, at any age — the cheapest read.')}
              </p>
            </div>

            <div>
              <div className="mb-1.5 flex items-center justify-between">
                <span className="text-[11px] font-semibold uppercase tracking-wider text-tertiary">
                  {t('Force a fresh crawl')}
                </span>
                <button
                  onClick={() => copyText(freshSnippet)}
                  className="inline-flex items-center gap-1 text-[11px] font-medium text-tertiary transition-colors hover:text-ink"
                >
                  <ClipboardDocumentIcon className="h-3.5 w-3.5" />
                  {t('Copy')}
                </button>
              </div>
              <pre className="overflow-x-auto rounded-lg bg-ink px-4 py-3 font-mono text-[11px] leading-relaxed text-green-400">
                {freshSnippet}
              </pre>
              <p className="mt-1 text-[11px] text-tertiary">
                {t('Ignores any saved window. Same as sending max_age: 0.')}
              </p>
            </div>
          </div>
        )}

        {/* 4 — the key */}
        <div className="border-t border-border pt-3">
          <span className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wider text-tertiary">
            {t('Your key')}
          </span>
          {mintedKey ? (
            <div className="flex items-center gap-2 rounded-lg border border-border bg-hover px-2.5 py-2">
              <CheckIcon className="h-4 w-4 shrink-0 text-green-600" />
              <code className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink">{mintedKey}</code>
              <button
                onClick={() => copyText(mintedKey)}
                className="shrink-0 text-tertiary transition-colors hover:text-ink"
                title={t('Copy')}
              >
                <ClipboardDocumentIcon className="h-3.5 w-3.5" />
              </button>
            </div>
          ) : (
            <button
              onClick={handleMintKey}
              disabled={minting}
              className="rounded-lg border border-border px-3 py-1.5 text-[12px] font-semibold text-ink transition-colors hover:bg-hover disabled:opacity-50"
            >
              {minting ? t('Creating…') : t('Create an API key')}
            </button>
          )}
          <p className="mt-1.5 text-[11px] leading-relaxed text-tertiary">
            {mintedKey
              ? t('Copy it now — it won’t be shown again.')
              : t('Creates a key scoped to crawls and their data only.')}{' '}
            <Link to="/developers" className="text-ink underline underline-offset-2 hover:text-secondary">
              {t('Manage API keys')}
            </Link>
          </p>
        </div>

        {/* 5 — how to read the answer */}
        <div className="space-y-1.5 border-t border-border pt-3 text-[11px] leading-relaxed text-tertiary">
          <p>
            {t('Every response carries')}{' '}
            <code className="font-mono text-[10px] text-secondary">_cache.hit</code>{' '}
            {t('and')} <code className="font-mono text-[10px] text-secondary">_cache.age_seconds</code>{' '}
            — {t('so you can always tell whether you got reused data or a fresh crawl.')}
          </p>
          <p>
            {t('A fresh crawl answers')}{' '}
            <code className="font-mono text-[10px] text-secondary">202</code>{' '}
            {t('with a crawl id and a status URL; add')}{' '}
            <code className="font-mono text-[10px] text-secondary">"wait": true</code>{' '}
            {t('to block for up to 300s instead.')}
          </p>
        </div>
      </div>
    </Modal>
  );
};

export default CrawlApiModal;
