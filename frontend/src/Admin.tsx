import { useEffect, useState } from 'react';
import { ApiError, json } from './api';
import type { User } from './Auth';
import { bytesLabel, cpuLabel, pollMonitor } from './adminPolling';
import { waitLabel } from './queue';

type Process = { host_pid: number; pid: number | null; pid_scope: string; pid_namespace: string;
  role: string; service: string; server_id: string | null; job_id: string | null; state: string;
  elapsed_seconds: number | null; cpu_percent: number | null; cpu_ticks: number; cpu_seconds: number; rss_bytes: number | null; metrics_status: string };
type Server = { server_id: string; kind: string; registry_state: string; registry_fresh: boolean;
  health: string; slot_limit: number; slot_used: boolean; job_id: string | null; job_state: string | null;
  processed_frames: number | null; total_frames: number | null };
type Monitor = { status: string; stale: boolean; age_seconds: number | null; sampled_at: string | null;
  environment: { cpu_count: number; cpu_percent: number | null; memory_total_bytes: number | null; memory_available_bytes: number | null } | null;
  services: { name: string; pid: number | null; running: boolean; health: string }[];
  servers: Server[]; queue: { job_id: string; state: string; queue_position: number | null; estimated_wait_seconds: number | null }[];
  queue_status?: string; capacity?: { base: number; maximum: number; ready: number | null; active: number | null };
  processes: Process[]; process_count?: number; processes_truncated?: boolean; scan_truncated?: boolean };

export function AdminPage({ user, logout }: { user: User; logout: () => Promise<void> }) {
  const [data, setData] = useState<Monitor | null>(null);
  const [error, setError] = useState('');
  useEffect(() => pollMonitor(signal => json<Monitor>('/api/admin/monitoring', { signal }),
    value => { setData(value); setError(''); }, failure => {
      setError(failure instanceof Error ? failure.message : 'Метрики недоступні');
      return !(failure instanceof ApiError && [401, 403].includes(failure.status));
    }), []);
  const env = data?.environment;
  return <>
    <header className="topbar"><a className="brand" href="/">MonoTrajectory<span>Web</span></a>
      <span>Адміністратор · {user.login}</span><a href="/">До задач</a><button className="quiet" onClick={() => void logout()}>Вийти</button></header>
    <main className="admin-layout">
      <section className="card"><span className="eyebrow">МОНІТОРИНГ</span><h1>Панель адміністратора</h1>
        <p>Тільки перегляд · оновлення приблизно раз на 2 секунди</p>
        <p data-testid="sample-time">Останній вимір: {data?.sampled_at ? new Date(data.sampled_at).toLocaleString('uk-UA') : 'недоступний'}
          {data?.age_seconds != null && ` · вік ${data.age_seconds.toFixed(1)} с`}</p>
        {(error || data?.stale) && <p className="error" role="alert">{error || 'Метрики застарілі або недоступні.'} Останні дані не підтверджують поточний стан.</p>}
        <div className="result-stats"><span>WSL CPU <strong>{cpuLabel(env?.cpu_percent)}</strong></span>
          <span>Логічних CPU <strong>{env?.cpu_count ?? 'недоступно'}</strong></span>
          <span>RAM доступно <strong>{bytesLabel(env?.memory_available_bytes)}</strong></span>
          <span>RAM усього <strong>{bytesLabel(env?.memory_total_bytes)}</strong></span>
          <span>Серверів ready / максимум <strong>{data?.capacity?.ready ?? '—'} / {data?.capacity?.maximum ?? '—'}</strong></span>
          <span>Зайнято слотів <strong>{data?.capacity?.active ?? 'недоступно'}</strong></span></div>
        <p className="muted">Загальний CPU: 100% усіх CPU WSL. CPU процесу: 100% одного логічного CPU, можливі значення понад 100%. RAM спільна для WSL і контейнера; RSS процесів не підсумовується. 1 MiB = 1 048 576 байтів.</p>
      </section>
      <section className="card"><h2>Служби проєкту</h2><div className="table-scroll"><table><thead><tr><th>Служба</th><th>Host PID</th><th>Процес</th><th>Health</th></tr></thead>
        <tbody>{data?.services.map(s => <tr key={s.name}><td>{s.name}</td><td>{s.pid ?? '—'}</td><td>{s.running ? 'наявний' : 'відсутній'}</td><td>{s.health}</td></tr>)}</tbody></table></div></section>
      <section className="card"><h2>Обчислювальні сервери</h2><div className="table-scroll"><table data-testid="servers"><thead><tr><th>Сервер</th><th>Тип</th><th>Registry / health</th><th>Слот</th><th>Задача / прогрес</th></tr></thead>
        <tbody>{data?.servers.map(s => <tr key={s.server_id}><td>{s.server_id}</td><td>{s.kind}</td><td>{s.registry_state}{s.registry_fresh ? '' : ' (не ready)'} / {s.health}</td>
          <td>{s.slot_used ? 'зайнятий' : 'вільний'} / {s.slot_limit}</td><td><code>{s.job_id ?? '—'}</code>{s.job_id && <p>{s.job_state} · {s.processed_frames ?? '—'} / {s.total_frames ?? '—'} кадрів</p>}</td></tr>)}</tbody></table></div></section>
      <section className="card"><h2>Черга</h2>{data?.queue_status !== 'healthy' ? <p>Стан черги недоступний.</p> : !data.queue.length ? <p>Черга порожня.</p> :
        <div className="table-scroll"><table><thead><tr><th>Позиція</th><th>Задача</th><th>Стан</th><th>Приблизне очікування</th></tr></thead><tbody>{data.queue.map(j =>
          <tr key={j.job_id}><td>{j.queue_position ?? '—'}</td><td><code>{j.job_id}</code></td><td>{j.state}</td><td>{waitLabel(j.estimated_wait_seconds)}</td></tr>)}</tbody></table></div>}</section>
      <section className="card"><h2>Процеси застосунку · {data?.process_count ?? '—'}</h2>
        {(data?.processes_truncated || data?.scan_truncated) && <p className="error">Перелік обмежено; показано не всі процеси.</p>}
        <div className="table-scroll"><table data-testid="processes"><thead><tr><th>Host PID / PID у просторі</th><th>Роль</th><th>Сервер / задача</th><th>Стан / час</th><th>CPU</th><th>RSS</th></tr></thead>
          <tbody>{data?.processes.map(p => <tr key={`${p.host_pid}:${p.role}`} data-role={p.role} data-job={p.job_id ?? ''}><td>{p.host_pid} / {p.pid ?? '—'}<small>{p.pid_scope}<br />{p.pid_namespace}</small></td>
            <td>{p.role}</td><td>{p.server_id ?? p.service}<small>{p.job_id ?? '—'}</small></td><td>{p.state} / {p.elapsed_seconds == null ? 'недоступно' : `${p.elapsed_seconds.toFixed(1)} с`}</td>
            <td>{cpuLabel(p.cpu_percent)}<small>{p.cpu_seconds.toFixed(2)} CPU-с</small></td><td>{bytesLabel(p.rss_bytes)}</td></tr>)}</tbody></table></div>
      </section>
    </main><footer>Захищений read-only моніторинг · WSL · localhost HTTPS</footer>
  </>;
}
