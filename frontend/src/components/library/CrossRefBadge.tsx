import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

interface FlowRef {
  id: number;
  name: string;
}

interface CrossRefBadgeProps {
  flows: FlowRef[];
}

export const CrossRefBadge: React.FC<CrossRefBadgeProps> = ({ flows }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [showTooltip, setShowTooltip] = useState(false);

  if (flows.length === 0) return null;

  return (
    <div className="relative inline-block">
      <button
        type="button"
        onMouseEnter={() => setShowTooltip(true)}
        onMouseLeave={() => setShowTooltip(false)}
        className="px-2 py-0.5 rounded-full bg-ink/5 text-ink text-[10px] font-medium hover:bg-ink/10 transition-colors"
      >
        {flows.length === 1 ? t('Used by {{n}} automation', { n: flows.length }) : t('Used by {{n}} automations', { n: flows.length })}
      </button>

      {showTooltip && (
        <div className="absolute left-0 top-full mt-1 z-20 w-48 bg-surface border border-border rounded-lg shadow-lg p-2">
          {flows.map(f => (
            <button
              key={f.id}
              onClick={() => navigate(`/automations/${f.id}`)}
              className="w-full text-left px-2 py-1.5 text-xs text-ink hover:bg-hover rounded transition-colors truncate"
            >
              {f.name || t('Untitled')}
            </button>
          ))}
        </div>
      )}
    </div>
  );
};

export function computeFlowRefs(
  allFlows: any[],
  matchFn: (blocks: any[]) => boolean,
): FlowRef[] {
  return allFlows
    .filter(f => f.blocks && matchFn(f.blocks))
    .map(f => ({ id: f.id, name: f.name }));
}
