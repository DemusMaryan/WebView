// Explicit opt-in real Chrome checks. No mocked API responses or synthetic SLAM data.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { trajectoryLines } from '../src/data.ts';

const native = process.env.MTW_TEST_NATIVE ?? String.raw`\\wsl.localhost\Ubuntu\home\demus\.local\share\monotrajectoryweb`;
const { chromium } = await import(pathToFileURL(path.join(native, 'frontend-deps/node_modules/playwright-core/index.mjs')).href);
const output = path.join(native, 'web/reports/prompt4-browser');
const smoke = process.argv.includes('--smoke');
const reportPath = path.join(output, smoke ? 'smoke-report.json' : 'report.json');
const base = 'http://127.0.0.1:8080';
const video = path.join(native, 'datasets/freiburg1_xyz-rgb-30fps.mp4');
const calibration = path.join(native, 'datasets/freiburg1_xyz-rgb-30fps.parameters.json');
const report = { started_at: new Date().toISOString(), checks: {}, jobs: {}, observations: [], errors: [], blocked_external: [], passed: false };
const snapshots = new Map();
const requests = new Map();
const terminal = status => ['succeeded', 'failed', 'cancelled', 'timed_out'].includes(status);
const digest = data => createHash('sha256').update(data).digest('hex');
await fs.mkdir(output, { recursive: true });
const save = () => fs.writeFile(reportPath, JSON.stringify(report, null, 2));
const browser = await chromium.launch({ channel: 'chrome', headless: true });
report.browser = browser.version();
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, locale: 'uk-UA' });
await context.route('**/*', route => {
  const url = new URL(route.request().url());
  if (['127.0.0.1', 'localhost'].includes(url.hostname)) return route.continue();
  report.blocked_external.push(url.origin); return route.abort();
});
await context.addInitScript(() => {
  const seen = new WeakSet();
  const stats = { createdBuffers: 0, deletedBuffers: 0, liveBuffers: 0, contexts: 0, lostContexts: 0 };
  window.__mtwGL = stats;
  window.__mtwViewMismatches = [];
  new MutationObserver(() => {
    const id = document.querySelector('.job-id')?.textContent;
    const image = document.querySelector('.frame-host img');
    if (id && image && image.dataset.jobId !== id) window.__mtwViewMismatches.push({ id, imageJob: image.dataset.jobId });
  }).observe(document, { childList: true, subtree: true, attributes: true });
  const get = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (...args) {
    const gl = get.apply(this, args);
    if (gl && String(args[0]).startsWith('webgl') && !seen.has(gl)) {
      seen.add(gl); stats.contexts++;
      const buffers = new Set();
      const create = gl.createBuffer.bind(gl), remove = gl.deleteBuffer.bind(gl);
      gl.createBuffer = function () { const value = create(); if (value) { buffers.add(value); stats.createdBuffers++; stats.liveBuffers++; } return value; };
      gl.deleteBuffer = function (value) { if (buffers.delete(value)) { stats.deletedBuffers++; stats.liveBuffers--; } return remove(value); };
      this.addEventListener('webglcontextlost', () => { stats.lostContexts++; stats.liveBuffers -= buffers.size; buffers.clear(); });
    }
    return gl;
  };
});
const page = await context.newPage();
page.on('pageerror', error => { report.errors.push(error.message); });
page.on('request', request => {
  const pathname = new URL(request.url()).pathname;
  requests.set(pathname, (requests.get(pathname) ?? 0) + 1);
});
page.on('response', async response => {
  const match = new URL(response.url()).pathname.match(/^\/api\/jobs\/([^/]+)\/snapshot$/);
  if (!match || response.status() !== 200) return;
  try {
    const snapshot = await response.json();
    if (!snapshots.has(match[1])) snapshots.set(match[1], new Map());
    snapshots.get(match[1]).set(snapshot.frame_id, snapshot);
  } catch { /* A page exit is allowed to abort an in-flight response. */ }
});
const count = id => (requests.get(`/api/jobs/${id}`) ?? 0) + (requests.get(`/api/jobs/${id}/snapshot`) ?? 0);
const api = async (endpoint, options) => {
  const response = await context.request.get(base + endpoint, options);
  assert.equal(response.status(), 200, await response.text()); return response.json();
};
const screenshot = name => page.screenshot({ path: path.join(output, name), fullPage: true });
const display = () => page.evaluate(() => {
  const image = document.querySelector('.frame-host img');
  const number = selector => Number(document.querySelector(selector)?.textContent?.match(/\d+/)?.[0]);
  const map = document.querySelector('section[aria-label="Розріджена карта"]');
  return { frame_id: image ? Number(image.alt.match(/#(\d+)/)?.[1]) : null,
    caption_frame_id: number('.view-grid .viewer:first-child .viewer-caption strong'),
    image_complete: image?.complete ?? false, width: image?.naturalWidth ?? 0, height: image?.naturalHeight ?? 0,
    points: Number(map?.querySelector('.viewer-caption strong')?.textContent ?? 0),
    edges: Number(map?.querySelector('.viewer-caption span:last-child')?.textContent?.match(/\d+/)?.[0] ?? 0),
    heading: document.querySelector('.job-summary h1')?.textContent,
    canvases: document.querySelectorAll('canvas').length, gpu: { ...window.__mtwGL } };
});

async function prepare({ seconds = '', corrupt = false } = {}) {
  await page.locator('#video').setInputFiles(corrupt
    ? { name: 'corrupt.mp4', mimeType: 'video/mp4', buffer: Buffer.from('not a video\x00\x01') }
    : video);
  await page.locator('#calibration-file').setInputFiles([]);
  await page.locator('#calibration-file').setInputFiles(calibration);
  await page.waitForFunction(() => document.querySelector('#calibration').value.includes('517.306408'));
  await page.locator('#execution').fill(seconds);
}

async function submit(name, options) {
  await prepare(options);
  const response = page.waitForResponse(response => new URL(response.url()).pathname === '/api/jobs' && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Створити задачу', exact: true }).click();
  const created = await response; assert.equal(created.status(), 202, await created.text());
  const job = await created.json(); report.jobs[name] = { id: job.id, status: job.status }; await save();
  await page.waitForFunction(id => document.querySelector('.job-id')?.textContent === id, job.id);
  return job.id;
}

async function waitTerminal(id, expected) {
  await page.waitForFunction(status => document.querySelector('.job-summary h1')?.textContent === status, expected, { timeout: 200000 });
  const job = await api(`/api/jobs/${id}`); assert(terminal(job.status), JSON.stringify(job));
  report.jobs[Object.keys(report.jobs).find(key => report.jobs[key].id === id)] = { ...job };
  assert.equal(await page.getByText('WebGL 2 недоступний', { exact: false }).count(), 0);
  return job;
}

async function verifyCleanup(id) {
  const dir = path.join(native, 'web/jobs', id);
  const cleanup = JSON.parse(await fs.readFile(path.join(dir, 'frame-work-cleanup.json'), 'utf8'));
  assert(cleanup.applied && cleanup.process_group_empty, JSON.stringify(cleanup));
  await assert.rejects(fs.access(path.join(dir, 'sequence/rgb')));
  await fs.access(path.join(dir, 'input.video'));
  const snapshot = await api(`/api/jobs/${id}/snapshot`);
  if (snapshot.frame_id !== null) {
    const response = await context.request.get(base + snapshot.frame_url);
    assert.equal(response.status(), 200); assert.equal(response.headers()['x-frame-id'], String(snapshot.frame_id));
    assert((await response.body()).subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])));
  }
  report.checks[`cleanup_${id}`] = cleanup; await save(); return cleanup;
}

try {
  await page.goto(base);
  await page.getByRole('heading', { name: 'Нова задача', exact: true }).waitFor();
  assert.equal(await page.locator('#calibration').inputValue(), '', 'No implicit TUM calibration');
  const config = await api('/api/config');
  assert((await page.locator('.limits').innerText()).includes(String(config.limits.frames)));
  const existing = process.env.MTW_EXISTING_JOB ?? (await api('/api/jobs?limit=100')).find(job => job.status === 'succeeded')?.id;
  assert(existing, 'An existing successful fixture job is required for the initial read-only smoke check');
  await page.goto(base + '/#jobs/' + existing);
  await page.getByRole('heading', { name: 'Фінальний результат', exact: true }).waitFor({ timeout: 30000 });
  assert.equal(await page.getByText('WebGL 2 недоступний', { exact: false }).count(), 0);
  report.checks.existing_result = await display();
  assert.equal(report.checks.existing_result.width, 640);
  assert.equal(report.checks.existing_result.canvases, 1);
  await screenshot('existing-result.png');
  if (smoke) {
    await page.locator('#calibration-file').setInputFiles(calibration);
    await page.waitForFunction(() => document.querySelector('#calibration').value.includes('517.306408'));
    await page.locator('#calibration').fill('{bad');
    await page.locator('#calibration-file').setInputFiles(calibration);
    await page.waitForFunction(() => document.querySelector('#calibration').value.includes('517.306408'));
    report.checks.same_file_reimport = true;
    const previous = JSON.parse(await fs.readFile(path.join(output, 'report.json'), 'utf8'));
    report.checks.reopened_jobs = [];
    const headings = { succeeded: 'Готово', cancelled: 'Скасовано', failed: 'Помилка', timed_out: 'Перевищено час' };
    for (const job of Object.values(previous.jobs)) {
      await page.locator('.job-link').filter({ hasText: job.id.slice(0, 8) }).click();
      await page.getByRole('heading', { name: headings[job.status], exact: true }).waitFor();
      const shown = await display();
      assert.equal(shown.frame_id, shown.image_complete ? shown.caption_frame_id : null);
      assert.deepEqual(await page.evaluate(() => window.__mtwViewMismatches), []);
      report.checks.reopened_jobs.push({ id: job.id, status: job.status, frame_id: shown.frame_id });
    }
    const missing = '00000000-0000-0000-0000-000000000000';
    await page.goto(base + '/#jobs/' + missing);
    await page.getByRole('heading', { name: 'Стан недоступний', exact: true }).waitFor();
    assert((await page.locator('.job-summary [role="alert"]').innerText()).includes('HTTP 404'));
    const failedCount = count(missing); await page.waitForTimeout(1200);
    assert.equal(count(missing), failedCount);
    report.checks.missing_job_error_stops_polling = true;
  }
  if (!smoke) {
    await page.getByRole('button', { name: 'Закрити перегляд', exact: true }).click();
    await prepare();
    await page.locator('#calibration').fill('{bad');
    const admissions = requests.get('/api/jobs') ?? 0;
    await page.getByRole('button', { name: 'Створити задачу', exact: true }).click();
    await page.locator('.upload .error').waitFor();
    assert.equal(requests.get('/api/jobs') ?? 0, admissions, 'Invalid JSON must not create a job');
    report.checks.invalid_calibration = await page.locator('.upload .error').innerText();
    const success = await submit('success');
    let firstFrame = null, closeChecked = false;
    const deadline = Date.now() + 190000;
    while (Date.now() < deadline) {
      const shown = await display();
      const snapshot = snapshots.get(success)?.get(shown.frame_id);
      if (shown.image_complete && shown.width && snapshot && !terminal(snapshot.status) && shown.frame_id >= 20) {
        assert.equal(shown.frame_id, shown.caption_frame_id); assert.equal(shown.width, 640); assert.equal(shown.height, 480);
        assert.equal(shown.points, Math.min(5000, snapshot.points.length));
        assert.equal(shown.edges, trajectoryLines(snapshot.trajectory, snapshot.map_id, snapshot.segment_id).length / 2);
        if (firstFrame === null || shown.frame_id >= report.observations.at(-1).frame_id + 50) {
          const mapImage = await page.locator('section[aria-label="Розріджена карта"]').screenshot({ path: path.join(output, `live-${shown.frame_id}.png`) });
          report.observations.push({ ...shown, server_id: snapshot.server_id, tracking_state: snapshot.tracking_state,
            pose_valid: snapshot.pose_valid, map_id: snapshot.map_id, segment_id: snapshot.segment_id,
            points_sha256: digest(JSON.stringify(snapshot.points)), map_png_sha256: digest(mapImage) });
          if (firstFrame === null) firstFrame = shown.frame_id;
          await save();
        }
        if (!closeChecked && report.observations.length >= 2) {
          await page.getByRole('button', { name: 'Закрити перегляд', exact: true }).click();
          await page.waitForFunction(() => !document.querySelector('canvas') && window.__mtwGL.liveBuffers === 0);
          await page.waitForTimeout(250);
          const stopped = count(success);
          await page.waitForTimeout(1200); assert.equal(count(success), stopped);
          report.checks.close_active_view = { requests_stopped: true, ...(await display()).gpu };
          await page.locator('.job-link').filter({ hasText: success.slice(0, 8) }).click();
          closeChecked = true;
        }
      }
      if ((await page.locator('.job-summary h1').textContent()) === 'Готово') break;
      await page.waitForTimeout(250);
    }
    assert(report.observations.length >= 2 && closeChecked, 'Missing real progressive map observations');
    assert.notEqual(report.observations[0].map_png_sha256, report.observations[1].map_png_sha256);
    await waitTerminal(success, 'Готово');
    const result = await api(`/api/jobs/${success}/result`);
    assert.equal(result.processed_frames, 798); assert(result.valid_live_poses > 0 && result.points_exported > 0);
    report.checks.success_result = result;
    const cleanup = await verifyCleanup(success);
    assert.equal(cleanup.removed_work_files, 798);
    const oldFrame = report.observations[0].frame_id;
    assert.equal((await context.request.get(`${base}/api/jobs/${success}/frames/${oldFrame}`)).status(), 200);
    await page.waitForTimeout(250);
    const stopped = count(success); await page.waitForTimeout(1200); assert.equal(count(success), stopped);
    report.checks.terminal_polling_stopped = true;
    await screenshot('completed.png');
    await page.reload();
    await page.getByRole('heading', { name: 'Фінальний результат', exact: true }).waitFor();
    const reopened = await display();
    assert.equal(reopened.frame_id, reopened.caption_frame_id); assert.equal(reopened.width, 640);
    report.checks.reload_after_cleanup = reopened;
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await screenshot('mobile-result.png');
    report.checks.mobile = true;
    await page.setViewportSize({ width: 1440, height: 1100 });

    const cancel = await submit('cancel');
    await page.waitForFunction(() => Number(document.querySelector('.view-grid .viewer:first-child .viewer-caption strong')?.textContent) >= 20, null, { timeout: 120000 });
    const cancellation = page.waitForResponse(response => response.url().endsWith(`/api/jobs/${cancel}/cancel`));
    await page.getByRole('button', { name: 'Скасувати задачу', exact: true }).click();
    assert.equal((await cancellation).status(), 202);
    await waitTerminal(cancel, 'Скасовано'); await verifyCleanup(cancel);
    await page.reload(); await page.getByRole('heading', { name: 'Скасовано', exact: true }).waitFor();
    assert.equal((await display()).width, 640); await screenshot('cancelled.png');
    report.checks.cancel_preview_after_reload = true;

    const failed = await submit('invalid_video', { corrupt: true });
    const failedJob = await waitTerminal(failed, 'Помилка');
    assert(failedJob.error.includes('InvalidDataError'));
    await verifyCleanup(failed);
    assert.equal(await page.locator('progress').getAttribute('value'), '0');
    await screenshot('invalid-video.png');

    const timeout = await submit('timeout', { seconds: '1' });
    const timed = await waitTerminal(timeout, 'Перевищено час');
    assert.equal(timed.status, 'timed_out'); await verifyCleanup(timeout);
    assert.equal(await page.locator('progress').getAttribute('value'), '0');
    report.checks.raw_jsonl_never_polled = ![...requests].some(([url]) => url.endsWith('snapshots.jsonl'));
    assert(report.checks.raw_jsonl_never_polled);
  }
  assert.equal(report.errors.length, 0, report.errors.join('\n'));
  assert.equal(report.blocked_external.length, 0);
  report.passed = true;
} catch (error) {
  report.failure = error.stack ?? String(error);
  await screenshot('failure.png').catch(() => {});
  throw error;
} finally {
  report.finished_at = new Date().toISOString(); report.requests = Object.fromEntries(requests);
  await save(); await browser.close();
  console.log(JSON.stringify({ passed: report.passed, smoke, jobs: report.jobs, report: reportPath, failure: report.failure }, null, 2));
}
