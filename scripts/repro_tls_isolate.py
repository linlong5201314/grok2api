"""Isolate which chat-specific session option produces 'invalid library' on the
sing-box + vless path: stream=True, CAINFO, impersonate."""
import asyncio
import json
import subprocess
import tempfile
import time
from pathlib import Path

import certifi
import shutil
from curl_cffi.const import CurlOpt
from curl_cffi.requests import AsyncSession

SB = r"D:\OpenClawTemp\singbox\sing-box-1.12.9-windows-amd64\sing-box.exe"

CFG = {
    "log": {"level": "warn"},
    "inbounds": [{"type": "mixed", "tag": "in", "listen": "127.0.0.1", "listen_port": 21099}],
    "outbounds": [
        {
            "type": "vless",
            "tag": "out",
            "server": "134.195.101.187",
            "server_port": 443,
            "uuid": "c83174f8-ccff-4dfe-bb55-1e96d3ad98a4",
            "flow": "xtls-rprx-vision",
            "tls": {
                "enabled": True,
                "server_name": "iosapps.itunes.apple.com",
                "utls": {"enabled": True, "fingerprint": "ios"},
                "reality": {
                    "enabled": True,
                    "public_key": "PuA87-QwgLpJr4WyqC0e2aMFShkeovMT6recdpHPaSc",
                    "short_id": "67af2e70",
                },
            },
        },
        {"type": "direct", "tag": "direct"},
    ],
    "route": {"rules": [{"inbound": ["in"], "outbound": "out"}], "final": "direct"},
}

CA_GOOD = Path(tempfile.gettempdir()) / "grok2api-cacert.pem"
if not CA_GOOD.exists():
    shutil.copyfile(certifi.where(), CA_GOOD)
CA_BAD = "/nonexistent/cacert.pem"


async def probe(label, **kw):
    try:
        base = {
            "proxies": {"http": "http://127.0.0.1:21099", "https": "http://127.0.0.1:21099"},
            "timeout": 15,
        }
        base.update(kw)
        async with AsyncSession(**base) as s:
            r = await s.post(
                "https://grok.com/rest/app-chat/conversations/new",
                json={"message": "hi", "modeId": "fast"},
                stream=True,
            )
            print(f"{label:34s} -> {r.status_code}")
    except Exception as e:
        print(f"{label:34s} -> ERR {str(e)[:100]}")


async def main():
    time.sleep(2.0)
    await probe("stream only")
    await probe("stream + CAINFO(good)", curl_options={CurlOpt.CAINFO: str(CA_GOOD)})
    await probe("stream + CAINFO(bad)", curl_options={CurlOpt.CAINFO: CA_BAD})
    await probe("stream + imp=chrome + CAINFO(good)", impersonate="chrome",
                curl_options={CurlOpt.CAINFO: str(CA_GOOD)})


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="sb-iso-"))
    (tmp / "cfg.json").write_text(json.dumps(CFG), encoding="utf-8")
    proc = subprocess.Popen([SB, "run", "-c", str(tmp / "cfg.json")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        asyncio.run(main())
    finally:
        proc.terminate()
