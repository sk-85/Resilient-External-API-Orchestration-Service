"""Optional authentication: static API keys or HS256 JWTs.

Mode is chosen with ``ORCH_AUTH_MODE`` (``none`` | ``api_key`` | ``jwt``).
The returned principal is stored as the ``actor`` in the audit trail.
"""

from __future__ import annotations

import hashlib
import hmac

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str, scheme: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": scheme})


async def get_principal(
    request: Request,
    api_key: str | None = Depends(_api_key_header),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    settings = request.app.state.settings

    if settings.auth_mode == "none":
        return "anonymous"

    if settings.auth_mode == "api_key":
        if api_key:
            for valid in settings.api_key_list:
                if hmac.compare_digest(api_key.encode(), valid.encode()):
                    # Log a short fingerprint, never the key itself.
                    return "api-key:" + hashlib.sha256(valid.encode()).hexdigest()[:8]
        raise _unauthorized("Invalid or missing API key", "ApiKey")

    # jwt
    if credentials is None:
        raise _unauthorized("Missing bearer token", "Bearer")
    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise _unauthorized("Invalid or expired token", "Bearer") from exc
    return str(claims["sub"])
