"""FTS search: fts_vector() helper + GIN index on messages

Revision ID: 012
Revises: 011
Create Date: 2026-10-21

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from src.db.fts import FTS_STATEMENTS

revision: str = '012'
down_revision: Union[str, None] = '011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for statement in FTS_STATEMENTS:
        op.execute(sa.text(statement))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS messages_fts"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS fts_vector(text)"))
