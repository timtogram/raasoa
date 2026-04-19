"""merge heads

Revision ID: 19dc365e7974
Revises: g7a8b9c0d1e2, g7b8c9d0e1f2
Create Date: 2026-04-19 20:30:43.200388
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '19dc365e7974'
down_revision: Union[str, None] = ('g7a8b9c0d1e2', 'g7b8c9d0e1f2')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
