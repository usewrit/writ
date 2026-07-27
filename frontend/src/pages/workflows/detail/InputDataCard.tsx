import React from 'react';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import { automationApi } from '../../../api/endpoints';
import { SecureDataFields, fieldsToFormData, formDataToFields, type DataField } from '../../../components/workflows/SecureDataFields';

/**
 * Self-contained Input Data editor: owns its field state, dirty flag, and save.
 *
 * Always rendered inside a titled <Section> ("Input data" + description), so its
 * inner SecureDataFields header is suppressed to avoid a duplicate heading.
 */
export const InputDataCard: React.FC<{ workflow: any; onSaved?: () => void }> = ({ workflow, onSaved }) => {
  const { t } = useTranslation();
  const [fields, setFields] = React.useState<DataField[]>(() => formDataToFields(workflow.form_data || {}));
  const [dirty, setDirty] = React.useState(false);
  const [saving, setSaving] = React.useState(false);

  // Re-seed from the workflow whenever its saved data is replaced (a refetch after
  // save, or a different workflow). Adjusted DURING RENDER — React's
  // derive-state-from-props escape hatch — so the editor never paints one frame
  // of the previous values.
  const formData = workflow?.form_data;
  const [seededFrom, setSeededFrom] = React.useState(formData);
  if (seededFrom !== formData) {
    setSeededFrom(formData);
    if (formData) {
      setFields(formDataToFields(formData));
      setDirty(false);
    }
  }

  const handleSave = async () => {
    setSaving(true);
    try {
      await automationApi.updateWorkflow(workflow.id, { form_data: fieldsToFormData(fields) });
      toast.success(t('Input data saved'));
      setDirty(false);
      onSaved?.();
    } catch { toast.error(t('Failed to save')); }
    finally { setSaving(false); }
  };

  return (
    <div>
      <SecureDataFields
        fields={fields}
        onChange={(f) => { setFields(f); setDirty(true); }}
        label=""
        description=""
      />
      {dirty && (
        <div className="flex items-center justify-end mt-3 gap-2">
          <button onClick={() => { setFields(formDataToFields(workflow.form_data || {})); setDirty(false); }}
            className="px-3 py-1.5 text-[12px] font-medium text-secondary hover:text-ink rounded-lg hover:bg-chrome transition-colors">
            {t('Discard')}
          </button>
          <button onClick={handleSave} disabled={saving}
            className="px-3 py-1.5 text-[12px] font-semibold shadow-sm bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 transition-colors disabled:opacity-50">
            {saving ? t('Saving...') : t('Save')}
          </button>
        </div>
      )}
    </div>
  );
};
