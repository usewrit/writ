import { useState, useCallback, useEffect, useRef } from 'react';
import i18n from '../i18n';
import { useQueryCache } from '../stores/queryCache';

interface UseMutationOptions {
  invalidate?: string[];
  invalidateMatching?: string[];
  onSuccess?: (data: any) => void;
  onError?: (error: string) => void;
}

export function useMutation<TArgs extends any[], TResult = any>(
  mutationFn: (...args: TArgs) => Promise<TResult>,
  options: UseMutationOptions = {}
) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const optionsRef = useRef(options);
  // Mirror the latest options AFTER commit, never during render: writing a ref
  // in the render path is a render-time mutation React may tear on. `execute` is
  // only ever called from an event handler / async continuation, so the mirror
  // is always current by the time it is read.
  useEffect(() => {
    optionsRef.current = options;
  });

  const loadingRef = useRef(false);

  const execute = useCallback(async (...args: TArgs): Promise<{ data: TResult | null; error: string | null }> => {
    if (loadingRef.current) return { data: null, error: i18n.t('Already in progress') };
    loadingRef.current = true;
    setLoading(true);
    setError(null);

    try {
      const data = await mutationFn(...args);

      const opts = optionsRef.current;
      const store = useQueryCache.getState();
      if (opts.invalidate?.length) store.invalidate(...opts.invalidate);
      if (opts.invalidateMatching?.length) {
        for (const pattern of opts.invalidateMatching) store.invalidateMatching(pattern);
      }

      opts.onSuccess?.(data);
      return { data, error: null };
    } catch (err: any) {
      const msg = err?.response?.data?.error || err?.message || i18n.t('An error occurred');
      setError(msg);
      optionsRef.current.onError?.(msg);
      return { data: null, error: msg };
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }, [mutationFn]);

  return { execute, loading, error };
}
