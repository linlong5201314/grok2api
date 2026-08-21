"""Subscription parser tests — base64 URI lists, Clash YAML, edge cases."""

import base64
import json


from app.control.proxy.subscription.parsers import (
    parse_clash_yaml,
    parse_subscription_payload,
)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _vmess_uri(**overrides) -> str:
    payload = {
        "v": "2",
        "ps": "HK-01",
        "add": "hk01.example.com",
        "port": "443",
        "id": "a3c1e2b4-1234-5678-9abc-def012345678",
        "aid": "0",
        "scy": "auto",
        "net": "ws",
        "type": "none",
        "host": "cdn.example.com",
        "path": "/wss",
        "tls": "tls",
        "sni": "hk01.example.com",
    }
    payload.update(overrides)
    return "vmess://" + _b64(json.dumps(payload))


class TestUriList:
    def test_base64_body_parses_all_protocols(self):
        links = "\n".join(
            [
                _vmess_uri(),
                "ss://" + base64.b64encode(b"aes-128-gcm:pw@1.2.3.4:8388").decode() + "#SS",
                "trojan://pw@example.com:443?sni=example.com#TJ",
                "socks://user:pass@5.6.7.8:1080#SK",
                "http://p.example.net:8080#HTTP",
                "hysteria2://auth@9.9.9.9:36712/?sni=hy.example.com#HY2",
            ]
        )
        nodes = parse_subscription_payload(_b64(links), source_id="s1")
        protocols = {n.protocol.value for n in nodes}
        assert protocols == {"vmess", "ss", "trojan", "socks5", "http", "hysteria2"}
        assert all(n.node_id for n in nodes)
        assert len({n.node_id for n in nodes}) == len(nodes)

    def test_plain_body_without_base64(self):
        nodes = parse_subscription_payload(
            "http://a.example.com:8080#A\nhttps://b.example.com:8443#B\n",
            source_id="s1",
        )
        assert [n.protocol.value for n in nodes] == ["http", "https"]

    def test_direct_vs_core_classification(self):
        links = "\n".join(
            [
                "http://a.example.com:8080#A",
                "socks5://b.example.com:1080#B",
                _vmess_uri(),
                "trojan://pw@c.example.com:443#C",
            ]
        )
        nodes = parse_subscription_payload(_b64(links), source_id="s1")
        by_proto = {n.protocol.value: n for n in nodes}
        assert by_proto["http"].is_direct and not by_proto["http"].needs_core
        assert by_proto["socks5"].is_direct
        assert by_proto["vmess"].needs_core and not by_proto["vmess"].is_direct
        assert by_proto["trojan"].needs_core

    def test_vmess_fields(self):
        node = parse_subscription_payload(_vmess_uri(), source_id="s1")[0]
        assert node.server == "hk01.example.com"
        assert node.port == 443
        assert node.credential == "a3c1e2b4-1234-5678-9abc-def012345678"
        assert node.transport == "ws"
        assert node.path == "/wss"
        assert node.host_header == "cdn.example.com"
        assert node.name == "HK-01"

    def test_ss_userinfo_variants(self):
        # userinfo-base64 form
        inner = base64.b64encode(b"chacha20-ietf-poly1305:secret@2.3.4.5:1234").decode()
        n1 = parse_subscription_payload(f"ss://{inner}#n1", source_id="s")[0]
        assert (n1.method, n1.credential, n1.server, n1.port) == (
            "chacha20-ietf-poly1305",
            "secret",
            "2.3.4.5",
            1234,
        )
        # plain userinfo form
        n2 = parse_subscription_payload("ss://aes-128-gcm:pw@6.6.6.6:9999#n2", source_id="s")[0]
        assert (n2.method, n2.credential) == ("aes-128-gcm", "pw")

    def test_dedup_by_identity(self):
        link = "http://same.example.com:8080#A\nhttp://same.example.com:8080#B\n"
        nodes = parse_subscription_payload(link, source_id="s")
        assert len(nodes) == 1

    def test_malformed_lines_skipped_not_fatal(self):
        body = "not-a-uri\nhttp://ok.example.com:80#OK\nvmess://!!!invalid-b64!!!\n"
        nodes = parse_subscription_payload(body, source_id="s")
        assert len(nodes) == 1
        assert nodes[0].protocol.value == "http"

    def test_empty_and_garbage_bodies(self):
        assert parse_subscription_payload("", source_id="s") == []
        assert parse_subscription_payload("   \n\n", source_id="s") == []
        assert parse_subscription_payload(_b64("nothing here"), source_id="s") == []


CLASH_YAML = """
mixed-port: 7890
mode: rule
proxies:
  - name: "JP-SS"
    type: ss
    server: jp1.example.com
    port: 8388
    cipher: aes-256-gcm
    password: "sspass"
    udp: true
  - name: "SG-VMESS"
    type: vmess
    server: sg1.example.com
    port: 443
    uuid: b3c1e2b4-1234-5678-9abc-def012345679
    alterId: 0
    cipher: auto
    tls: true
    servername: sg1.example.com
    network: ws
    ws-opts:
      path: /v2ray
      headers:
        Host: sg1.example.com
  - name: TR-DIRECT
    type: trojan
    server: tr.example.com
    port: 443
    password: trojanpw
    skip-cert-verify: true
    alpn:
      - h2
      - http/1.1
proxy-groups: []
rules:
  - MATCH,DIRECT
"""


class TestClashYaml:
    def test_parses_all_items(self):
        nodes = parse_clash_yaml(CLASH_YAML, source_id="clash")
        assert len(nodes) == 3

    def test_ss_node(self):
        node = next(n for n in parse_clash_yaml(CLASH_YAML) if n.name == "JP-SS")
        assert node.protocol.value == "ss"
        assert node.method == "aes-256-gcm"
        assert node.credential == "sspass"
        assert node.udp is True

    def test_nested_ws_opts_stay_nested(self):
        node = next(n for n in parse_clash_yaml(CLASH_YAML) if n.name == "SG-VMESS")
        assert node.transport == "ws"
        assert node.path == "/v2ray"
        assert node.host_header == "sg1.example.com"
        assert node.sni == "sg1.example.com"

    def test_trojan_alpn_list_and_insecure(self):
        node = next(n for n in parse_clash_yaml(CLASH_YAML) if n.name == "TR-DIRECT")
        assert node.alpn == ["h2", "http/1.1"]
        assert node.allow_insecure is True
        assert node.credential == "trojanpw"

    def test_inline_dict_items(self):
        yaml_text = (
            "proxies:\n"
            "  - {name: inline1, server: in.example.com, port: 443, type: trojan, password: pw}\n"
        )
        nodes = parse_clash_yaml(yaml_text, source_id="c")
        assert len(nodes) == 1
        assert nodes[0].server == "in.example.com"
        assert nodes[0].protocol.value == "trojan"

    def test_no_proxies_section(self):
        assert parse_clash_yaml("port: 7890\nmode: rule\n") == []


class TestRedaction:
    def test_redacted_projection_hides_secrets(self):
        import json as _json

        node = parse_subscription_payload(_vmess_uri(), source_id="s")[0]
        data = _json.dumps(node.redacted())
        assert node.credential not in data
        assert node.raw_uri not in data
        assert "hk01.example.com" not in data  # host masked
