"""Small video hints for queue estimation; legacy rows are not backfilled."""
from alembic import op
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_video_info"
down_revision = "0003_authentication"


def upgrade():
    op.add_column("jobs", Column("video_info", JSONB, nullable=True))


def downgrade():
    op.drop_column("jobs", "video_info")
