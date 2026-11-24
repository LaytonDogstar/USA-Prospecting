"""per-market budgets and usage; the markets each user can see

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('provider_budget', sa.Column('market', sa.String(length=2), nullable=False, server_default='US'))
    op.drop_constraint('provider_budget_pkey', 'provider_budget', type_='primary')
    op.create_primary_key('provider_budget_pkey', 'provider_budget', ['provider', 'market'])
    op.add_column('api_usage', sa.Column('market', sa.String(length=2), nullable=False, server_default='US'))
    op.add_column('app_user', sa.Column('markets', postgresql.ARRAY(sa.String(length=2)), nullable=False,
                                        server_default='{US,AU}'))


def downgrade() -> None:
    op.drop_column('app_user', 'markets')
    op.drop_column('api_usage', 'market')
    op.execute("DELETE FROM provider_budget WHERE market <> 'US'")
    op.drop_constraint('provider_budget_pkey', 'provider_budget', type_='primary')
    op.create_primary_key('provider_budget_pkey', 'provider_budget', ['provider'])
    op.drop_column('provider_budget', 'market')
