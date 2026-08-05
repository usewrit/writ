import React from 'react';
import { UserCircleIcon } from '@heroicons/react/24/outline';
import { useTranslation } from 'react-i18next';
import { useQuery } from '../../../hooks/useQuery';
import { personasApi } from '../../../api/endpoints';
import type { Persona } from '../../../types/api';
import { Select } from '../../ui';

interface PersonaPickerProps {
  value: number | null;
  onChange: (id: number | null) => void;
  /** Optional host — domain-matching personas are listed first. */
  domain?: string;
  label?: string;
  allowClear?: boolean;
  disabled?: boolean;
}

function hostMatches(persona: Persona, domain?: string): boolean {
  if (!domain || !persona.target_domain) return false;
  const d = domain.toLowerCase().replace(/^\./, '');
  const pd = persona.target_domain.toLowerCase().replace(/^\./, '');
  return d === pd || d.endsWith('.' + pd) || pd.endsWith('.' + d);
}

/**
 * Block-editor persona selector — queries personasApi.list(domain?) and lets the
 * user bind a saved identity to an action. Domain-matching personas sort first.
 */
export const PersonaPicker: React.FC<PersonaPickerProps> = ({
  value,
  onChange,
  domain,
  label,
  allowClear = true,
  disabled = false,
}) => {
  const { t } = useTranslation();
  const { data: personas } = useQuery<Persona[]>(
    ['flow-personas', domain || 'all'],
    () => personasApi.list(domain),
  );

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

  return (
    <div className="space-y-1.5">
      <label className="flex items-center gap-1.5 text-xs font-medium text-secondary">
        <UserCircleIcon className="w-4 h-4" />
        {label || t('Persona')}
      </label>
      <Select<number>
        value={value ?? undefined}
        disabled={disabled}
        onChange={(v) => onChange(v === -1 ? null : v)}
        aria-label={label || t('Persona')}
        placeholder={allowClear ? t('No persona') : undefined}
        options={[
          ...(allowClear ? [{ value: -1, label: t('No persona') }] : []),
          ...sorted.map((p) => ({
            value: p.id,
            label: `${p.name}${p.target_domain ? ` (${p.target_domain})` : ''}${p.twofa_method !== 'none' ? ` · ${t('2FA')}` : ''}`,
          })),
        ]}
        className="w-full"
      />
    </div>
  );
};

export default PersonaPicker;
