import { useEffect, useRef, useCallback, useState } from 'react';
import i18n from '../i18n';

export const usePolling = <T,>(
  fetchFunction: () => Promise<T>,
  interval: number = 5000,
  enabled: boolean = true
) => {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const timeoutRef = useRef<number | null>(null);
  const isMountedRef = useRef(true);
  const fetchFunctionRef = useRef(fetchFunction);

  // Update ref when function changes, but don't trigger re-render
  useEffect(() => {
    fetchFunctionRef.current = fetchFunction;
  }, [fetchFunction]);

  const fetchData = useCallback(async () => {
    try {
      const result = await fetchFunctionRef.current();
      if (isMountedRef.current) {
        setData(result);
        setError(null);
        setLoading(false);
      }
    } catch (err: any) {
      if (isMountedRef.current) {
        setError(err.message || i18n.t('An error occurred'));
        setLoading(false);
      }
    }
  }, []); // Remove fetchFunction from deps

  useEffect(() => {
    isMountedRef.current = true;
    let pausedByVisibility = false;

    if (!enabled) {
      return;
    }

    // Initial fetch
    fetchData();

    // Set up polling
    const poll = () => {
      timeoutRef.current = setTimeout(async () => {
        await fetchData();
        if (isMountedRef.current && enabled && !pausedByVisibility) {
          poll();
        }
      }, interval) as unknown as number;
    };

    poll();

    // Pause polling when tab is hidden, resume when visible
    const handleVisibility = () => {
      if (document.hidden) {
        pausedByVisibility = true;
        if (timeoutRef.current) {
          clearTimeout(timeoutRef.current);
          timeoutRef.current = null;
        }
      } else {
        pausedByVisibility = false;
        if (isMountedRef.current && enabled) {
          fetchData(); // Catch up immediately
          poll();
        }
      }
    };

    document.addEventListener('visibilitychange', handleVisibility);

    return () => {
      isMountedRef.current = false;
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
      }
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  }, [fetchData, interval, enabled]);

  const refresh = useCallback(() => {
    fetchData();
  }, [fetchData]);

  return { data, error, loading, refresh };
};
