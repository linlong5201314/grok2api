"""API-key authentication dependencies for FastAPI routes."""

import hmac

from fastapi import Header, HTTPException, Query, Request, status
from fastapi.security import HTTPBearer

from app.platform.auth.ratelimit import (
    check_lockout,
    record_failure,
    record_success,
)
from app.platform.config.snapshot import get_config

_security = HTTPBearer(auto_error=False, scheme_name="API Key")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_keys() -> list[str]:
    raw = get_config("app.api_key", "")
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(k).strip() for k in raw if str(k).strip()]
    return [k.strip() for k in str(raw).split(",") if k.strip()]


def get_admin_key() -> str:
    """Return configured ``app.app_key`` (admin password)."""
    return str(get_config("app.app_key", "grok2api") or "")


def get_webui_key() -> str:
    """Return configured ``app.webui_key`` (webui access key)."""
    return str(get_config("app.webui_key", "") or "")


def is_webui_enabled() -> bool:
    """Whether the webui entry is enabled."""
    val = get_config("app.webui_enabled", False)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "on"}
    return bool(val)


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _safe_equals(a: str, b: str) -> bool:
    """Constant-time string comparison that tolerates non-ASCII input.

    ``hmac.compare_digest`` raises TypeError when either str argument
    contains non-ASCII characters, which would turn a malformed
    Authorization header (latin-1 decoded high bytes) or a non-ASCII
    configured key into a 500 instead of a clean 401/403.
    """
    try:
        return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
    except (UnicodeEncodeError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

async def verify_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> None:
    """Validate Bearer token against configured ``api_key``.

    Accepts either ``Authorization: Bearer <key>`` (OpenAI / grok2api style)
    or ``X-API-Key: <key>`` (official Anthropic SDK style) so that agents
    targeting the Anthropic-compatible endpoint work without reconfiguration.

    When ``app.api_key`` is empty the endpoint stays open (legacy behaviour)
    unless ``app.reject_empty_api_key`` is enabled.
    """
    allowed_keys = _get_keys()
    if not allowed_keys:
        if get_config("app.reject_empty_api_key", False):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "API key is not configured; set app.api_key (or disable "
                "app.reject_empty_api_key to allow unauthenticated access).",
            )
        return

    check_lockout(request)
    token = _extract_bearer(authorization) or x_api_key or None
    if token is None:
        record_failure(request)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid Authorization header.")

    if not any(_safe_equals(token, k) for k in allowed_keys):
        record_failure(request)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid API key.")
    record_success(request)


async def verify_admin_key(
    request: Request,
    authorization: str | None = Header(default=None),
    app_key: str | None = Query(default=None),
) -> None:
    """Validate Bearer token against ``app.app_key`` (admin access).

    Accepts either ``Authorization: Bearer <key>`` header or ``?app_key=<key>``
    query parameter (the latter is needed for EventSource which cannot send
    headers).  Failed attempts are rate-limited per client IP.
    """
    check_lockout(request)
    key = get_admin_key()
    if not key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Admin key is not configured.")
    if key == "grok2api" and get_config("app.reject_default_app_key", False):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Admin key is still the default 'grok2api'; set app.app_key or "
            "disable app.reject_default_app_key.",
        )

    token = _extract_bearer(authorization) or app_key
    if token is None:
        record_failure(request)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing authentication token.")

    if not _safe_equals(token, key):
        record_failure(request)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid authentication token.")
    record_success(request)


async def verify_webui_key(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """Validate Bearer token for webui endpoints (rate-limited)."""
    check_lockout(request)
    webui_key = get_webui_key()

    if not webui_key:
        if is_webui_enabled():
            return
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "WebUI access is disabled.")

    token = _extract_bearer(authorization)
    if token is None:
        record_failure(request)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing authentication token.")

    if not _safe_equals(token, webui_key):
        record_failure(request)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid authentication token.")
    record_success(request)

__all__ = [
    "verify_api_key",
    "verify_admin_key",
    "verify_webui_key",
    "get_admin_key",
    "get_webui_key",
    "is_webui_enabled",
]
