"""Persist fair slot selection without changing existing jobs or ownership."""
from alembic import op
import sqlalchemy as sa

revision = '0007_slot_assignment'
down_revision = '0006_admin_role'


def upgrade():
    op.add_column('compute_servers', sa.Column('last_assigned_at', sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column('compute_servers', 'last_assigned_at')
