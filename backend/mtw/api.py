import asyncio
import json
from pathlib import Path
import shutil
from typing import Annotated
from uuid import UUID, uuid4
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy import func, select, text
from starlette.concurrency import run_in_threadpool
from .config import LIMITS, TERMINAL, job_dir
from .contracts import JobParameters, empty_snapshot
from .db import Job, Session, now, serialize
from .files import atomic_json, read_json
from .capacity import ensure_space
from .frame_work import POLICY, cleanup_decoded_frames

app = FastAPI(title="MonoTrajectoryWeb local API", version="0.4.0")


class UploadLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = LIMITS["file_bytes"] + 65536
        length = dict(scope["headers"]).get(b"content-length")
        if length and int(length) > limit:
            return await JSONResponse({"detail": "Request exceeds file size limit"}, status_code=413)(scope, receive, send)
        consumed = 0
        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if consumed > limit:
                raise HTTPException(413, "Request exceeds file size limit")
            return message
        await self.app(scope, bounded_receive, send)


app.add_middleware(UploadLimit)


def get_job(session, job_id):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/health")
def health():
    with Session() as session:
        session.execute(text("SELECT 1"))
    return {"status": "ok", "service": "api", "authentication": "planned", "local_only": True}


@app.get("/api/config")
def public_config():
    return {"limits": LIMITS, "poll_interval_ms": round(LIMITS["snapshot_interval_seconds"] * 1000),
            "camera_model": "pinhole_opencv", "scale": "relative", "authentication": "planned"}


@app.post("/api/jobs", status_code=202)
async def create_job(video: Annotated[UploadFile, File()], parameters: Annotated[str, Form(max_length=8192)]):
    try:
        params = JobParameters.model_validate_json(parameters)
    except ValidationError as error:
        raise HTTPException(422, json.loads(error.json(include_url=False)))
    try:
        ensure_space()
    except ValueError as error:
        raise HTTPException(507, str(error))
    job_id = uuid4()
    directory = job_dir(job_id)
    directory.mkdir()
    path = directory / "input.video"
    try:
        count = 0
        with path.open("wb") as target:
            while chunk := await video.read(1024 * 1024):
                try:
                    ensure_space()
                except ValueError as error:
                    raise HTTPException(507, str(error))
                count += len(chunk)
                if count > params.file_bytes:
                    raise HTTPException(413, f"Video exceeds file_bytes={params.file_bytes}")
                await run_in_threadpool(target.write, chunk)
        if count == 0:
            raise HTTPException(422, "Empty video")
        (directory / "parameters.json").write_text(params.model_dump_json(), encoding="utf-8")
        atomic_json(directory / "frame-work-policy.json", POLICY)
        def persist():
            with Session.begin() as session:
                # Serialize admission so concurrent uploads cannot bypass the queue cap.
                session.execute(text("SELECT pg_advisory_xact_lock(7132101)"))
                pending = session.scalar(select(func.count()).select_from(Job).where(Job.status.not_in(TERMINAL)))
                if pending >= LIMITS["queued_jobs"]:
                    raise HTTPException(429, "Local pending job limit reached")
                job = Job(id=job_id, owner_id=None, parameters=params.model_dump(), input_ref="input.video")
                session.add(job)
                session.flush()
                return serialize(job)
        result = await run_in_threadpool(persist)
        return result | {"status_url": f"/api/jobs/{job_id}"}
    except BaseException:
        # Only this server-generated UUID directory can be removed.
        shutil.rmtree(directory)
        raise
    finally:
        await video.close()


@app.get("/api/jobs")
def list_jobs(limit: int = 50):
    if not 1 <= limit <= 100:
        raise HTTPException(422, "limit must be 1..100")
    with Session() as session:
        return [serialize(job) for job in session.scalars(select(Job).order_by(Job.created_at.desc()).limit(limit))]


@app.get("/api/jobs/{job_id}")
def status(job_id: UUID):
    with Session() as session:
        return serialize(get_job(session, job_id))


@app.get("/api/jobs/{job_id}/snapshot")
def snapshot(job_id: UUID):
    with Session() as session:
        job = get_job(session, job_id)
        value = read_json(job_dir(job_id) / "latest.json")
        if value is None:
            return empty_snapshot() | {"job_id": str(job_id), "server_id": job.server_id, "status": job.status,
                                       "stage": job.stage, "total_frames": job.total_frames}
        # A small atomically replaced file; never scan the raw JSONL in this route.
        return value | {"status": job.status, "stage": job.stage if job.status in TERMINAL else value["stage"]}


@app.get("/api/jobs/{job_id}/result")
def result(job_id: UUID):
    with Session() as session:
        job = get_job(session, job_id)
        if job.status != "succeeded":
            raise HTTPException(409, {"status": job.status, "error": job.error})
    value = read_json(job_dir(job_id) / "result.json")
    if value is None:
        raise HTTPException(503, "Result artifact missing")
    return value | {"artifacts": {name: f"/api/jobs/{job_id}/artifacts/{name}" for name in value["artifacts"]}}


@app.get("/api/jobs/{job_id}/artifacts/{name}")
def artifact(job_id: UUID, name: str):
    if name not in {"trajectory.json", "map_points.json", "snapshots.jsonl", "native-summary.json"}:
        raise HTTPException(404, "Unknown artifact")
    with Session() as session:
        if get_job(session, job_id).status != "succeeded":
            raise HTTPException(409, "Final result is not ready")
    path = job_dir(job_id) / "native" / name
    if not path.is_file():
        raise HTTPException(404, "Artifact missing")
    return FileResponse(path, filename=name, media_type="application/x-ndjson" if name.endswith("jsonl") else "application/json")


@app.get("/api/jobs/{job_id}/frames/{frame_id}")
def frame(job_id: UUID, frame_id: int):
    with Session() as session:
        job = get_job(session, job_id)
        if frame_id < 0 or frame_id >= job.parameters["frames"]:
            raise HTTPException(404, "Frame not found")
    directory = job_dir(job_id)
    path = directory / "previews" / f"{frame_id:06d}.png"
    if not path.is_file() and not (directory / "frame-work-policy.json").exists():
        path = directory / "sequence/rgb" / f"{frame_id:06d}.png"
    if not path.is_file():
        raise HTTPException(404, "Frame preview not published")
    return FileResponse(path, media_type="image/png", headers={"X-Frame-Id": str(frame_id)})


@app.post("/api/jobs/{job_id}/cancel", status_code=202)
def cancel(job_id: UUID):
    with Session.begin() as session:
        job = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None:
            raise HTTPException(404, "Job not found")
        if job.status not in TERMINAL:
            job.cancel_requested = True
            job.updated_at = now()
            if job.status in {"queued", "dispatching"}:
                cleanup_decoded_frames(job_dir(job_id))
                job.status = job.stage = "cancelled"
                job.finished_at = now()
        return serialize(job)
