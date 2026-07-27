import React, { useRef, useState } from 'react';
import toast from 'react-hot-toast';
import { useTranslation } from 'react-i18next';
import {
  QrCodeIcon, ArrowUpTrayIcon, ArrowLeftIcon, KeyIcon, CheckCircleIcon,
} from '@heroicons/react/24/outline';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { Checkbox } from '../ui';
import { personasApi } from '../../api/endpoints';
import type { AuthImportEntryPreview, AuthImportSelection, Persona } from '../../types/api';
import { decodeQrFromImageFile, canDecodeQr, isMigrationUri } from '../../utils/otpauth';

const inputCls =
  'w-full px-3 py-2 text-sm border border-border rounded-lg bg-surface text-ink placeholder:text-tertiary focus:outline-none focus:ring-1 focus:ring-ink/10';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  /** Called with the personas created by the import. */
  onImported: (created: Persona[]) => void;
  /** Prefill the target domain for every imported account (e.g. the recorded site). */
  defaultDomain?: string;
}

interface Row extends AuthImportEntryPreview {
  selected: boolean;
  name: string;     // editable, seeded from suggested_name
  domain: string;   // editable, seeded from suggested_domain
}

/**
 * Import EXISTING authenticator secrets into TOTP personas — no per-account
 * re-enrollment. Paste otpauth:// links, scan a QR image, or upload a Google
 * Authenticator "Export accounts" QR. The secrets are parsed + staged server-side;
 * the user picks which accounts to keep here, and only those become personas.
 */
export const AuthenticatorImportModal: React.FC<Props> = ({ isOpen, onClose, onImported, defaultDomain }) => {
  const { t } = useTranslation();
  const [phase, setPhase] = useState<'input' | 'preview'>('input');
  const [text, setText] = useState('');
  const [migration, setMigration] = useState<string | null>(null);
  const [importToken, setImportToken] = useState('');
  const [rows, setRows] = useState<Row[]>([]);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const reset = () => {
    setPhase('input'); setText(''); setMigration(null);
    setImportToken(''); setRows([]); setBusy(false);
  };
  const close = () => { reset(); onClose(); };

  const onPickQr = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = ''; // allow re-picking the same file
    if (!file) return;
    if (!canDecodeQr()) {
      toast.error(t('This browser can’t scan QR images — paste the setup link instead.'));
      return;
    }
    const decoded = await decodeQrFromImageFile(file);
    if (!decoded) {
      toast.error(t('No QR code found in that image.'));
      return;
    }
    if (isMigrationUri(decoded)) {
      setMigration(decoded);
      toast.success(t('Google Authenticator export detected — continue to preview the accounts.'));
    } else if (/^otpauth:\/\//i.test(decoded)) {
      setText((prev) => (prev ? `${prev.trim()}\n${decoded}` : decoded));
      toast.success(t('Added one account from the QR code.'));
    } else {
      toast.error(t('That QR code isn’t an authenticator export.'));
    }
  };

  const onParse = async () => {
    const lines = text.split('\n').map((l) => l.trim()).filter(Boolean);
    const uris = lines.filter((l) => /^otpauth:\/\//i.test(l));
    // A migration URI can also be pasted into the box rather than scanned.
    const pastedMigration = lines.find((l) => isMigrationUri(l));
    const migrationPayload = migration || pastedMigration || undefined;
    if (!uris.length && !migrationPayload) {
      toast.error(t('Paste at least one otpauth:// link or scan an export QR.'));
      return;
    }
    setBusy(true);
    try {
      const res = await personasApi.parseAuthenticatorImport({
        otpauth_uris: uris.length ? uris : undefined,
        migration_payload: migrationPayload,
      });
      if (!res.entries.length) {
        toast.error(t('No usable accounts found in that import.'));
        return;
      }
      setImportToken(res.import_token);
      setRows(res.entries.map((e) => ({
        ...e,
        selected: true,
        name: e.suggested_name,
        domain: e.suggested_domain || defaultDomain || '',
      })));
      setPhase('preview');
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('Could not read that authenticator import.'));
    } finally {
      setBusy(false);
    }
  };

  const onCommit = async () => {
    const chosen = rows.filter((r) => r.selected);
    if (!chosen.length) {
      toast.error(t('Select at least one account to import.'));
      return;
    }
    const selections: AuthImportSelection[] = chosen.map((r) => ({
      idx: r.idx,
      name: r.name.trim() || undefined,
      target_domain: r.domain.trim() || undefined,
    }));
    setBusy(true);
    try {
      const res = await personasApi.commitAuthenticatorImport(importToken, selections);
      const made = res.created.length;
      toast.success(made === 1
        ? t('Imported 1 persona.')
        : t('Imported {{count}} personas.', { count: made }));
      if (res.skipped?.length) {
        toast(t('{{count}} account(s) were skipped.', { count: res.skipped.length }));
      }
      onImported(res.created);
      close();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('Import failed.'));
    } finally {
      setBusy(false);
    }
  };

  const selectedCount = rows.filter((r) => r.selected).length;

  const footer = phase === 'input' ? (
    <div className="flex items-center justify-between gap-3">
      <p className="text-[11px] text-tertiary">
        {t('TOTP secrets are encrypted at rest and never shown again.')}
      </p>
      <div className="flex items-center gap-2">
        <Button variant="ghost" size="sm" onClick={close}>{t('Cancel')}</Button>
        <Button size="sm" onClick={onParse} loading={busy}>{t('Continue')}</Button>
      </div>
    </div>
  ) : (
    <div className="flex items-center justify-between gap-3">
      <Button variant="ghost" size="sm" onClick={() => setPhase('input')} disabled={busy}>
        <ArrowLeftIcon className="w-4 h-4" /> {t('Back')}
      </Button>
      <Button size="sm" onClick={onCommit} loading={busy} disabled={!selectedCount}>
        {selectedCount === 1
          ? t('Import 1 account')
          : t('Import {{count}} accounts', { count: selectedCount })}
      </Button>
    </div>
  );

  return (
    <Modal
      isOpen={isOpen}
      onClose={close}
      size="lg"
      title={t('Import from authenticator')}
      subtitle={t('Reuse the 2FA secrets you already have — no need to re-enroll each account.')}
      footer={footer}
    >
      {phase === 'input' ? (
        <div className="space-y-4">
          <div className="rounded-lg border border-border bg-canvas p-3 space-y-2">
            <div className="flex items-center gap-2 text-sm font-medium text-ink">
              <KeyIcon className="w-4 h-4" /> {t('Paste authenticator setup links')}
            </div>
            <textarea
              className={`${inputCls} font-mono h-28 resize-none`}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={'otpauth://totp/GitHub:octocat?secret=…\notpauth://totp/AWS:root?secret=…'}
            />
            <p className="text-[11px] text-tertiary">
              {t('One otpauth:// link per line. Most apps (2FAS, Aegis, Bitwarden) can export these.')}
            </p>
          </div>

          <div className="rounded-lg border border-border bg-canvas p-3 space-y-2">
            <div className="flex items-center gap-2 text-sm font-medium text-ink">
              <QrCodeIcon className="w-4 h-4" /> {t('Or scan a QR image')}
            </div>
            <div className="flex items-center gap-2">
              <Button variant="secondary" size="sm" onClick={() => fileRef.current?.click()}>
                <ArrowUpTrayIcon className="w-4 h-4" /> {t('Upload QR image')}
              </Button>
              {migration && (
                <span className="inline-flex items-center gap-1 text-[11px] text-ink">
                  <CheckCircleIcon className="w-4 h-4" /> {t('Google export ready')}
                </span>
              )}
            </div>
            <input ref={fileRef} type="file" accept="image/*" className="hidden" onChange={onPickQr} />
            <p className="text-[11px] text-tertiary">
              {t('In Google Authenticator, tap ⋮ → “Export accounts”, then upload a screenshot of the QR. The export holds every account — you choose which to keep next.')}
            </p>
            {!canDecodeQr() && (
              <p className="text-[11px] text-ink">
                {t('Heads up: this browser can’t decode QR images. Paste the links above instead.')}
              </p>
            )}
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <p className="text-xs text-secondary">
              {t('{{count}} account(s) found. Pick which to turn into personas.', { count: rows.length })}
            </p>
            <div className="flex items-center gap-2 text-[11px]">
              <button className="text-secondary hover:text-ink"
                onClick={() => setRows((rs) => rs.map((r) => ({ ...r, selected: true })))}>
                {t('Select all')}
              </button>
              <span className="text-tertiary">·</span>
              <button className="text-secondary hover:text-ink"
                onClick={() => setRows((rs) => rs.map((r) => ({ ...r, selected: false })))}>
                {t('None')}
              </button>
            </div>
          </div>
          <div className="space-y-2 max-h-[48vh] overflow-y-auto pr-1">
            {rows.map((r, i) => (
              <div key={r.idx}
                className={`rounded-lg border p-3 transition-colors ${r.selected ? 'border-ink/30 bg-surface' : 'border-border bg-canvas opacity-60'}`}>
                <div className="flex items-start gap-3">
                  <Checkbox
                    className="mt-1"
                    checked={r.selected}
                    onChange={(e) => setRows((rs) => rs.map((x, j) => j === i ? { ...x, selected: e.target.checked } : x))}
                  />
                  <div className="flex-1 min-w-0 space-y-2">
                    <div className="text-xs text-tertiary truncate">
                      {r.issuer ? `${r.issuer} · ` : ''}{r.label || t('(no label)')}
                      <span className="ml-2">{r.algorithm} · {r.digits} {t('digits')} · {r.period}s</span>
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      <input
                        className={inputCls}
                        value={r.name}
                        placeholder={t('Persona name')}
                        onChange={(e) => setRows((rs) => rs.map((x, j) => j === i ? { ...x, name: e.target.value } : x))}
                      />
                      <input
                        className={inputCls}
                        value={r.domain}
                        placeholder={t('Site domain (optional)')}
                        onChange={(e) => setRows((rs) => rs.map((x, j) => j === i ? { ...x, domain: e.target.value } : x))}
                      />
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </Modal>
  );
};

export default AuthenticatorImportModal;
