from datetime import datetime, timezone
from uuid import UUID, uuid4
from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .config import database_url


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    login: Mapped[str] = mapped_column(String(32), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ComputeServer(Base):
    __tablename__ = "compute_servers"
    __table_args__ = (CheckConstraint("server_id IN ('compute-1','compute-2','compute-3')",name="ck_compute_server_ids"),
        CheckConstraint("state IN ('stopped','starting','ready','failed','stopping')",name="ck_compute_server_states"))
    server_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), default="stopped")
    launch_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    process: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("uq_jobs_active_server", "server_id", unique=True,
        postgresql_where=text("status IN ('dispatching', 'running') AND server_id IS NOT NULL")),
        Index("ix_jobs_owner_created_id", "owner_id", "created_at", "id"))
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
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
    video_info: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


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
        "has_snapshot": job.snapshot_ref is not None, "eta_seconds": None,
        "worker_pid_scope": "container:compute-3" if job.server_id == "compute-3" else "host",
        "queue_position": None, "estimated_wait_seconds": None}
