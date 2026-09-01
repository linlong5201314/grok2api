"""Per-account stable fingerprint derivation.

Correlation risk being addressed
--------------------------------
A single global User-Agent / Accept-Language across every account lets the
upstream cluster requests by identical fingerprints even when egress IPs
differ.  This module derives a *stable per-account* profile from a fingerprint
seed (the account token), so:

* one account always presents the same "device" (consistent identity);
* different accounts present different devices (no cross-account correlation).

Consistency constraint
----------------------
The chosen UA must map to a browser profile that curl_cffi can actually
impersonate — a Chrome/150 UA over a chrome120 TLS handshake is itself a
bot signal.  The UA pool is therefore built from the impersonation targets
the installed curl_cffi supports, discovered at runtime.
"""

from __future__ import annotations

import hashlib
import re

from app.platform.logging.logger import logger

# ---------------------------------------------------------------------------
# UA pool construction
# ---------------------------------------------------------------------------

_UA_TEMPLATES = {
    "windows": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36"
    ),
    "macos": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36"
    ),
    "linux": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36"
    ),
}

# Accept-Language pools look like real browser configs (one primary + base).
_LANG_POOLS = [
    "en-US,en;q=0.9",
    "en-US,en;q=0.9,zh-CN;q=0.8",
    "zh-CN,zh;q=0.9,en-US;q=0.8",
    "ja-JP,ja;q=0.9,en-US;q=0.8",
    "ko-KR,ko;q=0.9,en-US;q=0.8",
    "de-DE,de;q=0.9,en-US;q=0.8",
    "fr-FR,fr;q=0.9,en-US;q=0.8",
    "es-ES,es;q=0.9,en-US;q=0.8",
    "pt-BR,pt;q=0.9,en-US;q=0.8",
    "ru-RU,ru;q=0.9,en-US;q=0.8",
]


def _supported_chrome_versions() -> list[int]:
    """Chrome major versions curl_cffi can impersonate, sorted ascending."""
    try:
        from typing import get_args

        from curl_cffi.requests.impersonate import BrowserTypeLiteral

        versions: set[int] = set()
        for item in get_args(BrowserTypeLiteral):
            m = re.fullmatch(r"chrome(\d+)", str(item))
            if m:
                versions.add(int(m.group(1)))
        return sorted(versions)
    except Exception:  # noqa: BLE001 — fall back to a conservative pool
        return [120, 124, 131, 133, 136]


# Ancient Chrome versions (<= 3 years old) are a bot signal on their own —
# Cloudflare's current detection treats a Chrome/99 UA or TLS hello as an
# immediate red flag.  Keep the pool to roughly the last few majors.
_MIN_CHROME_VERSION = 124


def _ua_pool() -> list[str]:
    versions = [v for v in _supported_chrome_versions() if v >= _MIN_CHROME_VERSION]
    if not versions:
        versions = [136]
    pool: list[str] = []
    for ver in versions:
        for template in _UA_TEMPLATES.values():
            pool.append(template.format(ver=ver))
    return pool


_POOL: list[str] | None = None


def _pool() -> list[str]:
    global _POOL
    if _POOL is None:
        _POOL = _ua_pool()
        logger.debug("fingerprint ua pool built: size={}", len(_POOL))
    return _POOL


# ---------------------------------------------------------------------------
# Deterministic derivation
# ---------------------------------------------------------------------------


def _digest(seed: str, domain: bytes) -> bytes:
    return hashlib.sha256(domain + seed.encode("utf-8", errors="ignore")).digest()


def stable_user_agent(seed: str) -> str:
    """Deterministic UA for *seed*, guaranteed impersonatable."""
    pool = _pool()
    d = int.from_bytes(_digest(seed, b"ua:")[:8], "big")
    return pool[d % len(pool)]


def stable_accept_language(seed: str) -> str:
    d = int.from_bytes(_digest(seed, b"lang:")[:8], "big")
    return _LANG_POOLS[d % len(_LANG_POOLS)]


def stable_timezone_hint(seed: str) -> str:
    """IANA zone consistent with the Accept-Language pick (coarse pairing)."""
    lang = stable_accept_language(seed)
    primary = lang.split(",")[0]
    pairing = {
        "en-US": "America/New_York",
        "zh-CN": "Asia/Shanghai",
        "ja-JP": "Asia/Tokyo",
        "ko-KR": "Asia/Seoul",
        "de-DE": "Europe/Berlin",
        "fr-FR": "Europe/Paris",
        "es-ES": "Europe/Madrid",
        "pt-BR": "America/Sao_Paulo",
        "ru-RU": "Europe/Moscow",
    }
    return pairing.get(primary, "UTC")


def fingerprint_profile(seed: str) -> dict[str, str]:
    """Full stable profile for *seed*: ua + accept-language (+ tz hint)."""
    if not seed:
        return {}
    return {
        "user_agent": stable_user_agent(seed),
        "accept_language": stable_accept_language(seed),
        "timezone": stable_timezone_hint(seed),
    }


__all__ = [
    "fingerprint_profile",
    "stable_user_agent",
    "stable_accept_language",
    "stable_timezone_hint",
]
