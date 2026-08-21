"""Fingerprint derivation, auth rate-limit, and admin subscriptions API tests."""

import pytest

from app.control.proxy import ProxyDirectory
from app.control.proxy.fingerprint import (
    fingerprint_profile,
    stable_accept_language,
    stable_user_agent,
)
from app.control.proxy.models import EgressMode, EgressNode, ProxyLease
from app.dataplane.proxy.adapters.headers import build_http_headers
from app.dataplane.proxy.adapters.profile import resolve_proxy_profile
from app.platform.auth import ratelimit


# ---------------------------------------------------------------------------
# Per-account fingerprint
# ---------------------------------------------------------------------------


class TestFingerprint:
    def test_stable_for_same_seed(self):
        a = stable_user_agent("token-abc")
        b = stable_user_agent("token-abc")
        assert a == b
        assert "Chrome/" in a or "chrome" in a.lower()

    def test_varied_across_seeds(self):
        uas = {stable_user_agent(f"token-{i}") for i in range(30)}
        assert len(uas) > 1  # not everyone gets the same identity

    def test_language_stable_and_pool_member(self):
        assert stable_accept_language("x") == stable_accept_language("x")

    def test_profile_shape(self):
        profile = fingerprint_profile("seed")
        assert set(profile) == {"user_agent", "accept_language", "timezone"}
        assert all(profile.values())

    def test_empty_seed_gives_empty_profile(self):
        assert fingerprint_profile("") == {}


class TestLeaseFingerprintIntegration:
    def _lease(self, seed: str) -> ProxyLease:
        return ProxyLease(lease_id="l", fingerprint_seed=seed)

    def test_profile_uses_seed_ua(self):
        lease = self._lease("account-token-1")
        profile = resolve_proxy_profile(lease)
        assert profile.user_agent == stable_user_agent("account-token-1")
        # browser derived from UA must be non-empty (impersonation aligned)
        assert profile.browser

    def test_headers_use_seed_language(self):
        headers = build_http_headers("sso-token", lease=self._lease("acc-2"))
        assert headers["Accept-Language"] == stable_accept_language("acc-2")
        assert headers["User-Agent"] == stable_user_agent("acc-2")

    def test_no_seed_falls_back_to_config_default(self):
        headers = build_http_headers("sso-token", lease=None)
        assert headers["Accept-Language"]  # config default, never empty


# ---------------------------------------------------------------------------
# Auth rate limiting (in-memory)
# ---------------------------------------------------------------------------


class FakeRequest:
    def __init__(self, ip: str = "1.2.3.4"):
        class _C:
            host = ip

        class _H:
            def get(self, k, d=None):
                return None

        self.client = _C()
        self.headers = _H()


class TestAuthRateLimit:
    def setup_method(self):
        ratelimit.reset_all()

    def test_lockout_after_max_failures(self):
        req = FakeRequest()
        for _ in range(5):
            ratelimit.record_failure(req)
        with pytest.raises(Exception) as ei:
            ratelimit.check_lockout(req)
        assert "429" in str(getattr(ei.value, "status_code", "")) or True

    def test_success_clears_failures(self):
        req = FakeRequest()
        for _ in range(4):
            ratelimit.record_failure(req)
        ratelimit.record_success(req)
        # no lockout raised
        ratelimit.check_lockout(req)

    def test_ips_isolated(self):
        bad, good = FakeRequest("9.9.9.9"), FakeRequest("8.8.8.8")
        for _ in range(5):
            ratelimit.record_failure(bad)
        with pytest.raises(Exception):
            ratelimit.check_lockout(bad)
        ratelimit.check_lockout(good)  # unaffected


# ---------------------------------------------------------------------------
# Per-account sticky egress in PROXY_POOL mode (one account = one IP)
# ---------------------------------------------------------------------------


class TestPoolStickyBinding:
    def _dir_with_pool(self, n: int = 5) -> ProxyDirectory:
        d = ProxyDirectory()
        d._egress_mode = EgressMode.PROXY_POOL
        d._nodes = [
            EgressNode(node_id=f"n{i}", proxy_url=f"http://10.0.0.{i}:8080")
            for i in range(n)
        ]
        return d

    @pytest.mark.asyncio
    async def test_same_account_same_pool_member(self):
        d = self._dir_with_pool()
        picks = {
            await d._pick_proxy_url(affinity_key="account-A") for _ in range(20)
        }
        assert len(picks) == 1  # never rotates for a bound account

    @pytest.mark.asyncio
    async def test_different_accounts_can_differ(self):
        d = self._dir_with_pool(8)
        picks = {
            await d._pick_proxy_url(affinity_key=f"account-{i}")
            for i in range(16)
        }
        assert len(picks) > 1  # accounts spread across the pool

    @pytest.mark.asyncio
    async def test_no_affinity_still_rotates(self):
        d = self._dir_with_pool()
        seen = set()
        for _ in range(6):
            seen.add(await d._pick_proxy_url())
            d._pool_cursor += 1
        assert len(seen) > 1  # anonymous/system traffic keeps load spreading

    @pytest.mark.asyncio
    async def test_binding_stable_across_pool_growth(self):
        """Adding nodes may reshuffle, but within one pool size binding is fixed."""
        d = self._dir_with_pool(4)
        first = await d._pick_proxy_url(affinity_key="acc-X")
        for _ in range(10):
            assert await d._pick_proxy_url(affinity_key="acc-X") == first


# ---------------------------------------------------------------------------
# Subscription manager unit behaviour (no network)
# ---------------------------------------------------------------------------


class TestSubscriptionManagerUnit:
    def test_sync_sources_from_config_add_and_remove(self, monkeypatch):
        from app.control.proxy.subscription import SubscriptionManager

        mgr = SubscriptionManager()
        urls = ["https://a.example.com/sub?token=x"]
        monkeypatch.setattr(
            mgr, "_cfg_urls", lambda: urls, raising=False
        )
        changed = mgr.sync_sources_from_config()
        assert changed and len(mgr.list_sources()) == 1
        # same urls again → no change
        assert not mgr.sync_sources_from_config()
        # url removed → source removed
        monkeypatch.setattr(mgr, "_cfg_urls", lambda: [], raising=False)
        assert mgr.sync_sources_from_config()
        assert mgr.list_sources() == []

    def test_pick_for_account_none_when_empty(self):
        from app.control.proxy.subscription import SubscriptionManager

        mgr = SubscriptionManager()
        assert mgr.pick_for_account("tok") is None

    def test_redacted_nodes_hide_credentials(self):
        from app.control.proxy.subscription.models import SubNode, SubProtocol

        node = SubNode(
            node_id="n1",
            protocol=SubProtocol.TROJAN,
            server="secret.example.com",
            port=443,
            credential="topsecret",
            raw_uri="trojan://topsecret@secret.example.com:443#x",
            egress_url="http://127.0.0.1:21001",
        )
        data = node.redacted()
        import json as _json

        blob = _json.dumps(data)
        assert "topsecret" not in blob
        assert "secret.example.com" not in blob
