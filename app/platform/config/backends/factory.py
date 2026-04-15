"""Config backend factory — follows ACCOUNT_STORAGE automatically."""

import os
from pathlib import Path

from app.control.account.backends.factory import get_repository_backend
from app.platform.paths import data_path
from .base import ConfigBackend

_BACKEND_ALIASES = {
    "postgres": "postgresql",
    "pgsql": "postgresql",
    "pg": "postgresql",
    "mariadb": "mysql",
}


def get_config_backend_name() -> str:
    """Return the active config backend name (mirrors ACCOUNT_STORAGE)."""
    return get_repository_backend()


def _normalize_backend(raw: str) -> str:
    val = str(raw or "").strip().lower()
    return _BACKEND_ALIASES.get(val, val)


def _legacy_storage_url_for(expected_backend: str) -> str:
    legacy_url = os.getenv("SERVER_STORAGE_URL", "").strip()
    if not legacy_url:
        return ""
    legacy_backend = _normalize_backend(os.getenv("SERVER_STORAGE_TYPE", "local"))
    return legacy_url if legacy_backend == expected_backend else ""


def create_config_backend() -> ConfigBackend:
    """Instantiate the config backend that matches the account storage backend.

    ``ACCOUNT_STORAGE=local``       → TOML file (``${DATA_DIR}/config.toml``)
    ``ACCOUNT_STORAGE=redis``       → Redis  (ACCOUNT_REDIS_URL)
    ``ACCOUNT_STORAGE=mysql``       → MySQL  (ACCOUNT_MYSQL_URL)
    ``ACCOUNT_STORAGE=postgresql``  → PostgreSQL (ACCOUNT_POSTGRESQL_URL)

    No extra env vars needed — reuses the same connection settings as accounts.
    """
    backend = get_config_backend_name()

    if backend == "local":
        return _make_toml()
    if backend == "redis":
        return _make_redis()
    if backend in ("mysql", "postgresql"):
        return _make_sql(backend)

    raise ValueError(f"Unknown account storage backend: {backend!r}")


def _make_toml() -> ConfigBackend:
    from .toml import TomlConfigBackend

    path_str = os.getenv("CONFIG_LOCAL_PATH", str(data_path("config.toml"))).strip()
    path = Path(path_str)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[5] / path
    return TomlConfigBackend(path)


def _make_redis() -> ConfigBackend:
    from redis.asyncio import Redis
    from .redis import RedisConfigBackend

    url = os.getenv("ACCOUNT_REDIS_URL", "").strip() or _legacy_storage_url_for("redis")
    if not url:
        raise ValueError("Redis config backend requires ACCOUNT_REDIS_URL (or legacy SERVER_STORAGE_URL for redis)")
    r = Redis.from_url(url, decode_responses=False)
    return RedisConfigBackend(r)


def _make_sql(dialect: str) -> ConfigBackend:
    from .sql import SqlConfigBackend
    from app.control.account.backends.sql import (
        create_mysql_engine,
        create_pgsql_engine,
    )

    if dialect == "mysql":
        url = os.getenv("ACCOUNT_MYSQL_URL", "").strip() or _legacy_storage_url_for("mysql")
        if not url:
            raise ValueError("MySQL config backend requires ACCOUNT_MYSQL_URL (or legacy SERVER_STORAGE_URL with SERVER_STORAGE_TYPE=mysql)")
        engine = create_mysql_engine(url)
    else:
        url = os.getenv("ACCOUNT_POSTGRESQL_URL", "").strip() or _legacy_storage_url_for("postgresql")
        if not url:
            raise ValueError("PostgreSQL config backend requires ACCOUNT_POSTGRESQL_URL (or legacy SERVER_STORAGE_URL with SERVER_STORAGE_TYPE=pgsql/postgres/postgresql)")
        engine = create_pgsql_engine(url)

    return SqlConfigBackend(engine, dialect=dialect, dispose_engine=False)


__all__ = ["create_config_backend", "get_config_backend_name"]
