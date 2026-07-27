import React from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { WrenchScrewdriverIcon, ChevronDownIcon } from '@heroicons/react/24/outline';
import { formatRelativeTime } from '../../../utils/format';

/** Heading line for a repair entry — recorded-step vs streaming script/function. */
const repairLabel = (repair: any, t: (k: string, o?: any) => string): string => {
  switch (repair.repair_type) {
    case 'script':
      return t('Streaming script repaired');
    case 'function':
      return repair.target_name
        ? t('Function repaired — {{name}}', { name: repair.target_name })
        : t('Function repaired');
    case 'simple':
      return t('Selector fixed — step {{n}}', { n: (repair.failed_step_index || 0) + 1 });
    default:
      return t('Flow repaired — step {{n}}', { n: (repair.failed_step_index || 0) + 1 });
  }
};

export const RepairHistory: React.FC<{ history: any[] }> = ({ history }) => {
  const { t } = useTranslation();
  const [expandedIdx, setExpandedIdx] = React.useState<number | null>(null);

  return (
    <div className="space-y-1">
      {history.slice().reverse().map((repair: any, idx: number) => {
        const isCodeRepair = repair.repair_type === 'script' || repair.repair_type === 'function';
        return (
        <div key={idx}>
          <button
            onClick={() => setExpandedIdx(expandedIdx === idx ? null : idx)}
            className="w-full flex items-center justify-between px-3 py-2.5 rounded-xl hover:bg-chrome transition-colors"
          >
            <div className="flex items-center gap-2">
              <WrenchScrewdriverIcon className="w-3.5 h-3.5 text-tertiary" />
              <span className="text-[13px] text-ink">{repairLabel(repair, t)}</span>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-[11px] text-tertiary">{repair.repaired_at ? formatRelativeTime(repair.repaired_at) : ''}</span>
              <ChevronDownIcon className={clsx('w-3.5 h-3.5 text-tertiary transition-transform duration-200', expandedIdx === idx && 'rotate-180')} />
            </div>
          </button>
          <div className={clsx('grid transition-all duration-200 ease-out', expandedIdx === idx ? 'grid-rows-[1fr] opacity-100' : 'grid-rows-[0fr] opacity-0')}>
            <div className="overflow-hidden">
              <div className="px-3 pb-3 pt-1 space-y-2">
                {repair.error_message && <p className="text-[11px] text-tertiary">{repair.error_message}</p>}
                {isCodeRepair && repair.explanation && (
                  <p className="text-[11px] text-secondary">{repair.explanation}</p>
                )}
                {isCodeRepair ? (
                  <div className="grid grid-cols-1 @split/stage:grid-cols-2 gap-3">
                    <div>
                      <p className="text-[10px] text-tertiary mb-1">{t('Before')}</p>
                      <pre className="bg-canvas rounded-lg p-2 max-h-48 overflow-auto text-[10px] font-mono text-secondary whitespace-pre-wrap break-all">
                        {repair.old_code || ''}
                      </pre>
                    </div>
                    <div>
                      <p className="text-[10px] text-tertiary mb-1">{t('After')}</p>
                      <pre className="bg-canvas rounded-lg p-2 max-h-48 overflow-auto text-[10px] font-mono text-ink whitespace-pre-wrap break-all">
                        {repair.new_code || ''}
                      </pre>
                    </div>
                  </div>
                ) : (
                  <div className="grid grid-cols-1 @split/stage:grid-cols-2 gap-3">
                    <div>
                      <p className="text-[10px] text-tertiary mb-1">{t('Before')}</p>
                      <div className="bg-canvas rounded-lg p-2 max-h-40 overflow-auto">
                        {(repair.old_steps || []).map((s: any, si: number) => (
                          <div key={si} className={clsx('text-[10px] font-mono py-0.5', si === repair.failed_step_index ? 'text-tertiary line-through' : 'text-secondary')}>
                            {si + 1}. {s.type} {s.selector || s.config?.selector || s.url || ''}
                          </div>
                        ))}
                      </div>
                    </div>
                    <div>
                      <p className="text-[10px] text-tertiary mb-1">{t('After')}</p>
                      <div className="bg-canvas rounded-lg p-2 max-h-40 overflow-auto">
                        {(repair.new_steps || []).map((s: any, si: number) => (
                          <div key={si} className="text-[10px] font-mono text-ink py-0.5">
                            {si + 1}. {s.type} {s.selector || s.config?.selector || s.url || ''}
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
        );
      })}
    </div>
  );
};
