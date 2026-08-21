"""Node speed testing — TCP reachability + HTTP TTFB probe with EWMA scoring.

Probing runs through the exact same transport stack used for production
traffic (curl_cffi with browser impersonation), so measured latency reflects
real egress quality rather than a synthetic TCP handshake alone.

Scoring model
-------------
* ``latency_ms``      — last successful HTTP probe TTFB (TCP fallback).
* ``loss_rate``       — EWMA of probe outcomes (alpha=0.3).
* ``score``           — ``1000 / max(latency,1) * (1 - loss_rate)``; higher is
  better.  Unprobed nodes score 0 and rank below any probed healthy node.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_ms

from .models import NodeState, SubNode

_ALPHA = 0.3          # EWMA weight for the newest sample
_DEAD_THRESHOLD = 4   # consecutive failures before a node is marked dead
_HEALTHY_MS = 900.0   # at or below this TTFB a node counts as healthy
_DEGRADED_MS = 2500.0


@dataclass(slots=True)
class ProbeOutcome:
    node_id: str
    ok: bool
    latency_ms: float | None = None
    error: str = ""


class NodeSpeedTester:
    """Probe subscription nodes and maintain their health state."""

    def __init__(
        self,
        *,
        concurrency: int = 8,
        timeout_s: float = 6.0,
        probe_url: str = "https://grok.com/",
    ) -> None:
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._timeout_s = max(1.0, timeout_s)
        self._probe_url = probe_url or "https://grok.com/"

    # ------------------------------------------------------------------
    # Batch entry point
    # ------------------------------------------------------------------

    async def probe_all(self, nodes: list[SubNode]) -> list[ProbeOutcome]:
        """Probe every usable node concurrently (bounded)."""
        targets = [n for n in nodes if n.egress_url]
        if not targets:
            return []
        tasks = [asyncio.create_task(self.probe(n)) for n in targets]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[ProbeOutcome] = []
        for node, res in zip(targets, results):
            if isinstance(res, BaseException):
                out.append(ProbeOutcome(node.node_id, ok=False, error=str(res)))
            else:
                out.append(res)
        return out

    # ------------------------------------------------------------------
    # Single node probe
    # ------------------------------------------------------------------

    async def probe(self, node: SubNode) -> ProbeOutcome:
        async with self._semaphore:
            latency = await self._http_probe(node)
            if latency is None:
                latency = await self._tcp_probe(node)
            if latency is None:
                self._apply_failure(node)
                return ProbeOutcome(
                    node.node_id, ok=False, error="tcp+http probe failed"
                )
            self._apply_success(node, latency)
            return ProbeOutcome(node.node_id, ok=True, latency_ms=latency)

    async def _http_probe(self, node: SubNode) -> float | None:
        """Measure TTFB of *probe_url* through the node's egress URL."""
        from curl_cffi.requests import AsyncSession

        t0 = time.perf_counter()
        session: AsyncSession | None = None
        try:
            session = AsyncSession(proxy=node.egress_url, timeout=self._timeout_s)
            resp = await session.head(
                self._probe_url,
                allow_redirects=False,
                # A plain HEAD without impersonation is enough for latency;
                # impersonation adds CPU cost per probe across many nodes.
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            # Any HTTP answer (even 403 from CF) proves the tunnel works.
            if resp.status_code > 0:
                return elapsed_ms
            return None
        except Exception:  # noqa: BLE001 — probe failures are expected noise
            return None
        finally:
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    pass

    async def _tcp_probe(self, node: SubNode) -> float | None:
        """Fallback: raw TCP connect time to server:port."""
        t0 = time.perf_counter()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(node.server, node.port),
                timeout=self._timeout_s,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return (time.perf_counter() - t0) * 1000.0
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # State updates
    # ------------------------------------------------------------------

    def _apply_success(self, node: SubNode, latency_ms: float) -> None:
        node.latency_ms = round(latency_ms, 1)
        node.loss_rate = _ALPHA * 0.0 + (1.0 - _ALPHA) * node.loss_rate
        node.fail_count = 0
        node.ok_count += 1
        node.last_probe_at = now_ms()
        if latency_ms <= _HEALTHY_MS:
            node.state = NodeState.HEALTHY
        elif latency_ms <= _DEGRADED_MS:
            node.state = NodeState.DEGRADED
        else:
            node.state = NodeState.DEGRADED
        node.score = self.compute_score(node)

    def _apply_failure(self, node: SubNode) -> None:
        node.loss_rate = _ALPHA * 1.0 + (1.0 - _ALPHA) * node.loss_rate
        node.fail_count += 1
        node.last_probe_at = now_ms()
        if node.fail_count >= _DEAD_THRESHOLD:
            node.state = NodeState.DEAD
            node.score = 0.0
        elif node.state != NodeState.NEW:
            node.state = NodeState.DEGRADED
        node.score = self.compute_score(node)

    @staticmethod
    def compute_score(node: SubNode) -> float:
        if node.state == NodeState.DEAD:
            return 0.0
        latency = node.latency_ms
        if latency is None or latency <= 0:
            return 0.0
        base = 1000.0 / max(latency, 1.0)
        penalty = 1.0 - min(max(node.loss_rate, 0.0), 1.0)
        return round(base * penalty, 4)


def rank_nodes(nodes: list[SubNode]) -> list[SubNode]:
    """Rank usable nodes best-first (probed+scored, then unprobed stable order)."""
    usable = [n for n in nodes if n.is_usable]

    def sort_key(n: SubNode) -> tuple[int, float, str]:
        probed = 1 if n.score > 0 else 0
        return (-probed, -n.score, n.node_id)

    return sorted(usable, key=sort_key)


def affinity_index(key: str, bucket_count: int) -> int:
    """Stable index in [0, bucket_count) derived from an affinity key."""
    import hashlib

    if bucket_count <= 1:
        return 0
    digest = hashlib.sha1(str(key).encode("utf-8", errors="ignore")).digest()
    return int.from_bytes(digest[:8], "big") % bucket_count


def pick_for_affinity(nodes: list[SubNode], key: str, spread: int) -> SubNode | None:
    """Pick a sticky node for *key* among the top-*spread* ranked nodes.

    The same key deterministically maps to the same node while that node stays
    within the top ranks, giving each account a consistent egress IP.  When a
    bound node degrades out of the candidate set the account transparently
    re-binds to the next best node.
    """
    ranked = rank_nodes(nodes)
    candidates = ranked[: max(1, spread)]
    if not candidates:
        return None
    idx = affinity_index(key, len(candidates))
    chosen = candidates[idx]
    logger.debug(
        "affinity picked: key_tail={} node={} score={} rank={}/{}",
        str(key)[-6:],
        chosen.display_name(),
        chosen.score,
        idx,
        len(candidates),
    )
    return chosen


__all__ = [
    "NodeSpeedTester",
    "ProbeOutcome",
    "rank_nodes",
    "pick_for_affinity",
    "affinity_index",
]
