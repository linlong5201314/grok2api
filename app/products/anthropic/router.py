"""Anthropic Messages API router (/v1/messages)."""

from typing import Any

import orjson
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.platform.auth.middleware import verify_api_key
from app.platform.errors import AppError, ValidationError
from app.control.model import registry as model_registry


router = APIRouter(prefix="/v1", dependencies=[Depends(verify_api_key)])
_TAG_MESSAGES = "Anthropic - Messages"

_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------

class _ContentBlock(BaseModel):
    model_config = {"extra": "allow"}
    type: str = "text"


class _Message(BaseModel):
    model_config = {"extra": "allow"}
    role:    str
    content: Any = ""


class MessagesRequest(BaseModel):
    model_config = {"extra": "ignore"}

    model:       str
    messages:    list[_Message]
    system:      Any = None          # string or array of content blocks
    max_tokens:  int | None = None   # ignored (Grok doesn't expose this param)
    stream:      bool | None = None
    temperature: float | None = None
    top_p:       float | None = None
    tools:       list[dict] | None = None
    tool_choice: Any = None
    thinking:    Any = None          # {type:"enabled", budget_tokens:N} — used to enable thinking output


class CountTokensRequest(BaseModel):
    model_config = {"extra": "ignore"}

    model:    str
    messages: list[_Message] = []
    system:   Any = None
    tools:    list[dict] | None = None
    thinking: Any = None


# ---------------------------------------------------------------------------
# SSE error wrapper
# ---------------------------------------------------------------------------

_ANTHROPIC_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    429: "rate_limit_error",
    503: "overloaded_error",
}


def _anthropic_error_payload(exc: AppError) -> dict:
    """Map an AppError to Anthropic's canonical error.type vocabulary."""
    status = getattr(exc, "status", 500) or 500
    if isinstance(getattr(exc, "kind", None), str) and exc.kind in set(
        _ANTHROPIC_ERROR_TYPES.values()
    ):
        err_type = exc.kind
    else:
        err_type = _ANTHROPIC_ERROR_TYPES.get(
            status, "api_error" if status >= 500 else "invalid_request_error"
        )
    return {"type": err_type, "message": exc.message}


async def _safe_sse_anthropic(stream):
    """Wrap an Anthropic SSE stream, converting exceptions to error events."""
    try:
        async for chunk in stream:
            yield chunk
    except AppError as exc:
        payload = orjson.dumps(
            {"type": "error", "error": _anthropic_error_payload(exc)}
        ).decode()
        yield f"event: error\ndata: {payload}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as exc:
        payload = orjson.dumps({
            "type": "error",
            "error": {"type": "api_error", "message": str(exc)},
        }).decode()
        yield f"event: error\ndata: {payload}\n\n"
        yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# /v1/messages
# ---------------------------------------------------------------------------

@router.post("/messages", tags=[_TAG_MESSAGES])
async def messages_endpoint(req: MessagesRequest):
    from app.platform.config.snapshot import get_config

    # Model validation
    spec = model_registry.get(req.model)
    if spec is None or not spec.enabled:
        raise ValidationError(
            f"Model {req.model!r} does not exist or you do not have access to it.",
            param="model", code="model_not_found",
        )

    if not req.messages:
        raise ValidationError("messages cannot be empty", param="messages")

    cfg       = get_config()
    # Anthropic 兼容标准：未传 stream 时默认非流式 JSON。
    is_stream = bool(req.stream) if req.stream is not None else False

    # thinking flag: enable when request has thinking config or config default
    if req.thinking is not None and isinstance(req.thinking, dict):
        emit_think = req.thinking.get("type") != "disabled"
    else:
        emit_think = cfg.get_bool("features.thinking", True)

    # Convert Pydantic models → plain dicts
    messages = [m.model_dump() for m in req.messages]

    from .messages import create as messages_create
    result = await messages_create(
        model        = req.model,
        messages     = messages,
        system       = req.system,
        stream       = is_stream,
        emit_think   = emit_think,
        temperature  = req.temperature if req.temperature is not None else 0.8,
        top_p        = req.top_p if req.top_p is not None else 0.95,
        tools        = req.tools or None,
        tool_choice  = req.tool_choice,
    )

    if isinstance(result, dict):
        return JSONResponse(result)
    return StreamingResponse(
        _safe_sse_anthropic(result),
        media_type = "text/event-stream",
        headers    = _SSE_HEADERS,
    )


# ---------------------------------------------------------------------------
# /v1/messages/count_tokens
# ---------------------------------------------------------------------------

@router.post("/messages/count_tokens", tags=[_TAG_MESSAGES])
async def count_tokens_endpoint(req: CountTokensRequest):
    """Anthropic-compatible token estimation (tiktoken heuristic)."""
    from .messages import _parse_anthropic_messages
    from app.products.openai.chat import _extract_message
    from app.platform.tokens import estimate_prompt_tokens

    internal = _parse_anthropic_messages(
        [m.model_dump() for m in req.messages], req.system
    )
    text, _files = _extract_message(internal)
    return JSONResponse({"input_tokens": estimate_prompt_tokens(text)})


__all__ = ["router"]
