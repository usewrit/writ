import React from 'react';
import {
  PlusIcon,
  TrashIcon,
  LockClosedIcon,
  LockOpenIcon,
  ShieldCheckIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { Select } from '../ui';

export interface DataField {
  key: string;
  value: string;
  isSecret?: boolean;
}

/** A stored vault secret the user can attach by reference (value never leaves the vault). */
export interface VaultSecretOption {
  id: number;
  name: string;
  category?: string | null;
}

/** True when a field value is a live `{{vault:NAME}}` reference rather than an inline value. */
export function isVaultRef(value: string): boolean {
  return typeof value === 'string' && value.startsWith('{{vault:');
}

interface SecureDataFieldsProps {
  fields: DataField[];
  onChange: (fields: DataField[]) => void;
  label?: string;
  description?: string;
  addLabel?: string;
  /**
   * Stored vault secrets the user can attach by reference. When provided (and
   * non-empty) an "Attach existing secret" dropdown is shown; picking one appends
   * a locked field whose value is `{{vault:NAME}}` — resolved to the real secret
   * server-side at run time, never exposed to the browser or the AI.
   */
  vaultSecrets?: VaultSecretOption[];
}

/**
 * Reusable key-value input list with per-field secret toggle.
 *
 * When a field is marked as secret:
 * - The value input becomes type=password (masked)
 * - A lock icon indicates it's sensitive
 * - On submission, the key is prefixed with __secret_ so the backend encrypts it
 *
 * Usage:
 *   <SecureDataFields
 *     fields={availableData}
 *     onChange={(fields) => updateConfig({ availableData: fields })}
 *   />
 */
export const SecureDataFields: React.FC<SecureDataFieldsProps> = ({
  fields,
  onChange,
  label,
  description,
  addLabel,
  vaultSecrets,
}) => {
  const { t } = useTranslation();
  const labelText = label ?? t('Available Data');
  const descriptionText = description ?? t('Data the AI can use to fill forms. Mark sensitive fields as secret.');
  const addLabelText = addLabel ?? t('Add Field');
  const addField = () => onChange([...fields, { key: '', value: '', isSecret: false }]);

  // Secrets already attached by reference — hidden from the attach dropdown so the
  // same secret can't be added twice.
  const attachedVaultNames = new Set(
    fields
      .map((f) => (isVaultRef(f.value) ? f.value.replace(/^\{\{vault:/, '').replace(/\}\}$/, '').split('.')[0] : null))
      .filter((n): n is string => !!n),
  );
  const availableVaultSecrets = (vaultSecrets ?? []).filter((s) => !attachedVaultNames.has(s.name));

  const attachVaultSecret = (name: string) => {
    if (!name || attachedVaultNames.has(name)) return;
    onChange([...fields, { key: name, value: `{{vault:${name}}}`, isSecret: true }]);
  };

  const updateField = (index: number, updates: Partial<DataField>) => {
    onChange(fields.map((f, i) => (i === index ? { ...f, ...updates } : f)));
  };

  const removeField = (index: number) => {
    onChange(fields.filter((_, i) => i !== index));
  };

  const toggleSecret = (index: number) => {
    updateField(index, { isSecret: !fields[index].isSecret });
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div>
          {labelText && <label className="text-xs font-semibold text-secondary uppercase tracking-wider">{labelText}</label>}
          {descriptionText && <p className="text-[11px] text-tertiary mt-0.5">{descriptionText}</p>}
        </div>
        <div className="flex items-center gap-2">
          {availableVaultSecrets.length > 0 && (
            <Select
              size="sm"
              value=""
              onChange={(v) => { if (v) attachVaultSecret(v); }}
              aria-label={t('Attach a stored vault secret — resolved server-side, never shown to the AI')}
              placeholder={t('Attach existing secret…')}
              options={availableVaultSecrets.map((s) => ({
                value: s.name,
                label: `${s.name}${s.category ? ` (${s.category})` : ''}`,
              }))}
            />
          )}
          <button
            onClick={addField}
            className="flex items-center gap-1 px-2 py-1 text-xs font-medium text-secondary hover:text-ink hover:bg-hover rounded-lg transition-colors"
          >
            <PlusIcon className="w-3.5 h-3.5" /> {addLabelText}
          </button>
        </div>
      </div>

      {fields.length > 0 && (
        <div className="space-y-1.5">
          {fields.map((field, i) => {
            const vaultRef = isVaultRef(field.value);
            return (
            <div key={i} className="flex gap-2 items-center">
              {/* Key input */}
              <input
                type="text"
                value={field.key}
                onChange={(e) => updateField(i, { key: e.target.value })}
                placeholder={t('Field name')}
                readOnly={vaultRef}
                className={clsx(
                  'w-1/3 px-3 py-2 text-xs border rounded-lg bg-surface text-ink placeholder:text-tertiary',
                  field.isSecret ? 'border-zinc-400' : 'border-border',
                  vaultRef && 'opacity-80',
                )}
              />

              {vaultRef ? (
                /* Linked vault secret — read-only reference; value lives in the vault */
                <span className="flex-1 flex items-center gap-1.5 px-3 py-2 text-xs border border-zinc-400 rounded-lg bg-hover text-secondary font-mono">
                  <ShieldCheckIcon className="w-3.5 h-3.5 text-zinc-700 shrink-0" />
                  {field.value}
                </span>
              ) : (
                /* Value input — masked when secret */
                <input
                  type={field.isSecret ? 'password' : 'text'}
                  value={field.value}
                  onChange={(e) => updateField(i, { value: e.target.value })}
                  placeholder={field.isSecret ? '••••••••' : t('Value')}
                  className={clsx(
                    'flex-1 px-3 py-2 text-xs border rounded-lg bg-surface text-ink placeholder:text-tertiary',
                    field.isSecret ? 'border-zinc-400' : 'border-border',
                  )}
                />
              )}

              {/* Secret toggle — fixed (always secret) for vault refs */}
              <button
                onClick={() => { if (!vaultRef) toggleSecret(i); }}
                disabled={vaultRef}
                title={vaultRef ? t('Vault secret (resolved server-side)') : field.isSecret ? t('Marked as secret (encrypted)') : t('Click to mark as secret')}
                className={clsx(
                  'p-1.5 rounded-lg transition-colors',
                  field.isSecret
                    ? 'bg-zinc-900 text-white'
                    : 'text-tertiary hover:text-ink hover:bg-hover',
                  vaultRef && 'cursor-default',
                )}
              >
                {field.isSecret
                  ? <LockClosedIcon className="w-3.5 h-3.5" />
                  : <LockOpenIcon className="w-3.5 h-3.5" />
                }
              </button>

              {/* Delete */}
              <button
                onClick={() => removeField(i)}
                className="p-1.5 text-tertiary hover:text-red-600 rounded-lg hover:bg-hover"
              >
                <TrashIcon className="w-3.5 h-3.5" />
              </button>
            </div>
            );
          })}
        </div>
      )}
    </div>
  );
};


/**
 * Convert DataField[] to the form_data dict for the API.
 * Secret fields get the __secret_ prefix so the backend encrypts them.
 */
export function fieldsToFormData(fields: DataField[]): Record<string, string> {
  const result: Record<string, string> = {};
  for (const field of fields) {
    if (!field.key) continue;
    const key = field.isSecret ? `__secret_${field.key}` : field.key;
    result[key] = field.value;
  }
  return result;
}


/**
 * Convert a form_data dict (from the API) back to DataField[].
 * Detects __secret_ prefixed keys and restores the isSecret flag.
 * Secret values come back empty (backend doesn't expose them),
 * so we show a placeholder.
 */
export function formDataToFields(formData: Record<string, string>, _hasCredentials?: boolean): DataField[] {
  const fields: DataField[] = [];
  for (const [key, value] of Object.entries(formData || {})) {
    if (key.startsWith('__secret_')) {
      fields.push({ key: key.replace('__secret_', ''), value, isSecret: true });
    } else {
      fields.push({ key, value, isSecret: false });
    }
  }
  return fields;
}
