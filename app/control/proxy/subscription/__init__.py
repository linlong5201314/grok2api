"""SubscriptionManager — airport subscription lifecycle coordinator.

Owns the configured subscription sources, the parsed node registry, the
optional sing-box child process, and periodic refresh/speed-test cycles.
Selection logic (best-node ranking, per-account sticky binding) lives in
:mod:`.speedtest` and is exposed here for ProxyDirectory.

Persistence: ``{DATA_DIR}/proxy_subscriptions.json`` keeps sources and node
stats across restarts so scores survive reboots.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp
from pydantic import BaseModel

from app.platform.config.snapshot import get_config
from app.platform.logging.logger import logger
from app.platform.paths import data_path
from app.platform.runtime.clock import now_ms

from .core_runner import CoreRunner
from .models import NodeState, SubNode, SubscriptionFetchResult, SubscriptionSource
from .parsers import extract_provider_urls, parse_subscription_payload
from .speedtest import NodeSpeedTester, pick_for_affinity, rank_nodes

_STORE_FILE = "proxy_subscriptions.json"
_FETCH_TIMEOUT_S = 20.0
_MAX_BODY_BYTES = 8 * 1024 * 1024


def _decode_body(raw: bytes) -> str:
    """Decode subscription bytes to text (utf-8 → gb18030 → replace)."""
    for enc in ("utf-8", "utf-16", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _mask_url(url: str) -> str:
    """Redact query params from a provider URL for logs."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class ManagerStats(BaseModel):
    source_count: int = 0
    node_count: int = 0
    usable_count: int = 0
    healthy_count: int = 0
    needs_core_count: int = 0
    dead_count: int = 0
    last_refresh_at: int | None = None
    last_speedtest_at: int | None = None
    core_running: bool = False


class SubscriptionManager:
    """Central subscription state; one instance per process."""

    def __init__(self) -> None:
        self._sources: dict[str, SubscriptionSource] = {}
        self._nodes: dict[str, SubNode] = {}  # node_id -> node
        self._lock = asyncio.Lock()
        self._tester = NodeSpeedTester()
        self._core = CoreRunner()
        self._refreshing = False
        self._restored_stats: dict[str, dict] = {}
        # account_key -> node_id; survives restarts via the store file.
        # Score reordering must NEVER reshuffle an existing binding — an
        # account keeps its node until that node becomes unusable.
        self._affinity: dict[str, str] = {}
        self._affinity_flush_task: asyncio.Task | None = None
        self.stats = ManagerStats()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _cfg_urls(self) -> list[str]:
        cfg = get_config()
        urls = [u.strip() for u in cfg.get_list("proxy.subscription.urls", []) if u.strip()]
        return urls

    def sync_sources_from_config(self) -> bool:
        """Mirror config-listed URLs into managed sources.

        Config URLs get stable ids derived from the URL itself so restarts do
        not duplicate them.  Returns True when the source set changed.
        """
        import hashlib

        changed = False
        cfg_urls = self._cfg_urls()
        seen_ids: set[str] = set()
        for url in cfg_urls:
            sid = "cfg-" + hashlib.sha1(url.encode()).hexdigest()[:10]
            seen_ids.add(sid)
            if sid not in self._sources:
                self._sources[sid] = SubscriptionSource(
                    source_id=sid, name=f"订阅 {len(seen_ids)}", url=url
                )
                changed = True
        for sid in [
            s.source_id
            for s in self._sources.values()
            if s.source_id.startswith("cfg-") and s.source_id not in seen_ids
        ]:
            self._sources.pop(sid, None)
            changed = True
        return changed

    # ------------------------------------------------------------------
    # Source CRUD (admin-managed, persisted)
    # ------------------------------------------------------------------

    def list_sources(self) -> list[SubscriptionSource]:
        return sorted(self._sources.values(), key=lambda s: s.created_sort_key())

    async def add_source(self, *, name: str, url: str) -> SubscriptionSource:
        # Random id, NOT the process counter: next_hex() restarts at zero on
        # every deploy and can collide with ids restored from the store,
        # silently overwriting an existing subscription.
        import uuid

        async with self._lock:
            sid = uuid.uuid4().hex[:10]
            while sid in self._sources:
                sid = uuid.uuid4().hex[:10]
            src = SubscriptionSource(
                source_id=sid,
                name=name or "订阅",
                url=url.strip(),
                created_at=now_ms(),
            )
            self._sources[src.source_id] = src
        await self.persist()
        return src

    async def remove_source(self, source_id: str) -> bool:
        async with self._lock:
            src = self._sources.pop(source_id, None)
            if src is None:
                return False
            dead = [nid for nid, n in self._nodes.items() if n.source_id == source_id]
            for nid in dead:
                self._nodes.pop(nid, None)
        await self.persist()
        return True

    async def update_source(self, source_id: str, **fields: Any) -> SubscriptionSource | None:
        async with self._lock:
            src = self._sources.get(source_id)
            if src is None:
                return None
            data = {**src.model_dump(), **fields}
            self._sources[source_id] = SubscriptionSource(**data)
            updated = self._sources[source_id]
        await self.persist()
        return updated

    # ------------------------------------------------------------------
    # Fetching / parsing
    # ------------------------------------------------------------------

    async def fetch_source(self, source: SubscriptionSource) -> SubscriptionFetchResult:
        t0 = time.perf_counter()
        ua = (
            get_config().get_str("proxy.subscription.fetch_user_agent", "").strip()
            or "ClashMetaForAndroid/2.11.5.Meta"
        )
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_S),
                headers={"User-Agent": ua},
            ) as session:
                async with session.get(source.url) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    body = await resp.content.read(_MAX_BODY_BYTES)
        except Exception as exc:  # noqa: BLE001 — network errors are routine
            duration = int((time.perf_counter() - t0) * 1000)
            source.last_fetch_ok = False
            source.last_error = str(exc)[:200]
            source.last_fetch_at = now_ms()
            logger.warning(
                "subscription fetch failed: source={} url={} error={}",
                source.source_id,
                source.masked_url(),
                source.last_error,
            )
            return SubscriptionFetchResult(
                source_id=source.source_id,
                ok=False,
                error=source.last_error,
                duration_ms=duration,
            )

        nodes = parse_subscription_payload(body, source_id=source.source_id)
        # mihomo "thin" configs reference real nodes via proxy-providers
        # instead of inline proxies: fetch each provider URL and merge.
        if not nodes:
            provider_urls = extract_provider_urls(_decode_body(body))
            if provider_urls:
                for purl in provider_urls:
                    try:
                        async with aiohttp.ClientSession(
                            timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_S),
                            headers={"User-Agent": ua},
                        ) as p_session:
                            async with p_session.get(purl) as presp:
                                if presp.status != 200:
                                    logger.warning(
                                        "proxy-provider fetch failed: source={} url={} status={}",
                                        source.source_id,
                                        _mask_url(purl),
                                        presp.status,
                                    )
                                    continue
                                pbody = await presp.content.read(_MAX_BODY_BYTES)
                        pnodes = parse_subscription_payload(
                            pbody, source_id=source.source_id
                        )
                        logger.info(
                            "proxy-provider fetched: source={} provider_nodes={}",
                            source.source_id,
                            len(pnodes),
                        )
                        nodes.extend(pnodes)
                    except Exception as exc:  # noqa: BLE001 — network errors are routine
                        logger.warning(
                            "proxy-provider fetch failed: source={} url={} error={}",
                            source.source_id,
                            _mask_url(purl),
                            str(exc)[:200],
                        )
        duration = int((time.perf_counter() - t0) * 1000)
        source.last_fetch_ok = True
        source.last_error = ""
        source.last_fetch_at = now_ms()
        source.node_count = len(nodes)

        merged = self._merge_nodes(source.source_id, nodes)
        logger.info(
            "subscription fetched: source={} nodes={} merged_total={} dur_ms={}",
            source.source_id,
            len(nodes),
            merged,
            duration,
        )
        return SubscriptionFetchResult(
            source_id=source.source_id, ok=True, node_count=len(nodes), duration_ms=duration
        )

    def _merge_nodes(self, source_id: str, fresh: list[SubNode]) -> int:
        """Replace this source's nodes, preserving stats of surviving nodes.

        An empty fresh list is treated as a transient upstream failure
        (airports throttle rapid re-pulls with empty/error bodies) — the
        last-known-good pool is KEPT instead of being wiped.
        """
        if not fresh and any(n.source_id == source_id for n in self._nodes.values()):
            logger.warning(
                "subscription returned 0 nodes (likely throttled); "
                "keeping existing pool: source={}",
                source_id,
            )
            self._update_stats()
            return len(self._nodes)
        fresh_ids = {n.node_id for n in fresh}
        for nid in [
            nid
            for nid, n in self._nodes.items()
            if n.source_id == source_id and nid not in fresh_ids
        ]:
            self._nodes.pop(nid, None)
        for node in fresh:
            old = self._nodes.get(node.node_id)
            if old is None:
                old = self._take_restored_stats(node.node_id)
            if old is not None:
                # carry runtime stats across refreshes / restarts
                node.latency_ms = old.latency_ms
                node.loss_rate = old.loss_rate
                node.score = old.score
                node.ok_count = old.ok_count
                node.fail_count = old.fail_count
                node.last_probe_at = old.last_probe_at
                node.state = old.state
            self._nodes[node.node_id] = node
        self._update_stats()
        return len(self._nodes)

    def _take_restored_stats(self, node_id: str) -> SubNode | None:
        """Rebuild a transient node shell from persisted stats (first fetch)."""
        raw = self._restored_stats.pop(node_id, None)
        if not raw:
            return None
        try:
            return SubNode(
                node_id=node_id,
                latency_ms=raw.get("latency_ms"),
                loss_rate=float(raw.get("loss_rate", 0.0)),
                score=float(raw.get("score", 0.0)),
                ok_count=int(raw.get("ok_count", 0)),
                fail_count=int(raw.get("fail_count", 0)),
                last_probe_at=raw.get("last_probe_at"),
                state=NodeState(raw.get("state", "new")),
            )
        except Exception:  # noqa: BLE001
            return None

    async def refresh_all(self, *, force: bool = False) -> list[SubscriptionFetchResult]:
        """Re-fetch every enabled source, then rebuild egress endpoints."""
        if self._refreshing:
            return []
        self._refreshing = True
        try:
            self.sync_sources_from_config()
            sources = [s for s in self._sources.values() if s.enabled or force]
            results: list[SubscriptionFetchResult] = []
            sem = asyncio.Semaphore(4)

            async def _one(src: SubscriptionSource) -> SubscriptionFetchResult:
                async with sem:
                    return await self.fetch_source(src)

            results = list(await asyncio.gather(*(_one(s) for s in sources)))
            await self._rebuild_egress()
            self.stats.last_refresh_at = now_ms()
            await self.persist()
            return results
        finally:
            self._refreshing = False

    async def _rebuild_egress(self) -> None:
        """Assign egress URLs: direct protocols immediately, core nodes via sing-box."""
        direct_usable = 0
        need_core: list[SubNode] = []
        for node in self._nodes.values():
            if node.is_direct:
                node.egress_url = _direct_url(node)
                if node.state in (NodeState.NEW, NodeState.NEEDS_CORE):
                    node.state = NodeState.NEW
                direct_usable += 1
            elif node.needs_core:
                need_core.append(node)
            else:
                node.egress_url = ""
                node.state = NodeState.DEAD

        port_map = await self._core.ensure_running(need_core)
        for node in need_core:
            if port_map.get(node.node_id):
                node.state = NodeState.NEW
            else:
                # Core unavailable or failed for this node — clear any stale
                # egress assigned during config generation so the node is
                # excluded from selection instead of pointing at a dead port.
                node.state = NodeState.NEEDS_CORE
                node.egress_url = ""
        self._update_stats()
        logger.debug(
            "egress rebuilt: direct={} core_served={} needs_core={}",
            direct_usable,
            len(port_map),
            sum(1 for n in need_core if n.state == NodeState.NEEDS_CORE),
        )

    # ------------------------------------------------------------------
    # Speed testing
    # ------------------------------------------------------------------

    async def run_speedtest(self, *, only_stale: bool = False) -> int:
        """Probe all usable nodes; returns how many were probed."""
        nodes = [n for n in self._nodes.values() if n.egress_url]
        if only_stale:
            cutoff = now_ms() - 15 * 60 * 1000
            nodes = [n for n in nodes if not n.last_probe_at or n.last_probe_at < cutoff]
        outcomes = await self._tester.probe_all(nodes)
        probed = sum(1 for o in outcomes if o.ok)
        self.stats.last_speedtest_at = now_ms()
        self._update_stats()
        logger.info(
            "subscription speedtest done: probed={} ok={}/{}",
            len(outcomes),
            probed,
            len(outcomes),
        )
        return len(outcomes)

    # ------------------------------------------------------------------
    # Selection API (used by ProxyDirectory)
    # ------------------------------------------------------------------

    def ranked_nodes(self) -> list[SubNode]:
        return rank_nodes(list(self._nodes.values()))

    def pick_for_account(self, affinity_key: str | None) -> SubNode | None:
        """Pick the node bound to an account identity.

        Binding policy (anti-correlation contract):
        * First sight  — assign via ranked bucket and REMEMBER the choice.
        * Return visits — always return the remembered node while it stays
          usable, regardless of score reordering between speedtests.
        * Node lost    — drop the binding and transparently re-bind.
        Bindings persist across restarts through the store file.
        """
        spread = max(1, get_config().get_int("proxy.subscription.affinity_spread", 3))
        if affinity_key:
            bound_id = self._affinity.get(affinity_key)
            if bound_id:
                node = self._nodes.get(bound_id)
                if node is not None and node.is_usable:
                    return node
                # Stale binding (node gone / dead / no egress) — re-bind below.
                self._affinity.pop(affinity_key, None)
            chosen = pick_for_affinity(list(self._nodes.values()), affinity_key, spread)
            if chosen is not None:
                self._affinity[affinity_key] = chosen.node_id
                self._schedule_affinity_flush()
            return chosen
        ranked = self.ranked_nodes()
        return ranked[0] if ranked else None

    def _schedule_affinity_flush(self) -> None:
        """Debounced background persist after a binding change."""
        if self._affinity_flush_task is not None and not self._affinity_flush_task.done():
            return

        async def _flush() -> None:
            await asyncio.sleep(2.0)
            await self.persist()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._affinity_flush_task = loop.create_task(_flush())

    def node_by_egress(self, proxy_url: str) -> SubNode | None:
        for node in self._nodes.values():
            if node.egress_url and node.egress_url == proxy_url:
                return node
        return None

    def _update_stats(self) -> None:
        nodes = list(self._nodes.values())
        self.stats = ManagerStats(
            source_count=len(self._sources),
            node_count=len(nodes),
            usable_count=sum(1 for n in nodes if n.is_usable),
            healthy_count=sum(1 for n in nodes if n.state == NodeState.HEALTHY),
            needs_core_count=sum(1 for n in nodes if n.state == NodeState.NEEDS_CORE),
            dead_count=sum(1 for n in nodes if n.state == NodeState.DEAD),
            last_refresh_at=self.stats.last_refresh_at,
            last_speedtest_at=self.stats.last_speedtest_at,
            core_running=self._core.is_running,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def persist(self) -> None:
        # Drop bindings whose node no longer exists before writing.
        self._affinity = {
            k: nid for k, nid in self._affinity.items() if nid in self._nodes
        }
        payload = {
            "version": 1,
            "sources": [s.model_dump() for s in self._sources.values()],
            "affinity": dict(self._affinity),
            "nodes": {
                nid: {
                    "latency_ms": n.latency_ms,
                    "loss_rate": n.loss_rate,
                    "score": n.score,
                    "ok_count": n.ok_count,
                    "fail_count": n.fail_count,
                    "last_probe_at": n.last_probe_at,
                    "state": n.state.value,
                }
                for nid, n in self._nodes.items()
            },
        }
        try:
            path = data_path(_STORE_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            import orjson

            path.write_bytes(orjson.dumps(payload))
        except Exception as exc:  # noqa: BLE001
            logger.warning("subscription persist failed: error={}", exc)

    async def restore(self) -> None:
        path = data_path(_STORE_FILE)
        if not path.exists():
            return
        try:
            import orjson

            payload = orjson.loads(path.read_bytes())
            for sd in payload.get("sources", []):
                src = SubscriptionSource(**sd)
                self._sources[src.source_id] = src
            stats_map: dict[str, dict] = payload.get("nodes", {})
            self._restored_stats = stats_map
            raw_affinity = payload.get("affinity", {})
            if isinstance(raw_affinity, dict):
                self._affinity = {
                    str(k): str(v) for k, v in raw_affinity.items() if v
                }
            logger.info(
                "subscription store restored: sources={} stat_entries={} bindings={}",
                len(self._sources),
                len(stats_map),
                len(self._affinity),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("subscription restore failed: error={}", exc)

    # ------------------------------------------------------------------
    # Startup bootstrap
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        await self.restore()
        self.sync_sources_from_config()
        # Re-apply restored stats onto freshly parsed nodes after first fetch;
        # until then just rebuild egress from any persisted knowledge.
        if self._sources:
            await self.refresh_all()

    async def shutdown(self) -> None:
        await self._core.stop()


def _direct_url(node: SubNode) -> str:
    scheme = node.protocol.value
    if scheme == "socks5":
        scheme = "socks5h"
    if scheme == "socks4":
        scheme = "socks4a"
    auth = ""
    if node.credential:
        auth = f"{node.credential}@"
    return f"{scheme}://{auth}{node.server}:{node.port}"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_manager: SubscriptionManager | None = None


def get_subscription_manager() -> SubscriptionManager:
    global _manager
    if _manager is None:
        _manager = SubscriptionManager()
    return _manager


__all__ = ["SubscriptionManager", "ManagerStats", "get_subscription_manager"]
