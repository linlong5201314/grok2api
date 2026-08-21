"""End-to-end tests for the admin subscriptions API (no real network).

Boots the FastAPI app via TestClient (lifespan not executed — subscription
endpoints depend only on module singletons) and drives the full source
CRUD + node listing cycle with the fetch step stubbed out.
"""

import pytest
from fastapi.testclient import TestClient

from app.control.proxy.subscription import get_subscription_manager
from app.control.proxy.subscription.models import SubNode, SubProtocol


@pytest.fixture()
def client(monkeypatch):
    # Import after env is clean; default app_key is "grok2api".
    from app.main import app

    manager = get_subscription_manager()

    async def _fake_fetch(source):
        node = SubNode(
            node_id=f"{source.source_id}-n1",
            name="测试节点",
            protocol=SubProtocol.SOCKS5,
            server="5.6.7.8",
            port=1080,
            credential="pw",
            source_id=source.source_id,
            egress_url="socks5h://5.6.7.8:1080",
        )
        source.last_fetch_ok = True
        source.node_count = 1
        manager._merge_nodes(source.source_id, [node])
        from app.control.proxy.subscription import SubscriptionFetchResult

        return SubscriptionFetchResult(source_id=source.source_id, ok=True, node_count=1)

    async def _fake_rebuild():
        for n in manager._nodes.values():
            n.state = n.state or "new"

    monkeypatch.setattr(manager, "fetch_source", _fake_fetch)
    monkeypatch.setattr(manager, "_rebuild_egress", _fake_rebuild)

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, manager


AUTH = {"Authorization": "Bearer grok2api"}


class TestSubscriptionsAPI:
    def test_auth_required(self, client):
        c, _ = client
        r = c.get("/admin/api/proxy/subscriptions")
        assert r.status_code == 401

    def test_full_source_lifecycle(self, client):
        c, mgr = client
        # add
        r = c.post(
            "/admin/api/proxy/subscriptions",
            json={"name": "测试机场", "url": "https://sub.example.com/t=1"},
            headers=AUTH,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "success"
        assert body["fetch"]["ok"] is True
        sid = body["source"]["source_id"]
        assert body["source"]["masked_url"].startswith("https://sub.example.com")

        # list shows source + stats
        r = c.get("/admin/api/proxy/subscriptions", headers=AUTH)
        data = r.json()
        assert any(s["source_id"] == sid for s in data["sources"])
        assert data["stats"]["node_count"] >= 1

        # nodes ranked and redacted
        r = c.get("/admin/api/proxy/nodes", headers=AUTH)
        nodes = r.json()["nodes"]
        assert len(nodes) == 1
        blob = str(nodes[0])
        assert "pw" not in blob            # credential hidden
        assert "5.6.7.8" not in blob       # host masked

        # patch enable flag
        r = c.patch(
            f"/admin/api/proxy/subscriptions/{sid}",
            json={"enabled": False},
            headers=AUTH,
        )
        assert r.status_code == 200
        assert r.json()["source"]["enabled"] is False

        # delete
        r = c.delete(f"/admin/api/proxy/subscriptions/{sid}", headers=AUTH)
        assert r.status_code == 200
        r = c.get("/admin/api/proxy/subscriptions", headers=AUTH)
        assert all(s["source_id"] != sid for s in r.json()["sources"])

    def test_add_rejects_non_http_url(self, client):
        c, _ = client
        r = c.post(
            "/admin/api/proxy/subscriptions",
            json={"name": "x", "url": "ftp://bad.example.com"},
            headers=AUTH,
        )
        assert r.status_code == 400

    def test_delete_unknown_returns_404(self, client):
        c, _ = client
        r = c.delete("/admin/api/proxy/subscriptions/nope", headers=AUTH)
        assert r.status_code == 404

    def test_security_headers_present(self, client):
        c, _ = client
        r = c.get("/health")
        assert r.headers.get("x-content-type-options") == "nosniff"
