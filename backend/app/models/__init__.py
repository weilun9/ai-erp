"""Import every model module so Base.metadata is complete (Alembic autogenerate)."""

from app.models.core import (  # noqa: F401
    AppUser,
    Company,
    Device,
    Location,
    Permission,
    Tag,
    UserSession,
    tag_permission,
    user_location_scope,
    user_tag,
)
