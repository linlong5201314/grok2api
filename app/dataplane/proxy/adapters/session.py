"""curl_cffi session builder for reverse-proxy requests."""

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

from curl_cffi.const import CurlOpt

from app.platform.config.snapshot import get_config
from app.platform.errors import UpstreamError
from app.control.proxy.models import ProxyLease
from app.dataplane.proxy.adapters.profile import resolve_proxy_profile


def _skip_proxy_ssl(proxy_url: str) -> bool:
    if not proxy_url:
        return False
    cfg = get_config()
    return cfg.get_bool("proxy.egress.skip_ssl_verify", False)


def _impersonate_disabled() -> bool:
    """Global kill-switch for browser impersonation.

    Diagnostic/escape hatch for platforms where the bundled curl-impersonate
    build misbehaves *only when impersonation is combined with an HTTP proxy*
    (observed as TLS "invalid library" / WRONG_VERSION_NUMBER through
    sing-box inbounds on musl/Alpine while direct and plain requests work).
    """
    try:
        return get_config().get_bool("proxy.egress.disable_impersonate", False)
    except Exception:  # noqa: BLE001
        return False


_CA_BUNDLE: str | None = None


def _ca_bundle_path() -> str:
    """Return a CA bundle path that libcurl can actually open.

    On Windows, libcurl fails to fopen CA paths containing non-ASCII
    characters (curl error 77) — e.g. when the venv lives under a
    Chinese-named user directory.  Materialise certifi's bundle at a
    pure-ASCII location once per process in that case.
    """
    global _CA_BUNDLE
    if _CA_BUNDLE:
        return _CA_BUNDLE
    import certifi
    import shutil
    import tempfile
    from pathlib import Path

    src = certifi.where()
    try:
        src.encode("ascii")
        _CA_BUNDLE = src
        return src
    except UnicodeEncodeError:
        pass
    dst = Path(tempfile.gettempdir()) / "grok2api-cacert.pem"
    try:
        if not dst.exists() or dst.stat().st_size != Path(src).stat().st_size:
            shutil.copyfile(src, dst)
        _CA_BUNDLE = str(dst)
    except OSError:
        # Best effort: fall back to the original path rather than crash.
        _CA_BUNDLE = src
    return _CA_BUNDLE


def normalize_proxy_url(url: str) -> str:
    """Normalize SOCKS schemes for consistent DNS-over-proxy behaviour."""
    if not url:
        return url
    scheme = urlparse(url).scheme.lower()
    if scheme == "socks":
        return "socks5h://" + url[len("socks://") :]
    if scheme == "socks5":
        return "socks5h://" + url[len("socks5://") :]
    if scheme == "socks4":
        return "socks4a://" + url[len("socks4://") :]
    return url


def build_session_kwargs(
    *,
    lease: ProxyLease | None = None,
    browser_override: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build kwargs suitable for ``curl_cffi.requests.AsyncSession``."""
    kwargs: dict[str, Any] = dict(extra or {})

    # Browser impersonation.
    if not kwargs.get("impersonate") and not _impersonate_disabled():
        browser = browser_override or resolve_proxy_profile(lease).browser
        if browser:
            kwargs["impersonate"] = browser

    # Proxy URL.
    proxy_url = ""
    if lease is not None and lease.proxy_url:
        proxy_url = normalize_proxy_url(lease.proxy_url)
        scheme = urlparse(proxy_url).scheme.lower()
        if scheme.startswith("socks"):
            kwargs.setdefault("proxy", proxy_url)
        else:
            kwargs.setdefault("proxies", {"http": proxy_url, "https": proxy_url})

    # curl SSL options: ASCII-safe CA bundle (non-ASCII paths break libcurl),
    # plus optional proxy SSL verification skip.
    opts = dict(kwargs.get("curl_options") or {})
    opts.setdefault(CurlOpt.CAINFO, _ca_bundle_path())
    if _skip_proxy_ssl(proxy_url):
        opts[CurlOpt.PROXY_SSL_VERIFYPEER] = 0
        opts[CurlOpt.PROXY_SSL_VERIFYHOST] = 0
    kwargs["curl_options"] = opts

    return kwargs


def _wrap_transport_error(exc: BaseException) -> UpstreamError:
    if isinstance(exc, UpstreamError):
        return exc
    body = str(exc).replace("\n", "\\n")[:400]
    return UpstreamError(
        f"Transport request failed: {exc}",
        status=502,
        body=body,
    )


class ResettableSession:
    """AsyncSession wrapper that resets connection on configurable status codes.

    Designed for long-lived hot-path use; session is recreated transparently
    when a reset-triggering status code is received.
    """

    def __init__(
        self,
        *,
        lease: ProxyLease | None = None,
        browser_override: str | None = None,
        reset_on_status: set[int] | None = None,
        **session_kwargs: Any,
    ) -> None:
        self._kwargs = build_session_kwargs(
            lease=lease,
            browser_override=browser_override,
            extra=session_kwargs or None,
        )
        if reset_on_status is None:
            codes = get_config().get_list("retry.reset_session_status_codes", [403])
            reset_on_status = {int(c) for c in codes}
        self._reset_on = reset_on_status
        self._reset_pending = False
        self._lock = asyncio.Lock()
        self._session = self._create()

    @property
    def resolved_kwargs(self) -> dict[str, Any]:
        """The fully resolved constructor kwargs (pool identity key)."""
        return self._kwargs

    @property
    def closed(self) -> bool:
        return self._session is None

    def _create(self):
        from curl_cffi.requests import AsyncSession

        return AsyncSession(**self._kwargs)

    async def _maybe_reset(self) -> None:
        if not self._reset_pending:
            return
        async with self._lock:
            if not self._reset_pending:
                return
            self._reset_pending = False
            old, self._session = self._session, self._create()
            try:
                await old.close()
            except Exception:
                pass

    async def _request(self, method: str, *args: Any, **kwargs: Any):
        await self._maybe_reset()
        try:
            response = await getattr(self._session, method)(*args, **kwargs)
        except Exception as exc:
            self._reset_pending = True
            raise _wrap_transport_error(exc) from exc
        if self._reset_on and response.status_code in self._reset_on:
            self._reset_pending = True
        return response

    async def get(self, *args: Any, **kwargs: Any):
        return await self._request("get", *args, **kwargs)

    async def post(self, *args: Any, **kwargs: Any):
        return await self._request("post", *args, **kwargs)

    async def delete(self, *args: Any, **kwargs: Any):
        return await self._request("delete", *args, **kwargs)

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            finally:
                self._session = None  # type: ignore[assignment]

    async def __aenter__(self) -> "ResettableSession":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


# ---------------------------------------------------------------------------
# Session pool — reuse curl_cffi sessions instead of a TLS handshake per call
# ---------------------------------------------------------------------------


class PooledSession:
    """Checkout handle from :class:`SessionPool`.

    ``async with`` yields an object that proxies the underlying
    :class:`ResettableSession`; on exit the session returns to the pool
    unless :meth:`discard` was called (e.g. after a cancelled transfer,
    where the connection state is unknown).
    """

    def __init__(self, pool: "SessionPool", session: ResettableSession) -> None:
        self._pool = pool
        self._session = session
        self._discard = False
        self._done = False

    def discard(self) -> None:
        """Close instead of pooling on exit (connection state uncertain)."""
        self._discard = True

    async def __aenter__(self) -> "PooledSession":
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._done:
            return
        self._done = True
        if self._discard or self._session.closed:
            await self._pool.close_session(self._session)
        else:
            await self._pool.release(self._session)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


class SessionPool:
    """Process-wide cache of idle :class:`ResettableSession` instances.

    curl_cffi sessions pay a full TLS handshake per instance; creating one
    per request (and per quota-probe mode) wastes measurable latency and CPU.
    Sessions are keyed by their fully resolved kwargs, so a pooled entry is
    interchangeable with a fresh build. Checkouts are exclusive — a session
    is never shared by two concurrent requests; a second concurrent caller
    simply gets (or builds) another instance.
    """

    def __init__(
        self,
        *,
        max_idle_per_key: int = 2,
        idle_ttl_s: float = 300.0,
    ) -> None:
        self._idle: dict[tuple, list[tuple[float, ResettableSession]]] = {}
        self._lock = asyncio.Lock()
        self._max_idle = max(1, max_idle_per_key)
        self._idle_ttl = idle_ttl_s

    @staticmethod
    def _key(resolved_kwargs: dict[str, Any], reset_on_status: set[int] | None) -> tuple:
        base = tuple(sorted((k, repr(v)) for k, v in resolved_kwargs.items()))
        return base + (tuple(sorted(reset_on_status or ())),)

    async def acquire(
        self,
        *,
        lease: ProxyLease | None = None,
        browser_override: str | None = None,
        reset_on_status: set[int] | None = None,
        **session_kwargs: Any,
    ) -> PooledSession:
        if reset_on_status is None:
            # Resolve the effective reset set the same way ResettableSession
            # does, so the acquire key matches the release key.
            codes = get_config().get_list("retry.reset_session_status_codes", [403])
            reset_on_status = {int(c) for c in codes}
        resolved = build_session_kwargs(
            lease=lease,
            browser_override=browser_override,
            extra=session_kwargs or None,
        )
        key = self._key(resolved, reset_on_status)
        now = time.monotonic()
        async with self._lock:
            bucket = self._idle.get(key)
            while bucket:
                ts, session = bucket.pop()
                if now - ts <= self._idle_ttl:
                    return PooledSession(self, session)
                await self.close_session_unlocked(session)
            session = ResettableSession(
                lease=lease,
                browser_override=browser_override,
                reset_on_status=reset_on_status,
                **session_kwargs,
            )
            return PooledSession(self, session)

    async def release(self, session: ResettableSession) -> None:
        key = self._key(session.resolved_kwargs, session._reset_on)
        async with self._lock:
            bucket = self._idle.setdefault(key, [])
            if len(bucket) >= self._max_idle:
                await self.close_session_unlocked(session)
                return
            bucket.append((time.monotonic(), session))

    async def close_session(self, session: ResettableSession) -> None:
        async with self._lock:
            await self.close_session_unlocked(session)

    async def close_session_unlocked(self, session: ResettableSession) -> None:
        try:
            await session.close()
        except Exception:
            pass


_pool: SessionPool | None = None


def get_session_pool() -> SessionPool:
    """Return the process-wide session pool (created on first use)."""
    global _pool
    if _pool is None:
        _pool = SessionPool()
    return _pool


__all__ = [
    "ResettableSession",
    "SessionPool",
    "PooledSession",
    "get_session_pool",
    "build_session_kwargs",
    "normalize_proxy_url",
]
