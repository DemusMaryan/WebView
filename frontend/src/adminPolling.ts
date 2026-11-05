export const bytesLabel = (value: number | null | undefined) => value == null ? 'недоступно' : `${(value / 1048576).toFixed(1)} MiB`;
export const cpuLabel = (value: number | null | undefined) => value == null ? 'недоступно' : `${value.toFixed(1)}%`;

type Clock = { later: (fn: () => void, ms: number) => unknown; cancel: (id: unknown) => void };
export function pollMonitor<T>(load: (signal: AbortSignal) => Promise<T>, accept: (value: T) => void,
  failure: (error: unknown) => boolean, clock: Clock = {
    later: (fn, ms) => setTimeout(fn, ms), cancel: id => clearTimeout(id as ReturnType<typeof setTimeout>),
  }) {
  let alive = true; let timer: unknown;
  const controller = new AbortController();
  const tick = async () => {
    try { const value = await load(controller.signal); if (alive) accept(value); }
    catch (error) { if (alive && !controller.signal.aborted && !failure(error)) alive = false; }
    if (alive) timer = clock.later(() => void tick(), 2000);
  };
  void tick();
  return () => { alive = false; controller.abort(); if (timer !== undefined) clock.cancel(timer); };
}
