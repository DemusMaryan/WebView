// Two isolated contexts against the disposable localhost API. No SLAM/dispatcher.
import assert from 'node:assert/strict';
import { createHmac, randomBytes } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const native = String.raw`\\wsl.localhost\Ubuntu\home\demus\.local\share\monotrajectoryweb`;
const root = process.argv[2];
assert.match(root ?? '', /^\/home\/demus\/\.local\/share\/monotrajectoryweb\/web\/reports\/auth-browser-[0-9a-f]{32}$/);
const directory = path.join(native, root.split('/monotrajectoryweb/')[1]);
const fixture = JSON.parse(await fs.readFile(path.join(directory, 'credentials.json'), 'utf8'));
const fixtureSeed = await fs.readFile(path.join(directory, 'fixture-seed'));
for (const user of Object.values(fixture.users)) user.password = createHmac('sha256', fixtureSeed).update(user.login).digest('hex');
const base = fixture.base;
assert.equal(base, 'https://127.0.0.1:8444');
const sensitive = new Set(Object.values(fixture.users).map(user => user.password));
const clean = value => { let text = String(value); for (const secret of sensitive) text = text.replaceAll(secret, '[redacted]'); return text; };
const { chromium } = await import(pathToFileURL(path.join(native, 'frontend-deps/node_modules/playwright-core/index.mjs')).href);
const output = path.join(native, 'web/reports/prompt9-browser');
await fs.mkdir(output, { recursive: true });
const report = { started_at: new Date().toISOString(), base, passed: false, checks: {}, statuses: {},
  fixture_jobs: Object.fromEntries(Object.entries(fixture.users).map(([k,v]) => [k, { job_id: v.job_id, source_job_id: v.source_job_id }])),
  errors: [], blocked: [], mixed_content: [], requests: [], ignoreHTTPSErrors: true };
const browser = await chromium.launch({ channel: 'chrome', headless: true });
report.browser = browser.version();
const trustProbe = await browser.newContext({ ignoreHTTPSErrors: false });
const trustPage = await trustProbe.newPage();
try {
  await trustPage.goto(base);
  report.default_browser_trust = { trusted: true };
} catch (error) {
  report.default_browser_trust = { trusted: false, error: String(error.message).split('\n')[0] };
}
await trustProbe.close();
const a = await browser.newContext({ viewport: { width: 1440, height: 1100 }, locale: 'uk-UA', ignoreHTTPSErrors: true });
const b = await browser.newContext({ viewport: { width: 1440, height: 1100 }, locale: 'uk-UA', ignoreHTTPSErrors: true });
for (const context of [a,b]) {
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin === base) return route.continue();
    report.blocked.push(clean(route.request().url())); return route.abort();
  });
}
const pa = await a.newPage(), pb = await b.newPage();
for (const page of [pa,pb]) {
  page.on('pageerror', error => report.errors.push(clean(error.message)));
  page.on('console', message => { if (/mixed content/i.test(message.text())) report.mixed_content.push(clean(message.text())); });
  page.on('request', request => {
    if (request.url().startsWith(base + '/api/')) report.requests.push({ method: request.method(), path: new URL(request.url()).pathname });
  });
}
async function signIn(page, user) {
  await page.goto(base);
  await page.locator('#login').fill(user.login);
  await page.locator('#password').fill(user.password);
  await page.getByRole('button', { name: 'Увійти', exact: true }).click();
  await page.waitForFunction(login => document.querySelector('.current-user')?.textContent === login, user.login);
  await page.waitForFunction(() => document.querySelector('.history-total') !== null);
}
async function result(page, user) {
  await page.waitForFunction(({ id, frames }) => document.querySelector('.job-id')?.textContent === id
    && document.querySelector('.result-stats strong')?.textContent === String(frames)
    && document.querySelector('.frame-host img')?.complete, { id: user.job_id, frames: user.frames });
  assert.equal(await page.locator('.frame-host img').getAttribute('data-job-id'), user.job_id);
  await page.locator('.map-host canvas').waitFor();
}
try {
  report.statuses.anonymous_jobs = (await a.request.get(base + '/api/jobs')).status();
  assert.equal(report.statuses.anonymous_jobs, 401);
  await pa.goto(base);
  const wrong = randomBytes(24).toString('hex'); sensitive.add(wrong);
  await pa.locator('#login').fill(fixture.users.a.login);
  await pa.locator('#password').fill(wrong);
  await pa.getByRole('button', { name: 'Увійти', exact: true }).click();
  await pa.locator('.auth-card [role="alert"]').waitFor();
  assert.equal(await pa.locator('#password').inputValue(), '');
  report.checks.wrong_password_error_visible_and_input_cleared = true;
  await Promise.all([signIn(pa, fixture.users.a), signIn(pb, fixture.users.b)]);
  const [ha,hb] = await Promise.all([a.request.get(base + '/api/jobs').then(r=>r.json()), b.request.get(base + '/api/jobs').then(r=>r.json())]);
  assert.equal(ha.total, 21); assert.equal(hb.total, 1);
  assert.ok(ha.items.every(job => job.id !== fixture.users.b.job_id));
  assert.ok(hb.items.every(job => job.id !== fixture.users.a.job_id));
  assert.equal(ha.stored_job_limit, 100);
  assert.equal(await pa.locator('.stored-limit strong').innerText(), '100');
  await Promise.all([
    pa.locator('.history .job-link').filter({ hasText: fixture.users.a.job_id.slice(0,8) }).click(),
    pb.locator('.history .job-link').filter({ hasText: fixture.users.b.job_id.slice(0,8) }).click(),
  ]);
  await Promise.all([result(pa,fixture.users.a),result(pb,fixture.users.b)]);
  for (const page of [pa,pb]) {
    const unsafe = await page.evaluate(() => [...document.querySelectorAll('script[src],link[href],img[src],a[href]')]
      .map(element => element.getAttribute('src') ?? element.getAttribute('href'))
      .filter(value => value && new URL(value, location.href).protocol === 'http:'));
    assert.deepEqual(unsafe, []);
    assert.ok((await page.locator('.frame-host img').getAttribute('src')).startsWith('blob:https://'));
  }
  const resultRequests = report.requests.filter(r => r.path === `/api/jobs/${fixture.users.a.job_id}/result`).length;
  await pa.getByRole('button', { name: 'Наступна', exact: true }).click();
  await pa.waitForFunction(() => document.querySelector('.pagination')?.textContent?.includes('Сторінка 2 із 2')
    && !document.querySelector('.history [role="status"]'));
  assert.equal(await pa.locator('.history .job-link').count(), 1);
  await result(pa, fixture.users.a);
  await Promise.all([pa.waitForResponse(r => r.url().includes('/api/jobs?page=2&') && r.status() === 200),
    pa.getByRole('button', { name: 'Оновити список', exact: true }).click()]);
  await result(pa,fixture.users.a);
  assert.equal(report.requests.filter(r => r.path === `/api/jobs/${fixture.users.a.job_id}/result`).length, resultRequests);
  await pa.screenshot({ path: path.join(output,'owner-a-result.png'), fullPage: true });
  await pb.screenshot({ path: path.join(output,'owner-b-result.png'), fullPage: true });
  await pa.reload();
  await result(pa,fixture.users.a);
  report.checks.own_history_pagination_result_preview_refresh_and_reload = true;

  const snapshot = await a.request.get(`${base}/api/jobs/${fixture.users.a.job_id}/snapshot`).then(r=>r.json());
  const foreign = ['', '/snapshot', `/frames/${snapshot.frame_id}`, '/video', '/result', '/artifacts/trajectory.json'];
  for (const suffix of foreign) {
    const status = (await b.request.get(`${base}/api/jobs/${fixture.users.a.job_id}${suffix}`)).status();
    assert.equal(status,404); report.statuses[`foreign${suffix || '/state'}`] = status;
  }
  for (const context of [a,b]) assert.equal((await context.request.get(`${base}/api/jobs/${fixture.legacy_job_id}`)).status(),404);
  const session = await b.request.get(base + '/api/auth/session').then(r=>r.json());
  sensitive.add(session.csrf_token);
  const headers = { Origin: base, 'X-CSRF-Token': session.csrf_token };
  report.statuses.foreign_cancel = (await b.request.post(`${base}/api/jobs/${fixture.users.a.job_id}/cancel`, { headers })).status();
  assert.equal(report.statuses.foreign_cancel,404);
  report.statuses.missing_csrf = (await b.request.post(`${base}/api/jobs/${fixture.users.b.job_id}/cancel`, { headers: { Origin:base } })).status();
  assert.equal(report.statuses.missing_csrf,403);
  report.statuses.cross_origin = (await b.request.post(`${base}/api/jobs/${fixture.users.b.job_id}/cancel`,
    { headers: { ...headers, Origin:'http://localhost:9999' } })).status();
  assert.equal(report.statuses.cross_origin,403);
  report.statuses.own_terminal_cancel = (await b.request.post(`${base}/api/jobs/${fixture.users.b.job_id}/cancel`, { headers })).status();
  assert.equal(report.statuses.own_terminal_cancel,202);
  report.checks.foreign_routes_and_legacy_hidden = true;
  report.checks.csrf_and_origin_enforced = true;
  const ca = (await a.cookies()).find(c=>c.name==='mtw_session'), cb = (await b.cookies()).find(c=>c.name==='mtw_session');
  sensitive.add(ca.value); sensitive.add(cb.value);
  assert.ok(ca.httpOnly && cb.httpOnly && ca.sameSite === 'Strict' && cb.sameSite === 'Strict' && ca.secure && cb.secure);
  assert.ok(ca.value !== cb.value);
  assert.equal(await pa.evaluate(() => document.cookie.includes('mtw_session')), false);
  report.checks.independent_secure_httponly_strict_cookies = true;
  await pa.getByRole('button', { name: 'Вийти', exact: true }).click();
  await pa.locator('.auth-card h1').waitFor();
  assert.equal((await a.request.get(base + '/api/jobs')).status(),401);
  assert.equal((await b.request.get(`${base}/api/jobs/${fixture.users.b.job_id}`)).status(),200);
  await pa.getByRole('button', { name: 'Створити обліковий запис', exact: true }).click();
  const fresh = { login: `ui_${randomBytes(6).toString('hex')}`, password:randomBytes(24).toString('hex') };
  sensitive.add(fresh.password);
  await pa.locator('#login').fill(fresh.login); await pa.locator('#password').fill(fresh.password);
  await pa.getByRole('button', { name: 'Зареєструватися', exact: true }).click();
  await pa.waitForFunction(login => document.querySelector('.current-user')?.textContent === login, fresh.login);
  await pa.waitForFunction(() => document.querySelector('.history-total')?.textContent?.includes('Ваших задач: 0'));
  assert.equal((await a.request.get(base + '/api/jobs').then(r=>r.json())).total,0);
  assert.equal((await a.request.get(`${base}/api/jobs/${fixture.legacy_job_id}`)).status(),404);
  report.checks.ui_registration_logout_and_no_automatic_legacy_assignment = true;
  assert.deepEqual(report.errors,[]); assert.deepEqual(report.blocked,[]);
  assert.deepEqual(report.mixed_content,[]);
  report.checks.no_mixed_content = true;
  assert.ok(report.requests.every(r => !r.path.includes('/execute')));
  report.passed = true;
} catch (error) {
  report.errors.push(clean(error.stack ?? error));
  throw new Error(clean(error.message));
} finally {
  report.finished_at = new Date().toISOString();
  await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));
  await a.close(); await b.close(); await browser.close();
  console.log(JSON.stringify({ passed:report.passed, checks:report.checks, statuses:report.statuses, errors:report.errors }));
}
