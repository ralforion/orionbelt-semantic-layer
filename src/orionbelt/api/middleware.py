"""Middleware: security headers, body limits, request ID, timing, rate limiting."""

from __future__ import annotations

import collections
import logging
import threading
import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Body size limits (also enforced by Cloud Armor — keep in sync with
# infra/apply-cloud-armor.sh rules 103/106)
_MODEL_PATHS = ("/models", "/validate")
_MAX_BODY_MODEL = 5 * 1024 * 1024  # 5 MB for model load/validate
_MAX_BODY_DEFAULT = 1 * 1024 * 1024  # 1 MB for everything else


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Add a unique request ID for log correlation.

    Uses the incoming ``X-Request-Id`` header if present (e.g. from a load
    balancer), otherwise generates a UUID4.  The ID is:
    - returned in the ``X-Request-Id`` response header
    - bound to structlog context vars for automatic inclusion in all log entries
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response


class RequestTimingMiddleware(BaseHTTPMiddleware):
    """Add X-Request-Duration header with processing time."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000
        response.headers["X-Request-Duration-Ms"] = f"{duration_ms:.1f}"
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard security headers to every response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Gradio UI requires inline scripts/styles and external fonts —
        # use a relaxed CSP for /ui paths.  Swagger UI / ReDoc need the
        # jsdelivr CDN for JS + CSS assets.  All other API endpoints get
        # a strict policy.
        path = request.url.path
        if path.startswith("/ui"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; "
                "connect-src 'self'; "
                "img-src 'self' data:; "
                "frame-ancestors 'none'"
            )
        elif path in ("/docs", "/redoc", "/openapi.json"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "img-src 'self' https://fastapi.tiangolo.com data:; "
                "worker-src 'self' blob:; "
                "frame-ancestors 'none'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; frame-ancestors 'none'"
            )
        return response


class SessionRateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP sliding-window rate limit on ``POST /v1/sessions``.

    Rejects with 429 + ``Retry-After`` header when the limit is exceeded.
    Uses an in-memory deque per IP — safe for single-instance deployments.
    For multi-instance, rely on Cloud Armor or an external rate limiter.

    ``trusted_proxy_count`` controls whether ``X-Forwarded-For`` is used:
    - **0** (default): ignore forwarding headers, use the direct peer IP.
    - **N > 0**: take the Nth-from-last entry in ``X-Forwarded-For``
      (i.e. the value set by the outermost trusted proxy).

    Stale buckets are purged automatically to bound memory growth.
    """

    # Hard cap on tracked IPs to prevent memory exhaustion from spoofed keys.
    _MAX_BUCKETS = 50_000

    def __init__(
        self,
        app: object,
        max_requests: int = 10,
        window_seconds: int = 60,
        trusted_proxy_count: int = 0,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._max = max_requests
        self._window = window_seconds
        self._trusted_proxy_count = trusted_proxy_count
        self._lock = threading.Lock()
        self._buckets: dict[str, collections.deque[float]] = {}

    def _client_ip(self, request: Request) -> str:
        """Extract client IP.

        Only trusts ``X-Forwarded-For`` when ``trusted_proxy_count > 0``.
        With *N* trusted proxies the real client IP is at position ``-N``
        in the comma-separated list (rightmost entries are set by proxies
        closest to the server).
        """
        if self._trusted_proxy_count > 0:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                parts = [p.strip() for p in forwarded.split(",")]
                idx = -self._trusted_proxy_count
                if abs(idx) <= len(parts):
                    return parts[idx]
        return request.client.host if request.client else "unknown"

    def _purge_stale_buckets(self, now: float) -> None:
        """Remove buckets whose newest entry is older than the window.

        Called under ``self._lock``.
        """
        stale = [
            ip
            for ip, bucket in self._buckets.items()
            if not bucket or bucket[-1] < now - self._window
        ]
        for ip in stale:
            del self._buckets[ip]

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Only rate-limit session creation
        if request.method != "POST" or not request.url.path.rstrip("/").endswith("/sessions"):
            return await call_next(request)

        ip = self._client_ip(request)
        now = time.monotonic()

        with self._lock:
            # Periodically purge stale buckets to bound memory usage.
            if len(self._buckets) > self._MAX_BUCKETS:
                self._purge_stale_buckets(now)

            bucket = self._buckets.get(ip)
            if bucket is None:
                bucket = collections.deque()
                self._buckets[ip] = bucket

            # Evict timestamps outside the window
            while bucket and bucket[0] < now - self._window:
                bucket.popleft()

            if len(bucket) >= self._max:
                logger.warning(
                    "Session creation rate limit hit for IP %s (%d/%d in %ds)",
                    ip,
                    len(bucket),
                    self._max,
                    self._window,
                )
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": f"Rate limit exceeded: max {self._max} "
                        f"session creations per {self._window}s"
                    },
                    headers={"Retry-After": str(self._window)},
                )
            bucket.append(now)

        return await call_next(request)


class RequestBodyLimitMiddleware(BaseHTTPMiddleware):
    """Reject request bodies that exceed size limits.

    Model load and validate endpoints allow up to 5 MB; all other
    endpoints are capped at 1 MB.

    Two checks are performed:
    1. **Content-Length header** — cheap early rejection (also enforced at
       the Cloud Armor layer in front of the load balancer).
    2. **Streaming byte count** — reads the body via ``request.stream()``
       and aborts as soon as the limit is exceeded, avoiding buffering an
       arbitrarily large payload into memory.  The consumed bytes are
       cached on ``request._body`` so downstream handlers can still use
       ``await request.body()``.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        limit = _MAX_BODY_MODEL if path.endswith(_MODEL_PATHS) else _MAX_BODY_DEFAULT
        limit_mb = limit // (1024 * 1024)

        # Fast path: check Content-Length header first
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = -1  # treat unparseable as missing — fall through to streaming
            if declared > limit:
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"Request body too large (max {limit_mb} MB)"},
                )

        # Stream actual bytes — abort early if limit exceeded.
        # Apply to any method that might carry a body (not just POST/PUT/PATCH).
        has_body = content_length is not None or "transfer-encoding" in request.headers
        if request.method in ("POST", "PUT", "PATCH", "DELETE") or has_body:
            chunks: list[bytes] = []
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": f"Request body too large (max {limit_mb} MB)"},
                    )
                chunks.append(chunk)
            # Cache consumed body so downstream can call request.body()
            request._body = b"".join(chunks)  # noqa: SLF001

        return await call_next(request)
