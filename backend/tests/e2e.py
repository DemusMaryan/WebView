"""Opt-in real native/API acceptance run. Produces evidence outside Git, no mocks."""
import argparse
import copy
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
import time
import httpx
from mtw.config import CV_ROOT, DATA, LIMITS, PORTS, REPO, TERMINAL, job_dir
from mtw.files import atomic_json, read_json
from mtw.processes import identity, members
from mtw.worker import check_pose

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, default=DATA / "reports/prompt3-e2e.json")
args = parser.parse_args()
args.output.parent.mkdir(parents=True, exist_ok=True)
evidence = {"started_at": datetime.now(timezone.utc).isoformat(), "requests": [], "checks": {}}
client = httpx.Client(base_url=f"http://127.0.0.1:{PORTS['entry']}", timeout=30, trust_env=False)
video = CV_ROOT / "datasets/freiburg1_xyz-rgb-30fps.mp4"
parameters = read_json(video.with_suffix(".parameters.json"))


def save():
    atomic_json(args.output, evidence)


def request(method, path, expected=200, **kwargs):
    started = time.monotonic()
    response = client.request(method, path, **kwargs)
    elapsed = time.monotonic() - started
    assert response.status_code == expected, (path, response.status_code, response.text[:1000])
    content_type = response.headers.get("content-type", "")
    value = response.json() if "json" in content_type else None
    record = {"method": method, "path": path, "status": response.status_code, "seconds": elapsed,
              "bytes": len(response.content)}
    if path.endswith("/snapshot"):
        record["body_excerpt"] = {key: value.get(key) for key in (
            "job_id", "server_id", "status", "stage", "frame_id", "processed_frames", "total_frames",
            "tracking_state", "pose_valid", "map_id", "segment_id", "points_exported", "points_truncated")}
        record["body_excerpt"]["trajectory_records"] = len(value.get("trajectory", []))
    elif value is not None and not isinstance(value, list):
        record["body"] = value
    evidence["requests"].append(record)
    save()
    return response, value, elapsed


def submit(params=None, file=None, expected=202):
    source = file if file is not None else video
    with source.open("rb") as data:
        return request("POST", "/api/jobs", expected, files={"video": (source.name, data, "video/mp4")},
                       data={"parameters": json.dumps(params or parameters)})


def wait(job_id, target=None, observe=False, timeout=210):
    deadline = time.monotonic() + timeout
    seen = []
    fetched_frame = False
    while time.monotonic() < deadline:
        _, status, elapsed = request("GET", f"/api/jobs/{job_id}")
        assert elapsed < 3, "API state request blocked by compute"
        if observe:
            response, snapshot, _ = request("GET", f"/api/jobs/{job_id}/snapshot")
            assert {"job_id", "server_id", "processed_frames", "total_frames", "stage", "tracking_state",
                    "Twc", "map_id", "segment_id", "points", "trajectory"} <= snapshot.keys()
            assert len(snapshot.get("points", [])) <= LIMITS["snapshot_points"]
            assert len(snapshot.get("trajectory", [])) <= LIMITS["snapshot_trajectory"]
            assert len(response.content) < 256 * 1024
            if snapshot.get("frame_id") is not None:
                assert snapshot["processed_frames"] == snapshot["frame_id"] + 1
                check_pose(snapshot)
                for pose in snapshot["trajectory"]:
                    assert "map_id" in pose and "segment_id" in pose
                    assert pose["pose_valid"] == (pose["Twc"] is not None)
                if status["status"] not in TERMINAL and (not seen or seen[-1] != snapshot["frame_id"]):
                    seen.append(snapshot["frame_id"])
                if not fetched_frame:
                    response, _, _ = request("GET", snapshot["frame_url"])
                    assert response.headers["x-frame-id"] == str(snapshot["frame_id"])
                    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
                    assert struct.unpack(">II", response.content[16:24]) == (640, 480)
                    fetched_frame = True
        if target and target(status):
            return status, seen
        if status["status"] in TERMINAL:
            return status, seen
        time.sleep(.5)
    raise AssertionError(f"Job did not reach expected state: {job_id}")


def control(action, *names):
    completed = subprocess.run([sys.executable, "-m", "mtw.control", action, *names],
                               cwd=REPO, capture_output=True, text=True, timeout=45)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    evidence.setdefault("control", []).append({"action": action, "services": names, "output": completed.stdout})
    save()


def no_processes(job_id):
    directory = job_dir(job_id)
    worker = read_json(directory / "worker-process.json")
    child = read_json(directory / "child.json")
    assert worker and identity(worker["pid"]) is None
    assert not members(worker["pid"])
    if child:
        assert identity(child["pid"]) is None
    for port in (PORTS["compute_1"], PORTS["compute_2"]):
        deadline = time.monotonic() + 3
        while True:
            value = httpx.get(f"http://127.0.0.1:{port}/health", trust_env=False).json()
            if not value["busy"]:
                break
            assert time.monotonic() < deadline, value
            time.sleep(.05)
    return {"worker_pid": worker["pid"], "native_pid": child["pid"] if child else None,
            "remaining_group_members": members(worker["pid"]), "compute_slots_free": True}


try:
    request("GET", "/health")
    health = [httpx.get(f"http://127.0.0.1:{port}/health", trust_env=False).json()
              for port in (PORTS["compute_1"], PORTS["compute_2"])]
    assert {item["server_id"] for item in health} == {"compute-1", "compute-2"}
    evidence["checks"]["compute_health"] = health
    _, created, elapsed = submit()
    assert elapsed < 5, "POST waited for video computation"
    success_id = created["id"]
    evidence["success_job_id"] = success_id
    status, seen = wait(success_id, observe=True)
    assert status["status"] == "succeeded", status
    assert len(seen) >= 3, seen
    _, result, _ = request("GET", f"/api/jobs/{success_id}/result")
    assert result["processed_frames"] == 798
    assert result["valid_live_poses"] > 0 and result["points_exported"] > 0
    assert result["scale"] == "relative" and result["coordinate_convention"] == "Twc"
    evidence["checks"]["success"] = {"job_id": success_id, "server_id": status["server_id"],
        "distinct_live_frame_ids": seen, "post_seconds": elapsed, "result": result}
    # Download the complete artifacts once; snapshots never fetch or rescan them.
    _, trajectory, _ = request("GET", result["artifacts"]["trajectory.json"])
    _, points, _ = request("GET", result["artifacts"]["map_points.json"])
    for pose in trajectory["poses"]:
        check_pose(pose)
        assert (pose["map_id"], pose["segment_id"]) == (trajectory["map_id"], trajectory["segment_id"])
    assert (points["map_id"], points["segment_id"]) == (trajectory["map_id"], trajectory["segment_id"])
    assert len(points["points"]) == result["points_exported"]
    frames = valid = nulls = 0
    tracking = {}
    with client.stream("GET", result["artifacts"]["snapshots.jsonl"]) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            row = json.loads(line)
            assert row["frame_id"] == frames
            assert abs(row["timestamp"] - frames/30) < 1e-6
            check_pose(row)
            assert row["pose_valid"] == (row["Twc"] is not None)
            valid += row["pose_valid"]
            nulls += row["Twc"] is None
            tracking[row["tracking_state_name"]] = tracking.get(row["tracking_state_name"], 0) + 1
            frames += 1
    assert frames == 798 and valid == result["valid_live_poses"] and nulls > 0
    evidence["checks"]["artifact_frames"] = {"records": frames, "valid_poses": valid, "null_poses": nulls, "tracking": tracking}
    # A valid reservation token cannot replay a finished job, even on the other compute.
    from mtw.db import Job, Session
    with Session() as session:
        token = str(session.get(Job, success_id).dispatch_token)
    duplicate = httpx.post(f"http://127.0.0.1:{PORTS['compute_2']}/execute", trust_env=False,
                          json={"job_id": success_id, "dispatch_token": token})
    assert duplicate.status_code == 409
    evidence["checks"]["duplicate_execution"] = {"status": duplicate.status_code, "body": duplicate.json()}
    # Real malformed video and limits must fail in the actual decoder, before native SLAM.
    corrupt = DATA / "reports/corrupt.video"
    corrupt.write_bytes(b"not a video\x00\x01")
    _, bad, _ = submit(file=corrupt)
    failed, _ = wait(bad["id"])
    assert failed["status"] == "failed" and "InvalidDataError" in failed["error"], failed
    evidence["checks"]["corrupt"] = failed
    for name, changes, message in (
        ("frames", {"frames": 2}, "Decoded frame count"),
        ("duration", {"duration_seconds": .04}, "Decoded duration"),
        ("resolution", {"width": 320}, "Decoded resolution"),
    ):
        _, rejected, _ = submit(parameters | changes)
        failed, _ = wait(rejected["id"])
        assert failed["status"] == "failed" and message in failed["error"], failed
        evidence["checks"][name + "_limit"] = failed
    mismatch = copy.deepcopy(parameters)
    mismatch["calibration"]["width"] = 639
    _, rejected, _ = submit(mismatch)
    failed, _ = wait(rejected["id"])
    assert failed["status"] == "failed" and "explicit calibration" in failed["error"]
    evidence["checks"]["calibration_mismatch"] = failed
    submit(parameters | {"file_bytes": 1024}, expected=413)
    request("POST", "/api/jobs", 422, files={"video": ("bad", b"data")}, data={"parameters": "{}"})
    # Cancel while a real mono_export child is alive and tracking.
    _, cancel_job, _ = submit()
    cancel_id = cancel_job["id"]
    active, _ = wait(cancel_id, target=lambda s: s["processed_frames"] >= 10)
    assert active["status"] == "running", active
    child = read_json(job_dir(cancel_id) / "child.json")
    assert identity(child["pid"]) is not None
    before = members(active["worker_pid"])
    started_cancel = time.monotonic()
    request("POST", f"/api/jobs/{cancel_id}/cancel", 202)
    cancelled, _ = wait(cancel_id)
    assert cancelled["status"] == "cancelled", cancelled
    evidence["checks"]["cancel"] = {"job_id": cancel_id, "server_id": cancelled["server_id"],
        "members_before": before, "cancel_seconds": time.monotonic()-started_cancel, **no_processes(cancel_id)}
    # Timeout during native computation, budget starts on compute, not on admission.
    timeout_seconds = min(179, math.ceil(result["video"]["decode_seconds"] + 18))
    assert timeout_seconds < result["worker_wall_seconds"], (timeout_seconds, result["worker_wall_seconds"])
    _, timed, _ = submit(parameters | {"execution_seconds": timeout_seconds})
    timed_id = timed["id"]
    timed_status, timed_seen = wait(timed_id, observe=True)
    assert timed_status["status"] == "timed_out", timed_status
    assert timed_seen, "Timeout test did not reach the native runner"
    evidence["checks"]["timeout"] = {"job_id": timed_id, "server_id": timed_status["server_id"],
        "limit_seconds": timeout_seconds, "execution_seconds": timed_status["execution_seconds"],
        "live_frame_ids": timed_seen, **no_processes(timed_id)}
    # Persist both a successful result and a queued job over all project process/DB restarts.
    control("stop", "dispatcher")
    _, queued, _ = submit(parameters | {"execution_seconds": 1})
    queued_id = queued["id"]
    control("stop", "api", "compute-1", "compute-2", "nginx", "postgres")
    control("start", "api", "compute-1", "compute-2", "nginx")
    _, persisted, _ = request("GET", f"/api/jobs/{success_id}")
    assert persisted["status"] == "succeeded" and persisted["owner_id"] is None
    _, persisted_result, _ = request("GET", f"/api/jobs/{success_id}/result")
    assert persisted_result == result
    _, persisted_queue, _ = request("GET", f"/api/jobs/{queued_id}")
    assert persisted_queue["status"] == "queued" and persisted_queue["started_at"] is None
    control("start", "dispatcher")
    expired, _ = wait(queued_id)
    assert expired["status"] == "timed_out"
    queue_seconds = (datetime.fromisoformat(expired["started_at"])-datetime.fromisoformat(expired["created_at"])).total_seconds()
    assert queue_seconds > 1 and expired["execution_seconds"] >= 1
    evidence["checks"]["restart_persistence"] = {"success_job_id": success_id, "queued_job_id": queued_id,
        "queue_seconds": queue_seconds, "execution_seconds": expired["execution_seconds"], **no_processes(queued_id)}
    # Bounded admission under the persistent queue; cleanup by the actual cancel endpoint.
    control("stop", "dispatcher")
    pending = [submit(file=corrupt)[1]["id"] for _ in range(LIMITS["queued_jobs"])]
    submit(file=corrupt, expected=429)
    for job_id in pending:
        _, cancelled, _ = request("POST", f"/api/jobs/{job_id}/cancel", 202)
        assert cancelled["status"] == "cancelled"
    control("start", "dispatcher")
    evidence["checks"]["queue_limit"] = {"pending_limit": LIMITS["queued_jobs"], "overflow_status": 429,
                                           "queued_cancellations": len(pending)}
    request("GET", "/api/jobs")
    evidence["passed"] = True
except BaseException as error:
    evidence["passed"] = False
    evidence["error"] = f"{type(error).__name__}: {error}"
    raise
finally:
    evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
    save()
    print(json.dumps({"evidence": str(args.output), "passed": evidence.get("passed"),
                      "success_job_id": evidence.get("success_job_id"), "error": evidence.get("error")}, indent=2))
