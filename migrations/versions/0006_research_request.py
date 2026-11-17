"""research requests (manager feedback loop)

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('research_request',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('organisation_id', sa.BigInteger(), nullable=False),
    sa.Column('mode', sa.String(length=20), nullable=False),
    sa.Column('questions', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('context', sa.Text(), nullable=True),
    sa.Column('review_item_id', sa.BigInteger(), nullable=True),
    sa.Column('requested_by', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('job_run_id', sa.BigInteger(), nullable=True),
    sa.Column('result_item_id', sa.BigInteger(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisation.id']),
    sa.ForeignKeyConstraint(['review_item_id'], ['review_item.id']),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('research_request')
