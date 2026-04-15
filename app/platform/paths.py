"""Shared runtime paths derived from environment variables."""

import os
from pathlib import Path


_ROOT_DIR = Path(__file__).resolve().parents[2]


def _resolve_env_path(name: str, default: str) -> Path:
    raw = os.getenv(name, default).strip() or default
    path = Path(raw)
    if not path.is_absolute():
        path = _ROOT_DIR / path
    return path


def _is_serverless_runtime() -> bool:
    return bool(
        os.getenv("VERCEL")
        or os.getenv("AWS_LAMBDA_FUNCTION_NAME")
        or os.getenv("FUNCTIONS_WORKER_RUNTIME")
    )


def data_dir() -> Path:
    default = "/tmp/data" if _is_serverless_runtime() else "data"
    return _resolve_env_path("DATA_DIR", default)


def log_dir() -> Path:
    default = "/tmp/logs" if _is_serverless_runtime() else "logs"
    return _resolve_env_path("LOG_DIR", default)


def data_path(*parts: str) -> Path:
    return data_dir().joinpath(*parts)


def log_path(*parts: str) -> Path:
    return log_dir().joinpath(*parts)


__all__ = ["data_dir", "log_dir", "data_path", "log_path"]
