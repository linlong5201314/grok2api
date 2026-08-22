"""Asset upload transport — direct base64 upload to Grok.

Calls POST /rest/app-chat/upload-file with base64-encoded content and
returns the file metadata ID used as a file attachment reference in chat.

Input compatibility matrix (what upload_from_input accepts):
  - https://... / http://... URLs          (fetched, magic-sniffed, re-uploaded)
  - data:<mime>;base64,<payload>           (standard data URI)
  - data:<mime>,<percent-encoded payload>  (non-base64 data URIs, auto-converted)
  - base64 payloads with whitespace / missing padding (repaired)

Uploads enforce a configurable size cap and retry once on transient
(5xx / transport) failures so flaky egress doesn't fail the whole request.
"""

import asyncio
import base64
import binascii
import mimetypes
import re
from urllib.parse import unquote, urlparse

import orjson

from app.platform.logging.logger import logger
from app.platform.config.snapshot import get_config
from app.platform.errors import UpstreamError, ValidationError
from app.dataplane.proxy import get_proxy_runtime
from app.dataplane.proxy.adapters.headers import build_sso_cookie
from app.dataplane.proxy.adapters.headers import build_http_headers
from app.dataplane.proxy.adapters.session import ResettableSession, build_session_kwargs
from app.dataplane.reverse.protocol.xai_assets import resolve_asset_reference
from app.control.proxy.feedback import build_feedback
from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind

_UPLOAD_URL = "https://grok.com/rest/app-chat/upload-file"
_X_USER_ID_RE = re.compile(r"(?:^|;\s*)x-userid=([^;]+)")

# Global semaphore — limits concurrent upload_file() calls across all requests.
# Initialised lazily on first call so the event loop is guaranteed to be running.
_upload_sem: asyncio.Semaphore | None = None

def _get_upload_sem() -> asyncio.Semaphore:
    global _upload_sem
    if _upload_sem is None:
        n = max(1, int(get_config("batch.asset_upload_concurrency", 10)))
        _upload_sem = asyncio.Semaphore(n)
    return _upload_sem


# ---------------------------------------------------------------------------
# File-input parsing
# ---------------------------------------------------------------------------

def _is_url(value: str) -> bool:
    try:
        p = urlparse(value)
        return bool(p.scheme in {"http", "https"} and p.netloc)
    except Exception:
        return False


def _mime_from_name(filename: str, fallback: str = "application/octet-stream") -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or fallback


_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff",          "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n",     "image/png"),
    (b"GIF87a",                "image/gif"),
    (b"GIF89a",                "image/gif"),
    (b"%PDF-",                 "application/pdf"),
    (b"ID3",                   "audio/mpeg"),
    (b"\xff\xfb",              "audio/mpeg"),
    (b"OggS",                  "audio/ogg"),
)


def _sniff_mime(raw: bytes) -> str | None:
    """Best-effort content-type from magic bytes. None when unknown."""
    for sig, mime in _MAGIC_SIGNATURES:
        if raw.startswith(sig):
            return mime
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return "audio/wav"
    if raw[4:8] == b"ftyp":
        return "video/mp4"
    if raw.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return "text/html"
    return None


def _repair_b64(b64: str) -> str:
    """Strip whitespace and restore missing '=' padding."""
    b64 = re.sub(r"\s+", "", b64)
    return b64 + "=" * (-len(b64) % 4)


def parse_data_uri(data_uri: str) -> tuple[str, str, str]:
    """Split a data URI into (filename, base64_content, mime_type).

    Accepts both ``data:<mime>;base64,<payload>`` and URL-encoded
    ``data:<mime>,<payload>`` (converted to base64 automatically).
    Whitespace and missing base64 padding are repaired.

    Raises ``ValidationError`` on invalid input.
    """
    if not data_uri.startswith("data:"):
        raise ValidationError("File input must be a URL or data URI", param="content")

    try:
        header, payload = data_uri.split(",", 1)
    except ValueError:
        raise ValidationError("Malformed data URI: missing comma separator", param="content")

    mime = header[5:].split(";", 1)[0].strip() or "application/octet-stream"
    ext  = mime.split("/")[-1].split("+", 1)[0] or "bin"

    if ";base64" in header.lower():
        b64 = _repair_b64(payload)
        if not b64.rstrip("="):
            raise ValidationError("Data URI has empty payload", param="content")
        try:
            base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise ValidationError("Data URI payload is not valid base64", param="content")
        return f"file.{ext}", b64, mime

    # URL-encoded (non-base64) data URI — decode then re-encode.
    raw = unquote(payload).encode("utf-8", "surrogatepass")
    if not raw:
        raise ValidationError("Data URI has empty payload", param="content")
    return f"file.{ext}", base64.b64encode(raw).decode(), mime


def _sanitize_filename(name: str, mime: str) -> str:
    """Keep a filesystem-safe, length-capped filename with a sane extension."""
    name = unquote(name or "").strip().strip("/\\")
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
    if len(name) > 80:
        name = name[-80:]
    ext = mimetypes.guess_extension(mime) or (
        f".{mime.split('/')[-1]}" if "/" in mime else ".bin"
    )
    if "." not in name:
        name = f"{name or 'file'}{ext}"
    return name


def _max_upload_bytes() -> int:
    return max(0, get_config().get_int("asset.max_upload_bytes", 20 * 1024 * 1024))


def _check_size(size: int) -> None:
    cap = _max_upload_bytes()
    if cap and size > cap:
        raise ValidationError(
            f"Attachment too large: {size} bytes exceeds limit of {cap} bytes "
            "(asset.max_upload_bytes)",
            param="content",
        )


# ---------------------------------------------------------------------------
# Core upload function
# ---------------------------------------------------------------------------

async def upload_file(
    token:    str,
    filename: str,
    mime:     str,
    b64:      str,
) -> tuple[str, str]:
    """Upload base64-encoded file content to Grok.

    Args:
        token:    SSO session token.
        filename: Original file name (used for content-type inference).
        mime:     MIME type string (e.g. ``"image/png"``).
        b64:      Base64-encoded file content (no data-URI prefix).

    Returns:
        ``(file_id, file_uri)`` — file_id is used as a file attachment ref.

    Raises:
        ``UpstreamError`` on HTTP failure (one retry on 5xx/transport errors).
    """
    _check_size(len(b64) * 3 // 4)
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            async with _get_upload_sem():
                return await _upload_file_inner(token, filename, mime, b64)
        except UpstreamError as exc:
            transient = exc.status >= 500 or exc.status == 0 or exc.status == 429
            last_exc = exc
            if transient and attempt == 0:
                logger.warning(
                    "asset upload retrying after transient failure: status={}", exc.status,
                )
                await asyncio.sleep(0.5)
                continue
            raise
    raise last_exc  # pragma: no cover — loop always returns or raises


async def _upload_file_inner(
    token:    str,
    filename: str,
    mime:     str,
    b64:      str,
) -> tuple[str, str]:
    cfg       = get_config()
    timeout_s = cfg.get_float("asset.upload_timeout", 60.0)

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire(affinity_key=token)

    payload = orjson.dumps({
        "fileName":     filename,
        "fileMimeType": mime,
        "content":      b64,
    })
    headers = build_http_headers(token, lease=lease)
    kwargs  = build_session_kwargs(lease=lease)

    try:
        async with ResettableSession(**kwargs) as session:
            response = await session.post(
                _UPLOAD_URL,
                headers = headers,
                data    = payload,
                timeout = timeout_s,
            )

        body_bytes = response.content
        if response.status_code != 200:
            body_text = body_bytes.decode("utf-8", "replace")[:300]
            logger.error(
                "asset upload request failed: status={} body={}",
                response.status_code, body_text,
            )
            is_cloudflare = "just a moment" in body_text.lower()
            await proxy.feedback(
                lease,
                build_feedback(response.status_code, is_cloudflare=is_cloudflare),
            )
            raise UpstreamError(
                f"Asset upload returned {response.status_code}",
                status = response.status_code,
                body   = body_text,
            )

        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200),
        )

        result   = orjson.loads(body_bytes)
        file_id  = result.get("fileMetadataId") or result.get("fileId", "")
        file_uri = result.get("fileUri", "")
        logger.info("asset upload completed: filename={!r} file_id={}", filename, file_id)
        return file_id, file_uri

    except UpstreamError:
        raise
    except Exception as exc:
        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR),
        )
        raise UpstreamError(f"Asset upload transport error: {exc}", status=0) from exc


# ---------------------------------------------------------------------------
# URL fetch
# ---------------------------------------------------------------------------

async def _fetch_url_bytes(token: str, file_input: str) -> tuple[bytes, str, str]:
    """Fetch a remote file. Returns (raw_bytes, mime, filename)."""
    cfg      = get_config()
    timeout  = cfg.get_float("asset.fetch_timeout", 30.0)
    proxy    = await get_proxy_runtime()
    lease    = await proxy.acquire(affinity_key=token)

    headers = build_http_headers(token, lease=lease)
    kwargs  = build_session_kwargs(lease=lease)

    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            async with ResettableSession(**kwargs) as session:
                resp = await session.get(file_input, headers=headers, timeout=timeout)
            break
        except UpstreamError:
            raise
        except Exception as exc:
            last_exc = exc
            await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR))
            if attempt == 0:
                logger.warning("asset url fetch retrying after transport error: {}", exc)
                await asyncio.sleep(0.5)
                continue
            raise UpstreamError(f"Asset fetch transport error: {exc}", status=0) from exc
    else:  # pragma: no cover
        raise last_exc

    raw = resp.content
    if resp.status_code != 200:
        await proxy.feedback(
            lease,
            ProxyFeedback(
                kind        = ProxyFeedbackKind.UPSTREAM_5XX if resp.status_code >= 500
                              else ProxyFeedbackKind.FORBIDDEN,
                status_code = resp.status_code,
            ),
        )
        raise UpstreamError(
            f"Failed to fetch input URL: {resp.status_code}",
            status = resp.status_code,
        )
    if not raw:
        await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS))
        raise ValidationError("Input URL returned an empty file", param="content")

    declared_mime = (resp.headers.get("content-type", "").split(";")[0].strip()
                     or "application/octet-stream")
    sniffed = _sniff_mime(raw)

    # An HTML body means we were served a page (auth wall / error), not a file.
    if sniffed == "text/html":
        await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS))
        raise ValidationError(
            "Input URL points to an HTML page, not a downloadable file", param="content",
        )

    if sniffed and (declared_mime in ("application/octet-stream", "text/plain")
                    or sniffed.split("/")[0] != declared_mime.split("/")[0]):
        if sniffed != declared_mime:
            logger.debug(
                "asset mime corrected by magic bytes: declared={} sniffed={}",
                declared_mime, sniffed,
            )
        mime = sniffed
    else:
        mime = declared_mime

    filename = file_input.split("/")[-1].split("?")[0]
    await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS))
    return raw, mime, filename


# ---------------------------------------------------------------------------
# High-level entry
# ---------------------------------------------------------------------------

async def upload_from_input(token: str, file_input: str) -> tuple[str, str]:
    """High-level helper: parse *file_input* (URL or data URI) and upload.

    Returns ``(file_id, file_uri)``.
    """
    if _is_url(file_input):
        raw, mime, filename = await _fetch_url_bytes(token, file_input)
        _check_size(len(raw))
        filename = _sanitize_filename(filename, mime)
        b64      = base64.b64encode(raw).decode()
        return await upload_file(token, filename, mime, b64)

    # Data URI
    filename, b64, mime = parse_data_uri(file_input)
    return await upload_file(token, filename, mime, b64)


def resolve_uploaded_asset_reference(token: str, file_id: str, file_uri: str) -> str:
    """Resolve an uploaded asset to the content URL required by image-edit."""
    user_id = _extract_user_id(token)
    url = resolve_asset_reference(file_id, file_uri, user_id=user_id)
    if url:
        return url
    raise UpstreamError("Could not resolve uploaded asset reference URL")


def _extract_user_id(token: str) -> str | None:
    cookie = build_sso_cookie(token)
    match = _X_USER_ID_RE.search(cookie)
    if match:
        return match.group(1)
    return None


__all__ = [
    "upload_file",
    "upload_from_input",
    "parse_data_uri",
    "resolve_uploaded_asset_reference",
]
