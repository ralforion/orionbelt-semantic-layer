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
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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


class MetricsMiddleware:
    """Count each REST request for the Prometheus metrics (``service/metrics.py``).

    By method (``other`` for one HTTP does not define), the route's template
    (``/v1/sessions/{session_id}/...``, so the series do not grow with
    sessions; ``unmatched`` for a path no route has), and status: no label
    takes a value the client chooses. It marks the request's queries as the
    ``rest`` surface. A plain ASGI middleware: it passes requests straight on
    while the metrics are off.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from orionbelt.service import metrics

        if scope["type"] != "http" or not metrics.enabled():
            await self.app(scope, receive, send)
            return
        status = 500
        started = time.monotonic()
        surface = metrics.SURFACE.set("rest")

        async def record_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, record_status)
        finally:
            metrics.SURFACE.reset(surface)
            metrics.observe_http(
                _method_label(scope), _route_label(scope), status, time.monotonic() - started
            )


#: The methods HTTP defines (RFC 9110, and PATCH from RFC 5789). A client can
#: send any token as a method, so any other counts as ``other``.
_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)


def _method_label(scope: Scope) -> str:
    """The request's method, or ``other`` for one HTTP does not define."""
    method = scope.get("method", "")
    return method if method in _METHODS else "other"


def _route_label(scope: Scope) -> str:
    """The template of the route a request matched, ``unmatched`` for none.

    FastAPI keeps a route's path relative to the router that holds it
    (``/models`` for ``/v1/models``). The request's leading segments supply
    the routers' prefixes, and the result is kept only when it is a template
    the app declares, so no value of a path parameter can reach the label;
    otherwise the relative path is the label.
    """
    template = getattr(scope.get("route"), "path", None)
    if not isinstance(template, str) or not template:
        return "unmatched"
    path = scope.get("path", "")
    root = scope.get("root_path", "")
    if root and path.startswith(root):
        path = path[len(root) :]
    head = [part for part in path.split("/") if part]
    tail = [part for part in template.split("/") if part]
    if len(head) > len(tail):
        candidate = "/" + "/".join([*head[: len(head) - len(tail)], *tail])
        if candidate in _declared_templates(scope.get("app")):
            return candidate
    return template


_TEMPLATES: dict[int, frozenset[str]] = {}


def _declared_templates(app: object) -> frozenset[str]:
    """The path templates an app declares (its OpenAPI paths), read once per app."""
    if app is None:
        return frozenset()
    key = id(app)
    found = _TEMPLATES.get(key)
    if found is None:
        openapi = getattr(app, "openapi", None)
        try:
            found = frozenset(openapi().get("paths", {})) if callable(openapi) else frozenset()
        except Exception:  # noqa: BLE001 - a label without the prefix is still bounded
            found = frozenset()
        _TEMPLATES[key] = found
    return found


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard security headers to every response.

    ``docs_connect_src`` names the origins Swagger UI may fetch from: the
    identity provider's, when /docs signs in with it (the browser exchanges
    the sign-in code for a token there).
    """

    def __init__(self, app: ASGIApp, docs_connect_src: tuple[str, ...] = ()) -> None:
        super().__init__(app)
        self._docs_connect_src = " ".join(("'self'", *docs_connect_src))

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
        elif path in ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"):
            # /docs/oauth2-redirect is the page the IdP sends the sign-in popup
            # back to; its inline script hands the code to Swagger UI.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "img-src 'self' https://fastapi.tiangolo.com data:; "
                f"connect-src {self._docs_connect_src}; "
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
