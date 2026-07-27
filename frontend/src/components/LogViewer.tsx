import React from 'react';
import { useTranslation } from 'react-i18next';
import { LogEntry } from '../types/api';
import { formatDate } from '../utils/format';
import clsx from 'clsx';

interface LogViewerProps {
  logs: LogEntry[];
  className?: string;
}

const levelColors = {
  debug: 'text-tertiary bg-hover',
  info: 'text-ink bg-hover',
  warn: 'text-amber-700 bg-amber-50',
  error: 'text-red-700 bg-red-50',
};

export const LogViewer: React.FC<LogViewerProps> = ({ logs, className }) => {
  const { t } = useTranslation();
  if (logs.length === 0) {
    return (
      <div className={clsx('text-center py-8 text-sm text-secondary', className)}>
        {t('No logs to display')}
      </div>
    );
  }

  return (
    <div className={clsx('bg-surface rounded-lg border border-border overflow-hidden', className)}>
      <div className="overflow-x-auto">
        <div className="font-mono text-xs">
          {logs.map((log) => (
            <div
              key={log.id}
              className={clsx(
                'flex items-start gap-4 px-4 py-2 border-b border-border last:border-b-0 hover:bg-hover',
                levelColors[log.level]
              )}
            >
              <span className="text-tertiary whitespace-nowrap">
                {formatDate(log.timestamp)}
              </span>
              <span className="font-semibold uppercase w-12">
                {log.level}
              </span>
              {log.agentId && (
                <span className="text-tertiary truncate w-24" title={log.agentId}>
                  {log.agentId.substring(0, 8)}
                </span>
              )}
              <span className="flex-1 text-secondary">{log.message}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};
