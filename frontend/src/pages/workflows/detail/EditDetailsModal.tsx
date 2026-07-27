import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import { automationApi } from '../../../api/endpoints';
import { Modal } from '../../../components/ui/Modal';

// Edit details modal (name / description / entry URL).
// Steps themselves are edited in the Steps tab — this only covers workflow metadata.
export const EditDetailsModal: React.FC<{ workflow: any; isOpen: boolean; onClose: () => void; onSaved: () => void }> = ({ workflow, isOpen, onClose, onSaved }) => {
  const { t } = useTranslation();
  const [name, setName] = useState(workflow.name || '');
  const [description, setDescription] = useState(workflow.description || '');
  const [entryUrl, setEntryUrl] = useState(workflow.entry_url || '');
  const [saving, setSaving] = useState(false);

  // Re-seed the fields from the workflow each time the modal opens (and if the
  // workflow object itself is replaced under it). Adjusted DURING RENDER —
  // React's derive-state-from-props escape hatch — because the modal is already
  // on screen the frame `isOpen` flips: an effect would show the stale values
  // for one frame. The key goes null while closed so reopening re-seeds.
  const seedKey = isOpen ? workflow : null;
  const [seededFrom, setSeededFrom] = useState<any>(seedKey);
  if (seededFrom !== seedKey) {
    setSeededFrom(seedKey);
    if (isOpen) {
      setName(workflow.name || '');
      setDescription(workflow.description || '');
      setEntryUrl(workflow.entry_url || '');
    }
  }

  const handleSave = async () => {
    if (!name.trim()) { toast.error(t('Name is required')); return; }
    setSaving(true);
    try {
      await automationApi.updateWorkflow(workflow.id, {
        name: name.trim(),
        description: description.trim() || undefined,
        entry_url: entryUrl.trim() || undefined,
      } as any);
      toast.success(t('Workflow updated'));
      onSaved();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('Failed to save'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={t('Edit Workflow')} size="sm">
      <div className="space-y-4">
        <div>
          <label className="text-xs text-tertiary block mb-1.5">{t('Name')}</label>
          <input
            value={name}
            onChange={e => setName(e.target.value)}
            className="w-full px-3 py-2 text-sm bg-canvas border border-border rounded-lg outline-none focus:border-ink/30 focus:ring-2 focus:ring-ink/5"
            placeholder={t('Workflow name')}
          />
        </div>
        <div>
          <label className="text-xs text-tertiary block mb-1.5">{t('Description')}</label>
          <textarea
            value={description}
            onChange={e => setDescription(e.target.value)}
            rows={2}
            className="w-full px-3 py-2 text-sm bg-canvas border border-border rounded-lg outline-none focus:border-ink/30 focus:ring-2 focus:ring-ink/5 resize-none"
            placeholder={t('What does this workflow do?')}
          />
        </div>
        <div>
          <label className="text-xs text-tertiary block mb-1.5">{t('Entry URL')}</label>
          <input
            value={entryUrl}
            onChange={e => setEntryUrl(e.target.value)}
            className="w-full px-3 py-2 text-sm font-mono bg-canvas border border-border rounded-lg outline-none focus:border-ink/30 focus:ring-2 focus:ring-ink/5"
            placeholder="https://..."
          />
        </div>
        <div className="flex items-center justify-end gap-2 pt-1">
          <button onClick={onClose} className="px-4 py-2 text-sm font-medium text-secondary hover:text-ink rounded-lg hover:bg-chrome transition-colors">
            {t('Cancel')}
          </button>
          <button onClick={handleSave} disabled={saving}
            className="px-4 py-2 text-sm font-semibold shadow-sm bg-accent-strong text-accent-on rounded-lg hover:bg-accent-strong/90 transition-colors disabled:opacity-50">
            {saving ? t('Saving...') : t('Save')}
          </button>
        </div>
      </div>
    </Modal>
  );
};
