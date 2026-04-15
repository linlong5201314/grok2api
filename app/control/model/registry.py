"""Model registry — all supported model variants defined in one place."""

from .enums import Capability, ModeId, Tier
from .spec import ModelSpec

# ---------------------------------------------------------------------------
# Master model list.
# Add new models here; no other files need to change.
# ---------------------------------------------------------------------------

MODELS: tuple[ModelSpec, ...] = (
    # === Chat ==============================================================

    # Basic+
    ModelSpec("grok-4.20-0309-non-reasoning",           ModeId.FAST,   Tier.BASIC, Capability.CHAT,       True, "Grok 4.20 0309 Non-Reasoning"),
    ModelSpec("grok-4.20-0309",                         ModeId.AUTO,   Tier.BASIC, Capability.CHAT,       True, "Grok 4.20 0309"),
    ModelSpec("grok-4.20-0309-reasoning",               ModeId.EXPERT, Tier.BASIC, Capability.CHAT,       True, "Grok 4.20 0309 Reasoning"),
    # Super+
    ModelSpec("grok-4.20-0309-non-reasoning-super",     ModeId.FAST,   Tier.SUPER, Capability.CHAT,       True, "Grok 4.20 0309 Non-Reasoning Super"),
    ModelSpec("grok-4.20-0309-super",                   ModeId.AUTO,   Tier.SUPER, Capability.CHAT,       True, "Grok 4.20 0309 Super"),
    ModelSpec("grok-4.20-0309-reasoning-super",         ModeId.EXPERT, Tier.SUPER, Capability.CHAT,       True, "Grok 4.20 0309 Reasoning Super"),
    # Heavy+
    ModelSpec("grok-4.20-0309-non-reasoning-heavy",     ModeId.FAST,   Tier.BASIC, Capability.CHAT,       True, "Grok 4.20 0309 Non-Reasoning Heavy", prefer_best=True),
    ModelSpec("grok-4.20-0309-heavy",                   ModeId.AUTO,   Tier.BASIC, Capability.CHAT,       True, "Grok 4.20 0309 Heavy",               prefer_best=True),
    ModelSpec("grok-4.20-0309-reasoning-heavy",         ModeId.EXPERT, Tier.BASIC, Capability.CHAT,       True, "Grok 4.20 0309 Reasoning Heavy",     prefer_best=True),
    ModelSpec("grok-4.20-multi-agent-0309",             ModeId.HEAVY,  Tier.BASIC, Capability.CHAT,       True, "Grok 4.20 Multi-Agent 0309",         prefer_best=True),

    # --- 硬优先级反向选池 (heavy → super → basic) ---
    ModelSpec("grok-4.20-fast",                        ModeId.FAST,   Tier.BASIC, Capability.CHAT,       True, "Grok 4.20 Fast",          prefer_best=True),
    ModelSpec("grok-4.20-auto",                        ModeId.AUTO,   Tier.BASIC, Capability.CHAT,       True, "Grok 4.20 Auto",          prefer_best=True),
    ModelSpec("grok-4.20-expert",                      ModeId.EXPERT, Tier.BASIC, Capability.CHAT,       True, "Grok 4.20 Expert",        prefer_best=True),
    ModelSpec("grok-4.20-heavy",                       ModeId.HEAVY,  Tier.BASIC, Capability.CHAT,       True, "Grok 4.20 Heavy",         prefer_best=True),
    # Legacy chat aliases from older model pickers.
    ModelSpec("grok-3",                                ModeId.AUTO,   Tier.BASIC, Capability.CHAT,       True, "Grok 3",                  prefer_best=True),
    ModelSpec("grok-3-mini",                           ModeId.FAST,   Tier.BASIC, Capability.CHAT,       True, "Grok 3 Mini",             prefer_best=True),
    ModelSpec("grok-3-thinking",                       ModeId.EXPERT, Tier.BASIC, Capability.CHAT,       True, "Grok 3 Thinking",         prefer_best=True),
    ModelSpec("grok-4",                                ModeId.AUTO,   Tier.BASIC, Capability.CHAT,       True, "Grok 4",                  prefer_best=True),
    ModelSpec("grok-4-thinking",                       ModeId.EXPERT, Tier.BASIC, Capability.CHAT,       True, "Grok 4 Thinking",         prefer_best=True),
    ModelSpec("grok-4.1-expert",                       ModeId.EXPERT, Tier.BASIC, Capability.CHAT,       True, "Grok 4.1 Expert",         prefer_best=True),
    ModelSpec("grok-4.1-fast",                         ModeId.FAST,   Tier.BASIC, Capability.CHAT,       True, "Grok 4.1 Fast",           prefer_best=True),
    ModelSpec("grok-4.1-mini",                         ModeId.FAST,   Tier.BASIC, Capability.CHAT,       True, "Grok 4.1 Mini",           prefer_best=True),
    ModelSpec("grok-4.1-thinking",                     ModeId.EXPERT, Tier.BASIC, Capability.CHAT,       True, "Grok 4.1 Thinking",       prefer_best=True),
    ModelSpec("grok-4.20-beta",                        ModeId.AUTO,   Tier.BASIC, Capability.CHAT,       True, "Grok 4.20 Beta",          prefer_best=True),
    ModelSpec("grok-4-heavy",                          ModeId.HEAVY,  Tier.BASIC, Capability.CHAT,       True, "Grok 4 Heavy",            prefer_best=True),

    # === Image ==============================================================

    # Basic+
    ModelSpec("grok-imagine-image-lite",                ModeId.FAST,   Tier.BASIC, Capability.IMAGE,      True, "Grok Imagine Image Lite"),
    # Super+
    ModelSpec("grok-imagine-image",                     ModeId.AUTO,   Tier.SUPER, Capability.IMAGE,      True, "Grok Imagine Image"),
    ModelSpec("grok-imagine-image-pro",                 ModeId.AUTO,   Tier.SUPER, Capability.IMAGE,      True, "Grok Imagine Image Pro"),
    # Legacy image aliases.
    ModelSpec("grok-imagine-1.0-fast",                  ModeId.FAST,   Tier.BASIC, Capability.IMAGE,      True, "Grok Imagine 1.0 Fast"),
    ModelSpec("grok-imagine-1.0",                       ModeId.AUTO,   Tier.BASIC, Capability.IMAGE,      True, "Grok Imagine 1.0",        prefer_best=True),
    
    # === Image Edit =========================================================

    # Super+
    ModelSpec("grok-imagine-image-edit",                ModeId.AUTO,   Tier.SUPER, Capability.IMAGE_EDIT, True, "Grok Imagine Image Edit"),
    ModelSpec("grok-imagine-1.0-edit",                  ModeId.AUTO,   Tier.BASIC, Capability.IMAGE_EDIT, True, "Grok Imagine 1.0 Edit",   prefer_best=True),
    
    # === Video ==============================================================

    # Super+
    ModelSpec("grok-imagine-video",                     ModeId.AUTO,   Tier.SUPER, Capability.VIDEO,      True, "Grok Imagine Video"),
    ModelSpec("grok-imagine-1.0-video",                 ModeId.AUTO,   Tier.BASIC, Capability.VIDEO,      True, "Grok Imagine 1.0 Video",  prefer_best=True),
)

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
