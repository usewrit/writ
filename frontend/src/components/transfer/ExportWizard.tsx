import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import {
  ArrowDownTrayIcon,
  ArrowLeftIcon,
  ArrowRightIcon,
  ArrowPathIcon,
  ClipboardDocumentIcon,
  ExclamationTriangleIcon,
  ShieldExclamationIcon,
} from '@heroicons/react/24/outline';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { Input } from '../ui/Input';
import { Checkbox } from '../ui/Checkbox';
import { Switch } from '../ui/Switch';
import { Badge } from '../ui/Badge';
import { apiErrorDetail, apiErrorMessage } from '../../api/client';
import { formatNumber } from '../../utils/format';
import { automationApi, targetsApi } from '../../api/endpoints';
import {
  downloadPackage,
  suggestPassphrase,
  transferApi,
  type ExportPreview,
  type ExportSelection,
} from '../../api/transfer';

/**
 * ExportWizard — packaging this account's work into a portable `.writ` file.
 *
 * Four steps (DATA_PORTABILITY_SPEC §12):
 *   1 Choose      which assets; referenced logins are pulled in automatically
 *   2 Options     per-workflow data toggles, and the credentials opt-in
 *   3 Passphrase  with a generated suggestion and an unmissable "we cannot recover this"
 *   4 Download
 *
 * Step 2's two toggles are the ones that change what the file MEANS, so each says
 * so in place rather than in a tooltip: including data makes it a data file
 * (usually PII), and including credentials makes it a credential file that anyone
 * with the passphrase can use.
 */

type StepId = 'choose' | 'options' | 'passphrase' | 'download';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  /** Pre-select assets, e.g. from a workflow's own page. */
  initialSelection?: ExportSelection;
}

interface Pickable {
  id: number;
  name: string;
  detail?: string | null;
}

const ExportWizardBody: React.FC<Props> = ({ isOpen, onClose, initialSelection }) => {
  const { t } = useTranslation();

  const [step, setStep] = useState<StepId>('choose');
  const [busy, setBusy] = useState(false);

  const [workflows, setWorkflows] = useState<Pickable[]>([]);
  const [monitors, setMonitors] = useState<Pickable[]>([]);
  const [pickedWorkflows, setPickedWorkflows] = useState<number[]>(initialSelection?.workflows || []);
  const [pickedMonitors, setPickedMonitors] = useState<number[]>(initialSelection?.monitors || []);

  const [preview, setPreview] = useState<ExportPreview | null>(null);
  const [dataFor, setDataFor] = useState<number[]>([]);
  const [includeCredentials, setIncludeCredentials] = useState(false);
  const [password, setPassword] = useState('');
  const [label, setLabel] = useState('');
  const [passphrase, setPassphrase] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [done, setDone] = useState<{ filename: string } | null>(null);

  // No reset pass: `ExportWizard` below remounts this body on every open, so
  // every field above is already at its initial value. Unwinding a dozen
  // setState calls from an effect instead is what the cascading-render rule
  // objects to, and it also left the passphrase sitting in memory for as long
  // as the parent stayed mounted.
  useEffect(() => {
    if (!isOpen) return;
    // Load pickable assets. Names only — this is a chooser, not a browser.
    automationApi
      .listWorkflows()
      .then((rows) => {
        setWorkflows((rows || []).map((w: any) => ({ id: w.id, name: w.name, detail: w.entry_url })));
      })
      .catch(() => undefined);
    targetsApi
      .getAll()
      .then((rows) => {
        setMonitors((rows || []).map((m: any) => ({ id: m.id, name: m.url, detail: m.check_type })));
      })
      .catch(() => undefined);
  }, [isOpen]);

  const selection = useMemo<ExportSelection>(
    () => ({ workflows: pickedWorkflows, monitors: pickedMonitors, personas: 'referenced' }),
    [pickedWorkflows, pickedMonitors],
  );

  const anyPicked = pickedWorkflows.length + pickedMonitors.length > 0;

  const loadPreview = async () => {
    setBusy(true);
    try {
      const res = await transferApi.previewExport(
        selection,
        dataFor.length ? { workflows: dataFor } : {},
      );
      setPreview(res);
      setStep('options');
    } catch (err) {
      toast.error(apiErrorMessage(err, 'Could not work out what to export.'));
    } finally {
      setBusy(false);
    }
  };

  // Re-price the data section whenever the per-workflow toggles change, so the row
  // counts on screen are the ones that will actually be written.
  useEffect(() => {
    if (step !== 'options' || !anyPicked) return;
    let cancelled = false;
    transferApi
      .previewExport(selection, dataFor.length ? { workflows: dataFor } : {})
      .then((res) => { if (!cancelled) setPreview(res); })
      .catch(() => undefined);
    return () => { cancelled = true; };
  }, [step, dataFor, selection, anyPicked]);

  const download = async () => {
    setBusy(true);
    try {
      const { blob, filename } = await transferApi.exportPackage({
        label: label || undefined,
        select: selection,
        include_data: dataFor.length ? { workflows: dataFor } : {},
        include_credentials: includeCredentials,
        reauth: includeCredentials ? { password } : undefined,
        passphrase,
      });
      downloadPackage(blob, filename);
      setDone({ filename });
      setStep('download');
    } catch (err) {
      const detail = apiErrorDetail<any>(err);
      toast.error(detail?.message || apiErrorMessage(err, 'The export failed.'));
    } finally {
      setBusy(false);
    }
  };

  const steps: StepId[] = ['choose', 'options', 'passphrase', 'download'];
  const stepIndex = steps.indexOf(step);

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      size="2xl"
      title={t('Export a Writ package')}
      subtitle={t(SUBTITLE_KEY[step])}
      footer={
        <div className="flex items-center justify-between gap-3">
          <div className="text-xs text-secondary">
            {step !== 'download' && preview && (
              <>
                {Object.entries(preview.counts)
                  .filter(([k, n]) => n > 0 && k !== 'data_runs')
                  .map(([k, n]) => `${n} ${k.replace('_', ' ')}`)
                  .join(' · ')}
              </>
            )}
          </div>
          <div className="flex items-center gap-2">
            {stepIndex > 0 && step !== 'download' && (
              <Button variant="ghost" onClick={() => setStep(steps[stepIndex - 1])} disabled={busy}>
                <ArrowLeftIcon className="w-4 h-4 mr-1.5" />
                {t('Back')}
              </Button>
            )}
            {step === 'choose' && (
              <Button onClick={loadPreview} disabled={!anyPicked || busy} loading={busy}>
                {t('Continue')}
                <ArrowRightIcon className="w-4 h-4 ml-1.5" />
              </Button>
            )}
            {step === 'options' && (
              <Button
                onClick={() => { setPassphrase(suggestPassphrase()); setStep('passphrase'); }}
                disabled={busy || (includeCredentials && !password)}
              >
                {t('Continue')}
                <ArrowRightIcon className="w-4 h-4 ml-1.5" />
              </Button>
            )}
            {step === 'passphrase' && (
              <Button onClick={download} disabled={!passphrase.trim() || !confirmed || busy} loading={busy}>
                <ArrowDownTrayIcon className="w-4 h-4 mr-1.5" />
                {t('Create and download')}
              </Button>
            )}
            {step === 'download' && (
              <Button onClick={onClose}>{t('Done')}</Button>
            )}
          </div>
        </div>
      }
    >
      {step === 'choose' && (
        <div className="space-y-4">
          <Input
            label={t('Label (optional)')}
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder={t('e.g. laptop → cloud')}
          />
          {/* The label is the one thing readable without the passphrase, so say so
              here rather than letting someone put a client name in it unknowingly. */}
          <p className="text-xs text-secondary -mt-2">
            {t('Shown to anyone who opens the file, before they type the passphrase.')}
          </p>

          <PickList
            title={t('Workflows')}
            items={workflows}
            picked={pickedWorkflows}
            onToggle={(id) =>
              setPickedWorkflows((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
            }
            onAll={(all) => setPickedWorkflows(all ? workflows.map((w) => w.id) : [])}
          />
          <PickList
            title={t('Monitors')}
            items={monitors}
            picked={pickedMonitors}
            onToggle={(id) =>
              setPickedMonitors((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
            }
            onAll={(all) => setPickedMonitors(all ? monitors.map((m) => m.id) : [])}
          />
          <p className="text-xs text-secondary">
            {t('Logins these assets use are included automatically — their names and settings, never their passwords.')}
          </p>
        </div>
      )}

      {step === 'options' && preview && (
        <div className="space-y-4">
          {preview.personas_included.length > 0 && (
            <div className="rounded-lg border border-border p-3">
              <p className="text-sm font-medium text-ink">
                {preview.personas_included.length} login
                {preview.personas_included.length === 1 ? '' : 's'} included
              </p>
              <p className="text-xs text-secondary mt-0.5">
                {preview.personas_included.join(', ')} — as settings only, unless you include
                credentials below.
              </p>
            </div>
          )}

          {preview.data.length > 0 || preview.counts.workflows > 0 ? (
            <div>
              <p className="text-xs font-semibold uppercase tracking-wide text-secondary mb-1.5">
                {t('Collected data')}
              </p>
              <div className="rounded-lg border border-border divide-y divide-border">
                {pickedWorkflows.map((id) => {
                  const wf = workflows.find((w) => w.id === id);
                  const stats = preview.data.find((d) => d.workflow_id === id);
                  const on = dataFor.includes(id);
                  return (
                    <label key={id} className="p-3 flex items-center justify-between gap-3 cursor-pointer">
                      <span className="min-w-0">
                        <span className="block text-sm text-ink truncate">
                          {wf?.name || `Workflow ${id}`}
                        </span>
                        <span className="block text-xs text-secondary">
                          {on && stats
                            ? t('{{rows}} rows across {{runs}} runs', { rows: formatNumber(stats.rows), runs: formatNumber(stats.runs) })
                            : t('Configuration only')}
                          {stats?.truncated ? ` · ${t('older rows will be left out (row cap)')}` : ''}
                        </span>
                      </span>
                      <Checkbox
                        checked={on}
                        onChange={(e) =>
                          setDataFor((prev) => (e.target.checked ? [...prev, id] : prev.filter((x) => x !== id)))
                        }
                      />
                    </label>
                  );
                })}
              </div>
              <p className="text-xs text-secondary mt-1.5">
                {t('Data makes the file much larger and usually contains personal information. Leave it off to share just the automation.')}
              </p>
            </div>
          ) : null}

          <div className="rounded-lg border border-amber-200 bg-amber-50/60 p-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-sm font-medium text-amber-900">
                  <ShieldExclamationIcon className="w-4 h-4 inline mr-1" />
                  {t('Include my credentials')}
                </p>
                <p className="text-xs text-amber-800/80 mt-0.5">
                  {t('For moving your own account to another install. The file becomes a credential file: anyone with the passphrase can use these logins and keys. Leave it off to send this to someone else — they will attach their own.')}
                </p>
              </div>
              <Switch checked={includeCredentials} onChange={setIncludeCredentials} />
            </div>
            {includeCredentials && (
              <Input
                className="mt-3"
                type="password"
                label={t('Confirm your account password')}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={t('Your Writ password')}
              />
            )}
          </div>

          {preview.skipped.length > 0 && (
            <div className="rounded-lg border border-border p-3">
              <p className="text-sm font-medium text-ink">
                {t('{{count}} item(s) will not be included', { count: preview.skipped.length })}
              </p>
              <ul className="mt-1 text-xs text-secondary space-y-0.5">
                {preview.skipped.map((s, i) => (
                  <li key={i}>{s.name} — {s.detail || s.reason.replace('_', ' ')}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {step === 'passphrase' && (
        <div className="space-y-4">
          <div>
            <Input
              label={t('Passphrase')}
              value={passphrase}
              onChange={(e) => setPassphrase(e.target.value)}
              placeholder={t('Six words, or your own')}
            />
            <div className="flex items-center gap-2 mt-2">
              <Button variant="ghost" size="sm" onClick={() => setPassphrase(suggestPassphrase())}>
                <ArrowPathIcon className="w-3.5 h-3.5 mr-1" />
                {t('Suggest another')}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  navigator.clipboard?.writeText(passphrase);
                  toast.success('Passphrase copied');
                }}
                disabled={!passphrase}
              >
                <ClipboardDocumentIcon className="w-3.5 h-3.5 mr-1" />
                {t('Copy')}
              </Button>
            </div>
          </div>

          <div className="rounded-lg bg-red-50 border border-red-200 p-3">
            <div className="flex gap-2">
              <ExclamationTriangleIcon className="w-5 h-5 text-red-600 shrink-0" />
              <div className="text-sm text-red-800">
                <p className="font-medium">{t('Writ cannot recover this passphrase.')}</p>
                <p className="text-xs mt-0.5">
                  {t('It is never sent to us or stored anywhere. Without it the package cannot be opened — by you or by anyone else. Save it somewhere before you continue.')}
                </p>
              </div>
            </div>
          </div>

          <label className="flex items-start gap-2 cursor-pointer">
            <Checkbox checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
            <span className="text-sm text-ink">
              {t('I have saved this passphrase.')}
            </span>
          </label>

          {includeCredentials && (
            <Badge variant="error">
              {t('This package will contain your credentials')}
            </Badge>
          )}
        </div>
      )}

      {step === 'download' && done && (
        <div className="space-y-3">
          <p className="text-sm text-ink font-medium">{done.filename}</p>
          <p className="text-sm text-secondary">
            {t('Saved to your downloads. To bring it into another Writ install, open Import there and give it this passphrase.')}
          </p>
          {includeCredentials && (
            <p className="text-xs text-red-600">
              {t('This file contains credentials. Treat it like a password manager export — send it over something you trust, and delete it when you are done.')}
            </p>
          )}
        </div>
      )}
    </Modal>
  );
};

/**
 * A closed-then-reopened wizard must start blank. React's answer to that is a
 * fresh instance — a `key` — not an effect that unwinds every field with its own
 * setState the moment `isOpen` flips.
 *
 * The counter advances on the CLOSED→OPEN edge only. Bumping it on close would
 * tear down the subtree the modal's leave transition is still animating, and the
 * dialog would vanish instead of fading. Comparing against the previous prop
 * during render is React's documented way to adjust state from a prop without an
 * effect; it settles in the same commit, so nothing renders with a stale key.
 */
export const ExportWizard: React.FC<Props> = (props) => {
  const [session, setSession] = useState(0);
  const [wasOpen, setWasOpen] = useState(props.isOpen);
  if (props.isOpen !== wasOpen) {
    setWasOpen(props.isOpen);
    if (props.isOpen) setSession((n) => n + 1);
  }
  return <ExportWizardBody key={session} {...props} />;
};

/** Step subtitles, resolved through i18n at render time — a module-level `t()`
 *  would freeze at import, before the language is known. */
const SUBTITLE_KEY: Record<StepId, string> = {
  choose: 'Pick what travels',
  options: 'Data and credentials',
  passphrase: 'Protect the file',
  download: 'Ready',
};

const PickList: React.FC<{
  title: string;
  items: Pickable[];
  picked: number[];
  onToggle: (id: number) => void;
  onAll: (all: boolean) => void;
}> = ({ title, items, picked, onToggle, onAll }) => {
  const { t } = useTranslation();
  if (!items.length) return null;
  const allOn = picked.length === items.length;
  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <p className="text-xs font-semibold uppercase tracking-wide text-secondary">
          {title} · {picked.length}/{items.length}
        </p>
        <button
          type="button"
          className="text-xs text-accent-strong hover:underline"
          onClick={() => onAll(!allOn)}
        >
          {allOn
            ? t('Clear')
            : t('Select all')}
        </button>
      </div>
      <div
        className={clsx(
          'rounded-lg border border-border divide-y',
          'divide-border max-h-48 overflow-y-auto',
        )}
      >
        {items.map((item) => (
          <label key={item.id} className="p-2.5 flex items-center gap-2.5 cursor-pointer">
            <Checkbox checked={picked.includes(item.id)} onChange={() => onToggle(item.id)} />
            <span className="min-w-0">
              <span className="block text-sm text-ink truncate">{item.name}</span>
              {item.detail && <span className="block text-xs text-secondary truncate">{item.detail}</span>}
            </span>
          </label>
        ))}
      </div>
    </div>
  );
};

export default ExportWizard;
