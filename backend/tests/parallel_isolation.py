"""One opt-in cancel OR timeout pair; preserve the successful R5 pair, no retries."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import struct
import time
from datetime import datetime, timezone
import httpx
from mtw.capacity import ensure_space
from mtw.config import CV_ROOT, DATA, PORTS, REPO, TERMINAL, job_dir
from mtw.db import Job, Session
from mtw.files import atomic_json, read_json, JsonlTail
from mtw.processes import identity, group_processes
from mtw.worker import check_pose
from parallel_short import check_windows_thresholds, INITIAL_THRESHOLDS_BYTES, EMERGENCY_MINIMUM_BYTES, utc
from parallel_e2e import signature
from check_parallel_results import validate


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def run(scenario, context_path, output, timeout_seconds=22):
    if output.exists() or output.with_suffix(".jsonl").exists():
        raise ValueError("Preserve existing evidence; choose a fresh output")
    context = read_json(context_path)
    observer = runpy.run_path(str(REPO / "scripts/resource-baseline-wsl.py"))
    winlog = observer["windows_path"](context["windows_log"])
    client = httpx.Client(base_url=f"http://127.0.0.1:{PORTS['entry']}", trust_env=False, timeout=15)
    report = {"scenario": scenario, "started_at": utc(), "jobs": {}, "requests": [], "events": [],
        "monitor_context": context, "initial_thresholds_bytes": INITIAL_THRESHOLDS_BYTES,
        "emergency_minimum_bytes": EMERGENCY_MINIMUM_BYTES, "WSL_series": str(output.with_suffix(".jsonl")),
        "clock_ticks_per_second": os.sysconf("SC_CLK_TCK")}
    ids, history, previous_frames = {}, {}, {}
    series = output.with_suffix(".jsonl").open("x", buffering=1)
    log_path = DATA / "logs/nginx-access.log"
    log_offset = log_path.stat().st_size

    def save():
        atomic_json(output, report)

    def event(name, **values):
        report["events"].append({"event": name, "at": utc(), "monotonic": time.monotonic(), **values})
        save()

    def request(method, path, expected=200, **kwargs):
        before = time.monotonic()
        r = client.request(method, path, **kwargs)
        report["requests"].append({"method": method, "path": path, "at": utc(),
            "status": r.status_code, "seconds": time.monotonic()-before, "bytes": len(r.content)})
        assert r.status_code == expected, (path, r.status_code, r.text[:500])
        return r

    def resources():
        win = observer["latest_windows"](winlog)
        age = (datetime.now(timezone.utc)-datetime.fromisoformat(win["utc"])).total_seconds()
        assert -1 <= age < 3 and win["C"]["error"] is None, "Windows disk monitor unavailable/stale"
        memory = {k:int(v.split()[0])*1024 for k,v in
            (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
            if k in ("MemAvailable", "SwapFree")}
        return {"at": utc(), "monotonic": time.monotonic(), "C_free_bytes": shutil.disk_usage("/mnt/c").free,
            "native_free_bytes": shutil.disk_usage(DATA).free, "memory": memory, "loadavg": os.getloadavg(),
            "windows": {"utc": win["utc"], "C": win["C"], "memory": win["memory"], "monitor_pid": win["monitor_pid"]}}

    def native(job_id):
        child = read_json(job_dir(job_id)/"child.json")
        if not child:
            return None
        p = identity(child["pid"])
        if not p or str(CV_ROOT/"build/exporter/mono_export") not in p["argv"]:
            return None
        try:
            fields = Path(f"/proc/{child['pid']}/stat").read_text().rsplit(")", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            return None
        if fields[0] == "Z":
            return None
        return {"pid": child["pid"], "start_ticks": p["start_ticks"], "cpu_ticks": int(fields[11])+int(fields[12]),
                "rss_bytes": int(fields[21])*os.sysconf("SC_PAGE_SIZE"), "observed_monotonic": time.monotonic()}

    def sample():
        record = resources() | {"phase": "pair", "jobs": {}}
        series.write(json.dumps(record | {"phase": "resources"})+"\n")
        check_windows_thresholds(record["windows"], EMERGENCY_MINIMUM_BYTES, "Emergency")
        ensure_space()
        with Session() as s:
            active = s.query(Job).filter(Job.status.in_(["running", "dispatching"])).all()
            servers = [j.server_id for j in active if j.status == "running"]
            assert len(active) <= 2 and len(servers) == len(set(servers))
        for label, job_id in ids.items():
            state = request("GET", f"/api/jobs/{job_id}").json()
            snap = request("GET", f"/api/jobs/{job_id}/snapshot").json()
            assert snap["job_id"] == job_id and snap["server_id"] in (None, state["server_id"])
            assert len(snap["points"]) <= 256 and len(snap["trajectory"]) <= 240
            check_pose(snap)
            if snap["frame_id"] is not None and previous_frames.get(label) != snap["frame_id"]:
                assert snap["processed_frames"] == snap["frame_id"]+1
                preview = request("GET", snap["frame_url"])
                assert int(preview.headers["X-Frame-Id"]) == snap["frame_id"]
                assert struct.unpack(">II", preview.content[16:24]) == (640, 480)
                previous_frames[label] = snap["frame_id"]
            process = native(job_id)
            if process:
                h = history.setdefault(label, {"first_seen": record["at"], "first": process})
                assert h["first"]["pid"] == process["pid"] and h["first"]["start_ticks"] == process["start_ticks"]
                h.update(last_seen=record["at"], last=process)
            record["jobs"][label] = {"state": state, "native": process, "frame_id": snap["frame_id"],
                "processed_frames": snap["processed_frames"], "tracking_state": snap["tracking_state"],
                "pose_valid": snap["pose_valid"], "snapshot_points": len(snap["points"])}
        series.write(json.dumps(record, separators=(",", ":"))+"\n")
        report["last_observation"] = record
        return record

    try:
        ensure_space()
        preflight = resources()
        report["preflight"] = preflight
        series.write(json.dumps(preflight | {"phase": "preflight"})+"\n")
        check_windows_thresholds(preflight["windows"], INITIAL_THRESHOLDS_BYTES, "Initial")
        with Session() as s:
            old_jobs = {str(j.id): {"status": j.status, "signature": signature(job_dir(j.id))} for j in s.query(Job).all()}
        assert all(j["status"] in TERMINAL for j in old_jobs.values()), "Existing active jobs must be preserved"
        report["existing_jobs"] = old_jobs
        successful = DATA/"reports/parallel-short-20261103-commit2.json"
        proof_paths = [successful, successful.with_name("parallel-short-20261103-commit2-postcheck.json")]
        proof_hashes = {str(p):digest(p) for p in proof_paths}
        cv_hashes = read_json(successful)["cv_hashes"]
        assert all(digest(CV_ROOT/name) == value for name,value in cv_hashes.items())
        config_paths = [REPO/"config/local.toml", REPO/"config/r5-test-resources.toml"]
        config_hashes = {str(p):digest(p) for p in config_paths}
        defaults = request("GET", "/api/config").json()["limits"]
        report.update(cv_hashes=cv_hashes, successful_pair_proofs=proof_hashes,
                      config_hashes_before=config_hashes, public_limits_before=defaults)
        for label, seconds in (("target", 10), ("survivor", 12)):
            video = CV_ROOT/f"datasets/freiburg1_xyz-rgb-30fps-short-{seconds}s.mp4"
            params = read_json(video.with_suffix(".parameters.json"))
            if scenario == "timeout" and label == "target":
                params["execution_seconds"] = timeout_seconds  # Existing per-job parameter only.
            with video.open("rb") as data:
                created = request("POST", "/api/jobs", 202, files={"video":(video.name, data, "video/mp4")},
                    data={"parameters":json.dumps(params)}).json()
            ids[label] = created["id"]
            report["jobs"][label] = {"id": created["id"], "expected_frames": seconds*30, "created_response": created}
            save()
        started = time.monotonic()
        both_seen = cancel_sent = released = continued = False
        baseline = None
        expected = "cancelled" if scenario == "cancel" else "timed_out"
        while True:
            record = sample()
            target, survivor = record["jobs"]["target"], record["jobs"]["survivor"]
            ts, ss = target["state"], survivor["state"]
            assert ss["status"] not in TERMINAL or ss["status"] == "succeeded", ss
            assert ts["status"] not in TERMINAL or ts["status"] == expected, ts
            live = all(j["state"]["status"] == "running" and j["native"] and j["native"]["cpu_ticks"] > 0
                       for j in (target, survivor))
            if live:
                assert ts["server_id"] != ss["server_id"]
                if not both_seen:
                    event("both_native_working", jobs=record["jobs"])
                    both_seen = True
                if scenario == "timeout":
                    baseline = survivor
                elif not cancel_sent and all(j["pose_valid"] and j["frame_id"] is not None for j in (target, survivor)):
                    baseline = survivor
                    request("POST", f"/api/jobs/{ids['target']}/cancel", 202)
                    event("cancel_requested", jobs=record["jobs"])
                    cancel_sent = True
            if ts["status"] == expected and not released:
                worker = read_json(job_dir(ids["target"])/"worker-process.json")
                assert worker and not Path(f"/proc/{worker['pid']}").exists() and not group_processes(worker["pid"])
                child = read_json(job_dir(ids["target"])/"child.json")
                assert child and not Path(f"/proc/{child['pid']}").exists()
                assert target["native"] is None and ss["status"] == "running" and survivor["native"]
                port = PORTS[ts["server_id"].replace("-", "_")]
                health = request("GET", f"http://127.0.0.1:{port}/health").json()
                if not health["busy"]:
                    assert health["active_job"] is None
                    event("target_stopped_slot_free", jobs=record["jobs"], health=health)
                    released = True
            if released and not continued and survivor["native"]:
                assert baseline and survivor["native"]["pid"] == baseline["native"]["pid"]
                assert survivor["native"]["start_ticks"] == baseline["native"]["start_ticks"]
                if (survivor["native"]["cpu_ticks"] > baseline["native"]["cpu_ticks"]
                        and survivor["processed_frames"] > baseline["processed_frames"]):
                    event("survivor_continued", before=baseline, after=survivor)
                    continued = True
            if ts["status"] == expected and ss["status"] == "succeeded":
                break
            assert time.monotonic()-started < 210, "Isolation observation timeout; no retry"
            time.sleep(.5)
        assert both_seen and released and continued and (scenario != "cancel" or cancel_sent)
        if scenario == "timeout":
            assert ts["parameters"]["execution_seconds"] == timeout_seconds
            assert ts["execution_seconds"] >= timeout_seconds and "Execution exceeded" in ts["error"]
            assert ss["parameters"]["execution_seconds"] == defaults["execution_seconds"]
        report["jobs"]["survivor"].update(validate(client, ids["survivor"], 360))
        for label, job_id in ids.items():
            state = request("GET", f"/api/jobs/{job_id}").json()
            worker = read_json(job_dir(job_id)/"worker-process.json")
            assert worker and not Path(f"/proc/{worker['pid']}").exists() and not group_processes(worker["pid"])
            child = read_json(job_dir(job_id)/"child.json")
            assert child and not Path(f"/proc/{child['pid']}").exists()
            cleanup = read_json(job_dir(job_id)/"frame-work-cleanup.json")
            assert cleanup and not (job_dir(job_id)/"sequence/rgb").exists()
            snap = request("GET", f"/api/jobs/{job_id}/snapshot").json()
            if snap["frame_id"] is not None:
                preview = request("GET", snap["frame_url"])
                assert int(preview.headers["X-Frame-Id"]) == snap["frame_id"]
            report["jobs"][label].update(state=state, worker=worker, native_history=history[label],
                cleanup=cleanup, group_empty=True, termination=read_json(job_dir(job_id)/"termination.json"))
        request("GET", f"/api/jobs/{ids['target']}/result", 409)
        tail = JsonlTail(job_dir(ids["target"])/"native/snapshots.jsonl")
        partial_count = valid = 0
        for row in tail.rows():
            assert row["type"] == "frame" and row["frame_id"] == partial_count
            assert abs(row["timestamp"]-partial_count/30) < 1e-6 and row["pose_valid"] == (row["Twc"] is not None)
            check_pose(row)
            partial_count += 1
            valid += row["pose_valid"]
        report["jobs"]["target"]["partial_export"] = {"complete_records":partial_count,
            "valid_poses":valid, "incomplete_last_record_bytes":len(tail.pending), "is_final_result":False}
        with log_path.open() as source:
            source.seek(log_offset)
            logs = [x.strip() for x in source if "POST /execute " in x and any(j in x for j in ids.values())]
        report["nginx_execute_log"] = logs
        for label, job_id in ids.items():
            assert any(job_id in x and " 202 " in x and f"server={report['jobs'][label]['state']['server_id']}" in x for x in logs)
        with Session() as s:
            for job_id, old in old_jobs.items():
                assert s.get(Job, job_id).status == old["status"] and signature(job_dir(job_id)) == old["signature"]
        assert all(digest(Path(p)) == value for p,value in config_hashes.items())
        assert request("GET", "/api/config").json()["limits"] == defaults
        assert all(digest(CV_ROOT/name) == value for name,value in cv_hashes.items())
        assert all(digest(Path(p)) == value for p,value in proof_hashes.items())
        for port in (PORTS["compute_1"], PORTS["compute_2"]):
            assert not request("GET", f"http://127.0.0.1:{port}/health").json()["busy"]
        report["checks"] = {"both_native_observed":both_seen, "target_slot_freed":released,
            "survivor_continued":continued, "old_jobs_unchanged":len(old_jobs), "configs_and_defaults_unchanged":True,
            "successful_R5_proofs_unchanged":True, "cv_hashes_unchanged":True, "groups_empty":True, "slots_free":True}
        report["passed"] = True
    except BaseException as error:
        report["passed"] = False
        report["error"] = f"{type(error).__name__}: {error}"
        for job_id in ids.values():
            try:
                request("POST", f"/api/jobs/{job_id}/cancel", 202)
            except Exception:
                pass
        until = time.monotonic()+15
        while ids and time.monotonic() < until:
            with Session() as s:
                if all(s.get(Job, j).status in TERMINAL for j in ids.values()):
                    break
            time.sleep(.1)
    finally:
        for _ in range(10):
            try:
                after = resources() | {"phase":"after", "jobs":{}}
                with Session() as s:
                    for label, job_id in ids.items():
                        job = s.get(Job, job_id)
                        after["jobs"][label] = {"status":job.status,"error":job.error,"native":native(job_id),
                            "group_members":group_processes(job.worker_pid) if job.worker_pid else []}
                series.write(json.dumps(after)+"\n")
                report["post_jobs"] = after["jobs"]
            except Exception as error:
                report["post_monitor_error"] = f"{type(error).__name__}: {error}"
                break
            time.sleep(1)
        report.update(finished_at=utc(), created_jobs=len(ids), native_history=history,
                      C_free_after_bytes=shutil.disk_usage("/mnt/c").free)
        save()
        series.close()
        client.close()
        print(json.dumps({"passed":report.get("passed"), "scenario":scenario,
                          "error":report.get("error"), "jobs":ids, "output":str(output)}))
    return report.get("passed", False)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", choices=("cancel", "timeout"), required=True)
    p.add_argument("--context", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--timeout-seconds", type=float, default=22)
    args = p.parse_args()
    raise SystemExit(0 if run(args.scenario, args.context, args.output, args.timeout_seconds) else 1)
