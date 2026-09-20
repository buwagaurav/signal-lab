from __future__ import annotations

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .config import get_settings
from .db import SessionLocal
from .models import AuditLog

# Pure infra noise, not user or security activity -- excluded so the log
# stays a record of what people (and callers) actually did.
SKIP_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


class AuditLogMiddleware(BaseHTTPMiddleware):
    """Records method/path/status/user/client-ip for every request that isn't
    pure infra noise. Never blocks or fails the request it's observing -- a
    logging failure is swallowed, not surfaced, since observability must not
    become a new way to break the thing it's supposed to be watching.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path not in SKIP_PATHS:
            try:
                self._record(request, response.status_code)
            except Exception:
                pass
        return response

    def _record(self, request: Request, status_code: int) -> None:
        db = SessionLocal()
        try:
            db.add(AuditLog(
                user_id=self._user_id_from_request(request),
                method=request.method,
                path=request.url.path,
                status_code=status_code,
                client_ip=request.client.host if request.client else None,
            ))
            db.commit()
        finally:
            db.close()

    def _user_id_from_request(self, request: Request) -> int | None:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        try:
            payload = jwt.decode(auth[7:], get_settings().jwt_secret, algorithms=["HS256"])
            return int(payload["sub"])
        except Exception:
            # An invalid/expired token still gets logged as an anonymous
            # request -- the route itself will separately reject it with 401.
            return None
