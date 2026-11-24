"""markets: organisation.market and tracked_keyword.market (US / AU)

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('organisation', sa.Column('market', sa.String(length=2), nullable=False, server_default='US'))
    op.create_index(op.f('ix_organisation_market'), 'organisation', ['market'], unique=False)
    op.add_column('tracked_keyword', sa.Column('market', sa.String(length=2), nullable=False, server_default='US'))
    op.drop_constraint('tracked_keyword_keyword_state_key', 'tracked_keyword', type_='unique')
    op.create_unique_constraint('tracked_keyword_keyword_state_market_key', 'tracked_keyword', ['keyword', 'state', 'market'])


def downgrade() -> None:
    op.drop_constraint('tracked_keyword_keyword_state_market_key', 'tracked_keyword', type_='unique')
    op.create_unique_constraint('tracked_keyword_keyword_state_key', 'tracked_keyword', ['keyword', 'state'])
    op.drop_column('tracked_keyword', 'market')
    op.drop_index(op.f('ix_organisation_market'), table_name='organisation')
    op.drop_column('organisation', 'market')
