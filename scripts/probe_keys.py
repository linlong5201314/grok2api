"""Probe Grok SSO tokens against the upstream rate-limits endpoint.

Uses the project's own transport (build_http_headers + post_json) so the
probe is identical to what the account refresh service does. Classifies each
token as: OK (quota returned) / INVALID (invalid-credentials markers) /
CHALLENGE (Cloudflare) / RATE_LIMITED / ERROR.

Usage:
  uv run python scripts/probe_keys.py <keys_file> [--limit N] [--concurrency C] [--out file]
"""

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _ascii_ca_bundle() -> str:
    """libcurl on Windows fails to fopen CA paths containing non-ASCII
    characters (this checkout lives under a Chinese-named directory), so
    copy certifi's bundle to a pure-ASCII temp location once."""
    import certifi

    src = certifi.where()
    try:
        src.encode("ascii")
        return src
    except UnicodeEncodeError:
        pass
    dst = Path(tempfile.gettempdir()) / "grok2api-cacert.pem"
    if not dst.exists():
        shutil.copyfile(src, dst)
    return str(dst)


_CAINFO = _ascii_ca_bundle()
os.environ.setdefault("SSL_CERT_FILE", _CAINFO)

from curl_cffi.const import CurlOpt

from app.dataplane.proxy.adapters.headers import build_http_headers
from app.dataplane.proxy.adapters.session import get_session_pool
from app.dataplane.reverse.protocol.xai_usage import (
    is_invalid_credentials_error,
    parse_rate_limits,
)


async def probe_one(
    token: str,
    proxy_url: str = "",
    timeout_s: float = 20.0,
    cf_cookies: str = "",
    cf_ua: str = "",
) -> tuple[str, str]:
    """Return (status, detail) for one token."""
    import orjson
    from app.platform.errors import UpstreamError

    headers = build_http_headers(
        token,
        content_type="application/json",
        origin="https://grok.com",
        referer="https://grok.com/",
    )
    if cf_cookies:
        # cf_clearance from FlareSolverr is IP+UA bound — override both the
        # cookie jar and the UA to match the solving browser exactly.
        headers["Cookie"] = f"sso={token}; sso-rw={token}; {cf_cookies}"
        if cf_ua:
            headers["User-Agent"] = cf_ua
    pool = get_session_pool()
    extra = {"proxies": {"http": proxy_url, "https": proxy_url}} if proxy_url else {}
    extra["curl_options"] = {CurlOpt.CAINFO: _CAINFO}
    pooled = await pool.acquire(**extra)
    try:
        async with pooled as session:
            try:
                response = await asyncio.wait_for(
                    session.post(
                        "https://grok.com/rest/rate-limits",
                        headers=headers,
                        data=orjson.dumps({"modelName": "fast"}),
                    ),
                    timeout=timeout_s,
                )
                body_bytes = response.content
                if response.status_code not in (200, 201, 204):
                    body_text = body_bytes.decode("utf-8", "replace")[:300]
                    raise UpstreamError(
                        f"HTTP {response.status_code}",
                        status=response.status_code,
                        body=body_text,
                    )
                body = orjson.loads(body_bytes) if body_bytes.strip() else {}
            except asyncio.TimeoutError:
                pooled.discard()
                return ("ERROR", "timeout")
            except UpstreamError as exc:
                if is_invalid_credentials_error(exc):
                    return ("INVALID", f"{exc.status}: {str(exc.details.get('body',''))[:80]}")
                if exc.status == 429:
                    return ("RATE_LIMITED", "")
                if exc.status == 403:
                    return ("CHALLENGE", str(exc.details.get("body", ""))[:80])
                return ("ERROR", f"http {exc.status}: {str(exc.details.get('body',''))[:80]}")
            except Exception as exc:
                return ("ERROR", f"{type(exc).__name__}: {str(exc)[:100]}")

        data = parse_rate_limits(body)
        if data is None:
            return ("ERROR", f"no quota fields: {str(body)[:100]}")
        return (
            "OK",
            f"fast remaining={data['remaining']}/{data['total']} window={data['window_seconds']}s",
        )
    except Exception as exc:
        return ("ERROR", f"{type(exc).__name__}: {str(exc)[:100]}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("keys_file")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--proxy", default="")
    parser.add_argument("--cf-cookies", default="", help="extra cookies from FlareSolverr (IP+UA bound)")
    parser.add_argument("--cf-ua", default="", help="User-Agent that solved the challenge")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    keys_path = Path(args.keys_file)
    keys = [
        line.strip()
        for line in keys_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if args.limit:
        keys = keys[: args.limit]
    total = len(keys)
    print(f"loaded {total} tokens from {keys_path}", flush=True)

    sem = asyncio.Semaphore(args.concurrency)
    results: dict[str, str] = {}
    done = 0
    t0 = time.monotonic()

    async def worker(tok: str) -> None:
        nonlocal done
        async with sem:
            status, detail = await probe_one(
                tok,
                proxy_url=args.proxy,
                cf_cookies=args.cf_cookies,
                cf_ua=args.cf_ua,
            )
            results[tok] = f"{status}\t{detail}"
            done += 1
            if done % 10 == 0 or done == total:
                ok = sum(1 for v in results.values() if v.startswith("OK"))
                print(f"progress {done}/{total} ok={ok}", flush=True)

    await asyncio.gather(*(worker(t) for t in keys))

    elapsed = time.monotonic() - t0
    buckets: dict[str, list[str]] = {}
    for tok, val in results.items():
        buckets.setdefault(val.split("\t", 1)[0], []).append(tok)

    print("\n========== summary ==========")
    for status, toks in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        print(f"{status:14s} {len(toks)}")
    print(f"elapsed {elapsed:.1f}s  concurrency={args.concurrency}")

    ok_keys = buckets.get("OK", [])
    if args.out and ok_keys:
        Path(args.out).write_text("\n".join(ok_keys) + "\n", encoding="utf-8")
        print(f"wrote {len(ok_keys)} usable keys -> {args.out}")

    # show a few error details for diagnosis
    for status in ("INVALID", "CHALLENGE", "RATE_LIMITED", "ERROR"):
        toks = buckets.get(status, [])
        if toks:
            sample = toks[0]
            print(f"sample {status}: {results[sample]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
