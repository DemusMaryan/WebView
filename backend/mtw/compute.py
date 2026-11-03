"""One independently owned compute slot; HTTP acceptance is detached from work."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
import sys
import time
from uuid import UUID
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, text
from .config import REPO, job_dir
from .db import Job, Session, engine, now
from .files import atomic_json, read_json
from .processes import identity, members, group_processes, reap_group, subreaper, terminate_group
from .capacity import ensure_space
from .frame_work import cleanup_decoded_frames
from .scheduler import SERVERS
from .service_auth import require_service

SERVER_ID = os.environ["MTW_SERVER_ID"]
if SERVER_ID not in SERVERS:
    raise RuntimeError("Unknown compute server; only two configured slots are allowed")
slot = asyncio.Lock()
active = None
manager_task = None
shutdown = asyncio.Event()
lease = None


@asynccontextmanager
async def lifespan(app):
    global lease
    subreaper()
    lease = engine.connect()
    lock_id = 7132110 + SERVERS.index(SERVER_ID)
    if not lease.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_id}):
        lease.close()
        raise RuntimeError(f"Another process owns {SERVER_ID}")
    lease.commit()
    try:
        # A compute restart terminates its own stale groups, never another server.
        with Session() as session:
            stale = list(session.scalars(select(Job).where(Job.server_id == SERVER_ID, Job.status == "running")))
        for job in stale:
            if job.worker_pid:
                await terminate_group(job.worker_pid, job.worker_start_ticks, job.id, job_dir(job.id))
            cleanup_decoded_frames(job_dir(job.id), job.worker_pid)
            with Session.begin() as session:
                current = session.get(Job, job.id)
                current.status = current.stage = "cancelled" if current.cancel_requested else "failed"
                current.error = "Compute restarted; owned processes terminated; no automatic replay"
                current.finished_at = current.updated_at = now()
        shutdown.clear()
        yield
    finally:
        shutdown.set()
        if manager_task and not manager_task.done():
            await manager_task
        try:
            lease.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_id})
            lease.commit()
        finally:
            lease.close()


app = FastAPI(title=f"MonoTrajectoryWeb {SERVER_ID}", lifespan=lifespan,
              dependencies=[Depends(require_service)], docs_url=None, redoc_url=None, openapi_url=None)


class Execution(BaseModel):
    job_id: UUID
    dispatch_token: UUID


@app.get("/health")
def health():
    return {"status": "ok", "server_id": SERVER_ID, "busy": slot.locked(), "active_job": active}


def accepted(job, duplicate=False):
    return JSONResponse({"job_id": str(job.id), "server_id": SERVER_ID, "status": job.status,
                         "duplicate": duplicate}, status_code=202, headers={"X-MTW-Server": SERVER_ID})


@app.post("/execute")
async def execute(request: Execution):
    global active, manager_task
    acquired = False
    try:
        with Session.begin() as session:
            job = session.scalar(select(Job).where(Job.id == request.job_id).with_for_update())
            if job is None or job.dispatch_token != request.dispatch_token:
                raise HTTPException(409, "Invalid reservation")
            if job.status == "running" and job.server_id == SERVER_ID and active == str(job.id):
                return accepted(job, duplicate=True)
            if job.status != "dispatching":
                raise HTTPException(409, "Job has already been started or finished")
            if slot.locked():
                raise HTTPException(409, "Compute slot is occupied; retry same reservation")
            if job.cancel_requested:
                cleanup_decoded_frames(job_dir(job.id))
                job.status = job.stage = "cancelled"
                job.finished_at = job.updated_at = now()
                return accepted(job)
            await slot.acquire()
            acquired = True
            job.status = "running"
            job.stage = "decoding"
            job.server_id = SERVER_ID
            job.started_at = job.updated_at = now()
            budget = job.parameters["execution_seconds"]
            session.flush()  # Partial unique index also guards this server slot.
    except BaseException:
        if acquired:
            slot.release()
        raise
    active = str(request.job_id)
    manager_task = asyncio.create_task(run_job(request.job_id, budget))
    return accepted(job)


async def run_job(job_id, budget):
    global active
    directory = job_dir(job_id)
    command = [sys.executable, "-m", "mtw.worker", "--video", str(directory / "input.video"),
               "--parameters", str(directory / "parameters.json"), "--output", str(directory),
               "--job-id", active, "--server-id", SERVER_ID]
    child = None
    ticks = None
    started = time.monotonic()
    status, error = "failed", None
    try:
        # Immutable per-job launch guard, also surviving a dispatcher restart.
        with (directory / "execution-start.json").open("x") as launch:
            json.dump({"job_id": str(job_id), "server_id": SERVER_ID,
                       "accepted_at": now().isoformat()}, launch)
        with (directory / "worker.log").open("wb") as log:
            child = await asyncio.create_subprocess_exec(*command, cwd=REPO, stdout=log,
                stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
            process_identity = identity(child.pid)
            ticks = process_identity["start_ticks"]
            atomic_json(directory / "worker-process.json", process_identity)
            with Session.begin() as session:
                job = session.get(Job, job_id)
                job.worker_pid, job.worker_start_ticks = child.pid, ticks
            while True:
                with Session.begin() as session:
                    job = session.get(Job, job_id)
                    cancel = job.cancel_requested
                    latest = read_json(directory / "latest.json")
                    if latest:
                        job.stage = latest["stage"]
                        job.processed_frames = latest.get("processed_frames", 0)
                        job.total_frames = latest.get("total_frames")
                        job.snapshot_ref = "latest.json"
                        job.updated_at = now()
                if cancel or shutdown.is_set():
                    status = "cancelled"
                    error = "Cancelled by request; worker process group terminated"
                    await terminate_group(child.pid, ticks, job_id, directory)
                    break
                if time.monotonic() - started >= budget:
                    status = "timed_out"
                    error = f"Execution exceeded {budget:g} seconds; queue time excluded; process group terminated"
                    await terminate_group(child.pid, ticks, job_id, directory)
                    break
                lease.execute(text("SELECT 1"))
                lease.commit()
                ensure_space()
                if child.returncode is not None:
                    if child.returncode == 0 and (directory / "result.json").exists():
                        status = "succeeded"
                    else:
                        error = read_json(directory / "error.json", {}).get("error", f"Worker exit code {child.returncode}")
                    break
                await asyncio.sleep(0.1)
            await child.wait()
            await reap_group(child.pid)
            if members(child.pid):
                await terminate_group(child.pid, ticks, job_id, directory)
                if status == "succeeded":
                    status, error = "failed", "Worker left running descendant processes"
    except BaseException as exception:
        error = f"{type(exception).__name__}: {exception}"
        if child and ticks:
            await terminate_group(child.pid, ticks, job_id, directory)
            await child.wait()
            await reap_group(child.pid)
        status = "failed"
    finally:
        remaining = group_processes(child.pid) if child else []
        if remaining:
            # Fail closed: never free a DB/process slot while a descendant lives.
            with Session.begin() as session:
                job = session.get(Job, job_id)
                job.stage = "termination_failed"
                job.error = f"Process group still alive; slot retained: {error}"
                job.updated_at = now()
            atomic_json(directory / "termination.json", {"status": "running", "error": error,
                        "group_members_after": remaining, "slot_retained": True})
            return
        try:
            cleanup_decoded_frames(directory, child.pid if child else None)
        except Exception as cleanup_error:
            if status == "succeeded":
                status = "failed"
            error = (error + "; " if error else "") + f"Frame cleanup failed: {cleanup_error}"
        elapsed = time.monotonic() - started
        with Session.begin() as session:
            job = session.get(Job, job_id)
            job.status = job.stage = status
            job.finished_at = job.updated_at = now()
            job.execution_seconds = elapsed
            job.error = error
            if status == "succeeded":
                job.result_ref = "result.json"
                manifest = read_json(directory / "result.json")
                job.processed_frames = job.total_frames = manifest["processed_frames"]
        atomic_json(directory / "termination.json", {"status": status, "error": error,
                    "worker_pid": child.pid if child else None, "group_members_after": group_processes(child.pid) if child else [],
                    "execution_seconds": elapsed})
        active = None
        slot.release()
