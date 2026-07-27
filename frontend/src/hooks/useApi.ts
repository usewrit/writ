import { useState, useCallback } from 'react';
import i18n from '../i18n';

interface ApiState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

export const useApi = <T,>(apiFunction: (...args: any[]) => Promise<T>) => {
  const [state, setState] = useState<ApiState<T>>({
    data: null,
    loading: false,
    error: null,
  });

  const execute = useCallback(
    async (...args: any[]) => {
      setState({ data: null, loading: true, error: null });

      try {
        const data = await apiFunction(...args);
        setState({ data, loading: false, error: null });
        return { data, error: null };
      } catch (error: any) {
        const errorMessage = error.response?.data?.error || error.message || i18n.t('An error occurred');
        setState({ data: null, loading: false, error: errorMessage });
        return { data: null, error: errorMessage };
      }
    },
    [apiFunction]
  );

  return {
    ...state,
    execute,
  };
};
