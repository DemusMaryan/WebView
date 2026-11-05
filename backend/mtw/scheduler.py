"""PostgreSQL reserves work against fresh ready slots, bounded at three."""
from uuid import uuid4
from sqlalchemy import func, select, text
from .db import Job, Session, now
from .servers import SERVERS, ready_servers

ACTIVE = ("dispatching", "running")
MAX_ACTIVE = 3


def reserve_next(session_factory=Session):
    with session_factory.begin() as session:
        # Serialize count + claim across every dispatcher process/loop.
        session.execute(text("SELECT pg_advisory_xact_lock(7132103)"))
        count = session.scalar(select(func.count()).select_from(Job).where(Job.status.in_(ACTIVE)))
        if count >= min(MAX_ACTIVE,len(ready_servers(session))):
            return None
        job = session.scalar(select(Job).where(Job.status == "queued", Job.cancel_requested.is_(False))
                             .order_by(Job.created_at, Job.id).with_for_update().limit(1))
        if job is None:
            return None
        job.status = job.stage = "dispatching"
        job.dispatch_token = uuid4()
        job.updated_at = now()
        return {"job_id": str(job.id), "dispatch_token": str(job.dispatch_token)}


def outstanding(session_factory=Session):
    with session_factory() as session:
        return [{"job_id": str(j.id), "dispatch_token": str(j.dispatch_token)}
                for j in session.scalars(select(Job).where(Job.status == "dispatching"))]


def still_reserved(reservation, session_factory=Session):
    with session_factory() as session:
        job = session.get(Job, reservation["job_id"])
        return (job is not None and job.status == "dispatching"
                and str(job.dispatch_token) == reservation["dispatch_token"])
