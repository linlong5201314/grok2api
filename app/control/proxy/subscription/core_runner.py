"""Optional local-core integration (sing-box).

Airport subscriptions mostly contain ``vmess`` / ``vless`` / ``trojan`` /
``ss`` / ``hysteria2`` nodes which a plain HTTP client cannot dial directly.
When a `sing-box <https://sing-box.sagernet.org>`_ binary is available this
module generates one mixed (HTTP+SOCKS) inbound per node on ``127.0.0.1`` and
runs a single core process; each node's :attr:`SubNode.egress_url` then points
at its local inbound (``http://127.0.0.1:<port>``).

The runner is fully optional: without a core binary, direct-protocol nodes
(http/socks) still work and core-protocol nodes are reported as
``needs_core`` in the panel.

xray is intentionally not supported — its config schema differs materially
and maintaining two generators doubles the failure surface.  The docs point
users at sing-box.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections import deque
from pathlib import Path

from app.platform.logging.logger import logger
from app.platform.paths import data_path

from .models import SubNode


class CoreRunner:
    """Lifecycle manager for the optional sing-box child process."""

    def __init__(
        self,
        *,
        binary_path: str = "",
        start_port: int = 21001,
        max_nodes: int = 64,
    ) -> None:
        self._binary_path = binary_path or ""
        self._start_port = max(1024, start_port)
        self._max_nodes = max(1, max_nodes)
        self._process: asyncio.subprocess.Process | None = None
        self._config_path: Path | None = None
        self._port_map: dict[str, int] = {}  # node_id -> local port
        self._sig: tuple | None = None
        self._stderr_buf: deque[str] = deque(maxlen=200)
        self._drain_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def recent_stderr(self, limit: int = 50) -> list[str]:
        """Return the most recent sing-box stderr lines (drain buffer)."""
        if limit <= 0:
            return []
        return list(self._stderr_buf)[-limit:]

    def resolve_binary(self) -> str:
        """Return the sing-box executable path, or '' when unavailable.

        Re-reads ``proxy.subscription.core_path`` each call so a path set in
        the admin panel takes effect on the next refresh without a restart.
        """
        from app.platform.config.snapshot import get_config

        configured = get_config().get_str("proxy.subscription.core_path", "").strip()
        if configured:
            self._binary_path = configured
        if self._binary_path:
            found = shutil.which(self._binary_path)
            if found:
                return found
            p = Path(self._binary_path)
            if p.is_file():
                return str(p)
            return ""
        return shutil.which("sing-box") or ""

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @staticmethod
    def _listen_host() -> str:
        """Bind address for the per-node mixed inbounds.

        Defaults to 127.0.0.1 (only this process can use the ports).  On
        container platforms (e.g. Zeabur/Kubernetes) FlareSolverr runs in a
        *separate* container and must reach these ports over the internal
        network — set ``proxy.subscription.core_listen_host`` to ``0.0.0.0``
        there and point ``FLARESOLVERR_HOST_ALIAS`` at this service's
        internal hostname so the rewritten proxy URL resolves.
        """
        from app.platform.config.snapshot import get_config

        host = get_config().get_str("proxy.subscription.core_listen_host", "127.0.0.1").strip()
        return host or "127.0.0.1"

    def port_for(self, node_id: str) -> int:
        return self._port_map.get(node_id, 0)

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    async def ensure_running(self, nodes: list[SubNode]) -> dict[str, int]:
        """Start (or restart) the core serving *nodes* needing it.

        Returns ``{node_id: local_port}`` for successfully served nodes.
        """
        binary = self.resolve_binary()
        # Sort before truncating: airport node ordering churns between
        # fetches; a stable order keeps the served set (and thus the restart
        # signature) stable so a running core is not restarted needlessly.
        targets = sorted(
            (n for n in nodes if n.needs_core), key=lambda n: n.node_id
        )[: self._max_nodes]
        if not targets:
            await self.stop()
            return {}
        if not binary:
            logger.info(
                "subscription core unavailable: set proxy.subscription.core_path "
                "to a sing-box binary to enable vmess/vless/trojan/ss egress"
            )
            await self.stop()
            return {}

        signature = (binary, tuple(sorted((n.node_id, n.identity()) for n in targets)))
        if self.is_running and signature == getattr(self, "_sig", None):
            # The core kept running, but callers hand us freshly parsed node
            # objects whose egress_url was reset — re-apply the port map.
            for node in targets:
                port = self._port_map.get(node.node_id)
                if port:
                    node.egress_url = f"http://127.0.0.1:{port}"
                    node.local_port = port
            return dict(self._port_map)

        await self.stop()
        config = self._build_config(targets)
        if not config:
            return {}

        core_dir = data_path("core")
        core_dir.mkdir(parents=True, exist_ok=True)
        self._config_path = core_dir / "sing-box.json"
        self._config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        try:
            self._process = await asyncio.create_subprocess_exec(
                binary,
                "run",
                "-c",
                str(self._config_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                limit=1 << 20,
            )
        except OSError as exc:
            logger.warning("sing-box spawn failed: error={}", exc)
            self._process = None
            return {}

        # Drain stderr continuously into the ring buffer: an unread PIPE
        # fills its 1 MB limit and then blocks sing-box on write, silently
        # freezing every local inbound while is_running still reports True.
        self._drain_task = asyncio.create_task(
            self._drain_stderr(self._process), name="sing-box-stderr-drain"
        )

        ready = await self._wait_ports_ready(set(self._port_map.values()))
        if not ready:
            stderr = "\n".join(self.recent_stderr(20))[:500]
            logger.warning(
                "sing-box ports did not open; stopping core: detail={}",
                stderr,
            )
            await self.stop()
            return {}

        self._sig = signature
        logger.info(
            "sing-box started: pid={} nodes={} ports={}-{}",
            self._process.pid,
            len(self._port_map),
            min(self._port_map.values()),
            max(self._port_map.values()),
        )
        return dict(self._port_map)

    async def stop(self) -> None:
        proc, self._process = self._process, None
        self._sig = None
        drain, self._drain_task = self._drain_task, None
        if drain is not None:
            drain.cancel()
            try:
                await drain
            except (asyncio.CancelledError, Exception):
                pass
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("sing-box stop error: {}", exc)
        logger.info("sing-box stopped")

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """Continuously drain sing-box stderr into the ring buffer."""
        if proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                self._stderr_buf.append(line.decode("utf-8", "replace").rstrip())
        except (asyncio.CancelledError, Exception):
            return

    # ------------------------------------------------------------------
    # Config generation
    # ------------------------------------------------------------------

    def _build_config(self, nodes: list[SubNode]) -> dict | None:
        inbounds: list[dict] = []
        outbounds: list[dict] = [{"type": "direct", "tag": "direct"}]
        rules: list[dict] = []
        port = self._start_port
        used: set[int] = set()
        self._port_map = {}

        for node in nodes:
            outbound = _node_to_outbound(node)
            if outbound is None:
                continue
            # Skip past both already-assigned and occupied ports; previously a
            # single occupied port silently dropped the node from the pool.
            while port <= 65535 and (port in used or not _port_free(port)):
                port += 1
            if port > 65535:
                logger.warning(
                    "no free local port for core node: node={}", node.node_id
                )
                continue
            used.add(port)
            in_tag = f"in-{node.node_id}"
            out_tag = f"out-{node.node_id}"
            inbound = {
                "type": "mixed",
                "tag": in_tag,
                "listen": self._listen_host(),
                "listen_port": port,
            }
            outbound["tag"] = out_tag
            inbounds.append(inbound)
            outbounds.append(outbound)
            rules.append({"inbound": [in_tag], "outbound": out_tag})
            self._port_map[node.node_id] = port
            node.local_port = port
            node.egress_url = f"http://127.0.0.1:{port}"
            port += 1

        if not inbounds:
            logger.warning("no core-protocol nodes could be mapped to local ports")
            return None
        # NOTE: do NOT declare a "direct" outbound and do NOT set route.final.
        # Some sing-box builds (e.g. Alpine community) auto-include a direct
        # outbound, so an explicit one is a duplicate-tag FATAL; official
        # 1.12+ releases, on the other hand, refuse "route.final: direct"
        # without a declaration.  Since every inbound already has an explicit
        # rule to its node outbound, omitting both is compatible with all
        # builds — the final never fires.
        return {
            "log": {"level": "warn"},
            "inbounds": inbounds,
            "outbounds": outbounds,
            "route": {"rules": rules},
        }

    async def _wait_ports_ready(self, ports: set[int], timeout_s: float = 12.0) -> bool:
        if not ports:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        pending = set(ports)
        while pending and loop.time() < deadline:
            for port in list(pending):
                if await _tcp_ready(port):
                    pending.discard(port)
            if pending:
                await asyncio.sleep(0.25)
        return not pending


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _port_free(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


async def _tcp_ready(port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _node_to_outbound(node: SubNode) -> dict | None:
    """Translate a SubNode into a sing-box outbound (without tag)."""
    tls_block = None
    needs_tls = (
        node.sni
        or node.public_key
        or node.protocol.value in ("trojan", "hysteria2", "tuic", "anytls")
    )
    if needs_tls:
        tls_block = {
            "enabled": True,
            "server_name": node.sni or node.server,
            "insecure": bool(node.allow_insecure),
        }
        if node.alpn:
            tls_block["alpn"] = node.alpn
        # utls expects a browser name (chrome/ios/safari/...).  Hysteria2
        # nodes carry a 64-hex certificate *pin* in their `fingerprint`
        # field instead — feeding that to utls is a FATAL config error
        # ("unknown uTLS fingerprint"), so gate on the value's shape.
        fp = (node.client_fingerprint or "").strip()
        if fp and fp.isalpha() and len(fp) <= 16:
            tls_block["utls"] = {"enabled": True, "fingerprint": fp}
        if node.public_key:
            tls_block["reality"] = {
                "enabled": True,
                "public_key": node.public_key,
                "short_id": node.short_id,
            }

    transport_block = None
    if node.transport == "ws":
        transport_block = {"type": "ws", "path": node.path or "/"}
        if node.host_header:
            transport_block["headers"] = {"Host": node.host_header}
    elif node.transport == "grpc":
        transport_block = {"type": "grpc", "service_name": node.path or ""}

    base: dict = {"server": node.server, "server_port": node.port}
    p = node.protocol
    if p.value == "ss":
        if not node.method or not node.credential:
            return None
        base.update({"type": "shadowsocks", "method": node.method, "password": node.credential})
        if node.plugin:
            if node.plugin.startswith("v2ray-plugin"):
                base["plugin"] = "v2ray-plugin"
                base["plugin_opts"] = node.plugin.split("=", 1)[1]
            else:  # obfs mode string like "obfs=tls;obfs-host=host"
                base["plugin"] = "obfs-local"
                base["plugin_opts"] = node.plugin
    elif p.value == "http":
        base.update({"type": "http"})
        if node.credential:
            user, _, pwd = node.credential.partition(":")
            base["username"] = user
            base["password"] = pwd
    elif p.value == "https":
        base.update({"type": "http", "tls": {"enabled": True}})
        if node.credential:
            user, _, pwd = node.credential.partition(":")
            base["username"] = user
            base["password"] = pwd
    elif p.value == "socks5" or p.value == "socks4":
        base.update({"type": "socks"})
        if node.credential:
            user, _, pwd = node.credential.partition(":")
            base["username"] = user
            base["password"] = pwd
    elif p.value == "vmess":
        if not node.credential:
            return None
        base.update(
            {
                "type": "vmess",
                "uuid": node.credential,
                "security": node.method or "auto",
                "alter_id": 0,
            }
        )
    elif p.value == "vless":
        if not node.credential:
            return None
        base.update({"type": "vless", "uuid": node.credential})
        if node.flow:
            base["flow"] = node.flow
    elif p.value == "trojan":
        if not node.credential:
            return None
        base.update({"type": "trojan", "password": node.credential})
    elif p.value == "anytls":
        if not node.credential:
            return None
        base.update({"type": "anytls", "password": node.credential})
    elif p.value == "hysteria2":
        base.update({"type": "hysteria2", "password": node.credential})
    elif p.value == "tuic":
        if not node.credential:
            return None
        tuic_uuid, _, tuic_pwd = node.credential.partition(":")
        if not tuic_uuid:
            return None
        base.update(
            {
                "type": "tuic",
                "uuid": tuic_uuid,
                "password": tuic_pwd,
                "congestion_control": "bbr",
                "udp_relay_mode": "native",
            }
        )
    else:
        return None

    if transport_block is not None:
        base["transport"] = transport_block
    if tls_block is not None:
        base["tls"] = tls_block
    return base


__all__ = ["CoreRunner"]
