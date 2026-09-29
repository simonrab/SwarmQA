#!/usr/bin/env python3
"""Time /observe against a running swarm runner. Standard library only.

    python3 agents/swarm-runner/bench_observe.py --bundle-id com.apple.Preferences
    python3 agents/swarm-runner/bench_observe.py --port 8766 --bundle-id dev.swarmqa.PlantedBugs -n 50 --format jpeg

Launches the bundle through POST /launch (unless --no-launch), makes one
warm-up call, then times N POST /observe calls end to end (request to fully
read response) and prints p50/p95/max in milliseconds. Exits 1 if p95 is
over --budget-ms (default 300, the protocol's budget).
"""

from __future__ import annotations

import argparse
import http.client
import json
import statistics
import sys
import time


class Client:
    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.conn = http.client.HTTPConnection(host, port, timeout=timeout)

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"X-Swarm-Protocol": "1"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        self.conn.request(method, path, body=payload, headers=headers)
        response = self.conn.getresponse()
        return response.status, response.read()


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    k = (len(ordered) - 1) * pct / 100.0
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def count(elements: list[dict]) -> int:
    return sum(1 + count(e.get("children", [])) for e in elements)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bundle-id", help="app to launch before timing")
    parser.add_argument("--no-launch", action="store_true", help="observe whatever is already launched")
    parser.add_argument("-n", "--count", type=int, default=30)
    parser.add_argument("--format", choices=["png", "jpeg", "none"], default="png")
    parser.add_argument("--jpeg-quality", type=float, default=0.7)
    parser.add_argument("--budget-ms", type=float, default=300.0)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    client = Client(args.host, args.port, args.timeout)
    status, body = client.call("GET", "/health")
    if status != 200:
        print(f"health failed: {status} {body[:200]!r}", file=sys.stderr)
        return 2
    print(f"health: {body.decode()}")

    if not args.no_launch:
        if not args.bundle_id:
            parser.error("--bundle-id is required unless --no-launch")
        start = time.perf_counter()
        status, body = client.call("POST", "/launch", {"bundle_id": args.bundle_id, "args": [], "env": {}, "terminate_existing": True})
        if status != 200:
            print(f"launch failed: {status} {body[:300]!r}", file=sys.stderr)
            return 2
        print(f"launch {args.bundle_id}: {(time.perf_counter() - start) * 1000:.0f} ms")

    request = {"screenshot": args.format, "jpeg_quality": args.jpeg_quality}
    status, body = client.call("POST", "/observe", request)  # warm-up
    if status != 200:
        print(f"observe failed: {status} {body[:300]!r}", file=sys.stderr)
        return 2
    first = json.loads(body)
    shot = first.get("screenshot")
    print(
        f"observe shape: {count(first['elements'])} elements, size={first.get('size')}, "
        f"scale={first.get('scale')}, screenshot={shot['format'] if shot else None}, "
        f"response={len(body) / 1024:.0f} KiB"
    )

    timings: list[float] = []
    for _ in range(args.count):
        start = time.perf_counter()
        status, body = client.call("POST", "/observe", request)
        timings.append((time.perf_counter() - start) * 1000)
        if status != 200:
            print(f"observe failed: {status} {body[:300]!r}", file=sys.stderr)
            return 2

    p50, p95, worst = percentile(timings, 50), percentile(timings, 95), max(timings)
    print(
        f"/observe x{args.count} ({args.format}): p50={p50:.0f} ms  p95={p95:.0f} ms  "
        f"max={worst:.0f} ms  mean={statistics.fmean(timings):.0f} ms"
    )
    if p95 > args.budget_ms:
        print(f"p95 over budget ({args.budget_ms:.0f} ms)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
