from fastapi import APIRouter, FastAPI

from app.api.v1 import auth, health, locations, me
from app.core.config import get_settings
from app.core.errors import install_error_handlers


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0")
    install_error_handlers(app)

    app.include_router(health.router)

    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(auth.router)
    v1.include_router(me.router)
    v1.include_router(locations.router)
    app.include_router(v1)
    return app


app = create_app()
