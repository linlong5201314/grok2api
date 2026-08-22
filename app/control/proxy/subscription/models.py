"""Subscription domain models — nodes, sources, health stats.

A *subscription source* is an airport (机场) subscription URL.  Fetching it
yields a node list in one of the supported formats (base64 URI list, plain
URI list, or Clash YAML).  Each parsed node becomes a :class:`SubNode`.

Only a subset of protocols can be used directly as an HTTP client egress
(``http`` / ``https`` / ``socks4`` / ``socks5``).  The rest require a local
core process (sing-box / xray) which exposes a local mixed inbound per node;
:class:`SubNode.egress_url` carries the final usable proxy URL either way.
"""

from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field


class SubProtocol(StrEnum):
    """Supported subscription node protocols."""

    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"
    SS = "ss"                # shadowsocks
    VMESS = "vmess"
    VLESS = "vless"
    TROJAN = "trojan"
    HYSTERIA2 = "hysteria2"
    TUIC = "tuic"
    UNKNOWN = "unknown"


# Protocols usable directly as curl_cffi / aiohttp egress proxy URLs.
DIRECT_PROTOCOLS = frozenset(
    {
        SubProtocol.HTTP,
        SubProtocol.HTTPS,
        SubProtocol.SOCKS4,
        SubProtocol.SOCKS5,
    }
)

# Protocols that require a local core process to be usable.
CORE_PROTOCOLS = frozenset(
    {
        SubProtocol.SS,
        SubProtocol.VMESS,
        SubProtocol.VLESS,
        SubProtocol.TROJAN,
        SubProtocol.HYSTERIA2,
        SubProtocol.TUIC,
    }
)


def parse_protocol(value: str) -> SubProtocol:
    """Map a URI scheme / clash type string to a :class:`SubProtocol`."""
    v = (value or "").strip().lower()
    aliases = {
        "socks": SubProtocol.SOCKS5,
        "socks5": SubProtocol.SOCKS5,
        "socks5h": SubProtocol.SOCKS5,
        "socks4": SubProtocol.SOCKS4,
        "socks4a": SubProtocol.SOCKS4,
        "ss": SubProtocol.SS,
        "shadowsocks": SubProtocol.SS,
        "vmess": SubProtocol.VMESS,
        "vless": SubProtocol.VLESS,
        "trojan": SubProtocol.TROJAN,
        "trojan-go": SubProtocol.TROJAN,
        "hysteria2": SubProtocol.HYSTERIA2,
        "hy2": SubProtocol.HYSTERIA2,
        "tuic": SubProtocol.TUIC,
        "http": SubProtocol.HTTP,
        "https": SubProtocol.HTTPS,
    }
    return aliases.get(v, SubProtocol.UNKNOWN)


class NodeState(StrEnum):
    NEW = "new"            # parsed, not yet probed
    HEALTHY = "healthy"    # last probe succeeded
    DEGRADED = "degraded"  # high latency or intermittent failures
    DEAD = "dead"          # probe failures exceeded threshold
    NEEDS_CORE = "needs_core"  # protocol requires a core that is unavailable


class SubNode(BaseModel):
    """One parsed proxy node from a subscription."""

    # Assigned by parsers after construction (identity-derived); empty during
    # intermediate construction inside share-link parsing.
    node_id: str = ""
    name: str = ""
    protocol: SubProtocol = SubProtocol.UNKNOWN
    server: str = ""
    port: int = 0
    # Raw protocol credentials / options (kept for core config generation).
    credential: str = ""               # password / uuid
    method: str = ""                   # ss encryption method
    transport: str = ""                # tcp | ws | grpc | h2
    path: str = ""                     # ws path / http upgrade path
    host_header: str = ""              # ws Host header
    sni: str = ""
    alpn: list[str] = Field(default_factory=list)
    allow_insecure: bool = False
    udp: bool = True
    plugin: str = ""                   # ss plugin string (obfs/v2ray-plugin)
    raw_uri: str = ""                  # original share link (redact before display)
    source_id: str = ""

    # Runtime state (not part of identity).
    state: NodeState = NodeState.NEW
    egress_url: str = ""               # final proxy URL used by the app
    local_port: int = 0                # local core inbound port (core mode)
    latency_ms: float | None = None    # latest probe result
    loss_rate: float = 0.0             # EWMA of probe failures [0..1]
    score: float = 0.0                 # higher = better; 0 = unprobed
    last_probe_at: int | None = None   # ms epoch
    fail_count: int = 0
    ok_count: int = 0
    last_error: str = ""               # last probe failure reason (display)

    @property
    def is_direct(self) -> bool:
        return self.protocol in DIRECT_PROTOCOLS

    @property
    def needs_core(self) -> bool:
        return self.protocol in CORE_PROTOCOLS

    @property
    def is_usable(self) -> bool:
        return bool(self.egress_url) and self.state != NodeState.DEAD

    def identity(self) -> str:
        """Stable identity independent of node name ordering."""
        return f"{self.protocol.value}|{self.server}|{self.port}|{self.credential}"

    def display_name(self) -> str:
        return self.name or f"{self.server}:{self.port}"

    def redacted(self) -> dict[str, Any]:
        """Safe-for-UI projection — never exposes credentials or raw URIs."""
        d = self.model_dump(
            exclude={
                "credential", "raw_uri", "method", "path", "host_header",
                "sni", "alpn",
                # direct-protocol egress URLs embed credentials
                # (socks5h://user:pass@host:port) — never expose them.
                "egress_url",
            }
        )
        d["protocol"] = self.protocol.value
        d["state"] = self.state.value
        d["server_masked"] = _mask_host(self.server)
        del d["server"]
        return d


def _mask_host(server: str) -> str:
    """Mask the middle of hosts/IPs so the panel does not leak full endpoints."""
    if not server:
        return ""
    if len(server) <= 8:
        return server[:2] + "***"
    return server[:4] + "***" + server[-3:]


class SubscriptionSource(BaseModel):
    """One configured airport subscription URL."""

    source_id: str                     # short random hex
    name: str = ""
    url: str = ""
    enabled: bool = True
    refresh_interval_sec: int = 0      # 0 → global default
    created_at: int = 0                # ms epoch, 0 = config-seeded (sorts last)
    last_fetch_at: int | None = None   # ms
    last_fetch_ok: bool = False
    last_error: str = ""
    node_count: int = 0

    def created_sort_key(self) -> tuple[int, str]:
        return (self.created_at, self.source_id)

    def masked_url(self) -> str:
        """URL with userinfo and query redacted, safe for logs/UI."""
        try:
            p = urlparse(self.url)
            if not p.scheme:
                return "***"
            netloc = p.hostname or ""
            if p.port:
                netloc += f":{p.port}"
            return f"{p.scheme}://{netloc}{p.path}?***"
        except Exception:
            return "***"


class SubscriptionFetchResult(BaseModel):
    source_id: str
    ok: bool
    node_count: int = 0
    error: str = ""
    duration_ms: int = 0


__all__ = [
    "SubProtocol",
    "NodeState",
    "SubNode",
    "SubscriptionSource",
    "SubscriptionFetchResult",
    "DIRECT_PROTOCOLS",
    "CORE_PROTOCOLS",
    "parse_protocol",
]
