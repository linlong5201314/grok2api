"""Speed-test scoring and affinity binding tests (no network access)."""


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
