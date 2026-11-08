"""Rename device.can_edit to is_authorized (unauthorized computers cannot log in at all)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("device", "can_edit", new_column_name="is_authorized", schema="core")
    # Sessions opened from unauthorized computers under the old rule are no longer valid.
    op.execute(
        "UPDATE core.user_session s SET revoked_at = now() FROM core.device d "
        "WHERE s.device_id = d.id AND NOT d.is_authorized AND s.revoked_at IS NULL"
    )


def downgrade() -> None:
    op.alter_column("device", "is_authorized", new_column_name="can_edit", schema="core")
