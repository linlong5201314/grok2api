"""Find a Clash egress node that passes Cloudflare on grok.com.

Iterates leaf proxies via the Clash external-controller API, switches GLOBAL
to each candidate, and probes POST /rest/rate-limits with one sample token.
A node is usable when the response is NOT the CF "Just a moment" challenge.

Usage:
  uv run python scripts/find_grok_node.py <keys_file>
Env:
  CLASH_API (default http://127.0.0.1:9097), CLASH_SECRET, LOCAL_PROXY (default http://127.0.0.1:7897)
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

CLASH_API = os.environ.get("CLASH_API", "http://127.0.0.1:9097").rstrip("/")
CLASH_SECRET = os.environ.get("CLASH_SECRET", "set-your-secret")
LOCAL_PROXY = os.environ.get("LOCAL_PROXY", "http://127.0.0.1:7897")

GROUP_TYPES = {"Selector", "URLTest", "Fallback", "Direct", "Reject", "Compatible", "Relay"}


def _req(method: str, url: str, *, body=None, headers=None, timeout=10, proxy=None):
    req = urllib.request.Request(url, method=method)
    if CLASH_SECRET and url.startswith(CLASH_API):
        req.add_header("Authorization", f"Bearer {CLASH_SECRET}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    data = None
    if body is not None:
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    try:
        with opener.open(req, data=data, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return -1, str(exc)


def main(keys_file: str) -> int:
    token = ""
    for line in open(keys_file, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            token = line
            break
    if not token:
        print("no token in keys file")
        return 1

    st, body = _req("GET", f"{CLASH_API}/proxies")
    proxies = json.loads(body)["proxies"]
    global_now = proxies.get("GLOBAL", {}).get("now", "")
    print(f"current GLOBAL node: {global_now}")

    candidates = []
    for name, info in proxies.items():
        if info.get("type") in GROUP_TYPES:
            continue
        if "到期" in name or "流量" in name or "官网" in name:
            continue
        candidates.append(name)
    print(f"leaf candidates: {len(candidates)}")

    usable: list[str] = []
    tried = 0
    for name in candidates:
        tried += 1
        st, _ = _req("PUT", f"{CLASH_API}/proxies/GLOBAL", body={"name": name}, timeout=5)
        if st not in (200, 204):
            print(f"[{tried}/{len(candidates)}] switch failed: {name} ({st})")
            continue
        time.sleep(0.4)
        st, resp_body = _req(
            "POST",
            "https://grok.com/rest/rate-limits",
            body=json.dumps({"modelName": "fast"}).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": "https://grok.com",
                "Cookie": f"sso={token}; sso-rw={token}",
            },
            timeout=8,
            proxy=LOCAL_PROXY,
        )
        if st in (200, 201):
            print(f"[{tried}] OK-CF-PASS {name} -> {st}")
            usable.append(name)
        elif "Just a moment" in resp_body:
            print(f"[{tried}] challenge {name}")
        elif "invalid-credentials" in resp_body.lower() or "bad-credentials" in resp_body.lower():
            # Cloudflare passed; token itself is dead — node is still usable
            print(f"[{tried}] CF-PASS (token invalid) {name}")
            usable.append(name)
        else:
            snippet = resp_body[:60].replace("\n", " ")
            print(f"[{tried}] {st} {name} {snippet}")

    print("\n===== usable nodes (CF pass) =====")
    for n in usable:
        print(" ", n)

    # restore original selection
    if global_now:
        _req("PUT", f"{CLASH_API}/proxies/GLOBAL", body={"name": global_now}, timeout=5)
        print(f"restored GLOBAL -> {global_now}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: uv run python scripts/find_grok_node.py <keys_file>")
        raise SystemExit(1)
    raise SystemExit(main(sys.argv[1]))
