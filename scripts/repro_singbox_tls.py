"""Reproduce the Zeabur chat failure locally: sing-box + knjc vless Reality node."""
import asyncio
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import certifi
from curl_cffi.const import CurlOpt
from curl_cffi.requests import AsyncSession

SB = r"D:\OpenClawTemp\singbox\sing-box-1.12.9-windows-amd64\sing-box.exe"

cfg = {
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
        }
    ],
    "route": {"rules": [{"inbound": ["in"], "outbound": "out"}], "final": "direct"},
}

cfg["outbounds"].append({"type": "direct", "tag": "direct"})


async def probe(imp, url):
    try:
        kw = {
            "proxies": {"http": "http://127.0.0.1:21099", "https": "http://127.0.0.1:21099"},
            "timeout": 15,
            "curl_options": {CurlOpt.CAINFO: str(CA)},
        }
        if imp:
            kw["impersonate"] = imp
        async with AsyncSession(**kw) as s:
            r = await s.post(url, json={"modelName": "fast"})
            return f"{r.status_code} {r.text[:80]}"
    except Exception as e:
        return f"ERR {str(e)[:130]}"


tmp = Path(tempfile.mkdtemp(prefix="sb-repro-"))
(tmp / "cfg.json").write_text(json.dumps(cfg), encoding="utf-8")
proc = subprocess.Popen([SB, "run", "-c", str(tmp / "cfg.json")],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
CA = Path(tempfile.gettempdir()) / "grok2api-cacert.pem"
if not CA.exists():
    shutil.copyfile(certifi.where(), CA)


async def main():
    time.sleep(2.0)
    for imp in (None, "chrome", "chrome136"):
        r = await probe(imp, "https://grok.com/rest/rate-limits")
        print(f"imp={imp}: {r}")


try:
    asyncio.run(main())
finally:
    proc.terminate()
