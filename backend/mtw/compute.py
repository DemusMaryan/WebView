import asyncio
from contextlib import asynccontextmanager
import os
import sys
import time
from uuid import UUID
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from .config import REPO, TERMINAL, job_dir
from .db import Job, Session, now
from .files import atomic_json, read_json
from .processes import identity, members, reap_group, subreaper, terminate_group
from .capacity import ensure_space
from .frame_work import cleanup_decoded_frames

SERVER_ID = os.environ["MTW_SERVER_ID"]
slot = asyncio.Lock()
active = None


@asynccontextmanager
async def lifespan(app):
    subreaper()
    # A crash is reported explicitly; an interrupted job is never silently rerun.
    with Session() as session:
        stale = list(session.scalars(select(Job).where(Job.server_id == SERVER_ID, Job.status == "running")))
    for job in stale:
        if job.worker_pid:
            await terminate_group(job.worker_pid, job.worker_start_ticks, job.id, job_dir(job.id))
        cleanup_decoded_frames(job_dir(job.id), job.worker_pid)
        with Session.begin() as session:
            current = session.get(Job, job.id)
            current.status = current.stage = "cancelled" if current.cancel_requested else "failed"
            current.error = "Compute restarted during execution; owned processes terminated; no automatic replay"
            current.finished_at = current.updated_at = now()
    yield


app = FastAPI(title=f"MonoTrajectoryWeb {SERVER_ID}", lifespan=lifespan)


class Execution(BaseModel):
    job_id: UUID
    dispatch_token: UUID


@app.get("/health")
def health():
    return {"status": "ok", "server_id": SERVER_ID, "busy": slot.locked(), "active_job": active}


@app.post("/execute")
async def execute(request: Execution):
    global active
    if slot.locked():
        raise HTTPException(409, "Compute slot is occupied")
    async with slot:
        with Session.begin() as session:
            job = session.scalar(select(Job).where(Job.id == request.job_id).with_for_update())
            if job is None or job.dispatch_token != request.dispatch_token:
                raise HTTPException(409, "Invalid reservation")
            if job.status != "dispatching":
                raise HTTPException(409, "Job has already been started or finished")
            if job.cancel_requested:
                job.status = job.stage = "cancelled"
                job.finished_at = now()
                return {"job_id": str(job.id), "server_id": SERVER_ID, "status": "cancelled"}
            job.status = "running"
            job.stage = "decoding"
            job.server_id = SERVER_ID
            job.started_at = job.updated_at = now()
            budget = job.parameters["execution_seconds"]
        active = str(request.job_id)
        directory = job_dir(request.job_id)
        command = [sys.executable, "-m", "mtw.worker", "--video", str(directory / "input.video"),
                   "--parameters", str(directory / "parameters.json"), "--output", str(directory),
                   "--job-id", active, "--server-id", SERVER_ID]
        child = None
        ticks = None
        started = time.monotonic()
        status, error = "failed", None
        try:
            with (directory / "worker.log").open("wb") as log:
                child = await asyncio.create_subprocess_exec(*command, cwd=REPO, stdout=log,
                    stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
                process_identity = identity(child.pid)
                ticks = process_identity["start_ticks"]
                atomic_json(directory / "worker-process.json", process_identity)
                with Session.begin() as session:
                    job = session.get(Job, request.job_id)
                    job.worker_pid, job.worker_start_ticks = child.pid, ticks
                while True:
                    with Session.begin() as session:
                        job = session.get(Job, request.job_id)
                        cancel = job.cancel_requested
                        latest = read_json(directory / "latest.json")
                        if latest:
                            job.stage = latest["stage"]
                            job.processed_frames = latest.get("processed_frames", 0)
                            job.total_frames = latest.get("total_frames")
                            job.snapshot_ref = "latest.json"
                            job.updated_at = now()
                    if cancel:
                        status = "cancelled"
                        error = "Cancelled by request; worker process group terminated"
                        await terminate_group(child.pid, ticks, request.job_id, directory)
                        break
                    if time.monotonic() - started >= budget:
                        status = "timed_out"
                        error = f"Execution exceeded {budget:g} seconds; queue time excluded; process group terminated"
                        await terminate_group(child.pid, ticks, request.job_id, directory)
                        break
                    ensure_space()
                    if child.returncode is not None:
                        if child.returncode == 0 and (directory / "result.json").exists():
                            status = "succeeded"
                        else:
                            error = read_json(directory / "error.json", {}).get("error", f"Worker exit code {child.returncode}")
                        break
                    await asyncio.sleep(0.1)
                await child.wait()
                reap_group(child.pid)
                if members(child.pid):
                    await terminate_group(child.pid, ticks, request.job_id, directory)
                    if status == "succeeded":
                        status, error = "failed", "Worker left running descendant processes"
        except BaseException as exception:
            error = f"{type(exception).__name__}: {exception}"
            if child and ticks:
                await terminate_group(child.pid, ticks, request.job_id, directory)
                await child.wait()
                reap_group(child.pid)
            status = "failed"
        finally:
            try:
                cleanup_decoded_frames(directory, child.pid if child else None)
            except Exception as cleanup_error:
                if status == "succeeded":
                    status = "failed"
                error = (error + "; " if error else "") + f"Frame cleanup failed: {cleanup_error}"
            elapsed = time.monotonic() - started
            with Session.begin() as session:
                job = session.get(Job, request.job_id)
                job.status = job.stage = status
                job.finished_at = job.updated_at = now()
                job.execution_seconds = elapsed
                job.error = error
                if status == "succeeded":
                    job.result_ref = "result.json"
                    manifest = read_json(directory / "result.json")
                    job.processed_frames = job.total_frames = manifest["processed_frames"]
            atomic_json(directory / "termination.json", {"status": status, "error": error,
                        "worker_pid": child.pid if child else None, "group_members_after": members(child.pid) if child else [],
                        "execution_seconds": elapsed})
            active = None
        return {"job_id": str(request.job_id), "server_id": SERVER_ID, "status": status, "error": error}
