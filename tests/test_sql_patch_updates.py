"""Tests for the pure-function patch helper used by SqlAccountRepository.

Locks in the behaviour assumed by the grouped-executemany batching path in
``SqlAccountRepository.patch_accounts``:

* ``updated_at`` and ``revision`` are always set so every patch shares those
  bind parameters.
* ``tags`` and ``ext`` are always serialised even when the patch does not
  explicitly touch them, so patches differing only in selective fields still
  end up in the same column-signature group.
* ``clear_failures=True`` resets the auth-failure related columns.
* Only the explicitly requested patch fields appear as additional keys (so
  the signature grouping in ``patch_accounts`` works as documented).
"""

import json
import unittest

from app.control.account.backends.sql import _build_patch_updates
from app.control.account.commands import AccountPatch
from app.control.account.enums import AccountStatus
from app.control.account.models import AccountRecord


def _make_record(**overrides) -> AccountRecord:
    base = {
        "token":            "sso=" + "a" * 40,
        "pool":             "basic",
        "status":           AccountStatus.ACTIVE,
        "tags":             ["existing"],
        "ext":              {"keep": 1},
        "usage_use_count":  5,
        "usage_fail_count": 2,
        "usage_sync_count": 1,
    }
    base.update(overrides)
    return AccountRecord(**base)


class BuildPatchUpdatesTests(unittest.TestCase):
    def test_baseline_always_sets_timestamp_revision_tags_ext(self) -> None:
        record = _make_record()
        patch = AccountPatch(token=record.token)

        updates = _build_patch_updates(patch, record, ts=1700_000_000_000, rev=42)

        self.assertEqual(updates["updated_at"], 1700_000_000_000)
        self.assertEqual(updates["revision"],   42)
        # tags / ext are always serialised so all patches share the same
        # column signature even when the caller did not explicitly touch them.
        self.assertEqual(json.loads(updates["tags"]), ["existing"])
        self.assertEqual(json.loads(updates["ext"]),  {"keep": 1})
        # No other columns leak in.
        self.assertEqual(
            set(updates.keys()),
            {"updated_at", "revision", "tags", "ext"},
        )

    def test_add_tags_merges_without_duplicates(self) -> None:
        record = _make_record(tags=["nsfw", "vip"])
        patch = AccountPatch(token=record.token, add_tags=["nsfw", "verified"])

        updates = _build_patch_updates(patch, record, ts=1, rev=1)

        self.assertEqual(json.loads(updates["tags"]), ["nsfw", "vip", "verified"])

    def test_remove_tags_drops_requested_entries(self) -> None:
        record = _make_record(tags=["nsfw", "vip"])
        patch = AccountPatch(token=record.token, remove_tags=["nsfw"])

        updates = _build_patch_updates(patch, record, ts=1, rev=1)

        self.assertEqual(json.loads(updates["tags"]), ["vip"])

    def test_explicit_tags_overwrites_existing_list(self) -> None:
        record = _make_record(tags=["nsfw"])
        patch = AccountPatch(token=record.token, tags=["only-this"])

        updates = _build_patch_updates(patch, record, ts=1, rev=1)

        self.assertEqual(json.loads(updates["tags"]), ["only-this"])

    def test_usage_deltas_compose_with_existing_counters(self) -> None:
        record = _make_record(
            usage_use_count=5,
            usage_fail_count=2,
            usage_sync_count=1,
        )
        patch = AccountPatch(
            token=record.token,
            usage_use_delta=3,
            usage_fail_delta=-10,  # clamped at 0
            usage_sync_delta=4,
        )

        updates = _build_patch_updates(patch, record, ts=1, rev=1)

        self.assertEqual(updates["usage_use_count"],  8)
        self.assertEqual(updates["usage_fail_count"], 0)  # max(0, 2 - 10)
        self.assertEqual(updates["usage_sync_count"], 5)

    def test_clear_failures_resets_auth_state_and_ext_keys(self) -> None:
        record = _make_record(
            status=AccountStatus.DISABLED,
            usage_fail_count=7,
            last_fail_at=1234,
            last_fail_reason="forbidden",
            state_reason="operator_disabled",
            ext={
                "keep":             "yes",
                "cooldown_until":   123,
                "cooldown_reason":  "rl",
                "disabled_at":      999,
                "disabled_reason":  "ops",
                "expired_at":       111,
                "expired_reason":   "auth",
                "forbidden_strikes": 3,
            },
        )
        patch = AccountPatch(token=record.token, clear_failures=True)

        updates = _build_patch_updates(patch, record, ts=1, rev=1)

        self.assertEqual(updates["status"],           AccountStatus.ACTIVE.value)
        self.assertEqual(updates["usage_fail_count"], 0)
        self.assertIsNone(updates["last_fail_at"])
        self.assertIsNone(updates["last_fail_reason"])
        self.assertIsNone(updates["state_reason"])

        ext_after = json.loads(updates["ext"])
        # Unrelated extension keys are preserved.
        self.assertEqual(ext_after, {"keep": "yes"})

    def test_quota_fields_serialised_only_when_provided(self) -> None:
        record = _make_record()
        patch = AccountPatch(
            token=record.token,
            quota_auto={"remaining": 9, "total": 10},
        )

        updates = _build_patch_updates(patch, record, ts=1, rev=1)

        self.assertEqual(
            json.loads(updates["quota_auto"]),
            {"remaining": 9, "total": 10},
        )
        # The other quota windows should NOT be included in the column set so
        # patches that touch only quota_auto land in their own signature group.
        self.assertNotIn("quota_fast",   updates)
        self.assertNotIn("quota_expert", updates)
        self.assertNotIn("quota_heavy",  updates)

    def test_two_uniform_nsfw_patches_produce_identical_signature(self) -> None:
        """Regression: bulk NSFW tagging must yield the same column-set so the
        ``patch_accounts`` grouping collapses them into a single executemany."""
        record_a = _make_record(token="sso=" + "a" * 40, tags=[])
        record_b = _make_record(token="sso=" + "b" * 40, tags=["other"])
        patch_a = AccountPatch(token=record_a.token, add_tags=["nsfw"])
        patch_b = AccountPatch(token=record_b.token, add_tags=["nsfw"])

        updates_a = _build_patch_updates(patch_a, record_a, ts=1, rev=1)
        updates_b = _build_patch_updates(patch_b, record_b, ts=1, rev=1)

        # Identical column signatures → one executemany at the SQL layer.
        self.assertEqual(set(updates_a.keys()), set(updates_b.keys()))
        # But row-specific values still differ as expected.
        self.assertNotEqual(updates_a["tags"], updates_b["tags"])


if __name__ == "__main__":
    unittest.main()
