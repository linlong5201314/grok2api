"""Replicate the EXACT chat request shape (payload + headers + session kwargs)
through a local sing-box + knjc vless Reality node."""
import asyncio
import json
import subprocess
import tempfile
import time
from pathlib import Path

SB = r"D:\OpenClawTemp\singbox\sing-box-1.12.9-windows-amd64\sing-box.exe"
CFG = {
    "log": {"level": "warn"},
    "inbounds": [{"type": "mixed", "tag": "in", "listen": "127.0.0.1", "listen_port": 21099}],
    "outbounds": [
        {
            "type": "vless", "tag": "out",
            "server": "134.195.101.187", "server_port": 443,
            "uuid": "c83174f8-ccff-4dfe-bb55-1e96d3ad98a4",
            "flow": "xtls-rprx-vision",
            "tls": {
                "enabled": True, "server_name": "iosapps.itunes.apple.com",
                "utls": {"enabled": True, "fingerprint": "ios"},
                "reality": {"enabled": True,
                            "public_key": "PuA87-QwgLpJr4WyqC0e2aMFShkeovMT6recdpHPaSc",
                            "short_id": "67af2e70"},
            },
        },
        {"type": "direct", "tag": "direct"},
    ],
    "route": {"rules": [{"inbound": ["in"], "outbound": "out"}], "final": "direct"},
}


async def main() -> None:
    import orjson
    from app.dataplane.reverse.protocol.xai_chat import build_chat_payload
    from app.dataplane.proxy.adapters.headers import build_http_headers
    from app.dataplane.proxy.adapters.session import build_session_kwargs, ResettableSession
    from app.control.model.enums import ModeId

    token = "eyJ0eXAiOiJKV1Qi.fake-token-for-repro"
    payload = build_chat_payload(message="hi", mode_id=ModeId.FAST)
    payload_bytes = orjson.dumps(payload)
    headers = build_http_headers(
        token,
        content_type="application/json",
        origin="https://grok.com",
        referer="https://grok.com/",
    )
    kwargs = build_session_kwargs()
    kwargs["proxies"] = {"http": "http://127.0.0.1:21099", "https": "http://127.0.0.1:21099"}

    async with ResettableSession(**kwargs) as session:
        try:
            response = await asyncio.wait_for(
                session.post(
                    "https://grok.com/rest/app-chat/conversations/new",
                    headers=headers,
                    data=payload_bytes,
                    stream=True,
                ),
                timeout=20,
            )
            print("status:", response.status_code)
            body = response.content[:200] if response.status_code != 200 else b"<stream ok>"
            print("body head:", body[:150])
        except Exception as exc:
            print("ERR:", type(exc).__name__, str(exc)[:200])


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="sb-exact-"))
    (tmp / "cfg.json").write_text(json.dumps(CFG), encoding="utf-8")
    proc = subprocess.Popen([SB, "run", "-c", str(tmp / "cfg.json")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2.0)
        asyncio.run(main())
    finally:
        proc.terminate()
