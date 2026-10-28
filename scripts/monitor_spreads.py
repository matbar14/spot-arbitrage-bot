#!/usr/bin/env python3
"""Live spread monitoring across exchange linkages (dry-run, read-only).

Starts the bot's web scanner headlessly for one linkage (ARB_PAIR), polls
/api/snapshot, accumulates per-symbol+direction statistics and prints an
aggregated report. No orders are ever placed.

Usage from the repository root:
    python scripts/monitor_spreads.py --pair mexc-nonkyc --port 8791 \
        --min-cycles 10 --max-seconds 420

Results are merged into spread_monitor_results.json (see --out).
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections import defaultdict

import aiohttp

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(REPO, "spread_monitor_results.json")

POLL_EVERY_S = 10


def start_server(pair: str, port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": os.path.join(REPO, "src"),
        "LIVE_TRADING": "false",
        "SYMBOLS": "ALL",
        "MAX_SYMBOLS": "0",
        "ARB_PAIR": pair,
        "POLL_INTERVAL_SECONDS": "2",
        "MIN_NET_EDGE_BPS": "15",       # catch more candidates for analysis
        "NO_BROWSER": "true",
        "WEB_PORT": str(port),
    })
    return subprocess.Popen(
        [sys.executable, "-m", "arbitrage_bot.web", "--no-browser", "--port", str(port)],
        cwd=REPO, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


class SymStats:
    __slots__ = ("count", "edges", "max_row", "signal_hits", "best_age")

    def __init__(self):
        self.count = 0
        self.edges = []
        self.max_row = None      # row dict with the highest edge seen
        self.signal_hits = 0     # polls where edge >= min signal bps
        self.best_age = None

    def add(self, row: dict, signal_bps: float):
        self.count += 1
        e = row["net_edge_bps"]
        self.edges.append(e)
        if self.max_row is None or e > self.max_row["net_edge_bps"]:
            self.max_row = row
            self.best_age = row.get("book_age_s")
        if e >= signal_bps:
            self.signal_hits += 1


async def wait_up(session: aiohttp.ClientSession, port: int, timeout=60.0) -> bool:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/api/session"
    while time.monotonic() < deadline:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return False


async def monitor_linkage(pair: str, port: int, min_cycles: int, max_seconds: float,
                          results: dict):
    print(f"\n=== {pair} ===", flush=True)
    proc = start_server(pair, port)
    stats: dict = defaultdict(SymStats)
    errors = []
    try:
        async with aiohttp.ClientSession() as http:
            if not await wait_up(http, port):
                errors.append("server did not start")
                return
            base = f"http://127.0.0.1:{port}"
            async with http.post(f"{base}/api/scan/start") as r:
                if r.status != 200:
                    errors.append(f"scan start: HTTP {r.status}")
                    return
            started = time.monotonic()
            cycles_seen = 0
            polls = 0
            while time.monotonic() - started < max_seconds:
                await asyncio.sleep(POLL_EVERY_S)
                try:
                    async with http.get(f"{base}/api/snapshot",
                                        timeout=aiohttp.ClientTimeout(total=5)) as r:
                        snap = await r.json()
                except Exception as exc:
                    errors.append(f"snapshot: {exc}")
                    continue
                cycles_seen = max(cycles_seen, snap.get("cycles", 0))
                if snap.get("last_error"):
                    errors.append(f"scan: {snap['last_error']}")
                for row in snap.get("rows", []):
                    key = f"{row['symbol']}|{row['buy']}->{row['sell']}"
                    stats[key].add(row, signal_bps=15.0)
                polls += 1
                n_pos = sum(1 for s in stats.values() if max(s.edges) > 0)
                print(f"  poll {polls}: cycles={cycles_seen} combos_seen={len(stats)} positive={n_pos}",
                      flush=True)
                if cycles_seen >= min_cycles and polls >= 3:
                    break
            async with http.post(f"{base}/api/scan/stop") as r:
                pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # aggregate
    entries = []
    for key, st in stats.items():
        symbol, direction = key.split("|", 1)
        edges = st.edges
        row = st.max_row or {}
        qty = row.get("quantity") or 0.0
        price = row.get("buy_price") or 0.0
        entries.append({
            "symbol": symbol,
            "direction": direction,
            "buy": row.get("buy"),
            "sell": row.get("sell"),
            "max_edge_bps": max(edges),
            "avg_edge_bps": round(sum(edges) / len(edges), 2),
            "min_edge_bps": min(edges),
            "samples": st.count,
            "signal_hits": st.signal_hits,
            "last_quantity": qty,
            "last_volume_usdt": round(qty * price, 2),
            "book_age_s": st.best_age,
            "buy_price": row.get("buy_price"),
            "sell_price": row.get("sell_price"),
        })
    entries.sort(key=lambda e: e["max_edge_bps"], reverse=True)
    results[pair] = {
        "cycles": cycles_seen,
        "polls": polls,
        "errors": errors[-5:],
        "combos_seen": len(entries),
        "rows": entries,
    }
    pos = [e for e in entries if e["max_edge_bps"] > 0]
    sig = [e for e in pos if e["signal_hits"] > 0]
    print(f"  -> combos with any positive edge: {len(pos)}; with signal-level hits: {len(sig)}",
          flush=True)


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pair", required=True, help="exchange linkage, e.g. mexc-nonkyc")
    ap.add_argument("--port", type=int, required=True, help="local port for the scanner")
    ap.add_argument("--min-cycles", type=int, default=3)
    ap.add_argument("--max-seconds", type=float, default=420)
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()

    results = {}
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as fh:
                results = json.load(fh)
        except Exception:
            results = {}
    t0 = time.monotonic()
    try:
        await monitor_linkage(args.pair, args.port, args.min_cycles, args.max_seconds, results)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        results[args.pair] = {"error": str(exc)}
    elapsed = time.monotonic() - t0
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    print(f"\nDone in {elapsed/60:.1f} min -> {args.out}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
