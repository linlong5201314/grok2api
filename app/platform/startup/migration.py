"""First-boot data migration.

Config migration
----------------
local   : seeds ``${DATA_DIR}/config.toml`` from ``config.defaults.toml`` if
          the file does not exist yet - gives users an editable copy on first run.
redis / sql : if the backend is empty (version == 0) AND
          ``${DATA_DIR}/config.toml`` exists, migrates the user overrides into
          the DB backend. If it does not exist either, nothing is written
          (defaults are always loaded from ``config.defaults.toml`` at runtime).

Account migration
-----------------
Runs only when ACCOUNT_STORAGE != "local".
If ``${DATA_DIR}/accounts.db`` (the previous local SQLite store) exists AND the
target backend is empty (revision == 0), all accounts are copied into the
new backend - preserving pool, status, quota, usage stats, and timestamps.
After a successful migration the SQLite file is renamed to
``${DATA_DIR}/accounts.db.migrated`` so the same migration is never re-run.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
import sqlalchemy as sa

from app.platform.paths import data_path

if TYPE_CHECKING:
    from app.control.account.repository import AccountRepository
    from app.platform.config.backends.base import ConfigBackend

_BASE_DIR     = Path(__file__).resolve().parents[3]
_DEFAULTS_PATH = _BASE_DIR / "config.defaults.toml"
_USER_CFG_PATH = data_path("config.toml")
_LOCAL_DB_PATH = data_path("accounts.db")
_BATCH         = 500  # accounts per upsert/patch batch


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_startup_migrations(
    config_backend: "ConfigBackend",
    account_repo: "AccountRepository",
) -> None:
    """Run all first-boot migrations.  Safe to call on every startup."""
    await _migrate_config(config_backend)
    await _migrate_accounts(account_repo)


# ---------------------------------------------------------------------------
# Config migration
# ---------------------------------------------------------------------------

async def _migrate_config(backend: "ConfigBackend") -> None:
    from app.platform.config.backends.factory import get_config_backend_name
    from app.platform.config.loader import load_toml

    backend_name = get_config_backend_name()

    if backend_name == "local":
        # Seed ${DATA_DIR}/config.toml from defaults so users have an editable file.
        if not _USER_CFG_PATH.exists() and _DEFAULTS_PATH.exists():
            await asyncio.to_thread(shutil.copy2, _DEFAULTS_PATH, _USER_CFG_PATH)
            logger.info("config: seeded {} from config.defaults.toml", _USER_CFG_PATH)
        return

    # DB / Redis backends - migrate only if backend is empty.
    if await backend.version() != 0:
        return  # already has data, skip

    if _USER_CFG_PATH.exists():
        user_data = await asyncio.to_thread(load_toml, _USER_CFG_PATH)
        if user_data:
            await backend.apply_patch(user_data)
            logger.info(
                "config: migrated {} -> {} backend ({} keys)",
                _USER_CFG_PATH,
                backend_name,
                _count_keys(user_data),
            )
            return

    # Legacy SQL schema compatibility:
    # old versions stored config in `app_config` (section/key/value rows).
    if backend_name in ("mysql", "postgresql"):
        if await _migrate_legacy_sql_config(backend, backend_name):
            return

    logger.debug("config: {} backend is empty, no local overrides to migrate", backend_name)


# ---------------------------------------------------------------------------
# Account migration
# ---------------------------------------------------------------------------

async def _migrate_accounts(target_repo: "AccountRepository") -> None:
    from app.control.account.backends.factory import get_repository_backend

    backend = get_repository_backend()
    if backend == "local":
        return  # already on local, nothing to migrate

    # Check whether the target already has data.
    snapshot = await target_repo.runtime_snapshot()
    if snapshot.revision > 0 or snapshot.items:
        logger.debug("account: target backend not empty (revision={}), skipping migration", snapshot.revision)
        return

    sqlite_path = _LOCAL_DB_PATH
    if sqlite_path.exists():
        logger.info("account: migrating accounts from {} -> {} backend", sqlite_path, backend)
        count = await _copy_accounts(sqlite_path, target_repo)

        # Rename the SQLite file so this migration is never re-run.
        done_path = sqlite_path.with_suffix(".db.migrated")
        await asyncio.to_thread(sqlite_path.rename, done_path)
        logger.info("account: migration complete ({} accounts), renamed {} -> {}", count, sqlite_path.name, done_path.name)
        return

    # Legacy SQL schema compatibility:
    # old versions stored account data in `tokens`.
    if backend in ("mysql", "postgresql"):
        if await _migrate_legacy_sql_accounts(target_repo):
            return

    logger.debug("account: no local/legacy source data found for startup migration")

async def _copy_accounts(sqlite_path: Path, target: "AccountRepository") -> int:
    """Read all accounts from the local SQLite file and write to *target*."""
    from app.control.account.backends.local import LocalAccountRepository
    from app.control.account.commands import AccountPatch, AccountUpsert, ListAccountsQuery

    source = LocalAccountRepository(sqlite_path)
    await source.initialize()

    total = 0
    page = 1

    try:
        while True:
            result = await source.list_accounts(
                ListAccountsQuery(page=page, page_size=_BATCH, include_deleted=True)
            )
            records = result.items
            if not records:
                break

            # Step 1: upsert - creates records with token / pool / tags / ext.
            upserts = [
                AccountUpsert(token=r.token, pool=r.pool, tags=r.tags, ext=r.ext)
                for r in records
            ]
            await target.upsert_accounts(upserts)

            # Step 2: patch - fills status, quota, usage counters, timestamps.
            patches = [_record_to_patch(r) for r in records]
            await target.patch_accounts(patches)

            # Step 3: soft-delete records that were deleted in the source.
            deleted_tokens = [r.token for r in records if r.deleted_at is not None]
            if deleted_tokens:
                await target.delete_accounts(deleted_tokens)

            total += len(records)
            if page >= result.total_pages:
                break
            page += 1
    finally:
        await source.close()

    return total


def _record_to_patch(r) -> "AccountPatch":
    from app.control.account.commands import AccountPatch

    qs = r.quota_set()
    return AccountPatch(
        token=r.token,
        status=r.status,
        quota_auto=qs.auto.to_dict()   if qs.auto   else None,
        quota_fast=qs.fast.to_dict()   if qs.fast   else None,
        quota_expert=qs.expert.to_dict() if qs.expert else None,
        quota_heavy=qs.heavy.to_dict() if qs.heavy  else None,
        # Usage counts - target starts at 0, so actual value == delta.
        usage_use_delta=r.usage_use_count   or None,
        usage_fail_delta=r.usage_fail_count or None,
        usage_sync_delta=r.usage_sync_count or None,
        last_use_at=r.last_use_at,
        last_fail_at=r.last_fail_at,
        last_fail_reason=r.last_fail_reason,
        last_sync_at=r.last_sync_at,
        last_clear_at=r.last_clear_at,
        state_reason=r.state_reason,
        ext_merge=r.ext or None,
    )


async def _migrate_legacy_sql_config(backend: "ConfigBackend", backend_name: str) -> bool:
    """Migrate old SQL `app_config` rows into the new config backend."""
    engine = getattr(backend, "_engine", None)
    if engine is None:
        return False

    try:
        async with engine.connect() as conn:
            if not await _has_table(conn, "app_config"):
                return False
            rows = (
                await conn.execute(
                    sa.text("SELECT section, key_name, value FROM app_config")
                )
            ).fetchall()
    except Exception as exc:
        logger.debug("config: legacy app_config probe failed: {}", exc)
        return False

    if not rows:
        return False

    patch: dict[str, dict[str, object]] = {}
    for section, key_name, value in rows:
        section_str = str(section or "").strip()
        key_str = str(key_name or "").strip()
        if not section_str or not key_str:
            continue
        patch.setdefault(section_str, {})[key_str] = _parse_json_maybe(value)

    if not patch:
        return False

    await backend.apply_patch(patch)
    logger.info(
        "config: migrated legacy app_config -> {} backend ({} keys)",
        backend_name,
        _count_keys(patch),
    )
    return True


async def _migrate_legacy_sql_accounts(target_repo: "AccountRepository") -> bool:
    """Migrate old SQL `tokens` rows into the new `accounts` schema."""
    from app.control.account.commands import AccountPatch, AccountUpsert

    engine = getattr(target_repo, "_engine", None)
    if engine is None:
        return False

    try:
        async with engine.connect() as conn:
            if not await _has_table(conn, "tokens"):
                return False
            rows = (
                await conn.execute(
                    sa.text(
                        "SELECT token, pool_name, status, last_used_at, use_count, "
                        "fail_count, last_fail_at, last_fail_reason, last_sync_at, "
                        "tags, last_asset_clear_at "
                        "FROM tokens"
                    )
                )
            ).fetchall()
    except Exception as exc:
        logger.debug("account: legacy tokens probe failed: {}", exc)
        return False

    if not rows:
        return False

    upserts: list[AccountUpsert] = []
    patches: list[AccountPatch] = []
    for row in rows:
        data = row._mapping
        token = str(data.get("token") or "").strip()
        if not token:
            continue

        upserts.append(
            AccountUpsert(
                token=token,
                pool=_normalize_legacy_pool(data.get("pool_name")),
                tags=_normalize_legacy_tags(data.get("tags")),
            )
        )
        patches.append(
            AccountPatch(
                token=token,
                status=_normalize_legacy_status(data.get("status")),
                usage_use_delta=_to_int_or_none(data.get("use_count")),
                usage_fail_delta=_to_int_or_none(data.get("fail_count")),
                last_use_at=_to_int_or_none(data.get("last_used_at")),
                last_fail_at=_to_int_or_none(data.get("last_fail_at")),
                last_fail_reason=(
                    str(data.get("last_fail_reason")).strip()
                    if data.get("last_fail_reason") is not None
                    else None
                ) or None,
                last_sync_at=_to_int_or_none(data.get("last_sync_at")),
                last_clear_at=_to_int_or_none(data.get("last_asset_clear_at")),
            )
        )

    if not upserts:
        return False

    total = 0
    for i in range(0, len(upserts), _BATCH):
        upsert_result = await target_repo.upsert_accounts(upserts[i : i + _BATCH])
        await target_repo.patch_accounts(patches[i : i + _BATCH])
        total += upsert_result.upserted

    logger.info("account: migrated legacy SQL tokens -> accounts backend ({} accounts)", total)
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _has_table(conn: sa.ext.asyncio.AsyncConnection, table_name: str) -> bool:
    def _check(sync_conn) -> bool:
        return sa.inspect(sync_conn).has_table(table_name)

    return await conn.run_sync(_check)


def _parse_json_maybe(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    raw = str(value).strip()
    if raw == "":
        return ""
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _to_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_legacy_pool(value: object) -> str:
    pool = str(value or "").strip().lower()
    if pool == "super":
        return "super"
    if pool == "heavy":
        return "heavy"
    return "basic"


def _normalize_legacy_tags(value: object) -> list[str]:
    parsed = _parse_json_maybe(value)
    if isinstance(parsed, list):
        src = parsed
    elif isinstance(parsed, str):
        src = [p.strip() for p in parsed.split(",")]
    else:
        return []

    tags: list[str] = []
    for item in src:
        tag = str(item).strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def _normalize_legacy_status(value: object):
    from app.control.account.enums import AccountStatus

    raw = str(value or "").strip().lower()
    if raw == AccountStatus.COOLING.value:
        return AccountStatus.COOLING
    if raw in {AccountStatus.EXPIRED.value, "invalid", "unauthorized"}:
        return AccountStatus.EXPIRED
    if raw in {AccountStatus.DISABLED.value, "forbidden", "banned", "locked"}:
        return AccountStatus.DISABLED
    return AccountStatus.ACTIVE


def _count_keys(nested: dict, prefix: str = "") -> int:
    count = 0
    for v in nested.values():
        if isinstance(v, dict):
            count += _count_keys(v)
        else:
            count += 1
    return count

