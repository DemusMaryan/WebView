// Read-only acceptance against existing local jobs; never starts computation.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const native = process.env.MTW_TEST_NATIVE ?? String.raw`\\wsl.localhost\Ubuntu\home\demus\.local\share\monotrajectoryweb`;
const { chromium } = await import(pathToFileURL(path.join(native, 'frontend-deps/node_modules/playwright-core/index.mjs')).href);
const output = path.join(native, 'web/reports/prompt7-browser');
const base = 'http://127.0.0.1:8080';
const report = { started_at: new Date().toISOString(), passed: false, checks: {}, pages: [], results: [],
  requests: [], blocked: [], errors: [] };
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: 'chrome', headless: true });
report.browser = browser.version();
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, locale: 'uk-UA' });
await context.route('**/*', route => {
  const request = route.request(), url = new URL(request.url());
  if (['localhost', '127.0.0.1'].includes(url.hostname) && request.method() === 'GET') return route.continue();
  report.blocked.push({ method: request.method(), url: request.url() });
  return route.abort();
});
const page = await context.newPage();
page.on('pageerror', error => report.errors.push(error.message));
page.on('request', request => {
  if (request.url().startsWith(base + '/api/')) report.requests.push({ method: request.method(), path: new URL(request.url()).pathname });
});
async function get(url) {
  assert.ok(url.startsWith('/api/'));
  const response = await context.request.get(base + url);
  assert.equal(response.status(), 200, url);
  return response.json();
}
async function historyPage(number) {
  await page.waitForFunction(number => {
    const nav = document.querySelector('.pagination');
    return nav?.textContent?.includes(`Сторінка ${number} із`) && !document.querySelector('.history [role="status"]');
  }, number);
}
async function ready(id, manifest) {
  await page.waitForFunction(({ id, frames }) => document.querySelector('.job-id')?.textContent === id
    && document.querySelector('.result-stats strong')?.textContent === String(frames)
    && document.querySelector('.frame-host img')?.complete, { id, frames: manifest.processed_frames });
  const values = await page.locator('.result-stats strong').allTextContents();
  assert.deepEqual(values.map(Number), [manifest.processed_frames, manifest.valid_live_poses, manifest.final_poses, manifest.points_exported]);
  assert.equal(await page.locator('.frame-host img').getAttribute('data-job-id'), id);
  assert.equal(await page.locator('.job-summary h1').innerText(), 'Готово');
  assert.ok(await page.locator('.map-host canvas').count() || await page.locator('.viewer canvas').count());
}
try {
  const initial = await get('/api/jobs');
  assert.equal(initial.page_size, 20);
  assert.equal(initial.stored_job_limit, 100);
  const pages = [];
  for (let n = 1; n <= initial.total_pages; n++) {
    const value = await get(`/api/jobs?page=${n}&page_size=20`);
    pages.push(value);
    report.pages.push({ page: n, count: value.items.length, ids: value.items.map(item => item.id) });
  }
  const ids = pages.flatMap(value => value.items.map(item => item.id));
  assert.equal(ids.length, initial.total);
  assert.equal(new Set(ids).size, initial.total);
  await page.goto(base);
  await historyPage(1);
  assert.equal(await page.locator('.history-total').innerText(), `Збережено ${initial.total} із 100`);
  assert.equal(await page.locator('.stored-limit strong').innerText(), '100');
  assert.equal(await page.getByRole('button', { name: 'Попередня', exact: true }).isDisabled(), true);
  const first = pages[0].items.find(item => item.status === 'succeeded' && item.has_result);
  assert.ok(first, 'An existing result is needed on the first page');
  const firstManifest = await get(`/api/jobs/${first.id}/result`);
  await page.locator('.history .job-link').filter({ hasText: first.id.slice(0, 8) }).click();
  await ready(first.id, firstManifest);
  const initialResultRequests = report.requests.filter(r => r.path === `/api/jobs/${first.id}/result`).length;
  for (let n = 2; n <= pages.length; n++) {
    await page.getByRole('button', { name: 'Наступна', exact: true }).click();
    await historyPage(n);
    const shown = await page.locator('.history .job-link code').allTextContents();
    assert.deepEqual(shown, pages[n - 1].items.map(item => item.id.slice(0, 8)));
    await ready(first.id, firstManifest);
  }
  assert.equal(await page.getByRole('button', { name: 'Наступна', exact: true }).isDisabled(), true);
  await page.getByRole('button', { name: 'Оновити список', exact: true }).click();
  await historyPage(pages.length);
  await ready(first.id, firstManifest);
  assert.equal(report.requests.filter(r => r.path === `/api/jobs/${first.id}/result`).length, initialResultRequests,
    'History navigation/refresh must not reset the selected result or restart its requests');
  report.checks.selected_job_survives_navigation_and_refresh = true;
  report.results.push({ job_id: first.id, frames: firstManifest.processed_frames, points: firstManifest.points_exported });

  const old = pages.at(-1).items.find(item => item.status === 'succeeded' && item.has_result);
  assert.ok(old, 'An existing result is needed on the last history page');
  const oldManifest = await get(`/api/jobs/${old.id}/result`);
  await page.locator('.history .job-link').filter({ hasText: old.id.slice(0, 8) }).click();
  await ready(old.id, oldManifest);
  await page.screenshot({ path: path.join(output, 'history-old-result.png'), fullPage: true });
  await page.reload();
  await historyPage(1);
  await ready(old.id, oldManifest);
  assert.ok(page.url().endsWith(`#jobs/${old.id}`));
  report.checks.old_result_and_preview_survive_reload = true;
  report.results.push({ job_id: old.id, frames: oldManifest.processed_frames, points: oldManifest.points_exported });
  // Exercise reverse navigation too; keep the same old result open.
  await page.getByRole('button', { name: 'Наступна', exact: true }).click();
  await historyPage(2);
  await page.getByRole('button', { name: 'Попередня', exact: true }).click();
  await historyPage(1);
  await ready(old.id, oldManifest);
  report.checks.both_navigation_directions = true;
  report.checks.all_pages_unique = true;
  report.checks.limit_visible = true;
  const final = await get('/api/jobs');
  assert.equal(final.total, initial.total);
  assert.ok(report.requests.every(r => r.method === 'GET'));
  assert.deepEqual(report.blocked, []);
  assert.deepEqual(report.errors, []);
  report.checks.no_mutating_requests = true;
  report.total_before = initial.total;
  report.total_after = final.total;
  report.stored_job_limit = final.stored_job_limit;
  report.passed = true;
} catch (error) {
  report.errors.push(error.stack ?? String(error));
  await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {});
  throw error;
} finally {
  report.finished_at = new Date().toISOString();
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
  await context.close();
  await browser.close();
  console.log(JSON.stringify({ passed: report.passed, total: report.total_after, checks: report.checks, errors: report.errors }));
}
