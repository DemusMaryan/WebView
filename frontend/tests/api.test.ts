import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, json } from '../src/api.ts';

test('stored limit keeps its machine code and gives an understandable message', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: {
    code: 'stored_job_limit_reached', message: 'Stored job limit reached', count: 101, limit: 100,
  }}), { status: 429 });
  try {
    await assert.rejects(json('/api/jobs'), error => error instanceof ApiError
      && error.status === 429 && error.code === 'stored_job_limit_reached'
      && error.message.includes('101 із 100') && error.message.includes('результати залишаються доступними'));
  } finally { globalThis.fetch = original; }
});

test('pending cap is distinct; validation and non-JSON errors remain readable', async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: {
      code: 'pending_job_limit_reached', count: 10, limit: 10,
    }}), { status: 429 });
    await assert.rejects(json('/api/jobs'), error => error instanceof ApiError
      && error.code === 'pending_job_limit_reached' && error.message.includes('незавершених задач'));
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: [{ loc: ['query', 'page'], msg: 'invalid' }] }), { status: 422 });
    await assert.rejects(json('/api/jobs'), /query.page: invalid/);
    globalThis.fetch = async () => new Response('Request too large', { status: 413 });
    await assert.rejects(json('/api/jobs'), /перевищує дозволений розмір/);
  } finally { globalThis.fetch = original; }
});
