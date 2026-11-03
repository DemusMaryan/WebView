"""Opt-in one-pair R5 check, 300/360 real frames. No automatic retries."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import struct
import time
import tomllib
import httpx
from mtw.capacity import ensure_space
from mtw.config import CV_ROOT, DATA, PORTS, REPO, TERMINAL, job_dir
from mtw.db import Job, Session
from mtw.files import atomic_json, read_json
from mtw.processes import identity, members
from mtw.worker import check_pose
from check_parallel_results import validate
from parallel_e2e import signature

RESOURCE_CONFIG = tomllib.loads((REPO / "config/r5-test-resources.toml").read_text())
INITIAL_THRESHOLDS_BYTES = RESOURCE_CONFIG["initial_thresholds_bytes"]
EMERGENCY_MINIMUM_BYTES = RESOURCE_CONFIG["emergency_minimum_bytes"]


def check_windows_thresholds(win, thresholds, phase):
    values = {"C_free": win["C"]["available_bytes"]}
    for name, minimum in thresholds.items():
        if values[name] < minimum:
            raise RuntimeError(f"{phase} {name}: {values[name]} bytes < {minimum} bytes")
    return values


def utc():
    return datetime.now(timezone.utc).isoformat()


def summarize_overlap(overlap):
    assert len(overlap) >= 2, "No measured overlap of two native tracking processes"
    first, last = overlap[0], overlap[-1]
    # First sample's PID reads completed before the next sample began. Using
    # that next start and the last start excludes HTTP/PID-read uncertainty.
    start_bound = overlap[1]
    return {"first": first, "last": last, "samples": len(overlap),
        "window_start_upper_bound": start_bound["at"], "window_end_lower_bound": last["at"],
        "sampling_span_seconds": last["monotonic"]-first["monotonic"],
        "monotonic_seconds_lower_bound": last["monotonic"]-start_bound["monotonic"],
        "cpu_ticks_delta": {k:last["jobs"][k]["native"]["cpu_ticks"]-first["jobs"][k]["native"]["cpu_ticks"] for k in first["jobs"]},
        "frame_id_delta": {k:last["jobs"][k]["frame_id"]-first["jobs"][k]["frame_id"] for k in first["jobs"]}}


def run(context_path, output):
    if output.exists() or output.with_suffix(".jsonl").exists():
        raise ValueError("Evidence exists; choose a fresh path")
    context = read_json(context_path)
    observer = runpy.run_path(str(REPO / "scripts/resource-baseline-wsl.py"))
    winlog = observer["windows_path"](context["windows_log"])
    latest_windows = observer["latest_windows"]
    client = httpx.Client(base_url=f"http://127.0.0.1:{PORTS['entry']}", timeout=15, trust_env=False)
    evidence = {"started_at": utc(), "jobs": {}, "requests": [], "monitor_context": context,
                "WSL_series": str(output.with_suffix(".jsonl")), "checks": {},
                "clock_ticks_per_second": os.sysconf("SC_CLK_TCK"),
                "initial_thresholds_bytes": INITIAL_THRESHOLDS_BYTES,
                "emergency_minimum_bytes": EMERGENCY_MINIMUM_BYTES}
    ids, history, last_frames, overlap = {}, {}, {}, []
    log_path = DATA / "logs/nginx-access.log"
    log_start = log_path.stat().st_size
    series = output.with_suffix(".jsonl").open("x", buffering=1)

    def save():
        atomic_json(output, evidence)

    def request(method, path, expected=200, **kwargs):
        start = time.monotonic()
        response = client.request(method, path, **kwargs)
        evidence["requests"].append({"at": utc(), "method": method, "path": path,
            "status": response.status_code, "seconds": time.monotonic()-start, "bytes": len(response.content)})
        assert response.status_code == expected, (path, response.status_code, response.text[:500])
        return response

    def resources():
        win = latest_windows(winlog)
        assert (datetime.now(timezone.utc) - datetime.fromisoformat(win["utc"])).total_seconds() < 3, "Windows monitor stale"
        assert win["C"]["error"] is None
        mem = {k:int(v.split()[0])*1024 for k,v in
               (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
               if k in ("MemAvailable", "SwapFree")}
        return {"at": utc(), "monotonic": time.monotonic(), "C_free_bytes": shutil.disk_usage("/mnt/c").free,
                "native_free_bytes": shutil.disk_usage(DATA).free, "memory": mem, "loadavg": os.getloadavg(),
                "windows": {"utc": win["utc"], "monotonic": win["monotonic"],
                            "C": win["C"], "memory": win["memory"], "monitor_pid": win["monitor_pid"]}}

    def native(job_id):
        child = read_json(job_dir(job_id) / "child.json")
        if not child:
            return None
        process = identity(child["pid"])
        if not process or str(CV_ROOT / "build/exporter/mono_export") not in process["argv"]:
            return None
        try:
            fields = Path(f"/proc/{child['pid']}/stat").read_text().rsplit(")", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            return None
        if fields[0] == "Z":
            return None
        return {"pid": child["pid"], "start_ticks": process["start_ticks"],
                "cpu_ticks": int(fields[11])+int(fields[12]), "rss_bytes": int(fields[21])*os.sysconf("SC_PAGE_SIZE")}

    try:
        ensure_space()
        before = resources()
        evidence["preflight"] = before
        series.write(json.dumps(before | {"phase": "preflight"}) + "\n")
        check_windows_thresholds(before["windows"], INITIAL_THRESHOLDS_BYTES, "Initial")
        with Session() as session:
            jobs = session.query(Job).all()
            assert all(j.status in TERMINAL for j in jobs), "Preserve existing active/queued jobs; do not run test"
            old_jobs = {str(j.id): {"status": j.status, "signature": signature(job_dir(j.id))} for j in jobs}
        evidence["existing_jobs"] = old_jobs
        old_hashes = read_json(DATA / "reports/parallel-20261103-retry.json")["cv_hashes_before"]
        def hashes():
            return {name:hashlib.file_digest((CV_ROOT/name).open("rb"), "sha256").hexdigest() for name in old_hashes}
        assert hashes() == old_hashes, "CV binary changed before test"
        evidence["cv_hashes"] = old_hashes
        request("GET", "/health")
        for label, seconds in (("A", 10), ("B", 12)):
            video = CV_ROOT / f"datasets/freiburg1_xyz-rgb-30fps-short-{seconds}s.mp4"
            params = read_json(video.with_suffix(".parameters.json"))
            with video.open("rb") as data:
                created = request("POST", "/api/jobs", 202,
                    files={"video": (video.name, data, "video/mp4")}, data={"parameters": json.dumps(params)}).json()
            ids[label] = created["id"]
            evidence["jobs"][label] = {"id": created["id"], "expected_frames": seconds*30,
                "created_response": created, "fixture": read_json(video.with_suffix(".provenance.json"))}
            save()
        start = time.monotonic()
        while True:
            record = resources() | {"phase": "pair", "jobs": {}}
            # Persist the resource sample even if the guard stops this iteration.
            series.write(json.dumps(record | {"phase": "resources"}) + "\n")
            check_windows_thresholds(record["windows"], EMERGENCY_MINIMUM_BYTES, "Emergency")
            ensure_space()  # Existing immutable 5-GiB backing reserve.
            with Session() as session:
                active = session.query(Job).filter(Job.status.in_(["running", "dispatching"])).all()
                servers = [j.server_id for j in active if j.status == "running"]
                assert len(active) <= 2 and len(servers) == len(set(servers))
            states = {}
            for label, job_id in ids.items():
                state = request("GET", f"/api/jobs/{job_id}").json()
                snapshot = request("GET", f"/api/jobs/{job_id}/snapshot").json()
                assert snapshot["job_id"] == job_id and snapshot["server_id"] in (None, state["server_id"])
                assert len(snapshot["points"]) <= 256 and len(snapshot["trajectory"]) <= 240
                check_pose(snapshot)
                frame = snapshot["frame_id"]
                if frame is not None:
                    assert snapshot["processed_frames"] == frame+1 and frame < evidence["jobs"][label]["expected_frames"]
                    if last_frames.get(label) != frame:
                        preview = request("GET", snapshot["frame_url"])
                        assert int(preview.headers["X-Frame-Id"]) == frame
                        assert struct.unpack(">II", preview.content[16:24]) == (640, 480)
                    last_frames[label] = frame
                process = native(job_id)
                raw = job_dir(job_id) / "native/snapshots.jsonl"
                record["jobs"][label] = {"status": state["status"], "server_id": state["server_id"],
                    "frame_id": frame, "processed_frames": snapshot["processed_frames"],
                    "tracking_state": snapshot["tracking_state"], "pose_valid": snapshot["pose_valid"],
                    "snapshot_points": len(snapshot["points"]), "native": process,
                    "jsonl_bytes": raw.stat().st_size if raw.exists() else 0}
                if process:
                    h = history.setdefault(label, {"first_seen": record["at"], "first": process})
                    assert h["first"]["pid"] == process["pid"] and h["first"]["start_ticks"] == process["start_ticks"]
                    h.update(last_seen=record["at"], last=process)
                states[label] = state
            series.write(json.dumps(record, separators=(",", ":")) + "\n")
            evidence["last_observation"] = record
            if all(j["status"] == "running" and j["native"] and j["pose_valid"] and j["snapshot_points"]
                   for j in record["jobs"].values()):
                overlap.append(record)
            failed = {k:s for k,s in states.items() if s["status"] in TERMINAL and s["status"] != "succeeded"}
            assert not failed, {k: {"status": s["status"], "error": s["error"]} for k,s in failed.items()}
            if all(s["status"] == "succeeded" for s in states.values()):
                break
            assert time.monotonic()-start < 210, "Pair observation timed out; no retry"
            time.sleep(.5)
        evidence["overlap"] = summarize_overlap(overlap)
        assert evidence["overlap"]["monotonic_seconds_lower_bound"] > 1
        assert all(v > 0 for v in evidence["overlap"]["cpu_ticks_delta"].values())
        assert all(v > 0 for v in evidence["overlap"]["frame_id_delta"].values())
        assert states["A"]["server_id"] != states["B"]["server_id"]
        for label, job_id in ids.items():
            checked = validate(client, job_id, evidence["jobs"][label]["expected_frames"])
            worker = read_json(job_dir(job_id) / "worker-process.json")
            assert worker and identity(worker["pid"]) is None and not members(worker["pid"])
            cleanup = read_json(job_dir(job_id) / "frame-work-cleanup.json")
            assert cleanup and not (job_dir(job_id) / "sequence/rgb").exists()
            evidence["jobs"][label].update(checked, native_history=history[label], worker_pid=worker["pid"],
                cleanup=cleanup, process_group_empty=True)
        with log_path.open() as log:
            log.seek(log_start)
            lines = [line.strip() for line in log if "POST /execute " in line and any(j in line for j in ids.values())]
        evidence["nginx_execute_log"] = lines
        for label, job_id in ids.items():
            server = evidence["jobs"][label]["state"]["server_id"]
            assert any(job_id in line and " 202 " in line and f"server={server}" in line for line in lines)
        with Session() as session:
            for job_id, old in old_jobs.items():
                assert session.get(Job, job_id).status == old["status"] and signature(job_dir(job_id)) == old["signature"]
        assert hashes() == old_hashes
        for port in (PORTS["compute_1"], PORTS["compute_2"]):
            assert not httpx.get(f"http://127.0.0.1:{port}/health", trust_env=False).json()["busy"]
        evidence["checks"] = {"existing_jobs_unchanged": len(old_jobs), "cv_hashes_unchanged": True,
                              "all_groups_empty": True, "slots_free": True, "API_snapshots_previews_isolated": True}
        evidence["passed"] = True
    except BaseException as error:
        evidence["passed"] = False
        evidence["error"] = f"{type(error).__name__}: {error}"
        # Stop only jobs belonging to this invocation. No automatic next pair.
        for job_id in ids.values():
            try:
                client.post(f"/api/jobs/{job_id}/cancel")
            except httpx.HTTPError:
                pass
        deadline = time.monotonic()+15
        while ids and time.monotonic() < deadline:
            with Session() as session:
                if all(session.get(Job, j).status in TERMINAL for j in ids.values()):
                    break
            time.sleep(.1)
    finally:
        # Real post-event samples, no artificial delay inside the computation.
        for _ in range(10):
            try:
                after = resources() | {"phase": "after", "jobs": {}}
                with Session() as session:
                    for label, job_id in ids.items():
                        job = session.get(Job, job_id)
                        after["jobs"][label] = {"status": job.status, "error": job.error,
                            "native": native(job_id), "group_members": members(job.worker_pid) if job.worker_pid else []}
                series.write(json.dumps(after) + "\n")
                evidence["post_jobs"] = after["jobs"]
            except Exception as error:
                evidence["post_monitor_error"] = f"{type(error).__name__}: {error}"
                break
            time.sleep(1)
        evidence["finished_at"] = utc()
        evidence["native_history"] = history
        evidence["created_jobs"] = len(ids)
        evidence["C_free_after_bytes"] = shutil.disk_usage("/mnt/c").free
        save()
        series.close()
        client.close()
        print(json.dumps({"passed": evidence.get("passed"), "error": evidence.get("error"),
                          "jobs": ids, "evidence": str(output)}))
    return evidence.get("passed", False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if run(args.context, args.output) else 1)
