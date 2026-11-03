"""Local users, hashed server sessions and nullable legacy job ownership."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0003_authentication"
down_revision = "0002_active_server"


def upgrade():
    op.create_table("users", sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("login", sa.String(32), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("auth_sessions", sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_foreign_key("fk_jobs_owner", "jobs", "users", ["owner_id"], ["id"])
    op.create_index("ix_jobs_owner_created_id", "jobs", ["owner_id", "created_at", "id"])
    # No UPDATE of jobs: all legacy ownership stays unchanged.


def downgrade():
    op.drop_index("ix_jobs_owner_created_id", table_name="jobs")
    op.drop_constraint("fk_jobs_owner", "jobs", type_="foreignkey")
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("users")
