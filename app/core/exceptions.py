"""Unified error response envelope for every error path in the app:
Pydantic/FastAPI validation errors, raw HTTPExceptions, our own AppError
hierarchy, storage-layer errors, and unhandled exceptions all render as
the same JSON shape:

    {"error": {"code": "...", "message": "...", "request_id": "...", ...}}

so a frontend (or any API consumer) writes exactly one error-handling
code path, never one per exception type.
"""
import structlog


class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, **extra):
        self.message = message
        self.extra = extra
        super().__init__(message)


class NotFoundError(AppError):
    status_code, code = 404, "not_found"


class ForbiddenError(AppError):
    status_code, code = 403, "forbidden"


class UnauthorizedError(AppError):
    status_code, code = 401, "unauthorized"


class ConflictError(AppError):
    status_code, code = 409, "conflict"


class ValidationAppError(AppError):
    status_code, code = 422, "validation_error"


class RateLimitError(AppError):
    status_code, code = 429, "rate_limited"

    def __init__(self, retry_after: int):
        super().__init__("Too many requests", retry_after=retry_after)


class PaymentError(AppError):
    status_code, code = 402, "payment_error"


def _error_envelope(code: str, message: str, request_id: str | None, **extra) -> dict:
    return {"error": {"code": code, "message": message, "request_id": request_id, **extra}}


def register_exception_handlers(app) -> None:
    from fastapi import Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    log = structlog.get_logger("errors")

    def _request_id() -> str | None:
        return structlog.contextvars.get_contextvars().get("request_id")

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        if exc.status_code >= 500:
            log.error("app_error", code=exc.code, message=exc.message, **exc.extra)
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_envelope(exc.code, exc.message, _request_id(), **exc.extra),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        # Pydantic/FastAPI's own 422 shape is {"detail": [...]} — normalize
        # it into the same envelope as every other error, with per-field
        # detail preserved under "details" rather than dropped.
        details = [
            {"field": ".".join(str(p) for p in err["loc"] if p != "body"), "message": err["msg"],
             "type": err["type"]}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_error_envelope("validation_error", "Request validation failed",
                                     _request_id(), details=details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        # Catches FastAPI/Starlette's own raised HTTPExceptions (e.g. 404
        # for an unmatched route, 405 for a wrong method) that never pass
        # through our AppError hierarchy — without this handler these
        # would render in Starlette's default {"detail": "..."} shape
        # instead of our envelope.
        code = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized",
                403: "forbidden"}.get(exc.status_code, "http_error")
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_envelope(code, str(exc.detail), _request_id()),
        )

    # Imported lazily to avoid a module-load-order dependency from this
    # low-level core module onto app.storage — registered here so
    # storage-layer errors map to correct HTTP codes (404/400) instead of
    # falling through to a generic 500.
    from app.storage.base import InvalidStorageKeyError, ObjectNotFoundError, StorageError

    @app.exception_handler(ObjectNotFoundError)
    async def object_not_found_handler(request: Request, exc: ObjectNotFoundError):
        return JSONResponse(status_code=404, content=_error_envelope("not_found", "Object not found", _request_id()))

    @app.exception_handler(InvalidStorageKeyError)
    async def invalid_storage_key_handler(request: Request, exc: InvalidStorageKeyError):
        log.error("invalid_storage_key", message=str(exc))
        return JSONResponse(status_code=403, content=_error_envelope("forbidden", "Access denied", _request_id()))

    @app.exception_handler(StorageError)
    async def storage_error_handler(request: Request, exc: StorageError):
        log.error("storage_error", message=str(exc))
        return JSONResponse(
            status_code=502,
            content=_error_envelope("storage_error", "A storage operation failed", _request_id()),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        log.exception("unhandled_exception")
        return JSONResponse(
            status_code=500,
            content=_error_envelope("internal_error", "An unexpected error occurred", _request_id()),
        )
