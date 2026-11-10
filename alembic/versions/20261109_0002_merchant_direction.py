"""merchant rules per direction (money in vs money out)

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("merchants", sa.Column("direction", sa.String(length=3), nullable=True))
    # Existing rules: 'in' only if every transaction of that merchant was incoming.
    op.execute(
        """
        UPDATE merchants m SET direction = CASE
            WHEN EXISTS (SELECT 1 FROM transactions t WHERE t.merchant_id = m.id AND t.direction = 'out') THEN 'out'
            WHEN EXISTS (SELECT 1 FROM transactions t WHERE t.merchant_id = m.id AND t.direction = 'in') THEN 'in'
            ELSE 'out' END
        """
    )
    op.alter_column("merchants", "direction", nullable=False, server_default="out")
    op.drop_constraint("uq_merchants_normalized_name", "merchants", type_="unique")
    op.create_unique_constraint("uq_merchants_name_direction", "merchants", ["normalized_name", "direction"])


def downgrade() -> None:
    op.drop_constraint("uq_merchants_name_direction", "merchants", type_="unique")
    op.execute("DELETE FROM merchants WHERE direction = 'in' AND normalized_name IN (SELECT normalized_name FROM merchants WHERE direction = 'out')")
    op.create_unique_constraint("uq_merchants_normalized_name", "merchants", ["normalized_name"])
    op.drop_column("merchants", "direction")
