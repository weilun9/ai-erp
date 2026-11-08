"""Import every model module so Base.metadata is complete (Alembic autogenerate)."""

from app.models.core import (  # noqa: F401
    AppUser,
    Company,
    Location,
    Permission,
    Role,
    role_permission,
    user_location_scope,
    user_role,
)
