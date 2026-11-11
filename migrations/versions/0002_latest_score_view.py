"""latest_score view and score lookup index

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_score_entity_type_time", "score", ["entity_id", "score_type", "computed_at"])
    op.create_index("ix_claim_entity_attr", "claim", ["entity_id", "attribute"])
    op.execute("""
        CREATE VIEW latest_score AS
        SELECT DISTINCT ON (entity_id, score_type)
               id, entity_id, score_type, value, components, weights_version_id, computed_at
        FROM score
        ORDER BY entity_id, score_type, computed_at DESC, id DESC
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS latest_score")
    op.drop_index("ix_claim_entity_attr", "claim")
    op.drop_index("ix_score_entity_type_time", "score")
