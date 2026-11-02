import { useEffect, useState } from 'react';
import { json, loadFinal, loadFrame } from './api';
import type { Job, Presentation, Snapshot } from './types';
import { terminal } from './types';

export function useJob(id: string | null, interval: number, retry: number, onFinish: () => void) {
  const [job, setJob] = useState<Job | null>(null);
  const [view, setView] = useState<Presentation | null>(null);
  const [error, setError] = useState<{ id: string; message: string } | null>(null);

  useEffect(() => {
    setJob(null); setView(null); setError(null);
    if (!id) return;
    let disposed = false, finished = false, busy = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let request: AbortController | undefined;
    let generation = 0;
    const tick = async () => {
      if (disposed || finished || busy || document.hidden) return;
      busy = true;
      const current = ++generation;
      const controller = new AbortController(); request = controller;
      const started = performance.now();
      let imageUrl: string | null = null;
      try {
        const status = await json<Job>(`/api/jobs/${id}`, { signal: controller.signal });
        const snapshot = await json<Snapshot>(`/api/jobs/${id}/snapshot`, { signal: controller.signal });
        if (snapshot.job_id !== id) throw new Error('Snapshot належить іншій задачі.');
        imageUrl = await loadFrame(snapshot, controller.signal);
        const final = status.status === 'succeeded' ? await loadFinal(id, controller.signal) : null;
        if (disposed || current !== generation || controller.signal.aborted) {
          if (imageUrl) URL.revokeObjectURL(imageUrl); return;
        }
        // A decoded image and its snapshot enter React state together, never independently.
        setJob(status); setView({ snapshot, imageUrl, final }); imageUrl = null;
        if (terminal(status.status)) { finished = true; onFinish(); }
      } catch (failure) {
        if (imageUrl) URL.revokeObjectURL(imageUrl);
        if (!controller.signal.aborted && !disposed) {
          setError({ id, message: failure instanceof Error ? failure.message : String(failure) }); finished = true;
        }
      } finally {
        busy = false;
        if (!disposed && !finished && !document.hidden) timer = setTimeout(tick, Math.max(0, interval - (performance.now() - started)));
      }
    };
    const stop = () => { ++generation; clearTimeout(timer); request?.abort(); };
    const visibility = () => { if (document.hidden) stop(); else if (!finished) void tick(); };
    const leaving = () => { disposed = true; stop(); };
    const returning = () => { disposed = false; if (!finished) void tick(); };
    document.addEventListener('visibilitychange', visibility);
    window.addEventListener('pagehide', leaving);
    window.addEventListener('pageshow', returning);
    void tick();
    return () => { leaving(); document.removeEventListener('visibilitychange', visibility); window.removeEventListener('pagehide', leaving); window.removeEventListener('pageshow', returning); };
  }, [id, interval, retry, onFinish]);

  useEffect(() => () => { if (view?.imageUrl) URL.revokeObjectURL(view.imageUrl); }, [view?.imageUrl]);
  // Do not paint the previous job's frame/map during the render before effect cleanup.
  return { job: job?.id === id ? job : null, view: view?.snapshot.job_id === id ? view : null,
    error: error?.id === id ? error.message : '' };
}
