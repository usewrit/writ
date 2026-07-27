import React, { useState } from 'react';
import {
  CheckCircleIcon,
  XCircleIcon,
  ArrowPathIcon,
  ClockIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  PhotoIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { Button } from '../../ui/Button';

interface TestExecutionPanelProps {
  status: 'idle' | 'running' | 'success' | 'failed';
  durationMs: number | null;
  extractedData: Record<string, any> | null;
  error: string | null;
  screenshots?: Array<{ step: number; action: string; data_base64: string }>;
  selectorResults?: Array<{ selectorId: string; name?: string; matched: boolean; content: string }>;
  onRunTest: () => void;
  runLabel?: string;
}

const formatDuration = (ms: number): string => {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const min = Math.floor(ms / 60000);
  const sec = ((ms % 60000) / 1000).toFixed(0);
  return `${min}m ${sec}s`;
};

export const TestExecutionPanel: React.FC<TestExecutionPanelProps> = ({
  status,
  durationMs,
  extractedData,
  error,
  screenshots = [],
  selectorResults = [],
  onRunTest,
  runLabel,
}) => {
  const { t } = useTranslation();
  const [showData, setShowData] = useState(false);
  const [showScreenshots, setShowScreenshots] = useState(false);
  const [selectedScreenshot, setSelectedScreenshot] = useState(0);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <Button
          variant="primary"
          size="md"
          onClick={onRunTest}
          loading={status === 'running'}
        >
          {status === 'running' ? t('Running...') : (runLabel || t('Run Test'))}
        </Button>

        {status !== 'idle' && (
          <div className="flex items-center gap-3">
            {status === 'success' && (
              <span className="inline-flex items-center gap-1.5 text-sm font-medium text-success-fg">
                <CheckCircleIcon className="w-4 h-4" />
                {t('Passed')}
              </span>
            )}
            {status === 'failed' && (
              <span className="inline-flex items-center gap-1.5 text-sm font-medium text-danger">
                <XCircleIcon className="w-4 h-4" />
                {t('Failed')}
              </span>
            )}
            {status === 'running' && (
              <span className="inline-flex items-center gap-1.5 text-sm text-secondary">
                <ArrowPathIcon className="w-4 h-4 animate-spin" />
                {t('Executing...')}
              </span>
            )}
            {durationMs !== null && (
              <span className="inline-flex items-center gap-1 text-sm text-tertiary">
                <ClockIcon className="w-3.5 h-3.5" />
                {formatDuration(durationMs)}
              </span>
            )}
          </div>
        )}
      </div>

      {error && (
        <div className="p-3 bg-danger-bg border border-danger rounded-lg">
          <p className="text-sm text-danger-fg">{error}</p>
        </div>
      )}

      {selectorResults.length > 0 && (
        <div className="space-y-2">
          <p className="text-sm font-medium text-ink">{t('Selector Results')}</p>
          {selectorResults.map((r, i) => (
            <div
              key={i}
              className={clsx(
                'p-3 rounded-lg border text-sm',
                r.matched ? 'bg-success-bg border-success' : 'bg-danger-bg border-danger',
              )}
            >
              <div className="flex items-center gap-2 mb-1">
                {r.matched ? (
                  <CheckCircleIcon className="w-4 h-4 text-success" />
                ) : (
                  <XCircleIcon className="w-4 h-4 text-danger" />
                )}
                <span className="font-medium text-ink">{r.name || t('Selector {{n}}', { n: i + 1 })}</span>
              </div>
              {r.content && (
                <pre className="text-xs text-secondary mt-1 whitespace-pre-wrap max-h-24 overflow-y-auto font-mono bg-surface rounded p-2 border border-border">
                  {r.content.substring(0, 500)}
                </pre>
              )}
            </div>
          ))}
        </div>
      )}

      {extractedData && Object.keys(extractedData).length > 0 && (
        <div>
          <button
            onClick={() => setShowData(!showData)}
            className="flex items-center gap-1.5 text-sm font-medium text-ink hover:text-secondary transition-colors"
          >
            {showData ? <ChevronUpIcon className="w-3.5 h-3.5" /> : <ChevronDownIcon className="w-3.5 h-3.5" />}
            {t('Extracted Data ({{n}} fields)', { n: Object.keys(extractedData).length })}
          </button>
          {showData && (
            <pre className="mt-2 p-3 bg-surface border border-border rounded-lg text-xs font-mono text-secondary overflow-auto max-h-60">
              {JSON.stringify(extractedData, null, 2)}
            </pre>
          )}
        </div>
      )}

      {screenshots.length > 0 && (
        <div>
          <button
            onClick={() => setShowScreenshots(!showScreenshots)}
            className="flex items-center gap-1.5 text-sm font-medium text-ink hover:text-secondary transition-colors"
          >
            <PhotoIcon className="w-3.5 h-3.5" />
            {showScreenshots ? <ChevronUpIcon className="w-3.5 h-3.5" /> : <ChevronDownIcon className="w-3.5 h-3.5" />}
            {t('Screenshots ({{n}})', { n: screenshots.length })}
          </button>
          {showScreenshots && (
            <div className="mt-2 space-y-2">
              <div className="flex gap-2 overflow-x-auto pb-2">
                {screenshots.map((s, i) => (
                  <button
                    key={i}
                    onClick={() => setSelectedScreenshot(i)}
                    className={clsx(
                      'flex-shrink-0 w-20 h-14 rounded-lg border overflow-hidden',
                      selectedScreenshot === i ? 'border-ink ring-2 ring-ink/10' : 'border-border',
                    )}
                  >
                    <img src={`data:image/jpeg;base64,${s.data_base64}`} alt={t('Step {{n}}', { n: s.step })} className="w-full h-full object-cover" />
                  </button>
                ))}
              </div>
              {screenshots[selectedScreenshot] && (
                <div className="border border-border rounded-lg overflow-hidden">
                  <div className="px-3 py-1.5 bg-hover text-xs text-secondary border-b border-border">
                    {t('Step {{n}}:', { n: screenshots[selectedScreenshot].step })} {screenshots[selectedScreenshot].action}
                  </div>
                  <img src={`data:image/jpeg;base64,${screenshots[selectedScreenshot].data_base64}`} alt={t('Screenshot')} className="w-full" />
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
};
