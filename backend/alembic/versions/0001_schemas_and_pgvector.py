"""Create module schemas and enable pgvector

Revision ID: 0001
Revises:
Create Date: 2026-11-08
"""
from collections.abc import Sequence

from alembic import op

from app.db.base import SCHEMAS

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    for schema in SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")


def downgrade() -> None:
    for schema in reversed(SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    op.execute("DROP EXTENSION IF EXISTS vector")
