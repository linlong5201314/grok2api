"""Admin batch operations + SSE progress streaming.

Performance notes:
  - Uses ``run_batch`` for bounded-concurrency parallel execution
    (replaces old sequential for-loop)
  - Async mode: background task with SSE fan-out via AsyncTask
  - Sync mode: concurrent execution, single JSON response
  - Dispatch supports an ``on_complete(ok_tokens, fail_tokens)`` hook so
    handlers can defer DB writes into a single bulk patch after the upstream
    network burst, instead of issuing one transaction per token.
"""

import asyncio
from typing import TYPE_CHECKING, Any, Callable, Awaitable

import orjson
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from app.platform.config.snapshot import get_config
from app.platform.errors import AppError, ErrorKind, UpstreamError, ValidationError
from app.platform.logging.logger import logger
from app.platform.runtime.batch import run_batch
from app.platform.runtime.clock import now_ms
from app.platform.runtime.task import create_task, expire_task, get_task
from app.control.account.commands import AccountPatch, ListAccountsQuery
from app.control.account.enums import AccountStatus
from app.control.account.state_machine import is_manageable

if TYPE_CHECKING:
    from app.control.account.refresh import AccountRefreshService
    from app.control.account.repository import AccountRepository

from . import get_refresh_svc, get_repo

router = APIRouter(prefix="/batch", tags=["Admin - Batch"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _concurrency(override: int | None, config_key: str, fallback: int = 50) -> int:
    """Resolve effective concurrency: query-param → config → fallback."""
    if override is not None:
        return max(1, override)
    v = get_config(config_key, fallback)
    return max(1, int(v))


def _mask(token: str) -> str:
    return f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token


async def _list_all_tokens(repo: "AccountRepository") -> list[str]:
    page_num, tokens = 1, []
    while True:
        page = await repo.list_accounts(ListAccountsQuery(page=page_num, page_size=2000))
        tokens.extend(r.token for r in page.items if is_manageable(r))
        if page_num * 2000 >= page.total:
            break
        page_num += 1
    return tokens


def _json(data: Any, status_code: int = 200) -> Response:
    return Response(content=orjson.dumps(data), media_type="application/json", status_code=status_code)


class BatchRequest(BaseModel):
    tokens: list[str] = []


# ---------------------------------------------------------------------------
# Dispatch engine — sync (run_batch) or async (background task + SSE)
# ---------------------------------------------------------------------------

PostBatchHook = Callable[[list[str], list[str]], Awaitable[None]]


async def _dispatch(
    tokens: list[str],
    handler: Callable[[str], Awaitable[dict]],
    *,
    use_async: bool,
    concurrency: int = 10,
    on_complete: PostBatchHook | None = None,
) -> Response:
    if use_async:
        return await _dispatch_async(tokens, handler, concurrency, on_complete=on_complete)
    return await _dispatch_sync(tokens, handler, concurrency, on_complete=on_complete)


async def _safe_post_batch(
    on_complete: PostBatchHook | None,
    ok_tokens:   list[str],
    fail_tokens: list[str],
) -> None:
    if on_complete is None:
        return
    try:
        await on_complete(ok_tokens, fail_tokens)
    except Exception as exc:
        logger.warning(
            "admin batch post-hook failed: ok_count={} fail_count={} error={}",
            len(ok_tokens), len(fail_tokens), exc,
        )


async def _dispatch_sync(
    tokens: list[str],
    handler: Callable[[str], Awaitable[dict]],
    concurrency: int,
    *,
    on_complete: PostBatchHook | None = None,
) -> Response:
    """Concurrent execution, collect all results, return at once."""
    results: dict[str, Any] = {}
    ok_c = fail_c = 0
    ok_tokens: list[str] = []
    fail_tokens: list[str] = []

    async def _wrapped(token: str) -> tuple[str, dict | None, str | None]:
        try:
            data = await handler(token)
            return token, data, None
        except Exception as exc:
            return token, None, str(exc)

    raw = await run_batch(tokens, _wrapped, concurrency=concurrency)
    for token, data, err in raw:
        key = _mask(token)
        if err is None:
            ok_c += 1
            ok_tokens.append(token)
            results[key] = data
        else:
            fail_c += 1
            fail_tokens.append(token)
            results[key] = {"error": err}

    await _safe_post_batch(on_complete, ok_tokens, fail_tokens)

    return _json({
        "status": "success",
        "summary": {"total": len(tokens), "ok": ok_c, "fail": fail_c},
        "results": results,
    })


async def _dispatch_async(
    tokens: list[str],
    handler: Callable[[str], Awaitable[dict]],
    concurrency: int,
    *,
    on_complete: PostBatchHook | None = None,
) -> Response:
    """Background task with per-item progress via AsyncTask SSE."""
    task = create_task(len(tokens))

    async def _run() -> None:
        try:
            sem = asyncio.Semaphore(concurrency)
            results: dict[str, Any] = {}
            ok_c = fail_c = 0
            ok_tokens: list[str] = []
            fail_tokens: list[str] = []
            tokens_lock = asyncio.Lock()

            async def _one(token: str) -> None:
                nonlocal ok_c, fail_c
                if task.cancelled:
                    return
                async with sem:
                    # Re-check after acquiring slot: cancel may have been set
                    # while this coroutine was waiting for a semaphore slot.
                    if task.cancelled:
                        return
                    masked = _mask(token)
                    try:
                        data = await handler(token)
                        async with tokens_lock:
                            ok_c += 1
                            ok_tokens.append(token)
                            results[masked] = data
                        task.record(True, item=masked, detail=data)
                    except Exception as exc:
                        async with tokens_lock:
                            fail_c += 1
                            fail_tokens.append(token)
                            results[masked] = {"error": str(exc)}
                        task.record(False, item=masked, error=str(exc))

            await asyncio.gather(*[_one(t) for t in tokens])

            await _safe_post_batch(on_complete, ok_tokens, fail_tokens)

            if task.cancelled:
                task.finish_cancelled()
            else:
                task.finish({
                    "status": "success",
                    "summary": {"total": len(tokens), "ok": ok_c, "fail": fail_c},
                    "results": results,
                })
        except Exception as exc:
            task.fail_task(str(exc))
        finally:
            asyncio.create_task(expire_task(task.id, 300))

    asyncio.create_task(_run())
    return _json({"status": "success", "task_id": task.id, "total": len(tokens)})


# ---------------------------------------------------------------------------
# Per-token handlers
# ---------------------------------------------------------------------------

async def _nsfw_one(token: str, enabled: bool) -> dict:
    """Run the upstream NSFW toggle for *token* only.

    DB tagging is intentionally NOT done here — bulk callers register an
    ``on_complete`` hook on ``_dispatch`` so all successful tokens are
    written in a single ``patch_accounts`` call (which itself uses an
    executemany under the hood).  This collapses what used to be 3 network
    round-trips per token (set_birth, set_nsfw, DB SELECT+UPDATE) into
    1 reused TLS session + 1 deferred bulk patch.
    """
    from app.dataplane.reverse.protocol.xai_auth import nsfw_sequence, set_nsfw
    if enabled:
        # nsfw_sequence shares one proxy lease + one HTTPS session across
        # set_birth_date + enable_nsfw, halving TLS/proxy overhead.
        await nsfw_sequence(token)
    else:
        await set_nsfw(token, False)
    return {"success": True, "tagged": enabled}


async def _cache_clear_one(repo: "AccountRepository", token: str) -> dict:
    from app.control.account.invalid_credentials import mark_account_invalid_credentials
    from app.dataplane.reverse.transport.assets import list_assets, delete_asset
    try:
        resp = await list_assets(token)
        items = resp.get("assets", resp.get("items", []))

        async def _delete_one(item: dict) -> int:
            asset_id = item.get("id") or item.get("assetId")
            if not asset_id:
                return 0
            await delete_asset(token, asset_id)
            return 1

        results = await asyncio.gather(*[_delete_one(item) for item in items], return_exceptions=True)
        for result in results:
            if not isinstance(result, Exception):
                continue
            if await mark_account_invalid_credentials(repo, token, result, source="asset batch clear"):
                raise result
        return {"deleted": sum(r for r in results if isinstance(r, int))}
    except Exception as exc:
        await mark_account_invalid_credentials(repo, token, exc, source="asset batch clear")
        raise


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/nsfw")
async def batch_nsfw(
    req: BatchRequest,
    async_mode: bool = Query(False, alias="async"),
    concurrency: int | None = Query(None, ge=1),
    enabled: bool = Query(True),
    repo: "AccountRepository" = Depends(get_repo),
):
    tokens = [t.strip() for t in req.tokens if t.strip()]
    if not tokens:
        tokens = await _list_all_tokens(repo)
    if not tokens:
        raise ValidationError("No tokens available", param="tokens")

    async def _nsfw_call(token: str) -> dict:
        return await _nsfw_one(token, enabled)

    async def _bulk_tag(ok_tokens: list[str], _fail_tokens: list[str]) -> None:
        if not ok_tokens:
            return
        if enabled:
            patches = [AccountPatch(token=t, add_tags=["nsfw"]) for t in ok_tokens]
        else:
            patches = [AccountPatch(token=t, remove_tags=["nsfw"]) for t in ok_tokens]
        await repo.patch_accounts(patches)
        logger.info(
            "admin batch nsfw tags written: enabled={} ok_count={}",
            enabled, len(ok_tokens),
        )

    c = _concurrency(concurrency, "batch.nsfw_concurrency")
    return await _dispatch(
        tokens, _nsfw_call,
        use_async=async_mode, concurrency=c,
        on_complete=_bulk_tag,
    )


# ---------------------------------------------------------------------------
# Batch disable / restore — DB-only, no upstream calls (port from upstream PR518)
# ---------------------------------------------------------------------------


class BatchDisableRequest(BaseModel):
    tokens: list[str] = []
    disabled: bool = True


@router.post("/disable")
async def batch_disable(
    req: BatchDisableRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    """Disable or restore the given tokens in a single DB transaction.

    Pure DB operation — no upstream network calls — so a one-shot patch is the
    right shape (no need for the dispatch / SSE machinery used by NSFW etc.).
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in req.tokens:
        t = (raw or "").strip()
        if t and t not in seen:
            seen.add(t)
            cleaned.append(t)
    if not cleaned:
        raise ValidationError("No tokens provided", param="tokens")

    records = await repo.get_accounts(cleaned)
    if not records:
        raise AppError(
            "No matching accounts found",
            kind=ErrorKind.VALIDATION,
            code="account_not_found",
            status=404,
        )

    ts = now_ms()
    patches: list[AccountPatch] = []
    for record in records:
        if req.disabled:
            patches.append(AccountPatch(
                token=record.token,
                status=AccountStatus.DISABLED,
                state_reason="operator_disabled",
                ext_merge={
                    **record.ext,
                    "disabled_at": ts,
                    "disabled_reason": "operator_disabled",
                },
            ))
        else:
            patches.append(AccountPatch(
                token=record.token,
                status=AccountStatus.ACTIVE,
                clear_failures=True,
            ))

    result = await repo.patch_accounts(patches)
    logger.info(
        "admin batch disable applied: disabled={} requested_count={} patched_count={}",
        req.disabled, len(cleaned), result.patched,
    )
    return _json({
        "status": "success",
        "disabled": req.disabled,
        "summary": {
            "total": len(cleaned),
            "ok":    result.patched,
            "fail":  max(0, len(cleaned) - result.patched),
        },
    })


@router.post("/refresh")
async def batch_refresh(
    req: BatchRequest,
    async_mode: bool = Query(False, alias="async"),
    concurrency: int | None = Query(None, ge=1),
    refresh_svc: "AccountRefreshService" = Depends(get_refresh_svc),
):
    tokens = [t.strip() for t in req.tokens if t.strip()]
    if not tokens:
        raise ValidationError("No tokens provided", param="tokens")

    async def _refresh_one(token: str) -> dict:
        result = await refresh_svc.refresh_tokens([token])
        if not result.refreshed:
            raise UpstreamError("未获取到真实配额数据")
        return {"refreshed": result.refreshed}

    c = _concurrency(concurrency, "batch.refresh_concurrency")
    return await _dispatch(tokens, _refresh_one, use_async=async_mode, concurrency=c)


@router.post("/cache-clear")
async def batch_cache_clear(
    req: BatchRequest,
    async_mode: bool = Query(False, alias="async"),
    concurrency: int | None = Query(None, ge=1),
    repo: "AccountRepository" = Depends(get_repo),
):
    tokens = [t.strip() for t in req.tokens if t.strip()]
    if not tokens:
        tokens = await _list_all_tokens(repo)
    if not tokens:
        raise ValidationError("No tokens available", param="tokens")

    async def _clear_one(token: str) -> dict:
        return await _cache_clear_one(repo, token)

    c = _concurrency(concurrency, "batch.asset_delete_concurrency")
    return await _dispatch(tokens, _clear_one, use_async=async_mode, concurrency=c)


# ---------------------------------------------------------------------------
# SSE stream + cancel
# ---------------------------------------------------------------------------

@router.get("/{task_id}/stream")
async def batch_stream(task_id: str, request: Request):
    # Auth is handled by the parent router's verify_admin_key dependency,
    # which accepts both Bearer header and ?app_key= query param (for EventSource).
    task = get_task(task_id)
    if not task:
        raise AppError(
            "Task not found",
            kind=ErrorKind.VALIDATION,
            code="task_not_found",
            status=404,
        )

    async def _stream():
        queue = task.attach()
        try:
            yield f"data: {orjson.dumps({'type': 'snapshot', **task.snapshot()}).decode()}\n\n"

            final = task.final_event()
            if final:
                yield f"data: {orjson.dumps(final).decode()}\n\n"
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    final = task.final_event()
                    if final:
                        yield f"data: {orjson.dumps(final).decode()}\n\n"
                        return
                    continue

                yield f"data: {orjson.dumps(event).decode()}\n\n"
                if event.get("type") in ("done", "error", "cancelled"):
                    return
        finally:
            task.detach(queue)

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.post("/{task_id}/cancel")
async def batch_cancel(task_id: str):
    task = get_task(task_id)
    if not task:
        raise AppError(
            "Task not found",
            kind=ErrorKind.VALIDATION,
            code="task_not_found",
            status=404,
        )
    task.cancel()
    return {"status": "success"}
