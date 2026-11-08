from fastapi import APIRouter, FastAPI

from app.api.v1 import (
    ai,
    auth,
    company,
    einvoice,
    finance,
    health,
    inbox,
    kb,
    locations,
    masterdata,
    me,
    people,
    purchasing,
    reports,
    sales,
    stock,
    stockdocs,
)
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.core.request_context import RequestIdMiddleware


def create_app() -> FastAPI:
    settings = get_settings()
    # Interactive API docs only in dev; production exposes no schema or docs pages.
    docs = settings.is_dev
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    install_error_handlers(app)
    app.add_middleware(RequestIdMiddleware)

    app.include_router(health.router)

    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(auth.router)
    v1.include_router(me.router)
    v1.include_router(company.router)
    v1.include_router(locations.router)
    v1.include_router(people.router)
    v1.include_router(masterdata.router)
    v1.include_router(purchasing.router)
    v1.include_router(sales.router)
    v1.include_router(einvoice.router)
    v1.include_router(finance.router)
    v1.include_router(finance.issues_router)
    v1.include_router(inbox.router)
    v1.include_router(stock.router)
    v1.include_router(stockdocs.router)
    v1.include_router(reports.router)
    v1.include_router(ai.router)
    v1.include_router(kb.router)
    app.include_router(v1)
    return app


app = create_app()
