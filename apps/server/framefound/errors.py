"""Structured error responses.

Every non-2xx response uses one envelope so the web app and the future
Premiere panel can handle errors uniformly:

    {"error": {"code": "<machine_readable>", "message": "<human_readable>", "detail": ...}}

Internal details (stack traces, SQL, paths) never leave the server.
"""

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger()

_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "unavailable",
}


def error_response(
    status: int,
    message: str,
    detail: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = {"error": {"code": _CODES.get(status, "error"), "message": message, "detail": detail}}
    return JSONResponse(status_code=status, content=body, headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_exc(_req: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return error_response(exc.status_code, message, headers=dict(exc.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def validation_exc(_req: Request, exc: RequestValidationError) -> JSONResponse:
        # `ctx` carries the raw exception object for a model-level validator,
        # which json.dumps cannot encode — so the handler meant to turn a bad
        # request into a tidy 422 raised inside itself and the caller got a 500
        # instead. Dropping `ctx` fixes the cause rather than the symptom: the
        # human-readable reason is already in `msg`, and an exception instance
        # is exactly the kind of internal detail this module exists to keep in.
        # jsonable_encoder then covers whatever `input` happens to hold.
        detail = [
            {key: value for key, value in error.items() if key != "ctx"} for error in exc.errors()
        ]
        return error_response(422, "Invalid request", detail=jsonable_encoder(detail))

    @app.exception_handler(Exception)
    async def unhandled_exc(req: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_exception", path=req.url.path, exc_info=exc)
        return error_response(500, "Something went wrong on the server")
