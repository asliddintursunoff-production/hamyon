"""merchant default item

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-12 11:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('merchants', sa.Column('item_id', sa.Integer(), nullable=True))
    op.create_foreign_key(op.f('fk_merchants_item_id_categories'), 'merchants', 'categories', ['item_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint(op.f('fk_merchants_item_id_categories'), 'merchants', type_='foreignkey')
    op.drop_column('merchants', 'item_id')
