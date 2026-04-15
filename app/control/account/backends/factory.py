"""Account repository factory — selects the backend from startup env."""

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.platform.paths import data_path
from ..repository import AccountRepository

_SUPPORTED_BACKENDS = {"local", "redis", "mysql", "postgresql"}
_BACKEND_ALIASES = {
    "postgres": "postgresql",
    "pgsql": "postgresql",
    "pg": "postgresql",
    "mariadb": "mysql",
}


def create_repository() -> AccountRepository:
    """Instantiate the configured account storage backend.

    Startup env: ``ACCOUNT_STORAGE``  (default: ``"local"``)

    Supported values:
      ``local``      — SQLite (default, single-process)
      ``redis``      — Redis hash + sorted-set layout
      ``mysql``      — MySQL via aiomysql / SQLAlchemy
      ``postgresql`` — PostgreSQL via asyncpg / SQLAlchemy
    """
    backend = get_repository_backend()

    if backend == "local":
        return _make_local()
    if backend == "redis":
        return _make_redis()
    if backend == "mysql":
        return _make_sql("mysql")
    if backend == "postgresql":
        return _make_sql("postgresql")

    raise ValueError(f"Unknown account storage backend: {backend!r}")


def describe_repository_target() -> tuple[str, str]:
    """Return current storage backend and a log-safe target description."""
    backend = get_repository_backend()

    if backend == "local":
        return "local", str(_resolve_local_db_path())
    if backend == "redis":
        return "redis", _redact_url(_get_required_redis_url())
    if backend == "mysql":
        return "mysql", _redact_url(_get_required_sql_url("mysql"))
    if backend == "postgresql":
        return "postgresql", _redact_url(_get_required_sql_url("postgresql"))
    return backend, "<unknown>"


# ---------------------------------------------------------------------------
# Backend constructors
# ---------------------------------------------------------------------------

def get_repository_backend() -> str:
    """Return the configured account storage backend from startup env."""
    backend = _normalize_backend(_get_backend_env())
    if backend not in _SUPPORTED_BACKENDS:
        raise ValueError(f"Unknown account storage backend: {backend!r}")
    return backend


def _normalize_backend(raw: str) -> str:
    val = str(raw or "").strip().lower()
    return _BACKEND_ALIASES.get(val, val)


def _get_backend_env() -> str:
    # Backward compatibility: legacy deployments used SERVER_STORAGE_TYPE.
    return (
        _get_env("ACCOUNT_STORAGE")
        or _get_env("SERVER_STORAGE_TYPE")
        or "local"
    )


def _get_env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _get_required_env(name: str) -> str:
    value = _get_env(name)
    if not value:
        raise ValueError(f"Missing required env: {name}")
    return value


def _get_legacy_storage_url_for(expected_backend: str) -> str:
    legacy_url = _get_env("SERVER_STORAGE_URL")
    if not legacy_url:
        return ""

    legacy_backend = _normalize_backend(_get_env("SERVER_STORAGE_TYPE", "local"))
    return legacy_url if legacy_backend == expected_backend else ""


def _get_redis_url() -> str:
    return _get_env("ACCOUNT_REDIS_URL") or _get_legacy_storage_url_for("redis")


def _get_required_redis_url() -> str:
    url = _get_redis_url()
    if not url:
        raise ValueError("Missing required env: ACCOUNT_REDIS_URL (or legacy SERVER_STORAGE_URL for redis)")
    return url


def _get_sql_url(dialect: str) -> str:
    if dialect == "mysql":
        return _get_env("ACCOUNT_MYSQL_URL") or _get_legacy_storage_url_for("mysql")
    return _get_env("ACCOUNT_POSTGRESQL_URL") or _get_legacy_storage_url_for("postgresql")


def _get_required_sql_url(dialect: str) -> str:
    url = _get_sql_url(dialect)
    if url:
        return url
    if dialect == "mysql":
        raise ValueError("Missing required env: ACCOUNT_MYSQL_URL (or legacy SERVER_STORAGE_URL with SERVER_STORAGE_TYPE=mysql)")
    raise ValueError("Missing required env: ACCOUNT_POSTGRESQL_URL (or legacy SERVER_STORAGE_URL with SERVER_STORAGE_TYPE=pgsql/postgres/postgresql)")


def _resolve_local_db_path() -> Path:
    path_str = _get_env("ACCOUNT_LOCAL_PATH", str(data_path("accounts.db")))
    db_path = Path(path_str)
    if not db_path.is_absolute():
        db_path = Path(__file__).resolve().parents[4] / db_path
    return db_path


def _redact_url(url: Any) -> str:
    raw = str(url or "").strip()
    if not raw:
        return "<empty>"
    try:
        parts = urlsplit(raw)
    except Exception:
        return raw
    if not parts.scheme:
        return raw
    hostname = parts.hostname or ""
    if parts.port:
        hostname = f"{hostname}:{parts.port}"
    if parts.username:
        auth = f"{parts.username}:***@"
    elif parts.password:
        auth = "***@"
    else:
        auth = ""
    return urlunsplit((parts.scheme, f"{auth}{hostname}", parts.path, parts.query, parts.fragment))


def _make_local() -> AccountRepository:
    from .local import LocalAccountRepository

    return LocalAccountRepository(_resolve_local_db_path())


def _make_redis() -> AccountRepository:
    from redis.asyncio import Redis
    from .redis import RedisAccountRepository

    url = _get_required_redis_url()
    r   = Redis.from_url(url, decode_responses=False)
    return RedisAccountRepository(r)


def _make_sql(dialect: str) -> AccountRepository:
    from .sql import SqlAccountRepository, create_mysql_engine, create_pgsql_engine

    if dialect == "mysql":
        url    = _get_required_sql_url("mysql")
        engine = create_mysql_engine(url)
    else:
        url    = _get_required_sql_url("postgresql")
        engine = create_pgsql_engine(url)
    return SqlAccountRepository(engine, dialect=dialect, dispose_engine=False)


__all__ = ["create_repository", "describe_repository_target", "get_repository_backend"]
