export type Matrix = number[][];
export type JobStatus = 'queued' | 'dispatching' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'timed_out';
export type Calibration = {
  width: number; height: number; fx: number; fy: number; cx: number; cy: number;
  k1: number; k2: number; p1: number; p2: number; k3?: number; source: string;
};
export type Limits = {
  file_bytes: number; duration_seconds: number; frames: number; width: number; height: number;
  execution_seconds: number; queued_jobs: number; snapshot_points: number; snapshot_trajectory: number;
};
export type Config = { limits: Limits; poll_interval_ms: number };
export type Job = {
  id: string; status: JobStatus; stage: string; processed_frames: number; total_frames: number | null;
  server_id: string | null; error: string | null; cancel_requested: boolean;
  created_at: string; parameters: { calibration: Calibration; execution_seconds: number };
};
export type Pose = {
  frame_id: number; timestamp: number; map_id: number; segment_id: number; Twc: Matrix | null;
  pose_valid?: boolean; tracking_state?: number;
};
export type Point = { id: number; xyz: [number, number, number] };
export type Snapshot = {
  job_id: string; server_id: string | null; status: JobStatus; stage: string;
  frame_id: number | null; timestamp: number | null; processed_frames: number; total_frames: number | null;
  decoded_frames?: number; tracking_state: number | null; tracking_state_name: string | null;
  pose_valid: boolean; Twc: Matrix | null; map_id: number | null; segment_id: number | null;
  points: Point[]; points_total: number; points_exported: number; points_limit: number; points_truncated: boolean;
  trajectory: Pose[]; frame_url: string | null;
};
export type Manifest = {
  job_id: string; server_id: string; processed_frames: number; valid_live_poses: number; final_poses: number;
  points_total: number; points_exported: number; points_limit: number; points_truncated: boolean;
  worker_wall_seconds: number; artifacts: Record<string, string>;
};
export type FinalData = { points: Point[]; poses: Pose[]; map_id: number; segment_id: number; manifest: Manifest };
export type Presentation = { snapshot: Snapshot; imageUrl: string | null; final: FinalData | null };
export const terminal = (status: JobStatus) => ['succeeded', 'failed', 'cancelled', 'timed_out'].includes(status);
