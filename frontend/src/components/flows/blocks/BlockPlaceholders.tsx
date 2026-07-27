import React from 'react';
import { Disclosure } from '@headlessui/react';
import { ChevronDownIcon, InformationCircleIcon } from '@heroicons/react/24/outline';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { FlowBlock } from '../types';
import { getAncestorChain } from '../FlowBuilderContext';

interface BlockPlaceholderHintsProps {
  block: FlowBlock;
  blocks: FlowBlock[];
}

export const BlockPlaceholderHints: React.FC<BlockPlaceholderHintsProps> = ({ block, blocks }) => {
  const { t } = useTranslation();
  const ancestors = getAncestorChain(blocks, block.id);
  const hasChangeDetected = ancestors.some(b => b.blockType === 'change_detected');
  const hasAiSession = ancestors.some(b => ['ai_session', 'ai_session_completed', 'ai_session_started'].includes(b.blockType));
  const hasWorkflow = ancestors.some(b => ['workflow', 'workflow_completed', 'workflow_started'].includes(b.blockType));

  return (
    <Disclosure>
      {({ open }) => (
        <div className="mt-3 border-t border-border/50 pt-2">
          <Disclosure.Button className="w-full flex items-center justify-between text-[10px] text-tertiary hover:text-ink">
            <span className="flex items-center gap-1">
              <InformationCircleIcon className="h-3 w-3" />
              {t('Available Placeholders')}
            </span>
            <ChevronDownIcon className={clsx('h-3 w-3 transition-transform', open && 'rotate-180')} />
          </Disclosure.Button>
          <Disclosure.Panel className="mt-2 space-y-2 text-[10px]">
            <div>
              <p className="text-tertiary mb-1">{t('General:')}</p>
              <div className="flex flex-wrap gap-1">
                {['{{now}}', '{{now_time}}', '{{now_date}}', '{{target_name}}', '{{target_url}}'].map(p => (
                  <code key={p} className="bg-hover text-ink px-1 py-0.5 rounded">{p}</code>
                ))}
              </div>
            </div>

            {hasChangeDetected && (
              <div>
                <p className="text-tertiary mb-1">{t('From Content Change:')}</p>
                <div className="flex flex-wrap gap-1">
                  {['{{content}}', '{{diff_snippet}}', '{{extracted.*}}', '{{selector_name}}', '{{change_detected_at}}'].map(p => (
                    <code key={p} className="bg-hover text-ink px-1 py-0.5 rounded">{p}</code>
                  ))}
                </div>
              </div>
            )}

            {hasAiSession && (
              <div>
                <p className="text-tertiary mb-1">{t('From AI Session:')}</p>
                <div className="flex flex-wrap gap-1">
                  {['{{session_name}}', '{{session_status}}', '{{success}}', '{{session_steps_taken}}', '{{session_duration_seconds}}', '{{session_error}}', '{{ai_result.*}}'].map(p => (
                    <code key={p} className="bg-hover text-ink px-1 py-0.5 rounded">{p}</code>
                  ))}
                </div>
              </div>
            )}

            {hasWorkflow && (
              <div>
                <p className="text-tertiary mb-1">{t('From Workflow:')}</p>
                <div className="flex flex-wrap gap-1">
                  {['{{workflow_name}}', '{{workflow_status}}', '{{success}}', '{{workflow_duration_seconds}}', '{{workflow_error}}', '{{result.*}}'].map(p => (
                    <code key={p} className="bg-hover text-ink px-1 py-0.5 rounded">{p}</code>
                  ))}
                </div>
              </div>
            )}

            <p className="text-tertiary italic">{t('Data from all ancestor blocks flows down.')}</p>
          </Disclosure.Panel>
        </div>
      )}
    </Disclosure>
  );
};
