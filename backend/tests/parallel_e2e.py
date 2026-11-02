"""Opt-in six-job R5 acceptance: real video, native processes, Nginx, no mocks."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
import httpx
from mtw.capacity import ensure_space
from mtw.config import CV_ROOT, DATA, PORTS, REPO, TERMINAL, job_dir
from mtw.db import Job, Session, serialize
from mtw.files import atomic_json, read_json
from mtw.processes import identity, members
from mtw.worker import check_pose


def utc():
    return datetime.now(timezone.utc).isoformat()


def signature(directory):
    rows = [(str(p.relative_to(directory)), p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(directory.rglob("*")) if p.is_file()]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def run(output, isolation_only=False):
    ensure_space()
    if output.exists():
        raise ValueError("Evidence file already exists; choose a fresh path")
    output.parent.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(base_url=f"http://127.0.0.1:{PORTS['entry']}", timeout=20, trust_env=False)
    video = CV_ROOT / "datasets/freiburg1_xyz-rgb-30fps.mp4"
    parameters = read_json(video.with_suffix(".parameters.json"))
    with Session() as session:
        old_jobs = {str(j.id): {"status": j.status, "signature": signature(job_dir(j.id))}
                    for j in session.query(Job).all()}
    hashes = {str(p.relative_to(CV_ROOT)): hashlib.file_digest(p.open("rb"), "sha256").hexdigest()
              for p in [CV_ROOT / "build/exporter/mono_export", CV_ROOT / "src/ORB_SLAM3/lib/libORB_SLAM3.so",
                        CV_ROOT / "src/ORB_SLAM3/Thirdparty/DBoW2/lib/libDBoW2.so",
                        CV_ROOT / "src/ORB_SLAM3/Thirdparty/g2o/lib/libg2o.so",
                        CV_ROOT / "prefix/lib/libpango_core.so"]}
    evidence = {"started_at": utc(), "backing_free_before_bytes": shutil.disk_usage("/mnt/c").free,
                "native_free_before_bytes": shutil.disk_usage(CV_ROOT).free,
                "requests": [], "jobs": {}, "observations": [],
                "both_native_processing": [], "checks": {}, "existing_jobs": old_jobs, "cv_hashes_before": hashes}
    ids, states, latest, native_history = {}, {}, {}, {}
    log_start = (DATA / "logs/nginx-access.log").stat().st_size

    def save():
        atomic_json(output, evidence)

    def request(method, path, expected=200, **kwargs):
        t = time.monotonic()
        r = client.request(method, path, **kwargs)
        elapsed = time.monotonic() - t
        assert r.status_code == expected, (path, r.status_code, r.text[:500])
        evidence["requests"].append({"at": utc(), "method": method, "path": path,
                                     "status": r.status_code, "seconds": elapsed, "bytes": len(r.content)})
        return r

    def submit(label, *, corrupt=False, budget=None):
        params = parameters | ({"execution_seconds": budget} if budget else {})
        data = b"not a video\x00\x01" if corrupt else video.open("rb")
        try:
            value = request("POST", "/api/jobs", 202, files={"video": ("fixture.mp4", data, "video/mp4")},
                            data={"parameters": json.dumps(params)}).json()
        finally:
            if hasattr(data, "close"):
                data.close()
        ids[label] = value["id"]
        evidence["jobs"][label] = {"id": value["id"]}
        save()
        return value["id"]

    def native(label):
        c = read_json(job_dir(ids[label]) / "child.json")
        if not c:
            return None
        item = identity(c["pid"])
        if not item or str(CV_ROOT / "build/exporter/mono_export") not in item["argv"]:
            return None
        try:
            fields = Path(f"/proc/{c['pid']}/stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            return None
        return {"pid": c["pid"], "start_ticks": item["start_ticks"],
                "cpu_ticks": int(fields[11]) + int(fields[12]),
                "rss_bytes": int(fields[21]) * os.sysconf("SC_PAGE_SIZE")}

    def sample(labels):
        ensure_space()
        mem = {k: int(v.split()[0]) * 1024 for k, v in
               (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
               if k in ("MemAvailable", "SwapFree")}
        assert mem["MemAvailable"] > 512 * 1024**2, "Memory guard: less than 512 MiB available"
        record = {"at": utc(), "monotonic": time.monotonic(), "memory": mem,
                  "loadavg": os.getloadavg(), "backing_free_bytes": shutil.disk_usage("/mnt/c").free,
                  "native_free_bytes": shutil.disk_usage(CV_ROOT).free, "jobs": {}}
        cpu = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
        record["cpu"] = {"total_ticks": sum(cpu), "idle_ticks": cpu[3] + cpu[4]}
        with Session() as session:
            rows = session.query(Job).filter(Job.status.in_(["running", "dispatching"])).all()
            assert len(rows) <= 2
            servers = [j.server_id for j in rows if j.status == "running"]
            assert len(servers) == len(set(servers))
        for label in labels:
            j = ids[label]
            state = request("GET", f"/api/jobs/{j}").json()
            snap = request("GET", f"/api/jobs/{j}/snapshot").json()
            assert snap["job_id"] == j and snap["server_id"] in (None, state["server_id"])
            assert len(snap["points"]) <= 256 and len(snap["trajectory"]) <= 240
            check_pose(snap)
            for pose in snap["trajectory"]:
                check_pose(pose) if "quaternion_xyzw" in pose else None
            if snap["frame_id"] is not None:
                assert snap["processed_frames"] == snap["frame_id"] + 1
                if not latest.get(label) or latest[label]["frame_id"] != snap["frame_id"]:
                    p = request("GET", snap["frame_url"])
                    assert int(p.headers["X-Frame-Id"]) == snap["frame_id"]
                    assert struct.unpack(">II", p.content[16:24]) == (640, 480)
            states[label], latest[label] = state, snap
            n = native(label)
            observation = {"status": state["status"], "server_id": state["server_id"],
                "frame_id": snap["frame_id"], "points": len(snap["points"]), "native": n,
                "jsonl_bytes": (job_dir(j) / "native/snapshots.jsonl").stat().st_size
                    if (job_dir(j) / "native/snapshots.jsonl").exists() else 0}
            record["jobs"][label] = observation
            if n:
                history = native_history.setdefault(label, {"first_seen": record["at"], "first": n})
                history.update(last_seen=record["at"], last=n)
            elif label in native_history and "first_absent" not in native_history[label]:
                native_history[label]["first_absent"] = record["at"]
        evidence["observations"].append(record)
        save()
        return record

    def until(labels, predicate, timeout=240):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = sample(labels)
            if predicate(record):
                return record
            time.sleep(0.5)
        raise AssertionError(f"Timeout waiting for {labels}")

    def control(action, *names):
        r = subprocess.run([sys.executable, "-m", "mtw.control", action, *names], cwd=REPO,
                           capture_output=True, text=True, timeout=25)
        assert r.returncode == 0, r.stdout + r.stderr
        evidence.setdefault("control", []).append({"at": utc(), "action": action, "names": names})

    def native_ready(record, labels):
        return all(record["jobs"][k]["native"] and latest[k]["frame_id"] is not None
                   and latest[k]["frame_id"] >= 10 and states[k]["status"] == "running" for k in labels)

    def validate(label):
        j = ids[label]
        r = request("GET", f"/api/jobs/{j}/result").json()
        assert states[label]["status"] == "succeeded"
        assert r["job_id"] == j and r["server_id"] == states[label]["server_id"]
        assert r["processed_frames"] == 798 and r["valid_live_poses"] > 0
        assert r["scale"] == "relative" and r["coordinate_convention"] == "Twc"
        trajectory = request("GET", r["artifacts"]["trajectory.json"]).json()
        points = request("GET", r["artifacts"]["map_points.json"]).json()
        for pose in trajectory["poses"]:
            check_pose(pose)
            assert (pose["map_id"], pose["segment_id"]) == (trajectory["map_id"], trajectory["segment_id"])
        assert (points["map_id"], points["segment_id"]) == (trajectory["map_id"], trajectory["segment_id"])
        assert r["points_exported"] == len(points["points"]) == min(points["points_total"], points["points_limit"])
        assert points["points_limit"] == 100000
        assert points["points_truncated"] == (points["points_total"] > len(points["points"]))
        assert all(len(p["xyz"]) == 3 and all(math.isfinite(x) for x in p["xyz"]) for p in points["points"])
        count = valid = 0
        with client.stream("GET", r["artifacts"]["snapshots.jsonl"]) as raw:
            assert raw.status_code == 200
            for line in raw.iter_lines():
                row = json.loads(line)
                assert row["frame_id"] == count and abs(row["timestamp"] - count / 30) < 1e-6
                check_pose(row)
                assert row["pose_valid"] == (row["Twc"] is not None)
                valid += row["pose_valid"]
                count += 1
        assert count == 798 and valid == r["valid_live_poses"]
        c = r["native_summary"]["final_snapshot_consistency"]
        assert c["workers_finished"] and c["active_gba"] == 0 and c["geometry_stable"]
        assert c["geometry_digest_first"] == c["geometry_digest_second"]
        evidence["jobs"][label].update(result={k: r[k] for k in ["processed_frames", "valid_live_poses", "final_poses",
             "points_exported", "points_limit", "points_truncated", "worker_wall_seconds"]},
             validation={"valid": True, "jsonl_records": count, "null_poses": count-valid, "consistency": c})

    try:
        if not isolation_only:
            for label in ["A", "B"]:
                submit(label)
            until(["A", "B"], lambda r: all(states[k]["status"] == "running" for k in ["A", "B"]))
            submit("queued_error", corrupt=True)
            restarted = False
            while True:
                record = sample(["A", "B", "queued_error"])
                if all(states[k]["status"] == "running" for k in ["A", "B"]):
                    assert states["queued_error"]["status"] == "queued"
                    evidence["checks"]["third_waits_for_slot"] = True
                if native_ready(record, ["A", "B"]):
                    evidence["both_native_processing"].append(record)
                    if not restarted:
                        before = {k: native(k) for k in ["A", "B"]}
                        control("stop", "dispatcher")
                        control("start", "dispatcher")
                        assert all(native(k)["pid"] == before[k]["pid"] for k in ["A", "B"])
                        evidence["checks"]["dispatcher_restart_kept_native_pids"] = before
                        with Session() as s:
                            token = str(s.get(Job, ids["A"]).dispatch_token)
                        duplicates = []
                        for _ in range(4):
                            resp = httpx.post(f"http://127.0.0.1:{PORTS['balancer']}/execute", trust_env=False,
                                json={"job_id": ids["A"], "dispatch_token": token}, headers={"X-MTW-Job": ids["A"]})
                            assert resp.status_code in (202, 409)
                            duplicates.append({"status": resp.status_code, "body": resp.json()})
                        assert any(x["status"] == 202 and x["body"]["duplicate"] for x in duplicates)
                        assert native("A")["pid"] == before["A"]["pid"]
                        evidence["checks"]["duplicate_handoffs"] = duplicates
                        restarted = True
                if all(states[k]["status"] in TERMINAL for k in ["A", "B", "queued_error"]):
                    break
                assert time.monotonic() - evidence["observations"][0]["monotonic"] < 240
                time.sleep(.5)
            assert restarted and all(states[k]["status"] == "succeeded" for k in ["A", "B"]), states
            assert states["queued_error"]["status"] == "failed" and "InvalidDataError" in states["queued_error"]["error"]
            overlap = evidence["both_native_processing"]
            assert len(overlap) >= 2
            first, last = overlap[0], overlap[-1]
            evidence["overlap"] = {"start": first["at"], "end": last["at"],
                "seconds_lower_bound": last["monotonic"] - first["monotonic"],
                "cpu_ticks_delta": {k:last["jobs"][k]["native"]["cpu_ticks"]-first["jobs"][k]["native"]["cpu_ticks"] for k in ["A", "B"]}}
            assert evidence["overlap"]["seconds_lower_bound"] > 1
            assert all(v > 0 for v in evidence["overlap"]["cpu_ticks_delta"].values())
            assert states["A"]["server_id"] != states["B"]["server_id"]
        for label in ["cancel", "survivor"]:
            submit(label)
        until(["cancel", "survivor"], lambda r: native_ready(r, ["cancel", "survivor"]))
        survivor_before = native("survivor")
        survivor_frame = latest["survivor"]["frame_id"]
        request("POST", f"/api/jobs/{ids['cancel']}/cancel", 202)
        until(["cancel", "survivor"], lambda r: states["cancel"]["status"] == "cancelled")
        assert states["survivor"]["status"] == "running" and native("survivor")["pid"] == survivor_before["pid"]
        submit("timeout", budget=1)
        until(["survivor", "timeout"], lambda r: states["timeout"]["status"] == "timed_out")
        assert states["survivor"]["status"] == "running" and native("survivor")["pid"] == survivor_before["pid"]
        assert latest["survivor"]["frame_id"] > survivor_frame
        assert native("survivor")["cpu_ticks"] > survivor_before["cpu_ticks"]
        evidence["checks"]["cancel_and_timeout_isolated"] = {"survivor_pid": survivor_before["pid"],
            "frame_before": survivor_frame, "frame_after": latest["survivor"]["frame_id"]}
        until(["survivor"], lambda r: states["survivor"]["status"] in TERMINAL)
        for k in (["survivor"] if isolation_only else ["A", "B", "survivor"]):
            validate(k)
        for k, j in ids.items():
            worker = read_json(job_dir(j) / "worker-process.json")
            assert worker and identity(worker["pid"]) is None and not members(worker["pid"])
            cleanup = read_json(job_dir(j) / "frame-work-cleanup.json")
            assert cleanup and not (job_dir(j) / "sequence/rgb").exists()
            with Session() as s:
                evidence["jobs"][k].update(state=serialize(s.get(Job, j)), worker_pid=worker["pid"],
                    native_history=native_history.get(k), cleanup=cleanup, process_group_empty=True)
        with (DATA / "logs/nginx-access.log").open("r") as f:
            f.seek(log_start)
            lines = [line.strip() for line in f if 'POST /execute ' in line and any(j in line for j in ids.values())]
        evidence["nginx_execute_log"] = lines
        for k in (["survivor"] if isolation_only else ["A", "B", "survivor"]):
            assert any(ids[k] in line and " 202 " in line and f"server={states[k]['server_id']}" in line for line in lines)
        for key, item in old_jobs.items():
            assert signature(job_dir(key)) == item["signature"]
        for name, digest in hashes.items():
            assert hashlib.file_digest((CV_ROOT / name).open("rb"), "sha256").hexdigest() == digest
        for port in [PORTS["compute_1"], PORTS["compute_2"]]:
            assert not httpx.get(f"http://127.0.0.1:{port}/health", trust_env=False).json()["busy"]
        evidence["checks"].update(existing_jobs_unchanged=len(old_jobs), cv_hashes_unchanged=True,
                                 all_slots_free=True, all_groups_empty=True)
        evidence["passed"] = True
    except BaseException as error:
        evidence["passed"] = False
        evidence["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        # Only jobs created by this invocation may be cancelled on test failure.
        if not evidence.get("passed"):
            for j in ids.values():
                try:
                    client.post(f"/api/jobs/{j}/cancel")
                except httpx.HTTPError:
                    pass
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                with Session() as s:
                    pending = [j for j in s.query(Job).filter(Job.id.in_(list(ids.values())))
                               if j.status not in TERMINAL]
                if not pending:
                    break
                time.sleep(.1)
        evidence["backing_free_after_bytes"] = shutil.disk_usage("/mnt/c").free
        evidence["native_free_after_bytes"] = shutil.disk_usage(CV_ROOT).free
        evidence["finished_at"] = utc()
        save()
        print(json.dumps({"passed": evidence.get("passed"), "output": str(output), "error": evidence.get("error")}))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--isolation-only", action="store_true")
    args = p.parse_args()
    run(args.output, args.isolation_only)
