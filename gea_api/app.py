"""The Litestar application of the GEA API."""
from __future__ import annotations

import logging

from litestar import Litestar, Request, Response
from litestar.config.cors import CORSConfig
from litestar.datastructures import State
from litestar.exceptions import HTTPException

from .auth import Authenticator, authenticator_for
from .config import Settings
from .db import Database
from .errors import ApiError
from .routes import Gea, build_router, error_response

log = logging.getLogger("gea_api")

# What a browser on another origin may send and read. Without the exposed headers a
# Vue client could not see the ETag it has to send back, or where a new resource is.
REQUEST_HEADERS = ["Authorization", "Content-Type", "If-Match", "If-None-Match", "Idempotency-Key", "X-Request-Id"]
RESPONSE_HEADERS = ["ETag", "Location", "X-Request-Id", "Retry-After"]

_HTTP_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
               405: "bad_request", 413: "bad_request", 415: "bad_request", 422: "validation_failed",
               503: "service_unavailable"}


def on_api_error(request: Request, error: ApiError) -> Response:
    return error_response(request, error)


def on_http_exception(request: Request, error: HTTPException) -> Response:
    """Errors raised by Litestar itself (unknown route, malformed JSON, wrong method)."""
    status = error.status_code
    if status >= 500:
        return on_unexpected(request, error)
    code = _HTTP_CODES.get(status, "bad_request")
    message = {404: "There is nothing at this address.", 405: "This method is not allowed here."}.get(
        status, error.detail or None)
    return error_response(request, ApiError(status, code, message))


def on_unexpected(request: Request, error: Exception) -> Response:
    """Never leak an exception text: log it with the request id, answer with the generic error."""
    response = error_response(request, ApiError(500, "internal_error"))
    log.error("unhandled error, request %s %s", response.headers.get("X-Request-Id"), request.url.path,
              exc_info=error)
    return response


def create_app(settings: Settings | None = None, *, authenticator: Authenticator | None = None,
               database: Database | None = None) -> Litestar:
    settings = settings or Settings.from_environment()
    authenticator = authenticator or authenticator_for(settings.auth_mode)
    database = database or Database(settings.database_url, min_size=settings.pool_min_size,
                                    max_size=settings.pool_max_size,
                                    statement_timeout_ms=settings.statement_timeout_ms)
    return Litestar(
        route_handlers=[build_router()],
        state=State({"gea": Gea(db=database, authenticator=authenticator)}),
        on_startup=[database.open],
        on_shutdown=[database.close],
        cors_config=CORSConfig(
            allow_origins=list(settings.allowed_origins),
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=REQUEST_HEADERS, expose_headers=RESPONSE_HEADERS, max_age=600),
        exception_handlers={ApiError: on_api_error, HTTPException: on_http_exception, Exception: on_unexpected},
        openapi_config=None,        # the contract is gea/api/openapi.yaml, served at /gea/v1/openapi.yaml
        debug=False,
    )
