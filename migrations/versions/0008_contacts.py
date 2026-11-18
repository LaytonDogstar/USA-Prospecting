"""contacts: email status, confidence and do-not-contact on people

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('person', sa.Column('email_status', sa.String(length=20), nullable=True))
    op.add_column('person', sa.Column('email_confidence', sa.Integer(), nullable=True))
    op.add_column('person', sa.Column('do_not_contact', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('person', 'do_not_contact')
    op.drop_column('person', 'email_confidence')
    op.drop_column('person', 'email_status')
