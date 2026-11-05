"""Readiness and durable bounded launch reservation for an nspawn third slot."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0005_compute_servers"
down_revision = "0004_video_info"


def upgrade():
    table = op.create_table("compute_servers",
        sa.Column("server_id",sa.String(32),primary_key=True),
        sa.Column("kind",sa.String(16),nullable=False),
        sa.Column("state",sa.String(16),nullable=False),
        sa.Column("launch_token",UUID(as_uuid=True)),sa.Column("attempts",sa.Integer(),nullable=False),
        sa.Column("retry_after",sa.DateTime(timezone=True)),sa.Column("started_at",sa.DateTime(timezone=True)),
        sa.Column("ready_at",sa.DateTime(timezone=True)),sa.Column("heartbeat_at",sa.DateTime(timezone=True)),
        sa.Column("process",JSONB),sa.Column("error",sa.Text()),
        sa.CheckConstraint("server_id IN ('compute-1','compute-2','compute-3')",name="ck_compute_server_ids"),
        sa.CheckConstraint("state IN ('stopped','starting','ready','failed','stopping')",name="ck_compute_server_states"))
    op.bulk_insert(table,[{"server_id":f"compute-{i}","kind":"native" if i<3 else "nspawn","state":"stopped","attempts":0} for i in (1,2,3)])


def downgrade():
    op.drop_table("compute_servers")
