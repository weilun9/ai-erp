from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

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
    pos,
    purchasing,
    reports,
    sales,
    stock,
    stockdocs,
    supnotes,
)
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.core.request_context import RequestIdMiddleware

# Path prefixes that only answer GET on the public demo.
DEMO_READ_ONLY = tuple(
    f"/api/v1/{p}" for p in ("users", "tags", "devices", "company", "kb/documents", "inbox")
)


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

    @app.middleware("http")
    async def demo_guard(request: Request, call_next):
        """Public demo: visitors share the demo accounts, so nobody may change passwords,
        users, tags, devices or company settings, or upload files."""
        if (
            get_settings().demo_mode
            and request.method not in ("GET", "HEAD", "OPTIONS")
            and request.url.path.startswith(DEMO_READ_ONLY)
        ):
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "DEMO_READ_ONLY",
                        "message": "This is switched off on the public demo",
                        "details": {},
                    }
                },
            )
        return await call_next(request)

    v1 = APIRouter(prefix="/api/v1")

    @v1.get("/public/demo", tags=["public"])
    def demo_info() -> dict:
        """What the login page needs before anyone is logged in."""
        s = get_settings()
        if not s.demo_mode:
            return {"demo": False}
        accounts = [
            {"username": u.strip(), "label": label.strip()}
            for u, _, label in (a.partition(":") for a in s.demo_accounts.split(",") if a.strip())
        ]
        return {"demo": True, "accounts": accounts, "password": s.demo_password}

    v1.include_router(auth.router)
    v1.include_router(me.router)
    v1.include_router(company.router)
    v1.include_router(locations.router)
    v1.include_router(people.router)
    v1.include_router(masterdata.router)
    v1.include_router(purchasing.router)
    v1.include_router(supnotes.router)
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
    v1.include_router(pos.router)
    app.include_router(v1)
    return app


app = create_app()
