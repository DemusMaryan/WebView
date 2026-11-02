"""One video per process; native tracking stays in the verified mono_export binary."""
import argparse
from collections import deque
import json
import math
import os
from pathlib import Path
import subprocess
import time
import traceback
from .config import CV_ROOT, LIMITS
from .contracts import JobParameters, empty_snapshot
from .files import atomic_json, JsonlTail, read_json
from .video import decode


def check_pose(row):
    matrix = row.get("Twc")
    quaternion = row.get("quaternion_xyzw")
    if matrix is None:
        if quaternion is not None:
            raise ValueError("Quaternion exists without a pose")
        return
    if len(matrix) != 4 or any(len(line) != 4 for line in matrix):
        raise ValueError("Invalid Twc dimensions")
    if not all(math.isfinite(x) for line in matrix for x in line):
        raise ValueError("Nonfinite pose")
    if max(abs(matrix[3][i] - (1 if i == 3 else 0)) for i in range(4)) > 1e-5:
        raise ValueError("Invalid homogeneous row")
    if quaternion is None or len(quaternion) != 4 or abs(sum(x*x for x in quaternion) - 1) > 1e-4:
        raise ValueError("Invalid unit quaternion")
    for i in range(3):
        for j in range(3):
            if abs(sum(matrix[k][i]*matrix[k][j] for k in range(3)) - (i == j)) > 1e-4:
                raise ValueError("Twc rotation is not orthonormal")
    determinant = (matrix[0][0]*(matrix[1][1]*matrix[2][2]-matrix[1][2]*matrix[2][1])
                   - matrix[0][1]*(matrix[1][0]*matrix[2][2]-matrix[1][2]*matrix[2][0])
                   + matrix[0][2]*(matrix[1][0]*matrix[2][1]-matrix[1][1]*matrix[2][0]))
    if abs(determinant - 1) > 1e-4:
        raise ValueError("Twc rotation determinant is not +1")
    x, y, z, w = quaternion
    rotation = [[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]]
    if max(abs(rotation[i][j]-matrix[i][j]) for i in range(3) for j in range(3)) > 1e-4:
        raise ValueError("Quaternion and Twc rotation disagree")


def run(video, params, output, job_id, server_id):
    output.mkdir(parents=True, exist_ok=True)
    if (output / "sequence").exists() or (output / "native").exists():
        raise ValueError("Worker output has already been used; refusing duplicate execution")
    started = time.monotonic()
    base = {"job_id": job_id, "server_id": server_id, "scale": "relative", "coordinate_convention": "Twc"}
    def publish(values):
        atomic_json(output / "latest.json", empty_snapshot() | base | values)
    metadata = decode(video, output, params, publish)
    native = output / "native"
    native.mkdir()
    publish({"stage": "initializing", "decoded_frames": metadata["frames"], "processed_frames": 0,
             "total_frames": metadata["frames"], "tracking_state": None, "Twc": None,
             "points": [], "trajectory": [], "frame_id": None})
    orb = CV_ROOT / "src/ORB_SLAM3"
    binary = CV_ROOT / "build/exporter/mono_export"
    command = [str(binary), str(orb / "Vocabulary/ORBvoc.txt"), str(output / "camera.yaml"),
               str(output / "sequence"), str(native)]
    env = os.environ.copy()
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    env["LD_LIBRARY_PATH"] = ":".join(str(p) for p in (
        CV_ROOT / "prefix/lib", orb / "lib", orb / "Thirdparty/DBoW2/lib", orb / "Thirdparty/g2o/lib"))
    atomic_json(output / "command.json", {"argv": command, "calibration": params.calibration.model_dump(),
                                          "parameters": params.model_dump()})
    timestamps = [float(line.split()[0]) for line in (output / "sequence/rgb.txt").read_text().splitlines()]
    tail = JsonlTail(native / "snapshots.jsonl")
    trail = deque(maxlen=LIMITS["snapshot_trajectory"])
    processed = valid = 0
    latest = None
    last_publish = 0
    snapshot_events = []
    with (output / "native.log").open("wb") as log:
        child = subprocess.Popen(command, cwd=native, env=env, stdout=log, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL)
        atomic_json(output / "child.json", {"pid": child.pid, "worker_pid": os.getpid(),
                                            "process_group": os.getpgrp(), "command": command[0]})
        try:
            while True:
                for row in tail.rows():
                    if row.get("type") != "frame" or row["frame_id"] != processed:
                        raise ValueError("Duplicate, missing or out-of-order native frame_id")
                    if processed >= len(timestamps) or abs(row["timestamp"] - timestamps[processed]) > 1e-6:
                        raise ValueError("Native timestamp differs from decoded input")
                    if row["pose_valid"] != (row["Twc"] is not None):
                        raise ValueError("Invalid pose_valid/null contract")
                    check_pose(row)
                    processed += 1
                    valid += bool(row["pose_valid"])
                    trail.append({key: row[key] for key in (
                        "frame_id", "timestamp", "Twc", "pose_valid", "tracking_state", "map_id", "segment_id")})
                    latest = row
                alive = child.poll() is None
                if latest is not None and (not alive or time.monotonic() - last_publish >= LIMITS["snapshot_interval_seconds"]):
                    points = latest["points"][:LIMITS["snapshot_points"]]
                    publish({"stage": "processing" if alive else "finalizing", "total_frames": metadata["frames"],
                             "decoded_frames": metadata["frames"], "processed_frames": processed,
                             **{key: latest[key] for key in ("frame_id", "timestamp", "tracking_state",
                                "tracking_state_name", "pose_valid", "Twc", "quaternion_xyzw", "map_id",
                                "segment_id", "pose_unavailable_reason")},
                             "points": points, "points_total": latest["points_total"],
                             "points_exported": len(points), "points_limit": LIMITS["snapshot_points"],
                             "points_truncated": latest["points_total"] > len(points),
                             "trajectory": list(trail), "trajectory_limit": trail.maxlen,
                             "trajectory_is_live_history": True,
                             "frame_url": f"/api/jobs/{job_id}/frames/{latest['frame_id']}"})
                    if not snapshot_events or snapshot_events[-1]["processed_frames"] != processed:
                        snapshot_events.append({"processed_frames": processed, "child_running": alive,
                                                "elapsed_seconds": time.monotonic() - started})
                    last_publish = time.monotonic()
                if not alive:
                    # The previous loop read before poll; drain bytes written just before exit.
                    snapshot_path = native / "snapshots.jsonl"
                    if snapshot_path.exists() and tail.offset < snapshot_path.stat().st_size:
                        continue
                    break
                if time.monotonic() - started > params.execution_seconds:
                    raise TimeoutError("Worker execution time exceeded")
                time.sleep(0.05)
            if child.returncode != 0:
                raise RuntimeError(f"mono_export exit code {child.returncode}; see native.log")
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
    if tail.pending or processed != metadata["frames"] or valid == 0:
        raise ValueError("Incomplete or empty native frame export")
    summary = read_json(native / "native-summary.json")
    consistency = summary["final_snapshot_consistency"]
    if not (consistency["workers_finished"] and consistency["geometry_stable"] and consistency["active_gba"] == 0
            and consistency["geometry_digest_first"] == consistency["geometry_digest_second"]):
        raise ValueError("Final native map snapshot is not quiescent and stable")
    trajectory = read_json(native / "trajectory.json")
    points = read_json(native / "map_points.json")
    if not trajectory["poses"] or not points["points"]:
        raise ValueError("Empty final trajectory or map")
    for pose in trajectory["poses"]:
        check_pose(pose)
        if any(pose[key] != trajectory[key] for key in ("map_id", "segment_id")):
            raise ValueError("Final trajectory crosses coordinate segments")
    if any(points[key] != trajectory[key] for key in ("map_id", "segment_id")):
        raise ValueError("Final points and trajectory have different coordinate systems")
    if points["points_exported"] != len(points["points"]) or points["points_exported"] != min(points["points_total"], points["points_limit"]):
        raise ValueError("Invalid final point cap contract")
    if points["points_truncated"] != (points["points_total"] > points["points_exported"]):
        raise ValueError("Invalid final truncation flag")
    for point in points["points"]:
        if len(point["xyz"]) != 3 or not all(math.isfinite(v) for v in point["xyz"]):
            raise ValueError("Nonfinite map point")
    manifest = base | {"processed_frames": processed, "valid_live_poses": valid,
                       "final_poses": len(trajectory["poses"]), "points_exported": points["points_exported"],
                       "points_total": points["points_total"], "points_limit": points["points_limit"],
                       "points_truncated": points["points_truncated"], "video": metadata,
                       "worker_wall_seconds": time.monotonic() - started,
                       "native_summary": summary, "snapshot_events": snapshot_events,
                       "artifacts": ["trajectory.json", "map_points.json", "snapshots.jsonl", "native-summary.json"]}
    atomic_json(output / "result.json", manifest)
    publish(read_json(output / "latest.json") | {"stage": "completed"})
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--parameters", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--server-id", required=True)
    args = parser.parse_args()
    try:
        params = JobParameters.model_validate_json(args.parameters.read_text(encoding="utf-8"))
        run(args.video, params, args.output, args.job_id, args.server_id)
    except Exception as error:
        args.output.mkdir(parents=True, exist_ok=True)
        atomic_json(args.output / "error.json", {"error": f"{type(error).__name__}: {error}"})
        traceback.print_exc()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
