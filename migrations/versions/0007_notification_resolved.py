"""notifications can be resolved for everyone

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('notification', sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('notification', sa.Column('resolved_by', sa.Text(), nullable=True))
    op.add_column('notification', sa.Column('resolution', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('notification', 'resolution')
    op.drop_column('notification', 'resolved_by')
    op.drop_column('notification', 'resolved_at')
