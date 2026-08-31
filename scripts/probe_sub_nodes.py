"""End-to-end: parse a Clash subscription, spin up a standalone mihomo
instance, find nodes that pass Cloudflare on grok.com, then probe all keys
through a passing node.

Usage:
  uv run python scripts/probe_sub_nodes.py <subscription_yaml_file> <keys_file> [--out usable.txt]

Leaves no state behind: the temporary mihomo process is killed on exit.
"""

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

MIHOMO = r"D:\林龙\Clash Verge\verge-mihomo.exe"
API = "http://127.0.0.1:9098"
MIXED_PORT = 7899
LOCAL_PROXY = f"http://127.0.0.1:{MIXED_PORT}"

INFO_NAME_RE = re.compile(r"到期|官网|客服|邀请|注意|流量查询|永久官网")


def _api(method: str, path: str, body=None, timeout=10):
    req = urllib.request.Request(API + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return -1, str(exc)


def _http_get(url: str, timeout=10):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": LOCAL_PROXY, "https": LOCAL_PROXY})
    )
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return -1, str(exc)


def extract_proxies_section(text: str) -> str:
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^proxies\s*:\s*$", line):
            start = i + 1
            break
    if start is None:
        raise ValueError("no proxies: section found")
    out = []
    for line in lines[start:]:
        if line.strip() and re.match(r"^\S", line):
            break
        out.append(line)
    return "\n".join(out)


def extract_names(section: str) -> list[str]:
    names: list[str] = []
    for line in section.splitlines():
        s = line.strip()
        if not s.startswith("- "):
            continue
        m = (
            re.search(r"name\s*:\s*'([^']*)'", s)
            or re.search(r'name\s*:\s*"([^"]*)"', s)
            or re.search(r"name\s*:\s*([^,}]+)", s)
        )
        if m:
            names.append(m.group(1).strip())
    return names


def build_config(section: str, names: list[str]) -> str:
    group_lines = "\n".join(f"      - {json.dumps(n, ensure_ascii=False)}" for n in names)
    return f"""mixed-port: {MIXED_PORT}
allow-lan: false
mode: global
log-level: warning
external-controller: '127.0.0.1:9098'
tcp-concurrent: true
dns:
  enable: true
  nameserver: [223.5.5.5]
proxies:
{section}
proxy-groups:
  - name: grok
    type: select
    proxies:
{group_lines}
"""


def wait_api_up(timeout_s: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        st, _ = _api("GET", "/proxies", timeout=2)
        if st == 200:
            return True
        time.sleep(0.4)
    return False


def cf_challenge_check(sample_token: str) -> str:
    """'pass' | 'challenge' | 'other' — POST one token at rate-limits."""
    req = urllib.request.Request("https://grok.com/rest/rate-limits", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", "https://grok.com")
    req.add_header("Cookie", f"sso={sample_token}; sso-rw={sample_token}")
    body = json.dumps({"modelName": "fast"}).encode()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": LOCAL_PROXY, "https": LOCAL_PROXY})
    )
    try:
        with opener.open(req, data=body, timeout=10) as resp:
            text = resp.read().decode("utf-8", "replace")
            return "pass"
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        if "Just a moment" in text:
            return "challenge"
        return "pass"  # 4xx from upstream (invalid token etc.) means CF passed
    except Exception:
        return "other"


async def run_full_probe(keys_file: str, out_file: str, concurrency: int = 20) -> None:
    from probe_keys import probe_one  # scripts/probe_keys.py

    keys = [
        line.strip()
        for line in Path(keys_file).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, str] = {}
    done = 0
    total = len(keys)

    async def worker(tok: str) -> None:
        nonlocal done
        async with sem:
            status, detail = await probe_one(tok, proxy_url=LOCAL_PROXY)
            results[tok] = f"{status}\t{detail}"
            done += 1
            if done % 25 == 0 or done == total:
                ok = sum(1 for v in results.values() if v.startswith("OK"))
                print(f"probe progress {done}/{total} ok={ok}", flush=True)

    await asyncio.gather(*(worker(t) for t in keys))

    buckets: dict[str, list[str]] = {}
    for tok, val in results.items():
        buckets.setdefault(val.split("\t", 1)[0], []).append(tok)
    print("\n===== key probe summary =====")
    for status, toks in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        print(f"{status:14s} {len(toks)}")
    ok_keys = buckets.get("OK", [])
    if out_file and ok_keys:
        Path(out_file).write_text("\n".join(ok_keys) + "\n", encoding="utf-8")
        print(f"wrote {len(ok_keys)} usable keys -> {out_file}")
    for status in ("INVALID", "CHALLENGE", "RATE_LIMITED", "ERROR"):
        toks = buckets.get(status, [])
        if toks:
            print(f"sample {status}: {results[toks[0]]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sub_file")
    parser.add_argument("keys_file")
    parser.add_argument("--out", default="")
    parser.add_argument("--concurrency", type=int, default=20)
    args = parser.parse_args()

    text = Path(args.sub_file).read_text(encoding="utf-8")
    section = extract_proxies_section(text)
    names = extract_names(section)
    candidates = [n for n in names if not INFO_NAME_RE.search(n)]
    print(f"nodes in subscription: {len(names)}  probe candidates: {len(candidates)}")

    sample_token = next(
        (l.strip() for l in Path(args.keys_file).read_text(encoding="utf-8").splitlines() if l.strip()),
        "",
    )

    tempdir = tempfile.mkdtemp(prefix="mihomo-probe-")
    cfg_path = Path(tempdir) / "config.yaml"
    cfg_path.write_text(build_config(section, names), encoding="utf-8")

    proc = subprocess.Popen(
        [MIHOMO, "-d", tempdir, "-f", str(cfg_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_api_up():
            print("mihomo API did not come up — aborting")
            return 1
        print("mihomo up, scanning nodes for CF pass...")

        passing: list[str] = []
        for i, name in enumerate(candidates, 1):
            st, _ = _api("PUT", "/proxies/grok", {"name": name}, timeout=5)
            if st not in (200, 204):
                print(f"[{i}/{len(candidates)}] switch failed: {name} ({st})")
                continue
            time.sleep(0.35)
            verdict = cf_challenge_check(sample_token)
            mark = {"pass": "PASS-CF", "challenge": "challenge", "other": "net-error"}[verdict]
            print(f"[{i}/{len(candidates)}] {mark:9s} {name}")
            if verdict == "pass":
                passing.append(name)

        print(f"\nCF-passing nodes: {len(passing)}")
        for n in passing:
            print(f"  {n}")

        if not passing:
            print("no usable egress in this subscription — key probe skipped")
            return 2

        # Lock the first passing node and run the full key probe.
        _api("PUT", "/proxies/grok", {"name": passing[0]}, timeout=5)
        time.sleep(0.5)
        print(f"\nrunning full key probe through: {passing[0]}")
        asyncio.run(run_full_probe(args.keys_file, args.out, args.concurrency))
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
