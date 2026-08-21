"""In-memory brute-force protection for admin/webui authentication.

Sliding-window failure counter per client IP:

* ``max_failures`` failures inside ``window_sec`` → IP locked out for
  ``lockout_sec`` (429 with Retry-After).
* A successful authentication clears the IP's counter.

State is per-process by design: multi-worker deployments each protect their
own worker, which still caps request rate per worker; a shared Redis store
would be required for a global budget and is deliberately out of scope to
keep the default deployment dependency-free.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from app.platform.logging.logger import logger

_MAX_FAILURES = 5
_WINDOW_SEC = 300.0
_LOCKOUT_SEC = 900.0

_failures: dict[str, deque[float]] = defaultdict(deque)
_locked_until: dict[str, float] = {}


def client_ip(request: Request | None) -> str:
    """Best-effort client identity for rate limiting."""
    if request is None:
        return "unknown"
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _prune(now: float) -> None:
    cutoff = now - _WINDOW_SEC
    for ip in [ip for ip, dq in _failures.items() if not dq or dq[-1] < cutoff]:
        _failures.pop(ip, None)
    for ip in [ip for ip, until in _locked_until.items() if until <= now]:
        _locked_until.pop(ip, None)


def check_lockout(request: Request | None) -> None:
    """Raise 429 when the client is locked out."""
    now = time.monotonic()
    _prune(now)
    ip = client_ip(request)
    until = _locked_until.get(ip)
    if until and until > now:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed attempts. Try again later.",
            headers={"Retry-After": str(int(until - now))},
        )


def record_failure(request: Request | None) -> None:
    ip = client_ip(request)
    now = time.monotonic()
    dq = _failures[ip]
    dq.append(now)
    while dq and dq[0] < now - _WINDOW_SEC:
        dq.popleft()
    if len(dq) >= _MAX_FAILURES:
        _locked_until[ip] = now + _LOCKOUT_SEC
        logger.warning(
            "auth lockout engaged: ip={} failures={} lockout_sec={}",
            ip,
            len(dq),
            int(_LOCKOUT_SEC),
        )


def record_success(request: Request | None) -> None:
    ip = client_ip(request)
    _failures.pop(ip, None)
    _locked_until.pop(ip, None)


def reset_all() -> None:
    """Test helper — clear all state."""
    _failures.clear()
    _locked_until.clear()


__all__ = [
    "check_lockout",
    "record_failure",
    "record_success",
    "client_ip",
    "reset_all",
]
