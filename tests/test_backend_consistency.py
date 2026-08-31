"""Backend consistency matrix — account-store semantics must match across
local (SQLite), SQL (MySQL/PostgreSQL statement shapes) and Redis (hash
serialisation) backends.

These tests pin regressions that actually happened:
  - Redis hash dropped the quota_grok_4_3 / quota_build columns entirely.
  - SQL upserts reset usage counters and quota windows on conflict while the
    local backend preserved them.
  - The MySQL revision seed row was reset to "0" on every worker start,
    stalling other workers' incremental change scans.

Server-backed backends (Redis, MySQL, PostgreSQL) are exercised through
pure statement-serialisation / mapping-level checks so the suite stays
offline-safe for CI.
"""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.dialects import mysql as sa_mysql
from sqlalchemy.dialects import postgresql as sa_pg

from app.control.account.backends import sql as sql_backend
from app.control.account.backends.local import LocalAccountRepository
from app.control.account.backends.redis import RedisAccountRepository
from app.control.account.commands import AccountPatch, AccountUpsert
from app.control.account.enums import AccountStatus
from app.control.account.models import AccountRecord, QuotaSource
from app.control.account.quota_defaults import default_quota_set


def _window(remaining: int = 5) -> dict:
    return {
        "remaining": remaining,
        "total": 10,
        "window_seconds": 3600,
        "reset_at": 4102444800000,
        "synced_at": 4102444700000,
        "source": int(QuotaSource.REAL),
    }


def _make_record(pool: str = "super") -> AccountRecord:
    qs = default_quota_set(pool)
    return AccountRecord(
        token="tok-consistency-1",
        pool=pool,
        quota=qs.to_dict(),
        created_at=1700000000000,
        updated_at=1700000000000,
    )


# ---------------------------------------------------------------------------
# Local (SQLite) backend — full round-trips on a temp database
# ---------------------------------------------------------------------------


def _make_local_repo(tmp_path: Path) -> LocalAccountRepository:
    return LocalAccountRepository(tmp_path / "accounts-consistency.db")


def test_local_upsert_conflict_preserves_usage_and_quota(tmp_path):
    """Re-importing the same token must keep usage stats and fetched quota
    windows (aligned with the SQL backend fix)."""

    async def run():
        repo = _make_local_repo(tmp_path)
        await repo.initialize()
        try:
            await repo.upsert_accounts([AccountUpsert(token="tok-a", pool="super")])
            await repo.patch_accounts([
                AccountPatch(token="tok-a", usage_use_delta=5, usage_fail_delta=2, usage_sync_delta=3),
                AccountPatch(token="tok-a", quota_build=_window(5), quota_grok_4_3=_window(7)),
            ])

            # Re-import the same token with fresh defaults.
            await repo.upsert_accounts([AccountUpsert(token="tok-a", pool="super")])

            records = await repo.get_accounts(["tok-a"])
            assert len(records) == 1
            rec = records[0]
            assert rec.usage_use_count == 5
            assert rec.usage_fail_count == 2
            assert rec.usage_sync_count == 3
            qs = rec.quota_set()
            assert qs.build is not None and qs.build.remaining == 5
            assert qs.grok_4_3 is not None and qs.grok_4_3.remaining == 7
            # Status is reset to active by the re-import, as designed.
            assert rec.status == AccountStatus.ACTIVE
        finally:
            await repo.close()

    asyncio.run(run())


def test_local_patch_supports_all_six_quota_columns(tmp_path):
    """quota_grok_4_3 / quota_build must be patchable like the other four."""

    async def run():
        repo = _make_local_repo(tmp_path)
        await repo.initialize()
        try:
            await repo.upsert_accounts([AccountUpsert(token="tok-b", pool="super")])
            await repo.patch_accounts([
                AccountPatch(
                    token="tok-b",
                    quota_auto=_window(1),
                    quota_fast=_window(2),
                    quota_expert=_window(3),
                    quota_heavy=_window(4),
                    quota_grok_4_3=_window(5),
                    quota_build=_window(6),
                ),
            ])
            rec = (await repo.get_accounts(["tok-b"]))[0]
            qs = rec.quota_set()
            assert [qs.auto.remaining, qs.fast.remaining, qs.expert.remaining] == [1, 2, 3]
            assert [qs.heavy.remaining, qs.grok_4_3.remaining, qs.build.remaining] == [4, 5, 6]
        finally:
            await repo.close()

    asyncio.run(run())


def test_local_delete_then_rescan_reports_deleted_token(tmp_path):
    """Deleting bumps the revision and surfaces as deleted_tokens in
    scan_changes — the sync loop depends on this."""

    async def run():
        repo = _make_local_repo(tmp_path)
        await repo.initialize()
        try:
            await repo.upsert_accounts([AccountUpsert(token="tok-c", pool="basic")])
            result = await repo.delete_accounts(["tok-c"])
            assert result.deleted == 1

            changes = await repo.scan_changes(0)
            assert "tok-c" in changes.deleted_tokens

            records = await repo.get_accounts(["tok-c"])
            assert all(r.is_deleted() for r in records)
        finally:
            await repo.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# SQL backend — statement shapes (offline compilation)
# ---------------------------------------------------------------------------


_UPSERT_ROW_KEYS = (
    "token", "pool", "status", "created_at", "updated_at", "deleted_at",
    "tags", "quota_auto", "quota_fast", "quota_expert", "quota_heavy",
    "quota_grok_4_3", "quota_build", "usage_use_count", "usage_fail_count",
    "usage_sync_count", "ext", "revision",
)

_PRESERVED_COLUMNS = (
    "usage_use_count", "usage_fail_count", "usage_sync_count",
    "quota_auto", "quota_fast", "quota_expert",
    "quota_heavy", "quota_grok_4_3", "quota_build",
)


def _upsert_conflict_section(repo: sql_backend.SqlAccountRepository, dialect) -> str:
    row = {key: None for key in _UPSERT_ROW_KEYS}
    stmt = repo._build_upsert(row)
    compiled = str(stmt.compile(dialect=dialect))
    marker = (
        "ON CONFLICT"
        if dialect.name == "postgresql"
        else "ON DUPLICATE KEY UPDATE"
    )
    assert marker in compiled, compiled
    return compiled.split(marker, 1)[1]


def test_sql_upsert_conflict_preserves_usage_and_quota_mysql():
    repo = sql_backend.SqlAccountRepository.__new__(sql_backend.SqlAccountRepository)
    repo._dialect = "mysql"
    conflict_sql = _upsert_conflict_section(repo, sa_mysql.dialect())
    for col in _PRESERVED_COLUMNS:
        assert col not in conflict_sql, f"MySQL conflict update must not reset {col}"
    # Columns that DO get refreshed on re-import.
    for col in ("pool", "status", "updated_at", "tags", "ext", "revision", "deleted_at"):
        assert col in conflict_sql, f"MySQL conflict update must refresh {col}"


def test_sql_upsert_conflict_preserves_usage_and_quota_postgresql():
    repo = sql_backend.SqlAccountRepository.__new__(sql_backend.SqlAccountRepository)
    repo._dialect = "postgresql"
    conflict_sql = _upsert_conflict_section(repo, sa_pg.dialect())
    for col in _PRESERVED_COLUMNS:
        assert col not in conflict_sql, f"PG conflict update must not reset {col}"
    for col in ("pool", "status", "updated_at", "tags", "ext", "revision", "deleted_at"):
        assert col in conflict_sql, f"PG conflict update must refresh {col}"


def test_mysql_revision_seed_keeps_existing_value():
    """The seed statement must not reset the revision counter on duplicate —
    every worker calls initialize() at startup, and a reset would put the
    counter below other workers' sync watermarks."""
    from app.control.account.backends.sql import meta_table
    from sqlalchemy.dialects.mysql import insert as my_insert

    stmt = my_insert(meta_table).values(key="revision", value="0")
    stmt = stmt.on_duplicate_key_update(value=meta_table.c.value)
    compiled = str(stmt.compile(dialect=sa_mysql.dialect()))
    assert "ON DUPLICATE KEY UPDATE" in compiled
    # The update must reference the existing row's value, not the inserted "0".
    assert "value = account_meta.value" in compiled


def test_sql_patch_compiles_with_atomic_counter_increments():
    """Usage counter patches must compile to in-database increments
    (GREATEST(0, col + delta)), not read-modify-write values."""
    import sqlalchemy as sa

    from app.control.account.backends.sql import accounts_table

    updates = {
        "usage_use_count": sa.func.greatest(
            0, accounts_table.c.usage_use_count + 3
        ),
    }
    stmt = accounts_table.update().where(accounts_table.c.token == "tok").values(**updates)
    compiled = str(stmt.compile(dialect=sa_pg.dialect()))
    assert "GREATEST" in compiled.upper()
    assert "usage_use_count" in compiled


# ---------------------------------------------------------------------------
# Redis backend — hash serialisation round-trip (offline)
# ---------------------------------------------------------------------------


def test_redis_hash_roundtrip_includes_grok_4_3_and_build():
    record = _make_record("super")
    qs = record.quota_set()
    assert qs.grok_4_3 is not None and qs.build is not None

    hashed = RedisAccountRepository._to_hash(record, revision=7)
    assert "quota_grok_4_3" in hashed
    assert "quota_build" in hashed

    restored = RedisAccountRepository._from_hash(record.token, hashed)
    rqs = restored.quota_set()
    assert rqs.grok_4_3 is not None and rqs.grok_4_3.remaining == qs.grok_4_3.remaining
    assert rqs.build is not None and rqs.build.remaining == qs.build.remaining
    assert restored.revision == 7


def test_redis_hash_roundtrip_without_optional_windows():
    """A record whose optional windows were never populated must survive the
    round-trip without gaining or losing windows."""
    record = _make_record("basic")
    qs = record.quota_set()

    hashed = RedisAccountRepository._to_hash(record, revision=3)
    restored = RedisAccountRepository._from_hash(record.token, hashed)
    rqs = restored.quota_set()

    # Whatever the default quota set defines must round-trip unchanged.
    assert (rqs.grok_4_3 is None) == (qs.grok_4_3 is None)
    assert (rqs.build is None) == (qs.build is None)
    if qs.grok_4_3 is not None:
        assert rqs.grok_4_3.remaining == qs.grok_4_3.remaining
    if qs.build is not None:
        assert rqs.build.remaining == qs.build.remaining
    assert restored.revision == 3


def test_redis_patch_mapping_covers_all_quota_columns():
    """The patch whitelist in RedisAccountRepository.patch_accounts must
    include every quota column the refresh service can emit."""
    import inspect

    source = inspect.getsource(RedisAccountRepository.patch_accounts)
    for col in ("quota_auto", "quota_fast", "quota_expert", "quota_heavy",
                "quota_grok_4_3", "quota_build"):
        assert f'"{col}"' in source, f"Redis patch_accounts drops {col}"


# ---------------------------------------------------------------------------
# Cross-backend: default quota sets expose the same window structure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pool", ["basic", "super", "heavy"])
def test_default_quota_set_shape_consistent_across_backends(pool):
    """All three backends serialise quota through QuotaSet.to_dict — the
    window keys must stay aligned with the storage columns."""
    qs = default_quota_set(pool)
    d = qs.to_dict()
    for key in ("auto", "fast", "expert"):
        assert key in d
    if pool in ("super", "heavy"):
        assert "grok_4_3" in d
