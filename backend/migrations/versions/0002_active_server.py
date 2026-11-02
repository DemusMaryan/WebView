"""One active job per compute, including across process restarts."""
from alembic import op
import sqlalchemy as sa

revision = "0002_active_server"
down_revision = "0001_jobs"


def upgrade():
    op.create_index("uq_jobs_active_server", "jobs", ["server_id"], unique=True,
        postgresql_where=sa.text("status IN ('dispatching', 'running') AND server_id IS NOT NULL"))


def downgrade():
    op.drop_index("uq_jobs_active_server", table_name="jobs")
