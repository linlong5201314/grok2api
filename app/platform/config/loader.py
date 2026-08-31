"""TOML configuration loader with environment-variable override support."""

import os
from pathlib import Path
from typing import Any

import tomllib


def _flatten(mapping: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested dict into dotted keys."""
    out: dict[str, Any] = {}
    for k, v in mapping.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, full))
        else:
            out[full] = v
    return out


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base* (non-destructive)."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file and return the raw nested dict."""
    if not path.exists():
        return {}
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def apply_env_overrides(data: dict[str, Any], env_prefix: str = "GROK_") -> dict[str, Any]:
    """Apply ``GROK_*`` environment overrides onto a nested config dict.

    Two supported forms:

      ``GROK_APP_API_KEY``          → ``app.api_key``   (legacy: first ``_`` splits section/key)
      ``GROK_PROXY__EGRESS__MODE``  → ``proxy.egress.mode``  (``__`` separates path segments)

    The legacy form cannot express three-level keys (``GROK_PROXY_EGRESS_MODE``
    would map to ``proxy.egress_mode``, which is not a real config key), so
    deeper keys must use the double-underscore form.
    """
    prefix_len = len(env_prefix)
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(env_prefix):
            continue
        name = env_key[prefix_len:].lower()
        if not name:
            continue
        if "__" in name:
            segments = [seg for seg in name.split("__") if seg]
        else:
            head, _, tail = name.partition("_")
            segments = [head, tail] if tail else [head]
        if len(segments) < 2:
            continue
        _set_nested(data, segments, env_val)
    return data


def _set_nested(data: dict[str, Any], segments: list[str], value: Any) -> None:
    node = data
    for seg in segments[:-1]:
        child = node.get(seg)
        if not isinstance(child, dict):
            child = {}
            node[seg] = child
        node = child
    node[segments[-1]] = value


def load_config(
    defaults_path: Path,
    user_path: Path | None = None,
    env_prefix: str = "GROK_",
) -> dict[str, Any]:
    """Load configuration: defaults → user file → environment overrides.

    Environment variables use the format ``GROK_SECTION_KEY=value``
    (→ ``section.key``) or ``GROK_SECTION__SUB__KEY=value`` for deeper keys.
    """
    data = load_toml(defaults_path)
    if user_path and user_path.exists():
        user = load_toml(user_path)
        data = _deep_merge(data, user)

    return apply_env_overrides(data, env_prefix)


def get_nested(data: dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    """Retrieve a value from a nested dict using a dotted key path."""
    keys = dotted_key.split(".")
    node: Any = data
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k)
        if node is None:
            return default
    return node
