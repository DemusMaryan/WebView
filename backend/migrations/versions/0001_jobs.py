"""Persistent local queue and job metadata; large artifacts stay on disk."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0001_jobs"
down_revision = None


def upgrade():
    op.create_table("jobs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("parameters", pg.JSONB(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("processed_frames", sa.Integer(), nullable=False),
        sa.Column("total_frames", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("server_id", sa.String(64)),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("dispatch_token", pg.UUID(as_uuid=True)),
        sa.Column("input_ref", sa.Text(), nullable=False),
        sa.Column("result_ref", sa.Text()), sa.Column("snapshot_ref", sa.Text()),
        sa.Column("error", sa.Text()), sa.Column("worker_pid", sa.Integer()),
        sa.Column("worker_start_ticks", sa.String(32)), sa.Column("execution_seconds", sa.Float()),
        sa.CheckConstraint("status IN ('queued','dispatching','running','succeeded','failed','cancelled','timed_out')", name="job_status"),
        sa.CheckConstraint("processed_frames >= 0", name="job_progress_nonnegative"))
    op.create_index("ix_jobs_status", "jobs", ["status"])


def downgrade():
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_table("jobs")
