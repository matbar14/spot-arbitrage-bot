"""Headless paper-trading session across ALL linkages (dry-run, no orders).

Boots the web Session in all-linkages mode, enables the live panel in
paper mode (capital per venue, min edge) and drives scan cycles for a
fixed duration. Every signal above the threshold is re-validated on
fresh order books (walk-based executable volume for the configured
capital) before being recorded as a virtual round-trip trade.

Env knobs: PAPER_DURATION_S (default 360), PAPER_MAX_SYMBOLS (120),
PAPER_CAPITAL (100), PAPER_MIN_EDGE (25), ARB_PAIR=all.
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import arbitrage_bot.web as web
from arbitrage_bot.config import Settings
from arbitrage_bot.web import Session

DURATION_S = float(os.environ.get("PAPER_DURATION_S", "360"))
MAX_SYMBOLS = int(os.environ.get("PAPER_MAX_SYMBOLS", "120"))
CAPITAL = float(os.environ.get("PAPER_CAPITAL", "100"))
MIN_EDGE = float(os.environ.get("PAPER_MIN_EDGE", "25"))
OUT_PATH = os.environ.get("PAPER_OUT", "/home/z/my-project/download/paper_trading_report.json")


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


async def main() -> None:
    # Тестовый режим: разрешаем больше виртуальных сделок за чанк — каждая
    # всё равно проходит полную перепроверку на свежих стаканах + кулдаун 300с.
    web.PAPER_MAX_PER_CHUNK = 8

    settings = Settings()
    session = Session(settings)
    log("загружаю рынки всех площадок (all-режим)…")
    await session.configure(pair="all", max_symbols=MAX_SYMBOLS)
    session.configure_live(capital_per_venue=CAPITAL, min_edge_bps=MIN_EDGE, enabled=True)
    venues_alive = sorted(session.market.clients) if session.market else []
    log(f"площадки ({len(venues_alive)}): {', '.join(venues_alive)}")
    log(f"символов в universe: {len(session.active_symbols)} | капитал {CAPITAL} USDT/площадка | порог {MIN_EDGE} bps")

    deadline = time.monotonic() + DURATION_S
    cycle = 0
    while time.monotonic() < deadline:
        started = time.monotonic()
        try:
            await session.scan_once()
        except Exception as exc:
            log(f"cycle error: {exc}")
        cycle += 1
        stats = session.live_stats_payload()
        log(
            f"цикл {cycle} за {time.monotonic() - started:.1f}s | строк {len(session.rows)} | "
            f"сделок {stats['trades_total']} | P&L {stats['total_profit']:.4f} USDT"
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(max(session.poll_interval, 1.0), remaining))

    stats = session.live_stats_payload()
    payload = {
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_s": DURATION_S,
        "config": {
            "pair": session.pair_name,
            "venues_alive": venues_alive,
            "max_symbols": MAX_SYMBOLS,
            "capital_per_venue": CAPITAL,
            "min_edge_bps": MIN_EDGE,
            "cooldown_s": web.PAPER_COOLDOWN_S,
            "slippage_buffer_bps": settings.slippage_buffer_bps,
        },
        "cycles": session.cycles,
        "stats": stats,
        "trades": session.paper_trades,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    log(f"отчёт сохранён: {OUT_PATH}")
    print(json.dumps(stats, ensure_ascii=False, indent=2), flush=True)
    await session.close()


if __name__ == "__main__":
    asyncio.run(main())
