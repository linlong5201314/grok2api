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


def _is_cloudflare_challenge(resp) -> bool:
    """Detect a Cloudflare bot/challenge response vs a genuine origin answer.

    A challenged egress is unusable for grok.com API traffic even though the
    TCP tunnel works, so the probe must not count it as a healthy node.
    """
    headers = getattr(resp, "headers", None) or {}
    try:
        cf_mitigated = str(headers.get("cf-mitigated", "") or "").lower()
        if "challenge" in cf_mitigated:
            return True
        set_cookie = str(headers.get("set-cookie", "") or "").lower()
        if "cf_chl_" in set_cookie:
            return True
    except Exception:  # noqa: BLE001
        pass
    if int(getattr(resp, "status_code", 0) or 0) == 403:
        try:
            body = getattr(resp, "content", None)
            if body:
                raw = body if isinstance(body, bytes) else str(body)
                text = raw[:4096]
                if isinstance(text, bytes):
                    text = text.decode("utf-8", "replace")
                text = text.lower()
                if (
                    "just a moment" in text
                    or "cf-chl" in text
                    or "challenge-platform" in text
                    or "enable javascript" in text
                ):
                    return True
        except Exception:  # noqa: BLE001
            pass
    return False


def _clearance_enabled() -> bool:
    """Whether a Cloudflare clearance mode (manual/flaresolverr) is active.

    When it is, a challenged node is still production-usable — clearance is
    solved per node and the challenge is expected on most datacenter egress.
    """
    try:
        from app.platform.config.snapshot import get_config

        return get_config().get_str("proxy.clearance.mode", "none").lower() != "none"
    except Exception:  # noqa: BLE001
        return False


def _is_grok_origin_response(resp) -> bool:
    """Return True only for a genuine answer from the grok origin.

    The probe sends bogus credentials, so the sole legitimate reply is a
    401 whose body carries grok's own credentials error string
    (``Bad credentials`` / ``bad-credentials`` / ``unauthenticated``).
    Matching a bare ``code`` key would also pass generic auth-error pages
    served instantly by a dead relay's fallback site (observed as healthy
    nodes with physically impossible ~1.4 ms "latency", which then soak up
    every account binding).
    """
    if int(getattr(resp, "status_code", 0) or 0) != 401:
        return False
    try:
        body = getattr(resp, "content", None) or b""
        text = (body if isinstance(body, bytes) else str(body).encode())[:2000].lower()
        return b"credentials" in text
    except Exception:  # noqa: BLE001
        return False


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
            latency, http_state = await self._http_probe(node)
            # A Cloudflare challenge response means the tunnel works and the
            # egress reaches the grok edge — only the bot check blocks it.
            # When a clearance mode is enabled, that is exactly the traffic
            # profile clearance exists for, so the node stays usable (scored
            # by its real TTFB) instead of being failed out of the pool.
            if http_state == "challenge":
                node.last_error = "cloudflare-challenge"
                if _clearance_enabled() and latency is not None:
                    self._apply_success(node, latency)
                    return ProbeOutcome(
                        node.node_id,
                        ok=True,
                        latency_ms=latency,
                        error="cloudflare-challenge",
                    )
                self._apply_failure(node)
                return ProbeOutcome(
                    node.node_id,
                    ok=False,
                    error="cloudflare-challenge",
                )
            if latency is None:
                latency = await self._tcp_probe(node)
            if latency is None:
                self._apply_failure(node)
                reason = (
                    f"tcp+http probe failed ({http_state})"
                    if http_state
                    else "tcp+http probe failed"
                )
                node.last_error = reason
                return ProbeOutcome(node.node_id, ok=False, error=reason)
            node.last_error = ""
            self._apply_success(node, latency)
            return ProbeOutcome(node.node_id, ok=True, latency_ms=latency)

    async def _http_probe(self, node: SubNode) -> tuple[float | None, str]:
        """Measure TTFB through the node's egress URL.

        Returns ``(ttfb_ms, state)`` where ``state`` is one of:
          * ``"ok"``        — reachable, not Cloudflare-challenged
          * ``"challenge"`` — reachable but blocked by a Cloudflare challenge
          * ``"unreachable"`` — no HTTP response at all

        With the default probe URL the probe POSTs the grok rate-limits
        endpoint using bogus credentials: a 401 (bad credentials) proves the
        node passed Cloudflare and reached the grok origin — the exact
        usability signal production traffic depends on — while a 403
        challenge page marks the egress IP as blocked.
        """
        from curl_cffi.requests import AsyncSession

        t0 = time.perf_counter()
        session: AsyncSession | None = None
        try:
            session = AsyncSession(proxy=node.egress_url, timeout=self._timeout_s)
            is_default_probe = self._probe_url.rstrip("/") == "https://grok.com"
            if is_default_probe:
                resp = await session.post(
                    "https://grok.com/rest/rate-limits",
                    headers={
                        "Accept": "*/*",
                        "Content-Type": "application/json",
                        "Origin": "https://grok.com",
                        "Referer": "https://grok.com/",
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/136.0.0.0 Safari/537.36"
                        ),
                        "Cookie": "sso=probe-invalid; sso-rw=probe-invalid",
                    },
                    data=b'{"modelName":"fast"}',
                    allow_redirects=False,
                )
            else:
                resp = await session.head(
                    self._probe_url,
                    allow_redirects=False,
                    # A plain HEAD without impersonation is enough for latency;
                    # impersonation adds CPU cost per probe across many nodes.
                )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if resp.status_code > 0:
                if _is_cloudflare_challenge(resp):
                    return elapsed_ms, "challenge"
                # grok.com only ever answers 401 (bad credentials) to the
                # probe; any other 403 on the rate-limits endpoint is a
                # Cloudflare / geo block, not an auth answer.
                if is_default_probe and resp.status_code == 403:
                    return elapsed_ms, "challenge"
                if is_default_probe and not _is_grok_origin_response(resp):
                    # Not a genuine grok answer — e.g. sing-box returned its
                    # own error page because the outbound is dead.  Counting
                    # this as ok would rank dead nodes at the top.
                    return None, "unreachable"
                return elapsed_ms, "ok"
            return None, "unreachable"
        except Exception:  # noqa: BLE001 — probe failures are expected noise
            return None, "unreachable"
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
        # Decay rather than reset the consecutive-failure streak: probes use a
        # small request shape that can pass nodes (e.g. reality-vision) which
        # production traffic (larger/streaming requests) keeps killing.  If a
        # probe success fully reset the streak, every probe cycle would
        # resurrect those nodes at the top of the pool while real requests
        # continue to die — observed exactly that on Zeabur.
        node.fail_count = max(0, node.fail_count - 1)
        node.ok_count += 1
        node.last_probe_at = now_ms()
        # Slow-but-alive nodes degrade instead of dying so bound accounts get
        # a chance to re-bind only when the node truly stops answering.
        node.state = NodeState.HEALTHY if latency_ms <= _HEALTHY_MS else NodeState.DEGRADED
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
