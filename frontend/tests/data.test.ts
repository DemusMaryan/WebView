import { test } from 'node:test';
import assert from 'node:assert/strict';
import { boundedPoints, parseCalibration, scenePoint, scenePose, scenePosition, trajectoryLines } from '../src/data.ts';
import type { Pose } from '../src/types.ts';

const pose = (frame_id: number, map_id = 0, segment_id = 0, valid = true): Pose => ({
  frame_id, timestamp: frame_id / 30, map_id, segment_id, pose_valid: valid,
  Twc: valid ? [[1,0,0,frame_id],[0,1,0,2],[0,0,1,3],[0,0,0,1]] : null,
});
test('null poses, missing frames and map/segment boundaries break the trajectory', () => {
  const records = [pose(0), pose(1), pose(2,0,0,false), pose(3), pose(5), pose(6,1), pose(7), pose(8,0,1), pose(9)];
  assert.equal(trajectoryLines(records, 0, 0).length, 2);
  assert.deepEqual(trajectoryLines([pose(0),pose(1),pose(2)],0,0).map(p => p[0]), [0,1,1,2]);
});
test('coordinates use a proper two-axis rotation; position and complete pose agree', () => {
  const matrix = [[0,-1,0,1],[1,0,0,2],[0,0,1,3],[0,0,0,1]];
  assert.deepEqual(scenePosition(matrix), [1,-2,-3]);
  assert.deepEqual(scenePoint([1,2,3]), [1,-2,-3]);
  const transformed = scenePose(matrix);
  assert.deepEqual(transformed.map(row => row[3]), [1,-2,-3,1]);
  const [a,b,c] = transformed;
  assert.equal(a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0]), 1);
});
test('display cap samples actual point values and never changes export metadata', () => {
  const points = Array.from({ length: 11 }, (_, id) => ({ id, xyz: [id,0,0] as [number,number,number] }));
  assert.deepEqual(boundedPoints(points,3).map(p => p.id), [0,3,7]);
  assert.equal(points.length, 11);
});
test('calibration must be explicit, supporting either a calibration or parameters JSON file', () => {
  assert.throws(() => parseCalibration(''));
  assert.throws(() => parseCalibration('[]'));
  assert.deepEqual(parseCalibration('{"fx":500}'), { calibration: { fx: 500 } });
  assert.deepEqual(parseCalibration('{"calibration":{"fx":500}}'), { calibration: { fx: 500 } });
});
