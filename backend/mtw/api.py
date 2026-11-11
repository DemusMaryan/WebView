import asyncio
import json
from pathlib import Path
import shutil
from typing import Annotated
from uuid import UUID, uuid4
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import func, select, text
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .config import LIMITS, TERMINAL, job_dir
from .contracts import JobParameters, empty_snapshot
from .db import Job, Session, now, serialize
from .files import atomic_json, read_json
from .capacity import ensure_space
from .frame_work import POLICY, cleanup_decoded_frames
from .auth import BrowserSecurity, router as auth_router
from .proxy import TrustedTLSProxy
from .queue_eta import estimate_queue, probe_video
from .admin import router as admin_router
from .servers import SERVERS

app = FastAPI(title="MonoTrajectoryWeb local API", version="0.8.0")
app.include_router(auth_router)
app.include_router(admin_router)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exception):
    # Pydantic's default error includes input, which can contain a password.
    return JSONResponse({"detail": [{k: row[k] for k in ("loc", "msg", "type")}
                                     for row in exception.errors()]}, status_code=422)


class UploadLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = 8192 if scope["path"].startswith("/api/auth/") else LIMITS["file_bytes"] + 65536
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
app.add_middleware(BrowserSecurity)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
app.add_middleware(TrustedTLSProxy)


def owner(request):
    return UUID(request.state.auth["user"]["id"])


def get_job(session, job_id, request):
    job = session.scalar(select(Job).where(Job.id == job_id, Job.owner_id == owner(request)))
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/health")
def health():
    with Session() as session:
        session.execute(text("SELECT 1"))
    return {"status": "ok", "service": "api", "authentication": "sessions", "local_only": True}


@app.get("/api/config")
def public_config():
    return {"limits": LIMITS, "poll_interval_ms": round(LIMITS["snapshot_interval_seconds"] * 1000),
            "camera_model": "pinhole_opencv", "scale": "relative", "authentication": "sessions"}


@app.post("/api/jobs", status_code=202)
async def create_job(request: Request, video: Annotated[UploadFile, File()], parameters: Annotated[str, Form(max_length=8192)]):
    if set(await request.form()) - {"video", "parameters"}:
        raise HTTPException(422, "Only video and parameters form fields are accepted")
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
        video_info = await run_in_threadpool(probe_video, path)
        def persist():
            with Session.begin() as session:
                # The count and insert share this transaction/lock, including
                # completed jobs. Uploads that lose admission are removed below.
                session.execute(text("SELECT pg_advisory_xact_lock(7132101)"))
                stored = session.scalar(select(func.count()).select_from(Job))
                if stored >= LIMITS["stored_jobs"]:
                    raise HTTPException(429, {"code": "stored_job_limit_reached",
                        "message": "Stored job limit reached; history and results remain available",
                        "limit": LIMITS["stored_jobs"], "count": stored})
                pending = session.scalar(select(func.count()).select_from(Job).where(Job.status.not_in(TERMINAL)))
                if pending >= LIMITS["queued_jobs"]:
                    raise HTTPException(429, {"code": "pending_job_limit_reached",
                        "message": "Pending job limit reached", "limit": LIMITS["queued_jobs"], "count": pending})
                job = Job(id=job_id, owner_id=owner(request), parameters=params.model_dump(), input_ref="input.video", video_info=video_info)
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
def list_jobs(request: Request, page: int = Query(1, ge=1, le=2147483647),
              page_size: int = Query(20, ge=1, le=100)):
    unknown = set(request.query_params) - {"page", "page_size"}
    if unknown or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise HTTPException(422, {"code": "invalid_pagination_parameter",
                                 "message": "Use one page and page_size value; unknown parameters are not supported"})
    with Session.begin() as session:
        # Count and items describe one DB snapshot without blocking admission.
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        total = session.scalar(select(func.count()).select_from(Job).where(Job.owner_id == owner(request)))
        pages = (total + page_size - 1) // page_size
        items = [serialize(job) for job in session.scalars(select(Job).where(Job.owner_id == owner(request))
            .order_by(Job.created_at.desc(), Job.id.desc()).offset((page-1)*page_size).limit(page_size))]
        return {"items": items, "total": total, "page": page, "page_size": page_size,
                "total_pages": pages, "has_next": page < pages,
                "has_previous": page > 1 and total > 0, "stored_job_limit": LIMITS["stored_jobs"]}


@app.get("/api/jobs/{job_id}")
def status(job_id: UUID, request: Request):
    with Session.begin() as session:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        job = get_job(session, job_id, request)
        return serialize(job) | estimate_queue(session, job)


@app.get("/api/jobs/{job_id}/snapshot")
def snapshot(job_id: UUID, request: Request):
    with Session() as session:
        job = get_job(session, job_id, request)
        value = read_json(job_dir(job_id) / "latest.json")
        if value is None:
            return empty_snapshot() | {"job_id": str(job_id), "server_id": job.server_id, "status": job.status,
                                       "stage": job.stage, "total_frames": job.total_frames}
        # A small atomically replaced file; never scan the raw JSONL in this route.
        return value | {"status": job.status, "stage": job.stage if job.status in TERMINAL else value["stage"]}


@app.get("/api/jobs/{job_id}/result")
def result(job_id: UUID, request: Request):
    with Session() as session:
        job = get_job(session, job_id, request)
        if job.status != "succeeded":
            raise HTTPException(409, {"status": job.status, "error": job.error})
    value = read_json(job_dir(job_id) / "result.json")
    if value is None:
        raise HTTPException(503, "Result artifact missing")
    return value | {"artifacts": {name: f"/api/jobs/{job_id}/artifacts/{name}" for name in value["artifacts"]}}


@app.get("/api/jobs/{job_id}/artifacts/{name}")
def artifact(job_id: UUID, name: str, request: Request):
    if name not in {"trajectory.json", "map_points.json", "snapshots.jsonl", "native-summary.json"}:
        raise HTTPException(404, "Unknown artifact")
    with Session() as session:
        if get_job(session, job_id, request).status != "succeeded":
            raise HTTPException(409, "Final result is not ready")
    path = job_dir(job_id) / "native" / name
    if not path.is_file():
        raise HTTPException(404, "Artifact missing")
    return FileResponse(path, filename=name, media_type="application/x-ndjson" if name.endswith("jsonl") else "application/json")


@app.get("/api/jobs/{job_id}/frames/{frame_id}")
def frame(job_id: UUID, frame_id: int, request: Request):
    with Session() as session:
        job = get_job(session, job_id, request)
        if frame_id < 0 or frame_id >= job.parameters["frames"]:
            raise HTTPException(404, "Frame not found")
    directory = job_dir(job_id)
    path = directory / "previews" / f"{frame_id:06d}.png"
    if not path.is_file() and not (directory / "frame-work-policy.json").exists():
        path = directory / "sequence/rgb" / f"{frame_id:06d}.png"
    if not path.is_file():
        raise HTTPException(404, "Frame preview not published")
    return FileResponse(path, media_type="image/png", headers={"X-Frame-Id": str(frame_id)})


@app.get("/api/jobs/{job_id}/video")
def video(job_id: UUID, request: Request):
    with Session() as session:
        get_job(session, job_id, request)
    path = job_dir(job_id) / "input.video"
    if not path.is_file():
        raise HTTPException(404, "Source video missing")
    return FileResponse(path, filename="source.video", media_type="application/octet-stream")


@app.post("/api/jobs/{job_id}/cancel", status_code=202)
def cancel(job_id: UUID, request: Request):
    with Session.begin() as session:
        job = session.scalar(select(Job).where(Job.id == job_id, Job.owner_id == owner(request)).with_for_update())
        if job is None:
            raise HTTPException(404, "Job not found")
        if job.status not in TERMINAL:
            job.cancel_requested = True
            job.updated_at = now()
            if job.status == 'dispatching' and (job.server_id not in SERVERS or job.worker_pid
                    or (job_dir(job_id)/'execution-start.json').exists()):
                job.stage = 'assignment_unresolved'
                job.error = 'Cancellation requested; ambiguous legacy launch requires process reconciliation'
            elif job.status in {"queued", "dispatching"}:
                cleanup_decoded_frames(job_dir(job_id))
                job.status = job.stage = "cancelled"
                job.finished_at = now()
        return serialize(job)
