from datetime import datetime, timezone
from uuid import UUID, uuid4
from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .config import database_url


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("uq_jobs_active_server", "server_id", unique=True,
        postgresql_where=text("status IN ('dispatching', 'running') AND server_id IS NOT NULL")),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    parameters: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(24), index=True, default="queued")
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    processed_frames: Mapped[int] = mapped_column(Integer, default=0)
    total_frames: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    server_id: Mapped[str | None] = mapped_column(String(64))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    dispatch_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    input_ref: Mapped[str] = mapped_column(Text)
    result_ref: Mapped[str | None] = mapped_column(Text)
    snapshot_ref: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    worker_pid: Mapped[int | None] = mapped_column(Integer)
    worker_start_ticks: Mapped[str | None] = mapped_column(String(32))
    execution_seconds: Mapped[float | None] = mapped_column(Float)


engine = create_engine(database_url(), pool_pre_ping=True, pool_size=5, max_overflow=5)
Session = sessionmaker(engine, expire_on_commit=False)


def serialize(job):
    fields = ("id", "owner_id", "status", "stage", "processed_frames", "total_frames", "created_at",
              "started_at", "finished_at", "updated_at", "server_id", "cancel_requested", "error",
              "worker_pid", "execution_seconds")
    values = {key: getattr(job, key) for key in fields}
    return {key: (value.isoformat() if isinstance(value, datetime) else str(value) if isinstance(value, UUID) else value)
            for key, value in values.items()} | {
        "parameters": job.parameters, "has_result": job.result_ref is not None,
        "has_snapshot": job.snapshot_ref is not None, "eta_seconds": None}
