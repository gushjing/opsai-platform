#!/usr/bin/env python3
"""Post-deployment smoke test (standard library only).

Usage:
    python scripts/smoke_test.py --base http://localhost:8000
    python scripts/smoke_test.py --base http://localhost:8000 --password 'YourDemoPassword'
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

TIMEOUT = 15


def request(base, method, path, payload=None, token=None):
    url = base.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        return e.code, {}
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  connection failed: {e}")
        return 0, {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--password", default="OpsAI@Demo2026")
    args = ap.parse_args()

    failures = 0

    status, meta = request(args.base, "GET", "/api/meta")
    ok = status == 200 and isinstance(meta, dict)
    print(f"[{'PASS' if ok else 'FAIL'}] GET /api/meta -> {status}")
    if ok:
        print(f"       version={meta.get('version')} llm={meta.get('llm')} "
              f"mock={meta.get('datasource_mode')}")
    failures += 0 if ok else 1

    tokens = {}
    for username in ("admin", "engineer", "user", "leader"):
        status, body = request(args.base, "POST", "/api/auth/login",
                               {"username": username, "password": args.password})
        ok = status == 200 and bool(body.get("access_token"))
        print(f"[{'PASS' if ok else 'FAIL'}] login {username} -> {status}")
        failures += 0 if ok else 1
        if ok:
            tokens[username] = body["access_token"]

    # Optional: conversation endpoints are checked when available.
    if tokens:
        for username, token in tokens.items():
            status, body = request(args.base, "GET", "/api/chat/history", token=token)
            if status == 404:
                print(f"[SKIP] chat history endpoint not present")
                break
            ok = status == 200
            print(f"[{'PASS' if ok else 'FAIL'}] chat history {username} -> {status}")
            failures += 0 if ok else 1

    print("-" * 48)
    print("ALL PASS" if failures == 0 else f"{failures} CHECK(S) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
