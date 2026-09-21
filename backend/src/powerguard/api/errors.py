"""Uniform error envelope.

Every error the API returns has the same shape. Unexpected failures are logged
under a generated correlation id and answered generically: no stack traces,
paths, SQL, credentials, settings or raw broker payloads ever reach a client.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from powerguard.observability import log_event

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """An error with a controlled code, message and status."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class InvalidQueryError(ApiError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("INVALID_QUERY", message, status.HTTP_400_BAD_REQUEST, details)


class NotFoundError(ApiError):
    def __init__(self, message: str) -> None:
        super().__init__("NOT_FOUND", message, status.HTTP_404_NOT_FOUND)


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Field locations and messages only; input values are not echoed.
        fields = [
            {"field": ".".join(str(part) for part in item["loc"]), "issue": item["type"]}
            for item in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=error_body("VALIDATION_ERROR", "request validation failed", {"fields": fields}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == status.HTTP_404_NOT_FOUND else "HTTP_ERROR"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        correlation_id = uuid.uuid4().hex
        log_event(
            logger,
            logging.ERROR,
            "http_unexpected_error",
            correlation_id=correlation_id,
            method=request.method,
            path=request.url.path,
        )
        # The detail stays in the log, keyed by the id the client was given.
        logger.exception("http_unexpected_error_detail correlation_id=%s", correlation_id)
        del exc
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body(
                "INTERNAL_ERROR",
                "an unexpected error occurred",
                {"correlation_id": correlation_id},
            ),
        )
