from __future__ import annotations

import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class RateLimitMiddleware(BaseHTTPMiddleware):
    """In-memory sliding-window rate limiter, keyed by client IP + route bucket.

    In-process only -- correct for a single uvicorn worker, not for multiple
    workers or instances behind a load balancer (each would keep its own
    independent counts, so the effective limit multiplies with instance
    count). A shared store (e.g. Redis) is the production upgrade path; not
    adopted here since there's no multi-instance deployment yet to justify it.
    """

    AUTH_PATHS = {"/auth/login", "/auth/register"}
    AUTH_LIMIT = 10
    DEFAULT_LIMIT = 120
    WINDOW_SECONDS = 60

    def __init__(self, app):
        super().__init__(app)
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)  # never block CORS preflight

        client_ip = request.client.host if request.client else "unknown"
        bucket = "auth" if request.url.path in self.AUTH_PATHS else "default"
        limit = self.AUTH_LIMIT if bucket == "auth" else self.DEFAULT_LIMIT
        key = (client_ip, bucket)

        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.WINDOW_SECONDS:
            hits.popleft()

        if len(hits) >= limit:
            retry_after = max(1, int(self.WINDOW_SECONDS - (now - hits[0])))
            return JSONResponse(
                status_code=429,
                content={"detail": f"Rate limit exceeded, retry in {retry_after}s"},
                headers={"Retry-After": str(retry_after)},
            )

        hits.append(now)
        return await call_next(request)
