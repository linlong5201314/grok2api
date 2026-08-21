"""Speed-test scoring and affinity binding tests (no network access)."""

import pytest

from app.control.proxy.subscription.models import NodeState, SubNode, SubProtocol
from app.control.proxy.subscription.speedtest import (
    NodeSpeedTester,
    affinity_index,
    pick_for_affinity,
    rank_nodes,
)


def _node(node_id: str, *, latency: float | None = None, state=NodeState.HEALTHY,
          egress: str = "http://1.2.3.4:8080", loss: float = 0.0) -> SubNode:
    n = SubNode(
        node_id=node_id,
        name=node_id,
        protocol=SubProtocol.HTTP,
        server="1.2.3.4",
        port=8080,
        state=state,
        egress_url=egress,
        latency_ms=latency,
        loss_rate=loss,
    )
    n.score = NodeSpeedTester.compute_score(n)
    return n


class TestScoring:
    def test_faster_node_scores_higher(self):
        fast = _node("fast", latency=100.0)
        slow = _node("slow", latency=900.0)
        assert fast.score > slow.score > 0

    def test_dead_and_unprobed_score_zero(self):
        assert _node("dead", state=NodeState.DEAD).score == 0.0
        assert _node("new", latency=None).score == 0.0

    def test_loss_rate_penalises_score(self):
        clean = _node("clean", latency=200.0, loss=0.0)
        lossy = _node("lossy", latency=200.0, loss=0.5)
        assert clean.score > lossy.score

    def test_success_updates_state_and_ewma(self):
        tester = NodeSpeedTester()
        node = _node("n", latency=None, state=NodeState.NEW)
        node.loss_rate = 0.8
        tester._apply_success(node, 150.0)
        assert node.state == NodeState.HEALTHY
        assert node.latency_ms == 150.0
        # EWMA shrinks toward 0 but never reaches it in one step
        assert 0.0 < node.loss_rate < 0.8
        assert node.fail_count == 0

    def test_consecutive_failures_mark_dead(self):
        tester = NodeSpeedTester()
        node = _node("n", latency=100.0)
        for _ in range(4):
            tester._apply_failure(node)
        assert node.state == NodeState.DEAD
        assert node.score == 0.0


class TestRanking:
    def test_rank_puts_probed_first_then_by_score(self):
        unprobed = _node("u", latency=None, state=NodeState.NEW)
        slow = _node("s", latency=800.0)
        fast = _node("f", latency=120.0)
        dead = _node("d", state=NodeState.DEAD, egress="")
        ranked = rank_nodes([slow, dead, unprobed, fast])
        assert [n.node_id for n in ranked] == ["f", "s", "u"]
        # dead node without egress is not usable → excluded
        assert all(n.is_usable for n in ranked)

    def test_unusable_excluded(self):
        broken = _node("b", latency=50.0, egress="")
        ranked = rank_nodes([broken])
        assert ranked == []


class TestAffinity:
    def test_same_key_binds_same_node(self):
        nodes = [_node(f"n{i}", latency=100.0 + i * 10) for i in range(10)]
        first = pick_for_affinity(nodes, "account-token-abc", spread=3)
        again = pick_for_affinity(nodes, "account-token-abc", spread=3)
        assert first is not None and first is again

    def test_keys_distribute_across_candidates(self):
        nodes = [_node(f"n{i}", latency=100.0 + i * 10) for i in range(6)]
        picks = {
            pick_for_affinity(nodes, f"token-{i}", spread=3).node_id
            for i in range(50)
        }
        assert len(picks) > 1  # not everything lands on one node
        assert picks <= {n.node_id for n in nodes[:3]}  # only top-spread used

    def test_rebind_when_bound_node_dies(self):
        nodes = [_node(f"n{i}", latency=100.0 + i * 10) for i in range(5)]
        bound = pick_for_affinity(nodes, "tok", spread=2)
        nodes[0].state = NodeState.DEAD
        nodes[0].score = 0.0
        rebound = pick_for_affinity(nodes, "tok", spread=2)
        assert rebound is not None and rebound.node_id != bound.node_id

    def test_empty_pool_returns_none(self):
        assert pick_for_affinity([], "tok", spread=3) is None

    def test_affinity_index_stable_range(self):
        for i in range(100):
            idx = affinity_index(f"k{i}", 3)
            assert 0 <= idx < 3
        assert affinity_index("x", 1) == 0


# ---------------------------------------------------------------------------
# Manager-level persistent sticky binding (anti-correlation contract)
# ---------------------------------------------------------------------------

import app.control.proxy.subscription as _sub_pkg
from app.control.proxy.subscription import SubscriptionManager


def _live_node(node_id: str, server: str, score: float) -> SubNode:
    n = SubNode(
        node_id=node_id,
        name=node_id,
        protocol=SubProtocol.SOCKS5,
        server=server,
        port=1080,
        source_id="s1",
        egress_url=f"socks5h://{server}:1080",
        state=NodeState.HEALTHY,
    )
    n.score = score
    n.latency_ms = round(1000.0 / max(score, 0.001), 1)
    return n


class TestPersistentStickyBinding:
    def _mgr(self) -> SubscriptionManager:
        return SubscriptionManager()

    @staticmethod
    def _seed(mgr: SubscriptionManager, scores: dict[str, float]) -> None:
        mgr._merge_nodes(
            "s1",
            [_live_node(nid, f"10.0.0.{i}", sc) for i, (nid, sc) in enumerate(scores.items())],
        )

    @pytest.mark.asyncio
    async def test_binding_survives_score_reorder(self, monkeypatch):
        """THE anti-correlation fix: score drift must never reshuffle a live binding."""
        mgr = self._mgr()
        self._seed(mgr, {"A": 9.0, "B": 5.0, "C": 1.0})
        first = mgr.pick_for_account("acct-1")
        assert first is not None
        # Simulate a speedtest where B becomes fastest and A drops to last.
        mgr._nodes["A"].score = 0.5
        mgr._nodes["B"].score = 9.9
        mgr._nodes["C"].score = 2.0
        again = mgr.pick_for_account("acct-1")
        assert again.node_id == first.node_id, (
            "account hopped nodes after score reorder — stickiness broken"
        )

    @pytest.mark.asyncio
    async def test_rebind_only_when_node_unusable(self):
        mgr = self._mgr()
        self._seed(mgr, {"A": 8.0, "B": 4.0})
        bound = mgr.pick_for_account("acct-2")
        mgr._nodes[bound.node_id].state = NodeState.DEAD
        mgr._nodes[bound.node_id].egress_url = ""
        rebound = mgr.pick_for_account("acct-2")
        assert rebound is not None and rebound.node_id != bound.node_id
        assert mgr._affinity["acct-2"] == rebound.node_id

    @pytest.mark.asyncio
    async def test_binding_persists_across_restart(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_sub_pkg, "data_path", lambda name: tmp_path / name)
        mgr = self._mgr()
        self._seed(mgr, {"A": 7.0, "B": 3.0})
        chosen = mgr.pick_for_account("acct-3")
        await mgr.persist()

        mgr2 = self._mgr()
        await mgr2.restore()
        self._seed(mgr2, {"A": 7.0, "B": 3.0})
        assert mgr2.pick_for_account("acct-3").node_id == chosen.node_id

    @pytest.mark.asyncio
    async def test_stale_restored_binding_pruned_on_pick(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_sub_pkg, "data_path", lambda name: tmp_path / name)
        mgr = self._mgr()
        self._seed(mgr, {"A": 7.0, "B": 3.0})
        chosen = mgr.pick_for_account("acct-4")
        await mgr.persist()

        # Restart into a world where exactly the bound node vanished.
        survivor = "B" if chosen.node_id == "A" else "A"
        mgr2 = self._mgr()
        await mgr2.restore()
        self._seed(mgr2, {"A": 7.0, "B": 3.0})
        del mgr2._nodes[chosen.node_id]
        picked = mgr2.pick_for_account("acct-4")
        # Invariant: stale binding pruned, account re-bound to the survivor.
        assert picked is not None and picked.node_id == survivor
        assert mgr2._affinity["acct-4"] == survivor


class TestFailClosedOnEmptyPool:
    @staticmethod
    def _dir() -> "object":
        from app.control.proxy import ProxyDirectory
        from app.control.proxy.models import EgressMode

        d = ProxyDirectory()
        d._egress_mode = EgressMode.SUBSCRIPTION
        return d

    @pytest.mark.asyncio
    async def test_empty_pool_raises_by_default(self, monkeypatch):
        empty = SubscriptionManager()
        monkeypatch.setattr(
            _sub_pkg, "get_subscription_manager", lambda: empty
        )
        with pytest.raises(RuntimeError, match="no usable nodes"):
            await self._dir()._pick_proxy_url(affinity_key="tok-x")

    @pytest.mark.asyncio
    async def test_direct_fallback_escape_hatch(self, monkeypatch):
        empty = SubscriptionManager()
        monkeypatch.setattr(
            _sub_pkg, "get_subscription_manager", lambda: empty
        )

        class _Cfg:
            def get_bool(self, key, default=False):
                return True  # allow_direct_fallback=true

        import app.control.proxy as _proxy_pkg

        monkeypatch.setattr(_proxy_pkg, "get_config", lambda: _Cfg())
        assert await self._dir()._pick_proxy_url(affinity_key="tok-x") is None
