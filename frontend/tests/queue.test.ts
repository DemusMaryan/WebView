import { test } from 'node:test';
import assert from 'node:assert/strict';
import { waitLabel } from '../src/queue.ts';

test('queue wait is approximate, handles zero and unavailable estimates', () => {
  assert.equal(waitLabel(null), 'Оцінка поки недоступна');
  assert.equal(waitLabel(-1), 'Оцінка поки недоступна');
  assert.equal(waitLabel(Number.NaN), 'Оцінка поки недоступна');
  assert.equal(waitLabel(0), 'менше секунди');
  assert.equal(waitLabel(12.4), 'близько 13 с');
  assert.equal(waitLabel(61), 'близько 2 хв');
});
