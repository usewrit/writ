import { useEffect } from 'react';

const BRAND = 'writ';

/**
 * Sets the browser tab title for the current page and restores the bare brand
 * title on unmount. Pass a short page name (e.g. "Runs", "Workflows"); the
 * brand suffix is appended automatically. Pass an empty string for the Home
 * page to show just the brand.
 */
export function useDocumentTitle(title?: string): void {
  useEffect(() => {
    const previous = document.title;
    document.title = title ? `${title} · ${BRAND}` : BRAND;
    return () => {
      document.title = previous;
    };
  }, [title]);
}

export default useDocumentTitle;
