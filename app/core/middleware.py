"""CORS, request logging, and rate-limiting middleware."""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Callable

import structlog
from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

logger = structlog.get_logger(__name__)


class LoggingMiddleware(BaseHTTPMiddleware):
    """Structured request/response logging."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.perf_counter()
        log = logger.bind(
            method=request.method,
            path=request.url.path,
            client=request.client.host if request.client else "unknown",
        )
        log.info("request_started")
        try:
            response = await call_next(request)
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            log.info(
                "request_finished",
                status_code=response.status_code,
                elapsed_ms=elapsed_ms,
            )
            response.headers["X-Process-Time"] = f"{elapsed_ms}ms"
            return response
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            log.exception("request_error", elapsed_ms=elapsed_ms, error=str(exc))
            raise


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-process sliding-window rate limiter (per IP)."""

    def __init__(self, app: Callable, requests_per_minute: int = 60) -> None:
        super().__init__(app)
        self.rpm = requests_per_minute
        self._counters: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        window = now - 60.0

        hits = self._counters[ip]
        # Remove timestamps older than 1 minute
        self._counters[ip] = [t for t in hits if t > window]
        self._counters[ip].append(now)

        if len(self._counters[ip]) > self.rpm:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"error": {"code": "RATE_LIMITED", "message": "Too many requests"}},
                headers={"Retry-After": "60"},
            )
        return await call_next(request)
