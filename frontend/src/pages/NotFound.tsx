import React from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

export const NotFound: React.FC = () => {
  const { t } = useTranslation();
  return (
    <div className="min-h-screen bg-canvas flex items-center justify-center p-4">
      <div className="text-center">
        <p className="text-6xl font-semibold text-ink mb-4">404</p>
        <p className="text-secondary mb-6">{t("This page doesn't exist.")}</p>
        <Link
          to="/"
          className="inline-flex items-center gap-1.5 px-4 py-2 bg-accent-strong text-accent-on text-sm font-semibold rounded-lg shadow-sm hover:bg-accent-strong/90 transition-colors"
        >
          &larr; {t('Go home')}
        </Link>
      </div>
    </div>
  );
};
