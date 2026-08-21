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

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def resolve_binary(self) -> str:
        """Return the sing-box executable path, or '' when unavailable."""
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
        targets = [n for n in nodes if n.needs_core][: self._max_nodes]
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

        signature = (binary, tuple(sorted(n.node_id + n.identity() for n in targets)))
        if self.is_running and signature == getattr(self, "_sig", None):
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

        ready = await self._wait_ports_ready(set(self._port_map.values()))
        if not ready:
            stderr = b""
            if self._process.stderr is not None:
                try:
                    stderr = await asyncio.wait_for(
                        self._process.stderr.read(65536), timeout=2.0
                    )
                except Exception:
                    pass
            logger.warning(
                "sing-box ports did not open; stopping core: detail={}",
                stderr.decode(errors="replace")[:500],
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
            while port in used:
                port += 1
            if not _port_free(port):
                port += 1
                continue
            used.add(port)
            in_tag = f"in-{node.node_id}"
            out_tag = f"out-{node.node_id}"
            inbound = {
                "type": "mixed",
                "tag": in_tag,
                "listen": "127.0.0.1",
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
        return {
            "log": {"level": "warn"},
            "inbounds": inbounds,
            "outbounds": outbounds,
            "route": {"rules": rules, "final": "direct"},
        }

    async def _wait_ports_ready(self, ports: set[int], timeout_s: float = 12.0) -> bool:
        if not ports:
            return False
        deadline = asyncio.get_event_loop().time() + timeout_s
        pending = set(ports)
        while pending and asyncio.get_event_loop().time() < deadline:
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
    if node.sni or node.protocol.value in ("trojan", "hysteria2", "tuic"):
        tls_block = {
            "enabled": True,
            "server_name": node.sni or node.server,
            "insecure": bool(node.allow_insecure),
        }
        if node.alpn:
            tls_block["alpn"] = node.alpn

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
    elif p.value == "trojan":
        if not node.credential:
            return None
        base.update({"type": "trojan", "password": node.credential})
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
