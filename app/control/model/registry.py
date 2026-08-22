"""Model registry — all supported model variants defined in one place."""

from .enums import Capability, ModeId, Tier
from .spec import ModelSpec

# ---------------------------------------------------------------------------
# Master model list.
# Add new models here; no other files need to change.
# ---------------------------------------------------------------------------

# fmt: off
MODELS: tuple[ModelSpec, ...] = (
    # === Chat ==============================================================

    # Basic fast; auto/expert require Super+
    ModelSpec("grok-4.20-0309-non-reasoning",           ModeId.FAST,     Tier.BASIC, Capability.CHAT,       True, "Grok 4.20 0309 Non-Reasoning"),
    ModelSpec("grok-4.20-0309",                         ModeId.AUTO,     Tier.SUPER, Capability.CHAT,       True, "Grok 4.20 0309"),
    ModelSpec("grok-4.20-0309-reasoning",               ModeId.EXPERT,   Tier.SUPER, Capability.CHAT,       True, "Grok 4.20 0309 Reasoning"),
    # Super+
    ModelSpec("grok-4.20-0309-non-reasoning-super",     ModeId.FAST,     Tier.SUPER, Capability.CHAT,       True, "Grok 4.20 0309 Non-Reasoning Super"),
    ModelSpec("grok-4.20-0309-super",                   ModeId.AUTO,     Tier.SUPER, Capability.CHAT,       True, "Grok 4.20 0309 Super"),
    ModelSpec("grok-4.20-0309-reasoning-super",         ModeId.EXPERT,   Tier.SUPER, Capability.CHAT,       True, "Grok 4.20 0309 Reasoning Super"),
    # Heavy+
    ModelSpec("grok-4.20-0309-non-reasoning-heavy",     ModeId.FAST,     Tier.HEAVY, Capability.CHAT,       True, "Grok 4.20 0309 Non-Reasoning Heavy"),
    ModelSpec("grok-4.20-0309-heavy",                   ModeId.AUTO,     Tier.HEAVY, Capability.CHAT,       True, "Grok 4.20 0309 Heavy"),
    ModelSpec("grok-4.20-0309-reasoning-heavy",         ModeId.EXPERT,   Tier.HEAVY, Capability.CHAT,       True, "Grok 4.20 0309 Reasoning Heavy"),
    ModelSpec("grok-4.20-multi-agent-0309",             ModeId.HEAVY,    Tier.HEAVY, Capability.CHAT,       True, "Grok 4.20 Multi-Agent 0309"),

    # --- 硬优先级反向选池 (heavy → super → basic) ---
    ModelSpec("grok-4.20-fast",                         ModeId.FAST,     Tier.BASIC, Capability.CHAT,       True, "Grok 4.20 Fast",          prefer_best=True),
    ModelSpec("grok-4.20-auto",                         ModeId.AUTO,     Tier.SUPER, Capability.CHAT,       True, "Grok 4.20 Auto",          prefer_best=True),
    ModelSpec("grok-4.20-expert",                       ModeId.EXPERT,   Tier.SUPER, Capability.CHAT,       True, "Grok 4.20 Expert",        prefer_best=True),
    ModelSpec("grok-4.20-heavy",                        ModeId.HEAVY,    Tier.HEAVY, Capability.CHAT,       True, "Grok 4.20 Heavy",         prefer_best=True),

    # === grok-4.3 (grok-420-computer-use-sa) ==================================
    # Super+（basic 池不支持此模式）
    ModelSpec("grok-4.3-beta",                          ModeId.GROK_4_3, Tier.SUPER, Capability.CHAT,       True, "Grok 4.3 Beta"),

    # === grok-4.5 别名（2026-08 实测：上游 fast/expert/heavy 已是 Grok 4.5）====
    # 模式名未变，仅上游模型代际更新；提供新命名别名，旧名继续可用。
    ModelSpec("grok-4.5-fast",                          ModeId.FAST,     Tier.BASIC, Capability.CHAT,       True, "Grok 4.5 Fast",           prefer_best=True),
    ModelSpec("grok-4.5-auto",                          ModeId.AUTO,     Tier.SUPER, Capability.CHAT,       True, "Grok 4.5 Auto",           prefer_best=True),
    ModelSpec("grok-4.5-expert",                        ModeId.EXPERT,   Tier.SUPER, Capability.CHAT,       True, "Grok 4.5 Expert",         prefer_best=True),
    ModelSpec("grok-4.5-heavy",                         ModeId.HEAVY,    Tier.HEAVY, Capability.CHAT,       True, "Grok 4.5 Heavy",          prefer_best=True),

    # === grok-build（modeId="build"，2026-08 实测可用）=====================
    # 上游 web app-chat 直接接受 modeId="build"（Grok Build agent 模式），
    # SSE 流结构与 fast 完全一致；rate-limits 独立配额（basic 实测 10/10/2h）。
    # Tier.BASIC + prefer_best：优先重/超池，basic 池账号的 build 配额也可用。
    ModelSpec("grok-build",                             ModeId.BUILD,    Tier.BASIC, Capability.CHAT,       True, "Grok Build",              prefer_best=True),

    # === Image ==============================================================

    # Basic fast
    ModelSpec("grok-imagine-image-lite",                ModeId.FAST,     Tier.BASIC, Capability.IMAGE,      True, "Grok Imagine Image Lite"),
    # Super+
    ModelSpec("grok-imagine-image",                     ModeId.AUTO,     Tier.SUPER, Capability.IMAGE,      True, "Grok Imagine Image"),
    ModelSpec("grok-imagine-image-pro",                 ModeId.AUTO,     Tier.SUPER, Capability.IMAGE,      True, "Grok Imagine Image Pro"),

    # === Image Edit =========================================================

    # Super+
    ModelSpec("grok-imagine-image-edit",                ModeId.AUTO,     Tier.SUPER, Capability.IMAGE_EDIT, True, "Grok Imagine Image Edit"),

    # === Video ==============================================================

    # Super+
    ModelSpec("grok-imagine-video",                     ModeId.AUTO,     Tier.SUPER, Capability.VIDEO,      True, "Grok Imagine Video"),
)
# fmt: on

# ---------------------------------------------------------------------------
# Internal lookup structures — built once at import time.
# ---------------------------------------------------------------------------

_BY_NAME: dict[str, ModelSpec] = {m.model_name: m for m in MODELS}

_BY_CAP: dict[int, list[ModelSpec]] = {}
for _m in MODELS:
    _BY_CAP.setdefault(int(_m.capability), []).append(_m)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get(model_name: str) -> ModelSpec | None:
    """Return the spec for *model_name*, or ``None`` if not registered."""
    return _BY_NAME.get(model_name)


def resolve(model_name: str) -> ModelSpec:
    """Return the spec for *model_name*; raise ``ValueError`` if unknown."""
    spec = _BY_NAME.get(model_name)
    if spec is None:
        raise ValueError(f"Unknown model: {model_name!r}")
    return spec


def list_enabled() -> list[ModelSpec]:
    """Return all enabled models in registration order."""
    return [m for m in MODELS if m.enabled]


def list_by_capability(cap: Capability) -> list[ModelSpec]:
    """Return enabled models that include *cap* in their capability mask."""
    return [m for m in MODELS if m.enabled and bool(m.capability & cap)]


__all__ = ["MODELS", "get", "resolve", "list_enabled", "list_by_capability"]
