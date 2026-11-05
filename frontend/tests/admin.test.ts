import test from 'node:test';
import assert from 'node:assert/strict';
import { pollMonitor, bytesLabel, cpuLabel } from '../src/adminPolling.ts';

test('monitoring preserves unavailable metrics and uses binary memory units', () => {
  assert.equal(bytesLabel(null), 'недоступно'); assert.equal(cpuLabel(undefined), 'недоступно');
  assert.equal(bytesLabel(1048576), '1.0 MiB'); assert.equal(cpuLabel(250), '250.0%');
  assert.equal(cpuLabel(0), '0.0%');
});

test('polling uses 2s, cancels on leaving/logout and drops a late response', async () => {
  let scheduled: (() => void) | undefined; let delay = 0; let cancelled = false; let signal: AbortSignal | undefined;
  let resolve: ((value: number) => void) | undefined; const received: number[] = [];
  const stop = pollMonitor(s => { signal = s; return new Promise<number>(r => { resolve = r; }); }, v => received.push(v), () => true,
    { later: (fn, ms) => { scheduled = fn; delay = ms; return 1; }, cancel: () => { cancelled = true; } });
  resolve!(1); await new Promise(r => setImmediate(r));
  assert.equal(delay, 2000); assert.deepEqual(received, [1]);
  scheduled!(); stop(); assert.equal(signal?.aborted, true); assert.equal(cancelled, true);
  resolve!(2); await new Promise(r => setImmediate(r)); assert.deepEqual(received, [1]);
});

test('authorization failure stops polling', async () => {
  let scheduled = false;
  const stop = pollMonitor(async () => { throw new Error('403'); }, () => assert.fail(), () => false,
    { later: () => { scheduled = true; }, cancel: () => {} });
  await new Promise(r => setImmediate(r)); assert.equal(scheduled, false); stop();
});
