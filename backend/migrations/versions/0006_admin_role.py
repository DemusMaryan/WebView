"""Explicit admin role; existing and newly registered accounts stay ordinary."""
from alembic import op
import sqlalchemy as sa

revision='0006_admin_role'
down_revision='0005_compute_servers'


def upgrade():
    op.add_column('users',sa.Column('is_admin',sa.Boolean(),server_default=sa.false(),nullable=False))


def downgrade():
    op.drop_column('users','is_admin')
