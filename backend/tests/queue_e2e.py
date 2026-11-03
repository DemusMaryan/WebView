"""One explicitly invoked three-job HTTPS acceptance; no automatic retries."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import sys
import time
from uuid import uuid4
import httpx
from sqlalchemy import text
from mtw.capacity import ensure_space
from mtw.config import CV_ROOT, DATA, TERMINAL, job_dir
from mtw.db import engine
from mtw.files import atomic_json, read_json
from mtw.processes import group_processes, identity
from mtw import tls
from check_parallel_results import validate
from parallel_e2e import signature


def utc():
    return datetime.now(timezone.utc).isoformat()


def rows():
    with engine.connect() as c:
        return {str(r["id"]): dict(r) for r in c.execute(text("SELECT row_to_json(jobs) FROM jobs")).scalars()}


def private_json(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(value, f)


def prepare():
    ensure_space()
    if shutil.disk_usage("/mnt/c").free < 10*2**30:
        raise ValueError("Initial C: free below 10 GiB")
    saved = rows()
    assert len(saved) == 51 and all(r["status"] in TERMINAL for r in saved.values())
    root = DATA / "reports" / ("queue-" + uuid4().hex)
    root.mkdir(mode=0o700)
    original = read_json(DATA / "reports/prompt9-preservation-before.json")
    hashes = {name: hashlib.sha256((CV_ROOT / name).read_bytes()).hexdigest() for name in original["cv_hashes"]}
    proofs = {name: hashlib.sha256((DATA / "reports" / name).read_bytes()).hexdigest() for name in original["proofs"]}
    assert hashes == original["cv_hashes"] and proofs == original["proofs"]
    before = {"at": utc(), "rows": saved, "files": {j:signature(job_dir(j)) for j in saved},
              "cv_hashes":hashes, "proofs":proofs, "C_free_bytes":shutil.disk_usage("/mnt/c").free,
              "native_free_bytes":shutil.disk_usage(DATA).free,
              "nginx_log_offset":(DATA / "logs/nginx-access.log").stat().st_size}
    atomic_json(root / "before.json", before)
    private_json(root / "credentials.json", {"login":"queue_"+secrets.token_hex(6), "password":secrets.token_urlsafe(32)})
    print(json.dumps({"root":str(root), "existing_jobs":len(saved), "C_free_bytes":before["C_free_bytes"]}))


def client(root):
    secret = read_json(root / "session.json")
    return httpx.Client(base_url=tls.canonical_origin(), verify=ssl.create_default_context(cafile=tls.paths()["ca_cert"]),
        cookies={"mtw_session":secret["cookie"]}, headers={"Origin":tls.canonical_origin(),"X-CSRF-Token":secret["csrf"]},
        timeout=30, trust_env=False)


def native(job_id):
    child = read_json(job_dir(job_id) / "child.json")
    if not child: return None
    item = identity(child["pid"])
    if not item or str(CV_ROOT / "build/exporter/mono_export") not in item["argv"]: return None
    try:
        stat = Path(f"/proc/{child['pid']}/stat").read_text().rsplit(")",1)[1].split()
        return {"pid":item["pid"],"start_ticks":item["start_ticks"], "cpu_ticks":int(stat[11])+int(stat[12]),
                "rss_bytes":int(stat[21])*os.sysconf("SC_PAGE_SIZE")}
    except (FileNotFoundError,ProcessLookupError): return None


def observe(root):
    started = time.monotonic()
    try:
        with (root / "native-series.jsonl").open("x", buffering=1) as stream:
            while time.monotonic()-started < 300:
                manifest = read_json(root / "jobs.json", {})
                state = rows()
                active = [r for r in state.values() if r["status"] in {"running","dispatching"}]
                assert len(active) <= 2
                servers = [r["server_id"] for r in active if r["server_id"]]
                assert len(servers) == len(set(servers))
                ensure_space()  # RAM/commit deliberately never gate or cancel this test.
                sample = {"at":utc(), "monotonic":time.monotonic(), "C_free_bytes":shutil.disk_usage("/mnt/c").free,
                    "native_free_bytes":shutil.disk_usage(DATA).free, "loadavg":os.getloadavg(), "jobs":{}}
                for label, entry in manifest.items():
                    job_id = entry["id"]
                    if job_id not in state: continue
                    row = state[job_id]
                    latest = read_json(job_dir(job_id) / "latest.json", {})
                    sample["jobs"][label] = {k:row[k] for k in ("status","server_id","worker_pid","created_at","started_at","finished_at","processed_frames")}
                    sample["jobs"][label].update(native=native(job_id),frame_id=latest.get("frame_id"),tracking_state=latest.get("tracking_state"))
                stream.write(json.dumps(sample)+"\n")
                atomic_json(root / "observation.json", sample)
                if (root / "stop").exists(): break
                time.sleep(.5)
    except BaseException as error:
        atomic_json(root / "observer-error.json", {"at":utc(),"error":f"{type(error).__name__}: {error}"})
        if (root / "session.json").exists():
            with client(root) as c:
                for entry in read_json(root / "jobs.json", {}).values():
                    c.post(f"/api/jobs/{entry['id']}/cancel")
        raise


def finish(root):
    manifest = read_json(root / "jobs.json")
    before = read_json(root / "before.json")
    after = rows()
    checks = {"all_51_rows_owners_unchanged":all({k:after[j][k] for k in row} == row for j,row in before["rows"].items()),
        "all_51_file_signatures_unchanged":all(signature(job_dir(j)) == value for j,value in before["files"].items()),
        "exactly_three_new_jobs":set(after)-set(before["rows"]) == {v["id"] for v in manifest.values()},
        "core_hashes_unchanged":all(hashlib.sha256((CV_ROOT/name).read_bytes()).hexdigest()==v for name,v in before["cv_hashes"].items()),
        "R5_hashes_unchanged":all(hashlib.sha256((DATA/"reports"/name).read_bytes()).hexdigest()==v for name,v in before["proofs"].items())}
    validations = {}
    with client(root) as c:
        for label,entry in manifest.items():
            result = validate(c,entry["id"],entry["frames"])
            validations[label] = result
            directory = job_dir(entry["id"])
            terminal = read_json(directory / "termination.json")
            assert terminal["group_members_after"] == [] and group_processes(result["state"]["worker_pid"]) == []
            assert not (directory/"sequence/rgb").exists()
            snapshot = c.get(f"/api/jobs/{entry['id']}/snapshot").json()
            assert c.get(snapshot["frame_url"]).status_code == 200
    with (DATA / "logs/nginx-access.log").open() as log:
        log.seek(before["nginx_log_offset"])
        routes = [line.strip() for line in log if " /execute " in line and any(v["id"] in line for v in manifest.values())]
    atomic_json(root / "nginx-routes.json", routes)
    checks["three_nginx_handoffs"] = all(any(v["id"] in line and "202" in line for line in routes) for v in manifest.values())
    assert all(checks.values()), checks
    result = {"finished_at":utc(), "checks":checks,"jobs":validations,"C_free_after_bytes":shutil.disk_usage("/mnt/c").free,
              "C_free_before_bytes":before["C_free_bytes"],"saved_jobs":len(after),"all_validated":True}
    atomic_json(root / "validation.json", result)
    print(json.dumps({"passed":True,"jobs":{k:v["result"] for k,v in validations.items()},"saved_jobs":len(after)}))


def timing(root):
    """Read-only analysis of the saved run, usable after its session is revoked."""
    manifest = read_json(root/"jobs.json")
    validations = read_json(root/"validation.json")["jobs"]
    checks = {}
    samples = [json.loads(line) for line in (root/"native-series.jsonl").read_text().splitlines()]
    waiting = [s for s in samples if s["jobs"].get("C",{}).get("status") == "queued"
        and all(s["jobs"].get(k,{}).get("native") and s["jobs"][k]["processed_frames"] > 0 for k in ("A","B"))]
    checks["queued_while_two_real_native_processes_worked"] = len(waiting) >= 3 and all(s["jobs"]["C"]["native"] is None for s in waiting)
    first,last = waiting[0],waiting[-1]
    overlap = {"seconds_lower_bound":last["monotonic"]-waiting[1]["monotonic"],
        "native_pids":{k:first["jobs"][k]["native"]["pid"] for k in ("A","B")},
        "cpu_ticks_delta":{k:last["jobs"][k]["native"]["cpu_ticks"]-first["jobs"][k]["native"]["cpu_ticks"] for k in ("A","B")},
        "frames_delta":{k:last["jobs"][k]["processed_frames"]-first["jobs"][k]["processed_frames"] for k in ("A","B")}}
    checks["both_cpu_and_tracking_progress"] = all(v>0 for v in [*overlap["cpu_ticks_delta"].values(),*overlap["frames_delta"].values()])
    started = datetime.fromisoformat(validations["C"]["state"]["started_at"])
    ended = min(datetime.fromisoformat(validations[k]["state"]["finished_at"]) for k in ("A","B"))
    checks["third_started_after_slot_freed"] = started >= ended
    browser = read_json(root/"browser.json")
    restart = browser["dispatcher_restart"]
    checks["dispatcher_restart_preserved_queue_and_native_identity"] = (
        all(restart[phase]["jobs"]["C"]["status"] == "queued" for phase in ("before","after"))
        and all(all(restart["before"]["jobs"][k]["native"][field] == restart["after"]["jobs"][k]["native"][field]
            for field in ("pid","start_ticks")) for k in ("A","B")))
    queued = browser["first_queued"]
    prediction = queued["state"]["estimated_wait_seconds"]
    assert isinstance(prediction,(int,float)) and prediction >= 0
    wait = {"predicted_seconds":prediction, "observed_at":queued["at"],
        "actual_seconds_after_prediction":(started-datetime.fromisoformat(queued["at"].replace("Z","+00:00"))).total_seconds(),
        "actual_seconds_since_creation":(started-datetime.fromisoformat(manifest["C"]["created_at"])).total_seconds(),
        "slot_handoff_seconds":(started-ended).total_seconds()}
    wait["error_seconds"] = prediction-wait["actual_seconds_after_prediction"]
    assert all(checks.values()), checks
    result = {"checks":checks,"wait":wait,"overlap_while_third_queued":overlap}
    atomic_json(root / "queue-timing.json", result)
    print(json.dumps(result))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["prepare","session","observe","finish","timing"])
    p.add_argument("--root",type=Path)
    a=p.parse_args()
    if a.action == "prepare": return prepare()
    root=a.root.resolve()
    assert root.parent == (DATA/"reports").resolve() and root.name.startswith("queue-") and len(root.name)==38
    if a.action == "session":
        private_json(root/"session.json",json.load(sys.stdin))
    elif a.action == "observe": observe(root)
    elif a.action == "timing": timing(root)
    else: finish(root)


if __name__ == "__main__": main()
