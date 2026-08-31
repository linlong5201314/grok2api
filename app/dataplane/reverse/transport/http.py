"""HTTP transport for reverse-proxy requests.

Wraps curl_cffi AsyncSession; handles proxy selection, header building,
retry-on-reset, timeout, and session pooling (TLS handshakes are reused
across calls via the process-wide SessionPool).
"""

import asyncio
from typing import AsyncGenerator

from app.platform.logging.logger import logger
from app.platform.errors import UpstreamError
from app.control.proxy.models import ProxyLease
from app.dataplane.proxy.adapters.headers import build_http_headers
from app.dataplane.proxy.adapters.session import (
    ResettableSession,
    get_session_pool,
)


async def post_stream(
    url: str,
    token: str,
    payload: bytes,
    *,
    lease: ProxyLease | None = None,
    timeout_s: float = 120.0,
    content_type: str = "application/json",
    origin: str = "https://grok.com",
    referer: str = "https://grok.com/",
) -> AsyncGenerator[str, None]:
    """POST *url* and yield SSE lines from the streaming response.

    Raises ``UpstreamError`` on non-200 status.  ``timeout_s`` bounds the
    connect/headers phase and the per-line idle window — not the total
    transfer, so long-lived streams are never cut off mid-generation.
    """
    headers = build_http_headers(
        token,
        content_type=content_type,
        origin=origin,
        referer=referer,
        lease=lease,
    )
    pool = get_session_pool()
    pooled = await pool.acquire(lease=lease)
    try:
        session = await pooled.__aenter__()
        try:
            response = await asyncio.wait_for(
                session.post(url, headers=headers, data=payload, stream=True),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise UpstreamError(
                f"Upstream headers not received within {timeout_s:.0f}s",
                status=502,
            ) from exc

        if response.status_code != 200:
            try:
                body = (response.content).decode("utf-8", "replace")[:400]
            except Exception:
                body = ""
            logger.error(
                "http stream post failed: url={} status={} body={}",
                url,
                response.status_code,
                body,
            )
            raise UpstreamError(
                f"Upstream returned {response.status_code}",
                status=response.status_code,
                body=body,
            )
    except BaseException:
        await pooled.__aexit__(None, None, None)
        raise

    async def _lines() -> AsyncGenerator[str, None]:
        try:
            iterator = response.aiter_lines()
            while True:
                try:
                    line = await asyncio.wait_for(anext(iterator), timeout=timeout_s)
                except StopAsyncIteration:
                    break
                yield line
        except asyncio.TimeoutError as exc:
            pooled.discard()
            raise UpstreamError(
                f"Stream idle for {timeout_s:.0f}s", status=502
            ) from exc
        finally:
            await pooled.__aexit__(None, None, None)

    return _lines()


async def post_json(
    url: str,
    token: str,
    payload: bytes,
    *,
    lease: ProxyLease | None = None,
    timeout_s: float = 30.0,
    content_type: str = "application/json",
    origin: str = "https://grok.com",
    referer: str = "https://grok.com/",
    session: "ResettableSession | None" = None,
) -> dict:
    """POST *url* and return parsed JSON response body.

    Pass *session* to reuse an existing connection (avoids a new TLS handshake).
    When *session* is ``None`` a pooled session is checked out automatically.
    """
    headers = build_http_headers(
        token, content_type=content_type, origin=origin, referer=referer, lease=lease
    )

    import orjson

    async def _do(s: "ResettableSession") -> dict:
        response = await s.post(url, headers=headers, data=payload, timeout=timeout_s)
        body_bytes = response.content
        if response.status_code not in (200, 201, 204):
            body_text = body_bytes.decode("utf-8", "replace")[:400]
            logger.warning(
                "http json post failed: url={} status={} body={}",
                url,
                response.status_code,
                body_text,
            )
            # Keep the raised message short — it surfaces in admin toasts —
            # while the full preview stays in ``details["body"]``.
            snippet = body_text[:160].replace("\n", " ").replace("\r", "")
            raise UpstreamError(
                f"Upstream returned {response.status_code}: {snippet}",
                status=response.status_code,
                body=body_text,
            )
        return orjson.loads(body_bytes) if body_bytes.strip() else {}

    if session is not None:
        return await _do(session)

    pool = get_session_pool()
    pooled = await pool.acquire(lease=lease)
    async with pooled as s:
        return await _do(s)


async def get_json(
    url: str,
    token: str,
    *,
    params: dict | None = None,
    lease: ProxyLease | None = None,
    timeout_s: float = 30.0,
    origin: str = "https://grok.com",
    referer: str = "https://grok.com/",
) -> dict:
    """GET *url* and return parsed JSON response body."""
    headers = build_http_headers(
        token,
        content_type="application/json",
        origin=origin,
        referer=referer,
        lease=lease,
    )

    pool = get_session_pool()
    pooled = await pool.acquire(lease=lease)
    async with pooled as session:
        response = await session.get(
            url,
            headers=headers,
            params=params,
            timeout=timeout_s,
        )

        body_bytes = response.content
        if response.status_code != 200:
            body_text = body_bytes.decode("utf-8", "replace")[:400]
            logger.error(
                "http json get failed: url={} status={} body={}",
                url,
                response.status_code,
                body_text,
            )
            raise UpstreamError(
                f"Upstream returned {response.status_code}",
                status=response.status_code,
                body=body_text,
            )

        import orjson

        return orjson.loads(body_bytes)


async def delete_json(
    url: str,
    token: str,
    *,
    lease: ProxyLease | None = None,
    timeout_s: float = 30.0,
    origin: str = "https://grok.com",
    referer: str = "https://grok.com/",
) -> dict:
    """DELETE *url* and return parsed JSON response body (may be empty → {})."""
    headers = build_http_headers(
        token,
        content_type="application/json",
        origin=origin,
        referer=referer,
        lease=lease,
    )

    pool = get_session_pool()
    pooled = await pool.acquire(lease=lease)
    async with pooled as session:
        response = await session.delete(
            url,
            headers=headers,
            timeout=timeout_s,
        )

        body_bytes = response.content
        if response.status_code not in (200, 204):
            body_text = body_bytes.decode("utf-8", "replace")[:400]
            logger.error(
                "http json delete failed: url={} status={} body={}",
                url,
                response.status_code,
                body_text,
            )
            raise UpstreamError(
                f"Upstream returned {response.status_code}",
                status=response.status_code,
                body=body_text,
            )

        if not body_bytes.strip():
            return {}
        import orjson

        return orjson.loads(body_bytes)


async def get_bytes_stream(
    url: str,
    token: str,
    *,
    lease: ProxyLease | None = None,
    timeout_s: float = 120.0,
    origin: str = "https://assets.grok.com",
    referer: str = "https://grok.com/",
    extra_headers: dict | None = None,
) -> AsyncGenerator[bytes, None]:
    """GET *url* and yield raw bytes chunks from the streaming response.

    Raises ``UpstreamError`` on non-200 status.  ``timeout_s`` bounds the
    connect/headers phase and the per-chunk idle window — not the total
    transfer duration.
    """
    headers = build_http_headers(
        token,
        content_type=None,
        origin=origin,
        referer=referer,
        lease=lease,
    )
    if extra_headers:
        headers.update(extra_headers)
    if headers.get("Sec-Fetch-Mode") == "navigate":
        headers.pop("Content-Type", None)
        headers.pop("Origin", None)

    pool = get_session_pool()
    pooled = await pool.acquire(lease=lease)
    try:
        session = await pooled.__aenter__()
        try:
            response = await asyncio.wait_for(
                session.get(
                    url,
                    headers=headers,
                    timeout=timeout_s,
                    stream=True,
                    allow_redirects=True,
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise UpstreamError(
                f"Upstream headers not received within {timeout_s:.0f}s",
                status=502,
            ) from exc

        if response.status_code != 200:
            try:
                body = (response.content).decode("utf-8", "replace")[:400]
            except Exception:
                body = ""
            logger.error(
                "http byte stream get failed: url={} status={} body={}",
                url,
                response.status_code,
                body,
            )
            raise UpstreamError(
                f"Upstream returned {response.status_code}",
                status=response.status_code,
                body=body,
            )
    except BaseException:
        await pooled.__aexit__(None, None, None)
        raise

    async def _chunks() -> AsyncGenerator[bytes, None]:
        try:
            iterator = response.aiter_content()
            while True:
                try:
                    chunk = await asyncio.wait_for(anext(iterator), timeout=timeout_s)
                except StopAsyncIteration:
                    break
                if chunk:
                    yield chunk
        except asyncio.TimeoutError as exc:
            pooled.discard()
            raise UpstreamError(
                f"Stream idle for {timeout_s:.0f}s", status=502
            ) from exc
        finally:
            await pooled.__aexit__(None, None, None)

    return _chunks()


__all__ = ["post_stream", "post_json", "get_json", "delete_json", "get_bytes_stream"]
