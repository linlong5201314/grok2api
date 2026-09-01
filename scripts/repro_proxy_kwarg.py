"""proxy= vs proxies= dict through a sing-box mixed inbound."""
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

CA = Path(tempfile.gettempdir()) / "grok2api-cacert.pem"
if not CA.exists():
    shutil.copyfile(certifi.where(), CA)


async def t(label, **kw):
    try:
        base = {"timeout": 15, "curl_options": {CurlOpt.CAINFO: str(CA)}}
        base.update(kw)
        async with AsyncSession(**base) as s:
            r = await s.post("https://grok.com/rest/rate-limits", json={"modelName": "fast"})
            print(f"{label:22s} -> {r.status_code}")
    except Exception as e:
        print(f"{label:22s} -> ERR {str(e)[:110]}")


async def main():
    time.sleep(2.0)
    await t("proxy=", proxy="http://127.0.0.1:21099")
    await t("proxies-dict", proxies={"http": "http://127.0.0.1:21099", "https": "http://127.0.0.1:21099"})
    await t("proxies-dict + imp", proxies={"http": "http://127.0.0.1:21099", "https": "http://127.0.0.1:21099"},
            impersonate="chrome")


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="sb-px-"))
    (tmp / "cfg.json").write_text(json.dumps(CFG), encoding="utf-8")
    proc = subprocess.Popen([SB, "run", "-c", str(tmp / "cfg.json")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        asyncio.run(main())
    finally:
        proc.terminate()
