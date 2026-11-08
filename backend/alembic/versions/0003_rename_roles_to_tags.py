"""Rename roles to tags (role -> tag, role_permission -> tag_permission, user_role -> user_tag)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, old constraint name, new constraint name) after the table rename
_CONSTRAINTS = [
    ("tag", "pk_role", "pk_tag"),
    ("tag", "uq_role_company_id", "uq_tag_company_id"),
    ("tag", "fk_role_company_id_company", "fk_tag_company_id_company"),
    ("tag_permission", "pk_role_permission", "pk_tag_permission"),
    (
        "tag_permission",
        "fk_role_permission_permission_code_permission",
        "fk_tag_permission_permission_code_permission",
    ),
    ("tag_permission", "fk_role_permission_role_id_role", "fk_tag_permission_tag_id_tag"),
    ("user_tag", "pk_user_role", "pk_user_tag"),
    ("user_tag", "fk_user_role_role_id_role", "fk_user_tag_tag_id_tag"),
    ("user_tag", "fk_user_role_user_id_app_user", "fk_user_tag_user_id_app_user"),
]


def upgrade() -> None:
    op.rename_table("role", "tag", schema="core")
    op.rename_table("role_permission", "tag_permission", schema="core")
    op.rename_table("user_role", "user_tag", schema="core")
    op.alter_column("tag_permission", "role_id", new_column_name="tag_id", schema="core")
    op.alter_column("user_tag", "role_id", new_column_name="tag_id", schema="core")
    for table, old, new in _CONSTRAINTS:
        op.execute(f"ALTER TABLE core.{table} RENAME CONSTRAINT {old} TO {new}")
    op.execute("ALTER INDEX core.ix_core_role_company_id RENAME TO ix_core_tag_company_id")


def downgrade() -> None:
    op.execute("ALTER INDEX core.ix_core_tag_company_id RENAME TO ix_core_role_company_id")
    for table, old, new in _CONSTRAINTS:
        op.execute(f"ALTER TABLE core.{table} RENAME CONSTRAINT {new} TO {old}")
    op.alter_column("user_tag", "tag_id", new_column_name="role_id", schema="core")
    op.alter_column("tag_permission", "tag_id", new_column_name="role_id", schema="core")
    op.rename_table("user_tag", "user_role", schema="core")
    op.rename_table("tag_permission", "role_permission", schema="core")
    op.rename_table("tag", "role", schema="core")
