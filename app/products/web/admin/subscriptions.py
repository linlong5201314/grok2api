"""Admin subscription management API — airport sources, nodes, speed tests.

Endpoints (mounted under ``/admin/api``):
    GET    /proxy/subscriptions            — sources + manager stats
    POST   /proxy/subscriptions            — add a source {name, url}
    PATCH  /proxy/subscriptions/{id}       — enable/disable/rename
    DELETE /proxy/subscriptions/{id}       — remove source + its nodes
    POST   /proxy/subscriptions/refresh    — re-fetch all enabled sources
    GET    /proxy/nodes                    — ranked node list (redacted)
    POST   /proxy/nodes/speedtest          — start an async speed test

Node payloads are redacted: no credentials, no raw share links, masked hosts.
"""

import asyncio

import orjson
from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel

from app.control.proxy.subscription import get_subscription_manager
from app.platform.errors import AppError, ValidationError
from app.platform.logging.logger import logger

router = APIRouter(tags=["Admin - Subscriptions"])

_speedtest_task: asyncio.Task | None = None


def _not_found(param: str) -> AppError:
    return AppError("订阅不存在", code="subscription_not_found", status=404)


def _json(payload) -> Response:
    return Response(
        content=orjson.dumps(payload),
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class AddSourceRequest(BaseModel):
    name: str = ""
    url: str


class PatchSourceRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def _source_dict(src) -> dict:
    data = src.model_dump()
    data["masked_url"] = src.masked_url()
    return data


@router.get("/proxy/subscriptions")
async def list_subscriptions():
    manager = get_subscription_manager()
    return _json(
        {
            "stats": manager.stats.model_dump(),
            "sources": [_source_dict(s) for s in manager.list_sources()],
        }
    )


@router.post("/proxy/subscriptions")
async def add_subscription(req: AddSourceRequest):
    url = req.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValidationError("订阅链接必须是 http(s) URL", param="url")
    src = await get_subscription_manager().add_source(name=req.name, url=url)
    # Fetch immediately so the panel shows nodes right away.
    result = await get_subscription_manager().fetch_source(src)
    await get_subscription_manager()._rebuild_egress()  # noqa: SLF001
    await get_subscription_manager().persist()
    return _json(
        {
            "status": "success",
            "source": _source_dict(src),
            "fetch": result.model_dump(),
        }
    )


@router.patch("/proxy/subscriptions/{source_id}")
async def patch_subscription(source_id: str, req: PatchSourceRequest):
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if not fields:
        raise ValidationError("没有需要更新的字段", param="body")
    src = await get_subscription_manager().update_source(source_id, **fields)
    if src is None:
        raise _not_found("source_id")
    return _json({"status": "success", "source": _source_dict(src)})


@router.delete("/proxy/subscriptions/{source_id}")
async def delete_subscription(source_id: str):
    ok = await get_subscription_manager().remove_source(source_id)
    if not ok:
        raise _not_found("source_id")
    return _json({"status": "success"})


@router.post("/proxy/subscriptions/refresh")
async def refresh_subscriptions():
    results = await get_subscription_manager().refresh_all(force=True)
    return _json(
        {
            "status": "success",
            "results": [r.model_dump() for r in results],
        }
    )


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


@router.get("/proxy/nodes")
async def list_nodes():
    manager = get_subscription_manager()
    ranked = manager.ranked_nodes()
    running = _speedtest_task is not None and not _speedtest_task.done()
    return _json(
        {
            "running": running,
            "nodes": [
                {
                    **n.redacted(),
                    "rank": i + 1,
                }
                for i, n in enumerate(ranked)
            ],
        }
    )


@router.post("/proxy/nodes/speedtest")
async def start_speedtest():
    global _speedtest_task
    if _speedtest_task is not None and not _speedtest_task.done():
        return _json({"status": "already_running"})
    manager = get_subscription_manager()

    async def _run() -> None:
        try:
            await manager.run_speedtest()
            await manager.persist()
        except Exception as exc:  # noqa: BLE001
            logger.warning("manual speedtest failed: error={}", exc)

    _speedtest_task = asyncio.create_task(_run(), name="admin-speedtest")
    return _json({"status": "started"})
