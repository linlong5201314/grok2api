import argparse
import json
import sys
import urllib.error
import urllib.request


def request_json(base_url: str, path: str, api_key: str | None = None) -> tuple[int, object]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(base_url.rstrip("/") + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw
        return exc.code, payload


def check(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f" - {detail}"
    print(line)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args()

    passed = True

    status, payload = request_json(args.base_url, "/health")
    passed &= check("health", status == 200 and isinstance(payload, dict) and payload.get("status") == "ok", f"status={status}")

    status, payload = request_json(args.base_url, "/v1/models", args.api_key)
    if status in {401, 403} and not args.api_key:
        check("models", True, "skipped auth-protected endpoint; pass --api-key to validate")
    else:
        passed &= check("models", status == 200 and isinstance(payload, dict) and payload.get("object") == "list", f"status={status}")

    status, payload = request_json(args.base_url, "/v1/images/method", args.api_key)
    if status in {401, 403} and not args.api_key:
        check("images method", True, "skipped auth-protected endpoint; pass --api-key to validate")
    else:
        passed &= check(
            "images method",
            status == 200 and isinstance(payload, dict) and payload.get("image_generation_method") in {"legacy", "imagine_ws_experimental"},
            f"status={status}",
        )

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
