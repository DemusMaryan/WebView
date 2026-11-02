import type { Matrix, Point, Pose } from './types.ts';

export const MAX_POINTS = 5000;
export const MAX_POSES = 1200;
export type XYZ = [number, number, number];

// Proper rotation, determinant +1: OpenCV x/right,y/down,z/forward -> Three x/right,y/up,z/back.
export function scenePosition(matrix: Matrix): XYZ {
  return [matrix[0][3], -matrix[1][3], -matrix[2][3]];
}

export function scenePoint(xyz: XYZ): XYZ {
  return [xyz[0], -xyz[1], -xyz[2]];
}

export function scenePose(matrix: Matrix): Matrix {
  const signs = [1, -1, -1, 1];
  return matrix.map((row, r) => row.map((value, c) => signs[r] * value * signs[c])); // C Twc C^-1
}

export function boundedPoints(points: Point[], limit = MAX_POINTS): Point[] {
  if (points.length <= limit) return points;
  return Array.from({ length: limit }, (_, i) => points[Math.floor(i * points.length / limit)]);
}

export function poseVisible(pose: Pose, map: number, segment: number): boolean {
  return pose.Twc !== null && pose.pose_valid !== false && pose.map_id === map && pose.segment_id === segment;
}

export function trajectoryLines(poses: Pose[], map: number, segment: number): XYZ[] {
  const vertices: XYZ[] = [];
  let previous: Pose | null = null;
  for (const pose of poses.slice(-MAX_POSES)) {
    if (!poseVisible(pose, map, segment)) { previous = null; continue; }
    if (previous?.Twc && pose.Twc && pose.frame_id === previous.frame_id + 1) {
      vertices.push(scenePosition(previous.Twc), scenePosition(pose.Twc));
    }
    previous = pose;
  }
  return vertices;
}

export function parseCalibration(text: string): { calibration: unknown } {
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Калібрування має бути JSON-об’єктом.');
  return { calibration: 'calibration' in value ? value.calibration : value };
}
