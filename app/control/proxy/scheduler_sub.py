"""Subscription refresh + speed-test scheduler.

Runs on the scheduler-leader worker only (same advisory-lock discipline as
AccountRefreshScheduler / ProxyClearanceScheduler):

* every ``proxy.subscription.refresh_interval_sec``  → re-fetch all sources;
* every ``proxy.subscription.speedtest_interval_sec`` → probe usable nodes.

The two loops are independent tasks so a slow airport endpoint never delays
health probing.
"""

import asyncio

from app.platform.config.snapshot import get_config
from app.platform.logging.logger import logger


class SubscriptionScheduler:
    """Periodic subscription refresh and node speed testing."""

    def __init__(self) -> None:
        self._refresh_task: asyncio.Task | None = None
        self._probe_task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._refresh_task = asyncio.create_task(self._refresh_loop(), name="sub-refresh")
        self._probe_task = asyncio.create_task(self._probe_loop(), name="sub-speedtest")
        logger.info("subscription scheduler started")

    async def stop(self) -> None:
        self._running = False
        for task in (self._refresh_task, self._probe_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._refresh_task = None
        self._probe_task = None
        from .subscription import get_subscription_manager

        await get_subscription_manager().shutdown()
        logger.info("subscription scheduler stopped")

    # ------------------------------------------------------------------
    # Loops
    # ------------------------------------------------------------------

    async def _refresh_loop(self) -> None:
        from .subscription import get_subscription_manager

        manager = get_subscription_manager()
        # Initial fetch happens during ProxyDirectory bootstrap; wait a full
        # interval before the first scheduled refresh.
        while self._running:
            try:
                await asyncio.sleep(self._refresh_interval())
                if not self._running:
                    break
                await manager.refresh_all()
                await manager.persist()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "subscription refresh loop failed: error_type={} error={}",
                    type(exc).__name__,
                    exc,
                )
                await asyncio.sleep(60)

    async def _probe_loop(self) -> None:
        from .subscription import get_subscription_manager

        manager = get_subscription_manager()
        while self._running:
            try:
                await asyncio.sleep(self._speedtest_interval())
                if not self._running:
                    break
                await manager.run_speedtest()
                await manager.persist()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "subscription speedtest loop failed: error_type={} error={}",
                    type(exc).__name__,
                    exc,
                )
                await asyncio.sleep(60)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def _refresh_interval(self) -> int:
        return max(
            300,
            get_config().get_int("proxy.subscription.refresh_interval_sec", 3600),
        )

    def _speedtest_interval(self) -> int:
        return max(
            120,
            get_config().get_int("proxy.subscription.speedtest_interval_sec", 1800),
        )


__all__ = ["SubscriptionScheduler"]
