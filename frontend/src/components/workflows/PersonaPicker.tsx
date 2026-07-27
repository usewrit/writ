import React, { useState } from 'react';
import { UserCircleIcon, CloudIcon, PlusIcon, ComputerDesktopIcon } from '@heroicons/react/24/outline';
import { useTranslation } from 'react-i18next';
import { useQuery } from '../../hooks/useQuery';
import { personasApi } from '../../api/endpoints';
import { Q } from '../../stores/queryKeys';
import type { Persona } from '../../types/api';
import { PersonaCreateModal } from './PersonaCreateModal';
import { Select } from '../ui';

interface PersonaPickerProps {
  value: number | null;
  onChange: (id: number | null) => void;
  /** Workflow host (entry_url hostname) — domain-matching personas are listed first. */
  domain?: string;
  allowClear?: boolean;
  label?: string;
  disabled?: boolean;
}

function hostMatches(persona: Persona, domain?: string): boolean {
  if (!domain || !persona.target_domain) return false;
  const d = domain.toLowerCase().replace(/^\./, '');
  const pd = persona.target_domain.toLowerCase().replace(/^\./, '');
  return d === pd || d.endsWith('.' + pd) || pd.endsWith('.' + d);
}

/**
 * Reusable persona selector. Selecting a persona forces cloud execution
 * (the backend pins it to a trusted residential agent), surfaced as a note.
 */
export const PersonaPicker: React.FC<PersonaPickerProps> = ({
  value,
  onChange,
  domain,
  allowClear = true,
  label,
  disabled = false,
}) => {
  const { t } = useTranslation();
  const { data: personas, refresh } = useQuery<Persona[]>(
    Q.personas(domain),
    () => personasApi.list(domain),
  );
  const [showCreate, setShowCreate] = useState(false);

  const sorted = React.useMemo(() => {
    const list = [...(personas || [])];
    list.sort((a, b) => {
      const am = hostMatches(a, domain) ? 0 : 1;
      const bm = hostMatches(b, domain) ? 0 : 1;
      if (am !== bm) return am - bm;
      return a.name.localeCompare(b.name);
    });
    return list;
  }, [personas, domain]);

  const selected = sorted.find((p) => p.id === value) || null;

  return (
    <div className="space-y-1.5">
      <label className="flex items-center gap-1.5 text-xs font-medium text-secondary">
        <UserCircleIcon className="w-4 h-4" />
        {label || t('Persona')}
      </label>
      <div className="flex items-center gap-2">
        <Select<string>
          value={value == null ? '' : String(value)}
          disabled={disabled}
          onChange={(v) => onChange(v ? Number(v) : null)}
          className="flex-1"
          wrapperClassName="flex-1"
          options={[
            ...(allowClear ? [{ value: '', label: t('No persona — use manual login') }] : []),
            ...sorted.map((p) => ({
              value: String(p.id),
              label: `${p.name}${p.target_domain ? ` (${p.target_domain})` : ''}${p.twofa_method !== 'none' ? ` · ${t('2FA')}` : ''}`,
            })),
          ]}
        />
        <button
          type="button"
          disabled={disabled}
          onClick={() => setShowCreate(true)}
          title={t('Create a new persona')}
          className="flex items-center gap-1 px-2.5 py-2 text-xs font-medium text-secondary border border-border rounded-lg hover:bg-hover hover:text-ink transition-colors disabled:opacity-50 shrink-0"
        >
          <PlusIcon className="w-4 h-4" /> {t('New')}
        </button>
      </div>
      <PersonaCreateModal
        isOpen={showCreate}
        onClose={() => setShowCreate(false)}
        defaultDomain={domain}
        onCreated={(p) => { refresh(); onChange(p.id); }}
      />
      {selected && (
        <p className="flex items-center gap-1.5 text-[11px] text-tertiary">
          <CloudIcon className="w-3.5 h-3.5" />
          {t('Runs on Writ Cloud as')} <span className="text-secondary">{selected.login_username || selected.name}</span>
          {selected.twofa_method !== 'none' && t(' · 2FA handled automatically')}
        </p>
      )}
      {/* BYO-agent advisory: personas saved on a desktop device live there (their
          warm session + credentials never leave it), so a run using one can only
          execute on that device — copy the workflow to the desktop app to do so.
          Admin lists cloud personas only, so this educates rather than blocks. */}
      <p className="flex items-start gap-1.5 text-[11px] text-tertiary">
        <ComputerDesktopIcon className="w-3.5 h-3.5 mt-px shrink-0" />
        <span>{t('Have a persona saved on a desktop device? It runs on that device — copy this workflow to your desktop app to run it there with that persona.')}</span>
      </p>
    </div>
  );
};

export default PersonaPicker;
