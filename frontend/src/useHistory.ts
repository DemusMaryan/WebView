import { useCallback, useEffect, useState } from 'react';
import { json } from './api';
import type { JobPage } from './types';

export function useHistory() {
  const [page, setPage] = useState(1);
  const [version, setVersion] = useState(0);
  const [history, setHistory] = useState<JobPage | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  // Stable callback: history navigation must not restart useJob or its 3D view.
  const refresh = useCallback(() => setVersion(value => value + 1), []);
  const goToPage = useCallback((value: number) => setPage(value), []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void json<JobPage>(`/api/jobs?page=${page}&page_size=20`, { signal: controller.signal }).then(value => {
      if (controller.signal.aborted) return;
      const last = Math.max(1, value.total_pages);
      if (page > last) { setPage(last); return; }
      setHistory(value); setError('');
    }).catch(failure => {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : String(failure));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [page, version]);
  return { history, jobs: history?.items ?? [], error, loading, refresh, goToPage };
}
