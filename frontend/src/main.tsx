import React from 'react';
import ReactDOM from 'react-dom/client';
import toast from 'react-hot-toast';
import i18n, { i18nReady } from './i18n';
import App from './App';
import { ErrorBoundary } from './components/ErrorBoundary';
import './index.css';

// Focus modality — WebKit (the macOS WKWebView) matches :focus-visible on a
// pointer click, so tabindex list rows/controls get a stray offset focus outline
// after a MOUSE select (it can even linger on the previously keyboard-focused
// item). Track the input modality: `.using-mouse` on <html> while the pointer was
// the last input, removed on real keyboard navigation. index.css gates the focus
// outline on it so keyboard users still get a visible ring.
{
  const root = document.documentElement;
  const navKeys = new Set(['Tab', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'Enter', ' ', 'Escape']);
  root.classList.add('using-mouse');
  window.addEventListener('pointerdown', () => root.classList.add('using-mouse'), true);
  window.addEventListener('keydown', (e) => { if (navKeys.has(e.key)) root.classList.remove('using-mouse'); }, true);
}

// Promise rejections nothing awaited/caught (fire-and-forget API calls,
// missed .catch chains). Log always; toast at most once per 30s so a
// burst doesn't stack notifications.
let lastRejectionToast = 0;
window.addEventListener('unhandledrejection', (event) => {
  console.error('Unhandled promise rejection:', event.reason);
  const now = Date.now();
  if (now - lastRejectionToast > 30_000) {
    lastRejectionToast = now;
    const msg = event.reason?.response?.data?.detail
      || (typeof event.reason?.message === 'string' ? event.reason.message : null);
    toast.error(msg || i18n.t('An unexpected error occurred'));
  }
});

// Hold first paint until the active language dictionary is loaded (resolves
// instantly for English) so fr/es users don't see a flash of English keys.
i18nReady.finally(() => {
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <ErrorBoundary>
        <App />
      </ErrorBoundary>
    </React.StrictMode>
  );
});
