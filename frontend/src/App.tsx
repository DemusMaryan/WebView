import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { ApiError, json } from './api';
import { parseCalibration } from './data';
import type { MapModel } from './MapView';
import type { Config, Job, JobStatus } from './types';
import { terminal } from './types';
import { useJob } from './useJob';
import { useHistory } from './useHistory';

const MapView = lazy(() => import('./MapView').then(module => ({ default: module.MapView })));

const labels: Record<JobStatus, string> = { queued: 'У черзі', dispatching: 'Призначення сервера', running: 'Обробка',
  succeeded: 'Готово', failed: 'Помилка', cancelled: 'Скасовано', timed_out: 'Перевищено час' };
const stages: Record<string, string> = { decoding: 'Декодування RGB-кадрів', initializing: 'Ініціалізація ядра',
  processing: 'Відстеження й карта', finalizing: 'Фінальна перевірка карти', completed: 'Обробку завершено' };
const tum = { width: 640, height: 480, fx: 517.306408, fy: 516.469215, cx: 318.643040, cy: 255.313989,
  k1: .262383, k2: -.953104, p1: -.005358, p2: .002628, k3: 1.163314,
  source: 'Official ORB-SLAM3 TUM1.yaml; freiburg1_xyz 640x480 fixture only' };
const currentId = () => {
  const value = window.location.hash.replace(/^#jobs\//, '');
  return /^[a-f0-9-]{36}$/i.test(value) ? value : null;
};
const message = (error: unknown) => error instanceof Error ? error.message : String(error);

export function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const { history, jobs, error: historyError, loading: historyLoading, refresh, goToPage } = useHistory();
  const [id, setId] = useState<string | null>(currentId);
  const [video, setVideo] = useState<File | null>(null);
  const [calibration, setCalibration] = useState('');
  const [execution, setExecution] = useState('');
  const [formError, setFormError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [cancelId, setCancelId] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const { job, view, error } = useJob(id, config?.poll_interval_ms ?? 500, retry, refresh);

  useEffect(() => {
    const controller = new AbortController();
    void json<Config>('/api/config', { signal: controller.signal }).then(setConfig).catch(error => {
      if (!controller.signal.aborted) setFormError(message(error));
    });
    const changed = () => { setId(currentId()); setCancelId(null); };
    window.addEventListener('hashchange', changed);
    return () => { controller.abort(); window.removeEventListener('hashchange', changed); };
  }, [refresh]);

  const selected = (value: string | null) => {
    window.location.hash = value ? `jobs/${value}` : '';
    setId(value); setCancelId(null);
  };
  const create = async (event: React.FormEvent) => {
    event.preventDefault(); setFormError('');
    if (!config || !video) { setFormError('Оберіть відеофайл.'); return; }
    if (video.size > config.limits.file_bytes) { setFormError('Відеофайл перевищує фактичний ліміт backend.'); return; }
    try {
      const parameters = parseCalibration(calibration);
      const duration = execution === '' ? config.limits.execution_seconds : Number(execution);
      if (!Number.isFinite(duration) || duration < 1 || duration > config.limits.execution_seconds) {
        throw new Error(`Ліміт виконання має бути від 1 до ${config.limits.execution_seconds} с.`);
      }
      const body = new FormData(); body.append('video', video);
      body.append('parameters', JSON.stringify({ ...parameters, execution_seconds: duration }));
      setSubmitting(true);
      const created = await json<Job>('/api/jobs', { method: 'POST', body });
      selected(created.id); goToPage(1); refresh();
    } catch (failure) {
      setFormError(message(failure));
      if (failure instanceof ApiError && failure.code === 'stored_job_limit_reached') refresh();
    }
    finally { setSubmitting(false); }
  };
  const cancel = async () => {
    if (!id) return;
    setCancelId(id); setFormError('');
    try { await json(`/api/jobs/${id}/cancel`, { method: 'POST' }); refresh(); }
    catch (failure) { setCancelId(null); setFormError(message(failure)); }
  };

  const model = useMemo<MapModel>(() => {
    if (!view) return { points: [], poses: [], camera: null, map: -1, segment: -1 };
    if (view.final) {
      const final = view.final;
      const pose = final.poses.find(pose => pose.frame_id === view.snapshot.frame_id
        && pose.map_id === final.map_id && pose.segment_id === final.segment_id);
      return { points: final.points, poses: final.poses, camera: pose?.Twc ?? null, map: final.map_id, segment: final.segment_id };
    }
    const snapshot = view.snapshot;
    return { points: snapshot.points, poses: snapshot.trajectory, camera: snapshot.pose_valid ? snapshot.Twc : null,
      map: snapshot.map_id ?? -1, segment: snapshot.segment_id ?? -1 };
  }, [view]);
  const snapshot = view?.snapshot;
  const storedLimit = history?.stored_job_limit ?? config?.limits.stored_jobs;
  const storedLimitReached = !!history && history.total >= history.stored_job_limit;
  const total = snapshot?.total_frames ?? job?.total_frames;
  const processed = snapshot?.processed_frames ?? job?.processed_frames ?? 0;
  const tracking = snapshot?.tracking_state;
  const trackingText = tracking === 2 ? 'Відстеження працює' : tracking === 1 ? 'Карта ще ініціалізується'
    : tracking === 3 ? 'Відстеження тимчасово втрачено' : tracking === 4 ? 'Відстеження втрачено'
      : tracking === 5 ? 'Відстеження KLT' : 'Оцінка пози ще недоступна';

  return <>
    <header className="topbar"><div><strong className="brand">MonoTrajectory<span>Web</span></strong>
      <p>Монокулярне відео → траєкторія та розріджена карта</p></div><span className="local-badge">Локальний прототип</span></header>
    <main className="layout">
      <aside className="sidebar">
        <section className="card upload"><h1>Нова задача</h1><p className="muted">Записане RGB-відео та параметри його камери.</p>
          <form onSubmit={create}>
            <label htmlFor="video">Відеофайл</label>
            <input id="video" type="file" accept="video/*,.mkv,.avi" required onChange={event => setVideo(event.target.files?.[0] ?? null)} />
            <label htmlFor="calibration">Калібрування камери · JSON</label>
            <textarea id="calibration" value={calibration} onChange={event => setCalibration(event.target.value)} required rows={5}
              placeholder="Вставте перевірені width, height, fx, fy, cx, cy, k1, k2, p1, p2, k3 та source" />
            <label className="file-label" htmlFor="calibration-file">Або завантажте JSON-файл</label>
            <input id="calibration-file" type="file" accept="application/json,.json" onChange={async event => {
              const input = event.currentTarget;
              const file = input.files?.[0]; input.value = '';
              try {
                if (file && file.size <= 8192) { setCalibration(await file.text()); setFormError(''); }
                else if (file) setFormError('Файл калібрування має бути не більшим за 8 KiB.');
              } catch (failure) { setFormError(message(failure)); }
            }} />
            <button type="button" className="quiet profile" onClick={() => setCalibration(JSON.stringify(tum, null, 2))}>Профіль TUM freiburg1_xyz · 640×480</button>
            <p className="hint">Цей профіль лише для перевіреного TUM-ролика. Для іншої камери потрібне її власне калібрування.</p>
            <label htmlFor="execution">Ліміт виконання, с</label>
            <input id="execution" type="number" min="1" max={config?.limits.execution_seconds ?? 180} value={execution}
              placeholder={String(config?.limits.execution_seconds ?? '')} onChange={event => setExecution(event.target.value)} />
            {config && <p className="limits">Ліміти backend: {config.limits.file_bytes / 1048576} MiB · {config.limits.duration_seconds} с відео · {config.limits.frames} кадрів · {config.limits.width}×{config.limits.height}. Execution включає декодування.</p>}
            {formError && <p className="error" role="alert">{formError}</p>}
            {storedLimit !== undefined && <p className="stored-limit">Ліміт збережених задач: <strong>{storedLimit}</strong></p>}
            {storedLimitReached && <p className="error" role="status">Досягнуто ліміт збережених задач. Історія та результати доступні; нові задачі зараз не приймаються.</p>}
            <button className="primary" disabled={!config || submitting || storedLimitReached}>{submitting ? 'Завантаження…' : 'Створити задачу'}</button>
          </form>
        </section>
        <section className="card history"><div className="card-heading"><h2>Історія задач</h2><button className="quiet" onClick={refresh}>Оновити список</button></div>
          {history && <p className="history-total">Збережено {history.total} із {history.stored_job_limit}</p>}
          {historyError && <p className="error" role="alert">{historyError}</p>}
          <ul>{jobs.map(listed => { const item = job?.id === listed.id ? job : listed; return <li key={item.id}><button className={item.id === id ? 'job-link selected' : 'job-link'} onClick={() => selected(item.id)}>
            <span><code>{item.id.slice(0, 8)}</code><span className={`status ${item.status}`}>{labels[item.status]}</span></span>
            <small>{new Date(item.created_at).toLocaleString('uk-UA')} · {item.processed_frames} кадрів</small></button></li>; })}</ul>
          {!jobs.length && !historyLoading && !historyError && <p className="muted">Збережених задач поки немає.</p>}
          {historyLoading && <p className="muted" role="status">Оновлення історії…</p>}
          {history && <nav className="pagination" aria-label="Сторінки історії">
            <button className="quiet" disabled={historyLoading || !history.has_previous} onClick={() => goToPage(history.page - 1)}>Попередня</button>
            <span aria-live="polite">{history.total_pages ? `Сторінка ${history.page} із ${history.total_pages}` : 'Сторінок немає'}</span>
            <button className="quiet" disabled={historyLoading || !history.has_next} onClick={() => goToPage(history.page + 1)}>Наступна</button>
          </nav>}
        </section>
      </aside>
      <div className="workspace">
        {!id ? <section className="card empty"><h2>Почніть із відео</h2><p>Створіть задачу або відкрийте збережений результат з історії.</p><p className="muted">Тут з’являються тільки справжні дані обчислення. Масштаб — відносний.</p></section>
          : <>
            <section className="card job-summary"><div className="card-heading"><div><span className="eyebrow">ПОТОЧНА ЗАДАЧА</span><h1>{job ? labels[job.status] : error ? 'Стан недоступний' : 'Завантаження стану…'}</h1></div><button className="quiet" onClick={() => selected(null)}>Закрити перегляд</button></div>
              <code className="job-id">{id}</code>
              {error ? <div className="error" role="alert"><p>{error}</p><p>Опитування зупинено. Останній показаний стан може бути застарілим.</p><button onClick={() => setRetry(value => value + 1)}>Поновити стан</button></div>
                : <><p>{job ? stages[job.stage] ?? labels[job.status] : 'З’єднання з API'} {job?.server_id && <span className="server">{job.server_id}</span>}</p>
                  <progress max={total ?? 1} value={total ? processed : job && terminal(job.status) ? 0 : undefined} aria-label="Фактично оброблені кадри" />
                  <div className="progress-caption"><span>{processed} / {total ?? 'ще невідомо'} кадрів</span>{total ? <strong>{(processed / total * 100).toFixed(1)}%</strong> : <span>Декодовано: {snapshot?.decoded_frames ?? 0}</span>}</div></>}
              {job?.error && <p className="error" role="alert">{job.error}</p>}
              {job && !terminal(job.status) && <button className="danger" disabled={cancelId === id || job.cancel_requested} onClick={cancel}>
                {cancelId === id || job.cancel_requested ? 'Запит скасування надіслано' : 'Скасувати задачу'}</button>}
            </section>
            <div className="view-grid">
              <section className="card viewer"><div className="card-heading"><h2>Оброблений RGB-кадр</h2><span className="frame-label">Кадр {snapshot?.frame_id === null || !snapshot ? '—' : `#${snapshot.frame_id}`}</span></div>
                <div className="frame-host">{view?.imageUrl ? <img src={view.imageUrl} data-job-id={snapshot?.job_id} alt={`Оброблений RGB-кадр #${snapshot?.frame_id}`} /> : <p>{job && terminal(job.status) ? 'Для цієї задачі оброблений кадр не отримано.' : 'Кадр з’явиться після початку tracking.'}</p>}</div>
                <div className="viewer-caption"><span>Snapshot frame_id: <strong>{snapshot?.frame_id ?? '—'}</strong></span><span>{snapshot?.timestamp !== null && snapshot?.timestamp !== undefined ? `${snapshot.timestamp.toFixed(3)} с` : '—'}</span></div>
                <p className="tracking">{job && terminal(job.status) ? 'Останній стан: ' : ''}{trackingText}{snapshot && !snapshot.pose_valid && tracking === 2 ? '; валідна поза цього кадру недоступна' : ''}.</p>
              </section>
              <Suspense fallback={<section className="card viewer"><p>Завантаження 3D-перегляду…</p></section>}><MapView key={id} model={model} /></Suspense>
            </div>
            <section className="card metadata"><div className="card-heading"><h2>{view?.final ? 'Фінальний результат' : 'Дані поточного snapshot'}</h2><span>Карта {model.map < 0 ? '—' : model.map} · сегмент {model.segment < 0 ? '—' : model.segment}</span></div>
              <p className="muted">Показано одну систему координат. Null-пози й пропуски не інтерполюються. Одиниці відносні, без метричного масштабу.</p>
              {snapshot && <p>Камера: {model.camera ? 'поза доступна для показаного кадру' : view?.final ? 'фінальна поза показаного кадру відсутня' : 'валідна поза недоступна'}.</p>}
              {view?.final && <><div className="result-stats"><span>Кадри <strong>{view.final.manifest.processed_frames}</strong></span><span>Валідні live-пози <strong>{view.final.manifest.valid_live_poses}</strong></span><span>Фінальні пози <strong>{view.final.manifest.final_poses}</strong></span><span>Експортовані точки <strong>{view.final.manifest.points_exported}</strong></span></div>
                <p>Ліміт експорту точок: {view.final.manifest.points_limit}; обрізання: {view.final.manifest.points_truncated ? 'так' : 'ні'}. Час worker: {view.final.manifest.worker_wall_seconds.toFixed(2)} с, включно з декодуванням та експортом.</p>
                <div className="downloads">{Object.entries(view.final.manifest.artifacts).map(([name, url]) => <a key={name} href={url} download>{name}</a>)}</div></>}
              {model.camera && <details><summary>Twc показаного кадру · координати ядра</summary><pre>{model.camera.map(row => row.map(value => value.toFixed(5)).join('  ')).join('\n')}</pre></details>}
            </section>
          </>}
      </div>
    </main>
    <footer>Тільки localhost · авторизація та HTTPS заплановані · до двох задач одночасно</footer>
  </>;
}
