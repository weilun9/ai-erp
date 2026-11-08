"""Uniform error envelope: {"error": {"code", "message", "details"}} (design doc section 12)."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    status_code = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class BadRequest(AppError):
    status_code = 400
    code = "BAD_REQUEST"


class Unauthenticated(AppError):
    status_code = 401
    code = "UNAUTHENTICATED"


class Forbidden(AppError):
    status_code = 403
    code = "FORBIDDEN"


class DeviceNotAuthorized(Forbidden):
    """Correct credentials, but this computer has not been approved by the owner."""

    code = "DEVICE_NOT_AUTHORIZED"


class NotFound(AppError):
    """Also used when the caller may not see the row, so existence is not leaked."""

    status_code = 404
    code = "NOT_FOUND"


class Conflict(AppError):
    status_code = 409
    code = "CONFLICT"


class VersionConflict(Conflict):
    """Someone else saved this record after you loaded it; reload and retry."""

    code = "VERSION_CONFLICT"


class BusinessRule(AppError):
    status_code = 422
    code = "BUSINESS_RULE"


class StockInsufficient(BusinessRule):
    code = "STOCK_INSUFFICIENT"


class DocState(Conflict):
    """The document's status does not allow this action (e.g. editing a posted document)."""

    code = "DOC_STATE"


class DuplicateSupplierDoc(Conflict):
    code = "DUPLICATE_SUPPLIER_DOC"


def _body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


_HTTP_CODES = {
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(exc.code, exc.message, exc.details),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse(
            status_code=400,
            content=_body("VALIDATION_ERROR", "Invalid request", {"fields": fields}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(code, str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )
