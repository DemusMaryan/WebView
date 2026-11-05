import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, json, setCsrf } from './api';

export type User = { id: string; login: string; is_admin: boolean };
type Session = { user: User | null; csrf_token: string };

export function AuthGate({ children }: { children: (user: User, logout: () => Promise<void>) => React.ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [login, setLogin] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const pending = useRef<Promise<void> | null>(null);
  const accept = (value: Session) => { setCsrf(value.csrf_token); setSession(value); };
  const bootstrap = useCallback(() => {
    if (pending.current) return pending.current;
    pending.current = (async () => {
      try { accept(await json<Session>('/api/auth/session')); setError(''); }
      catch { setError('Не вдалося отримати сесію. Перевірте доступність API.'); }
      finally { pending.current = null; }
    })();
    return pending.current;
  }, []);
  useEffect(() => {
    void bootstrap();
    const expired = () => { setSession(null); setCsrf(null); void bootstrap(); };
    window.addEventListener('mtw-auth-expired', expired);
    return () => { window.removeEventListener('mtw-auth-expired', expired); setCsrf(null); };
  }, [bootstrap]);
  const logout = async () => {
    setBusy(true); setError('');
    try {
      await json('/api/auth/logout', { method: 'POST' });
      window.location.hash = ''; setSession(null); setCsrf(null); setPassword('');
      await bootstrap();
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) {
        window.location.hash = ''; setSession(null); setCsrf(null); await bootstrap();
      } else setError('Не вдалося завершити сесію. Повторіть вихід.');
    }
    finally { setBusy(false); }
  };
  if (session?.user) return <>{error && <p className="error auth-error" role="alert">{error}</p>}{children(session.user, logout)}</>;
  return <main className="auth-page"><section className="card auth-card"><strong className="brand">MonoTrajectory<span>Web</span></strong>
    <h1>{mode === 'login' ? 'Вхід' : 'Реєстрація'}</h1><p>Локальний обліковий запис. Задачі доступні лише їхньому власнику.</p>
    <form onSubmit={async event => {
      event.preventDefault(); setBusy(true); setError('');
      try {
        const value = await json<Session>(`/api/auth/${mode}`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ login, password }) });
        setPassword(''); window.location.hash = ''; accept(value);
      } catch { setPassword(''); setError(mode === 'login' ? 'Вхід не виконано. Перевірте логін і пароль; за потреби оновіть сторінку.' : 'Реєстрацію не виконано. Логін може бути зайнятий; перевірте формат даних.'); }
      finally { setBusy(false); }
    }}>
      <label htmlFor="login">Логін</label><input id="login" value={login} onChange={e => setLogin(e.target.value)} required minLength={3} maxLength={32} autoComplete="username" />
      <label htmlFor="password">Пароль</label><input id="password" type="password" value={password} onChange={e => setPassword(e.target.value)} required minLength={12} maxLength={128} autoComplete={mode === 'login' ? 'current-password' : 'new-password'} />
      <p className="hint">Логін: 3–32 символи a–z, 0–9, _, . або -. Пароль: 12–128 символів. Локальний застосунок працює через HTTPS.</p>
      {error && <p className="error" role="alert">{error}</p>}
      <button className="primary" disabled={!session || busy}>{busy ? 'Обробка…' : mode === 'login' ? 'Увійти' : 'Зареєструватися'}</button>
    </form>
    <button className="quiet" disabled={busy} onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setError(''); setPassword(''); }}>{mode === 'login' ? 'Створити обліковий запис' : 'Маю обліковий запис'}</button>
    {!session && <button className="quiet" onClick={() => void bootstrap()}>Повторити з’єднання</button>}
  </section></main>;
}
