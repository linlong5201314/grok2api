"""Subscription content parsers.

Supported payload formats:

1. **Base64 URI list** — the classic V2Ray/airport format: the whole body is
   base64 (url-safe variants tolerated) and decodes to one share link per
   line (``ss://``, ``vmess://``, ``vless://``, ``trojan://``,
   ``hysteria2://``, ``socks://``, ``http://`` ...).
2. **Plain URI list** — same links without base64 wrapping.
3. **Clash YAML** — a ``proxies:`` section with flat mapping nodes.  Parsed by
   a small indentation-based reader that covers the regular structure airport
   panels emit; exotic YAML features (anchors, block scalars) are ignored
   rather than mis-parsed.

All parsers are pure functions returning :class:`SubNode` lists; they never
raise — malformed entries are skipped and counted.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from app.platform.logging.logger import logger
from app.platform.runtime.ids import next_hex

from .models import SubNode, SubProtocol, parse_protocol


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def parse_subscription_payload(body: str | bytes, source_id: str = "") -> list[SubNode]:
    """Parse a subscription body in any supported format.

    Returns parsed nodes (possibly empty).  Never raises.
    """
    if isinstance(body, bytes):
        text = _decode_bytes(body)
    else:
        text = str(body)
    if not text.strip():
        return []

    nodes: list[SubNode] = []
    skipped: dict[str, int] = {}
    if _looks_like_clash_yaml(text):
        nodes = parse_clash_yaml(text, source_id=source_id)
    else:
        uris = _extract_uri_list(text)
        parsed: list[SubNode] = []
        for u in uris:
            n = _parse_share_uri(u, source_id=source_id)
            if n is None:
                scheme = u.split("://", 1)[0].lower()[:20]
                key = f"scheme:{scheme}" if _URI_RE.match(u) else "malformed"
                skipped[key] = skipped.get(key, 0) + 1
            else:
                parsed.append(n)
        nodes = parsed
        if skipped:
            logger.info(
                "subscription skipped entries: {} (unsupported schemes or "
                "malformed links are not usable for egress)",
                skipped,
            )

    # De-duplicate by identity keeping first occurrence.
    seen: set[str] = set()
    unique: list[SubNode] = []
    duplicates = 0
    for node in nodes:
        ident = node.identity()
        if ident in seen:
            duplicates += 1
            continue
        seen.add(ident)
        unique.append(node)
    logger.debug(
        "subscription parsed: source={} raw_nodes={} unique_nodes={} dupes={}",
        source_id or "-",
        len(nodes),
        len(unique),
        duplicates,
    )
    return unique


# ---------------------------------------------------------------------------
# Bytes / format detection helpers
# ---------------------------------------------------------------------------


def _decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "utf-16", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


_URI_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def _extract_uri_list(text: str) -> list[str]:
    """Return share URIs from plain or base64-wrapped payloads."""
    stripped = text.strip()
    candidate = stripped
    if not _URI_RE.match(candidate.splitlines()[0] if candidate else ""):
        decoded = _try_b64decode(candidate)
        if decoded:
            candidate = decoded
    uris: list[str] = []
    for line in candidate.splitlines():
        line = line.strip()
        if line and _URI_RE.match(line):
            uris.append(line)
    return uris


def _try_b64decode(text: str, *, require_uri: bool = True) -> str:
    """Decode base64 text; returns '' on any failure.

    ``require_uri=True`` guards whole-body detection (the decoded payload must
    look like a share-link list).  Inner-payload decoding (vmess JSON, ss
    userinfo) passes ``require_uri=False``.
    """
    compact = re.sub(r"\s+", "", text)
    if not compact or len(compact) % 4 != 0:
        # tolerate missing padding
        padded = compact + "=" * (-len(compact) % 4)
    else:
        padded = compact
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", padded):
        return ""
    for alt in ("base64", "base64url"):
        try:
            raw = base64.b64decode(padded, altchars=b"-_" if alt == "base64url" else None)
            out = raw.decode("utf-8")
            if not require_uri or _URI_RE.search(out):
                return out
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
    return ""


def _looks_like_clash_yaml(text: str) -> bool:
    head = text[:4000]
    return bool(re.search(r"(?m)^\s*proxies\s*:\s*$", head)) and "://" not in head.split(
        "proxies:", 1
    )[0][:200]


# ---------------------------------------------------------------------------
# Share-link parsers
# ---------------------------------------------------------------------------


def _make_node_id(source_id: str, identity: str) -> str:
    import hashlib

    h = hashlib.sha1(f"{source_id}|{identity}".encode()).hexdigest()
    return h[:16] or next_hex()[:16]


def _parse_share_uri(uri: str, source_id: str = "") -> SubNode | None:
    try:
        scheme = uri.split("://", 1)[0].lower()
        protocol = parse_protocol(scheme)
        if protocol == SubProtocol.UNKNOWN:
            return None
        parser = {
            SubProtocol.SS: _parse_ss,
            SubProtocol.VMESS: _parse_vmess,
            SubProtocol.VLESS: _parse_vless,
            SubProtocol.TROJAN: _parse_trojan,
            SubProtocol.HYSTERIA2: _parse_hysteria2,
            SubProtocol.TUIC: _parse_tuic,
            SubProtocol.SOCKS5: _parse_simple,
            SubProtocol.SOCKS4: _parse_simple,
            SubProtocol.HTTP: _parse_simple,
            SubProtocol.HTTPS: _parse_simple,
        }.get(protocol)
        if parser is None:
            return None
        node = parser(uri)
        if node is None or not node.server or not node.port:
            return None
        node.protocol = protocol
        node.source_id = source_id
        node.node_id = _make_node_id(source_id, node.identity())
        return node
    except Exception as exc:  # noqa: BLE001 — single bad link must not kill the batch
        logger.debug("share uri parse failed: scheme={} error={}", uri[:24], exc)
        return None


def _name_from_fragment(url: urlparse) -> str:
    return unquote(url.fragment or "").strip()


def _parse_ss(uri: str) -> SubNode | None:
    body = uri[len("ss://"):]
    name = ""
    if "#" in body:
        body, frag = body.split("#", 1)
        name = unquote(frag).strip()
    query = {}
    if "?" in body:
        body, qs = body.split("?", 1)
        query = {k: v[0] for k, v in parse_qs(qs).items()}

    userinfo, hostport = "", body
    if "@" in body:
        userinfo, hostport = body.rsplit("@", 1)

    method = password = ""
    if userinfo:
        decoded = _try_b64decode(
            userinfo + "=" * (-len(userinfo) % 4), require_uri=False
        )
        if decoded and ":" in decoded:
            method, password = decoded.split(":", 1)
        elif ":" in userinfo:
            method, password = userinfo.split(":", 1)
    else:
        # whole-part base64 form: ss://base64(method:pass@host:port)#name
        decoded = _try_b64decode(hostport, require_uri=False)
        if decoded and "@" in decoded:
            userinfo2, hostport = decoded.rsplit("@", 1)
            if ":" in userinfo2:
                method, password = userinfo2.split(":", 1)

    host, port = _split_hostport(hostport)
    if not host or not port:
        return None
    return SubNode(
        name=name,
        protocol=SubProtocol.SS,
        server=host,
        port=port,
        credential=password,
        method=method,
        transport="tcp",
        plugin=str(query.get("plugin", "")),
        raw_uri=uri,
    )


def _parse_vmess(uri: str) -> SubNode | None:
    payload = uri[len("vmess://"):]
    decoded = _try_b64decode(
        payload + "=" * (-len(payload) % 4), require_uri=False
    )
    if not decoded:
        return None
    data = json.loads(decoded)
    if not isinstance(data, dict):
        return None
    server = str(data.get("add", ""))
    try:
        port = int(data.get("port", 0))
    except (TypeError, ValueError):
        port = 0
    net = str(data.get("net", "tcp")).lower()
    tls = str(data.get("tls", "")).lower() == "tls"
    return SubNode(
        name=str(data.get("ps", "")),
        protocol=SubProtocol.VMESS,
        server=server,
        port=port,
        credential=str(data.get("id", "")),
        method=str(data.get("scy", "auto")),
        transport=net,
        path=str(data.get("path", "")),
        host_header=str(data.get("host", "")),
        sni=str(data.get("sni", "")) or (str(data.get("host", "")) if tls else ""),
        alpn=[a for a in str(data.get("alpn", "")).split(",") if a],
        allow_insecure=False,
        raw_uri=uri,
    )


def _parse_vless(uri: str) -> SubNode | None:
    url = urlparse(uri)
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    host, port = _split_hostport(url.netloc)
    security = q.get("security", "none")
    sni = q.get("sni", "") or host
    return SubNode(
        name=_name_from_fragment(url),
        protocol=SubProtocol.VLESS,
        server=host,
        port=port,
        credential=unquote(url.username or ""),
        method=q.get("encryption", "none"),
        transport=q.get("type", "tcp").lower(),
        path=unquote(q.get("path", "")),
        host_header=unquote(q.get("host", "")),
        sni=sni if security in ("tls", "reality") else "",
        alpn=[a for a in q.get("alpn", "").split(",") if a],
        allow_insecure=q.get("allowInsecure", "0") in ("1", "true"),
        raw_uri=uri,
    )


def _parse_trojan(uri: str) -> SubNode | None:
    url = urlparse(uri)
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    host, port = _split_hostport(url.netloc)
    return SubNode(
        name=_name_from_fragment(url),
        protocol=SubProtocol.TROJAN,
        server=host,
        port=port,
        credential=unquote(url.username or ""),
        transport=q.get("type", "tcp").lower(),
        path=unquote(q.get("path", "")),
        host_header=unquote(q.get("host", "")),
        sni=q.get("sni", "") or host,
        alpn=[a for a in q.get("alpn", "").split(",") if a],
        allow_insecure=q.get("allowInsecure", "0") in ("1", "true"),
        raw_uri=uri,
    )


def _parse_hysteria2(uri: str) -> SubNode | None:
    url = urlparse(uri)
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    host, port = _split_hostport(url.netloc)
    return SubNode(
        name=_name_from_fragment(url),
        protocol=SubProtocol.HYSTERIA2,
        server=host,
        port=port,
        credential=unquote(url.username or ""),
        transport="udp",
        sni=q.get("sni", "") or host,
        allow_insecure=q.get("insecure", "0") in ("1", "true"),
        raw_uri=uri,
    )


def _parse_tuic(uri: str) -> SubNode | None:
    """tuic://uuid:password@host:port?sni=…&alpn=h3&congestion_control=bbr#name"""
    url = urlparse(uri)
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    host, port = _split_hostport(url.netloc)
    uuid_part = unquote(url.username or "")
    password = unquote(url.password or "") if url.password else ""
    # credential carries both halves; core_runner splits on the last colon.
    credential = f"{uuid_part}:{password}" if password else uuid_part
    return SubNode(
        name=_name_from_fragment(url),
        protocol=SubProtocol.TUIC,
        server=host,
        port=port,
        credential=credential,
        transport="udp",
        sni=q.get("sni", "") or host,
        alpn=[a for a in unquote(q.get("alpn", "")).split(",") if a],
        allow_insecure=q.get("allow_insecure", q.get("insecure", "0")) in ("1", "true"),
        raw_uri=uri,
    )


def _parse_simple(uri: str) -> SubNode | None:
    url = urlparse(uri)
    host, port = _split_hostport(url.netloc)
    userinfo = unquote(url.username or "")
    password = unquote(url.password or "") if url.password else ""
    return SubNode(
        name=_name_from_fragment(url),
        server=host,
        port=port,
        credential=password or userinfo,
        transport="tcp",
        raw_uri=uri,
    )


def _split_hostport(netloc: str) -> tuple[str, int]:
    """Split ``[user:pass@]host:port`` handling IPv6 brackets."""
    netloc = netloc.strip()
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    if netloc.startswith("["):
        host, _, rest = netloc[1:].partition("]")
        rest = rest.lstrip(":")
        try:
            return host, int(rest) if rest else 0
        except ValueError:
            return host, 0
    host, _, port_s = netloc.rpartition(":")
    if not host:
        return "", 0
    try:
        return unquote(host), int(port_s)
    except ValueError:
        return unquote(host), 0


# ---------------------------------------------------------------------------
# Clash YAML subset parser
# ---------------------------------------------------------------------------

_KV_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$")


def parse_clash_yaml(text: str, source_id: str = "") -> list[SubNode]:
    """Parse the ``proxies:`` section of a Clash config (regular subset).

    Uses an indent stack so nested mappings (``ws-opts:``, ``headers:`` ...)
    land inside their parent instead of polluting the node root.  The
    half-indent trick (``indent + 0.5``) keeps sibling detection correct for
    any consistent indentation width.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^proxies\s*:\s*$", line):
            start = i + 1
            break
    if start is None:
        return []

    items: list[dict[str, Any]] = []
    # Stack of (effective_indent, container).  Root item pushed per "- " entry.
    stack: list[tuple[float, dict[str, Any]]] = []
    last_key: str | None = None
    # Deferred empty-value key: (indent, owner, key).  Resolved by the next
    # meaningful line: deeper mapping → child dict; sequence → list; same or
    # shallower level → YAML null.
    pending: tuple[int, dict[str, Any], str] | None = None

    for line in lines[start:]:
        if re.match(r"^\S", line):  # next top-level key → proxies section ended
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if stripped.startswith("- ") and stripped[2:].lstrip().startswith("{"):
            # Inline-mapping item form: - {name: x, server: y, port: 443}
            item = _parse_inline_dict(stripped[2:])
            items.append(item)
            stack = [(indent + 2, item)]
            pending = None
            last_key = None
            continue

        if stripped.startswith("- ") and _KV_RE.match(stripped[2:]):
            # New proxy item.
            if pending is not None:
                pending[1][pending[2]] = None  # unresolved key → null
                pending = None
            item = {}
            items.append(item)
            stack = [(indent + 2, item)]
            kv = _KV_RE.match(stripped[2:])
            assert kv is not None
            key, raw = kv.group(1), kv.group(2)
            last_key = key
            if raw == "":
                pending = (indent + 2, item, key)
            else:
                item[key] = _coerce(raw)
            continue

        if stripped.startswith("- "):
            # Sequence entry (e.g. alpn list items).
            value = _coerce(stripped[2:])
            if pending is not None:
                p_indent, owner, p_key = pending
                owner[p_key] = [value]
                pending = None
                last_key = p_key
            elif stack and last_key is not None:
                container = stack[-1][1]
                existing = container.get(last_key)
                if isinstance(existing, list):
                    existing.append(value)
                else:
                    container[last_key] = [value]
            continue

        kv = _KV_RE.match(stripped)
        if kv is None:
            continue
        key, raw = kv.group(1), kv.group(2)

        # Resolve the deferred key first: a deeper line becomes its child.
        if pending is not None and indent > pending[0]:
            p_indent, owner, p_key = pending
            child: dict[str, Any] = {}
            owner[p_key] = child
            stack.append((p_indent + 0.5, child))
        elif pending is not None:
            pending[1][pending[2]] = None  # sibling → null
        pending = None

        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        if not stack:
            continue
        container = stack[-1][1]
        last_key = key
        if raw == "":
            pending = (indent, container, key)
        else:
            container[key] = _coerce(raw)

    if pending is not None:
        pending[1][pending[2]] = None

    nodes: list[SubNode] = []
    for item in items:
        node = _clash_item_to_node(item, source_id=source_id)
        if node:
            nodes.append(node)
    return nodes


def _coerce(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return raw[1:-1]
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1]
    low = raw.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", ""):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    if raw.startswith("{") and raw.endswith("}"):
        return _parse_inline_dict(raw)
    return raw


def _parse_inline_dict(raw: str) -> dict[str, Any]:
    inner = raw.strip()[1:-1]
    out: dict[str, Any] = {}
    depth = 0
    buf = ""
    parts: list[str] = []
    for ch in inner:
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    for part in parts:
        if ":" not in part:
            continue
        k, _, v = part.partition(":")
        out[k.strip()] = _coerce(v)
    return out


def _clash_item_to_node(item: dict[str, Any], source_id: str) -> SubNode | None:
    ctype = str(item.get("type", "")).lower()
    protocol = parse_protocol(ctype)
    if protocol == SubProtocol.UNKNOWN:
        return None
    server = str(item.get("server", ""))
    try:
        port = int(item.get("port", 0))
    except (TypeError, ValueError):
        port = 0
    if not server or not port:
        return None

    network = str(item.get("network", "tcp") or "tcp").lower()
    ws_opts = item.get("ws-opts") if isinstance(item.get("ws-opts"), dict) else {}
    grpc_opts = item.get("grpc-opts") if isinstance(item.get("grpc-opts"), dict) else {}
    path = ""
    host_header = ""
    if ws_opts:
        path = str(ws_opts.get("path", ""))
        headers = ws_opts.get("headers")
        if isinstance(headers, dict):
            host_header = str(headers.get("Host", ""))
    tls = bool(item.get("tls", False)) or protocol in (
        SubProtocol.TROJAN,
        SubProtocol.HYSTERIA2,
    )
    sni = str(item.get("servername", "") or item.get("sni", "") or "")

    credential = ""
    method = ""
    if protocol == SubProtocol.SS:
        credential = str(item.get("password", ""))
        method = str(item.get("cipher", ""))
    elif protocol == SubProtocol.VMESS:
        credential = str(item.get("uuid", ""))
        method = str(item.get("cipher", "auto"))
    elif protocol in (SubProtocol.VLESS, SubProtocol.TROJAN, SubProtocol.HYSTERIA2):
        credential = str(item.get("password", "") or item.get("uuid", ""))
    elif protocol in (SubProtocol.SOCKS4, SubProtocol.SOCKS5):
        credential = str(item.get("username", "")) + (
            ":" + str(item.get("password", "")) if item.get("password") else ""
        )

    alpn_raw = item.get("alpn")
    alpn = [str(a) for a in alpn_raw] if isinstance(alpn_raw, list) else []

    node = SubNode(
        name=str(item.get("name", "")),
        protocol=protocol,
        server=server,
        port=port,
        credential=credential,
        method=method,
        transport=("grpc" if grpc_opts else network),
        path=path or str(grpc_opts.get("grpc-service-name", "") if grpc_opts else ""),
        host_header=host_header,
        sni=sni if tls else "",
        alpn=alpn,
        allow_insecure=bool(item.get("skip-cert-verify", False)),
        udp=bool(item.get("udp", True)),
        source_id=source_id,
    )
    node.node_id = _make_node_id(source_id, node.identity())
    return node


__all__ = [
    "parse_subscription_payload",
    "parse_clash_yaml",
]
