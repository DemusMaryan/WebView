import type { FinalData, Manifest, Point, Pose, Snapshot } from './types';

let csrf: string | null = null;
export const setCsrf = (value: string | null) => { csrf = value; };

export class ApiError extends Error {
  status: number;
  code: string | null;
  constructor(status: number, code: string | null, message: string) {
    super(message); this.name = 'ApiError'; this.status = status; this.code = code;
  }
}

export async function json<T>(url: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (!['GET', 'HEAD', 'OPTIONS'].includes((options.method ?? 'GET').toUpperCase()) && csrf) headers.set('X-CSRF-Token', csrf);
  const response = await fetch(url, { cache: 'no-store', credentials: 'same-origin', ...options, headers });
  if (!response.ok) {
    const text = await response.text();
    let message = text;
    let code: string | null = null;
    try {
      const detail = JSON.parse(text).detail;
      if (typeof detail?.code === 'string') code = detail.code;
      message = Array.isArray(detail)
        ? detail.map((row: { loc?: unknown[]; msg: string }) => `${row.loc?.join('.') ?? ''}: ${row.msg}`).join('; ')
        : typeof detail === 'string' ? detail : detail?.message ?? JSON.stringify(detail) ?? text;
      if (code === 'stored_job_limit_reached') {
        message = `Досягнуто ліміт збережених задач: ${detail.count} із ${detail.limit}. Нові задачі зараз не приймаються. Історія та результати залишаються доступними.`;
      } else if (code === 'pending_job_limit_reached') {
        message = `Досягнуто ліміт незавершених задач: ${detail.count} із ${detail.limit}. Дочекайтеся завершення або скасуйте поточну задачу.`;
      }
    } catch { /* Nginx may return a short HTML body, e.g. HTTP 413. */ }
    if (response.status === 413) message = 'Файл або тіло запиту перевищує дозволений розмір.';
    if (response.status === 401 && !url.startsWith('/api/auth/') && typeof window !== 'undefined') window.dispatchEvent(new Event('mtw-auth-expired'));
    throw new ApiError(response.status, code, `HTTP ${response.status}: ${message.slice(0, 1600)}`);
  }
  return response.status === 204 ? undefined as T : response.json();
}

export async function loadFrame(snapshot: Snapshot, signal: AbortSignal): Promise<string | null> {
  if (snapshot.frame_id === null || snapshot.frame_url === null) return null;
  const expected = `/api/jobs/${snapshot.job_id}/frames/${snapshot.frame_id}`;
  if (snapshot.frame_url !== expected) throw new Error('Некоректне посилання на кадр snapshot.');
  const response = await fetch(expected, { signal, cache: 'no-store' });
  if (!response.ok || response.headers.get('X-Frame-Id') !== String(snapshot.frame_id)) {
    throw new Error(`Не вдалося отримати узгоджений кадр #${snapshot.frame_id} (HTTP ${response.status}).`);
  }
  const url = URL.createObjectURL(await response.blob());
  try {
    const image = new Image(); image.src = url;
    await image.decode();
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
    return url;
  } catch (error) { URL.revokeObjectURL(url); throw error; }
}

export async function loadFinal(id: string, signal: AbortSignal): Promise<FinalData> {
  const manifest = await json<Manifest>(`/api/jobs/${id}/result`, { signal });
  const artifact = (name: string) => {
    const expected = `/api/jobs/${id}/artifacts/${name}`;
    if (manifest.artifacts[name] !== expected) throw new Error('Некоректне посилання на результат.');
    return expected;
  };
  const [points, trajectory] = await Promise.all([
    json<{ points: Point[]; map_id: number; segment_id: number }>(artifact('map_points.json'), { signal }),
    json<{ poses: Pose[]; map_id: number; segment_id: number }>(artifact('trajectory.json'), { signal }),
  ]);
  if (points.map_id !== trajectory.map_id || points.segment_id !== trajectory.segment_id) {
    throw new Error('Фінальні точки й траєкторія мають різні системи координат.');
  }
  if (points.points.length > 100000 || trajectory.poses.length > 1200) throw new Error('Перевищено контракт розміру результату.');
  return { points: points.points, poses: trajectory.poses, map_id: points.map_id, segment_id: points.segment_id, manifest };
}
