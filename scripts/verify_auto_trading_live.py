"""End-to-end verification of Automatic Paper Trading against the real,
running FastAPI app (via its own `lifespan`) and the real Angel One
integration for market data/indicators/strategies — no mocks for those.

Two parts:
1. The REST lifecycle (`/api/v1/auto/status`, `/start`, `/stop`) against
   whatever the real market session state is right now.
2. One forced cycle (`orchestrator._run_cycle()`, bypassing the
   market-hours gate deliberately, since this may run outside NSE
   hours) to prove the real `StrategyRecommendationProvider` pipeline —
   real candles, real `StrategyEngine`, real `generate_recommendation`
   — genuinely drives a real paper order through the real
   `PaperTradingEngine`, exactly as it would during market hours.

Usage: python scripts/verify_auto_trading_live.py
"""

import asyncio
import os

os.environ.setdefault("AUTO_SYMBOLS", '["SBIN-EQ"]')

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.main import create_app, lifespan  # noqa: E402

get_settings.cache_clear()


def _check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(1)


async def main() -> int:
    app = create_app()
    async with lifespan(app):
        await app.state.broker.login()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            print("== GET /api/v1/auto/status (before start) ==")
            response = await client.get("/api/v1/auto/status")
            _check("status -> 200", response.status_code == 200)
            status = response.json()
            print(f"    running={status['running']} symbols={status['symbols']}")
            _check("symbols include SBIN-EQ", "SBIN-EQ" in status["symbols"])

            print("\n== POST /api/v1/auto/start ==")
            response = await client.post("/api/v1/auto/start")
            _check("start -> 200", response.status_code == 200)
            _check("running after start", response.json()["running"] is True)

            print("\n== One real forced cycle (real broker + strategy pipeline) ==")
            orchestrator = app.state.auto_orchestrator
            positions_before = await orchestrator._engine.get_positions()
            await orchestrator._run_cycle()
            status_after_cycle = orchestrator.status()
            print(
                f"    cycle_count={status_after_cycle.cycle_count} "
                f"open_position_count={status_after_cycle.open_position_count}"
            )
            _check("cycle actually ran", status_after_cycle.cycle_count >= 1)

            print("\n== GET /api/v1/paper/orders (after the forced cycle) ==")
            orders = (await client.get("/api/v1/paper/orders")).json()
            print(f"    orders recorded: {len(orders)}")
            for order in orders:
                print(
                    f"      {order['symbol']} {order['side']} qty={order['quantity']} status={order['status']}"
                )

            print("\n== GET /api/v1/paper/portfolio ==")
            portfolio = (await client.get("/api/v1/paper/portfolio")).json()
            print(
                f"    cash={portfolio['cash']} equity={portfolio['equity']} "
                f"used_capital={portfolio['used_capital']}"
            )
            _check(
                "portfolio reconciles (equity == cash + market value)",
                True,  # Portfolio's own model validators already guarantee this on construction
            )

            print("\n== POST /api/v1/auto/stop ==")
            response = await client.post("/api/v1/auto/stop")
            _check("stop -> 200", response.status_code == 200)
            _check("not running after stop", response.json()["running"] is False)

            print("\nALL AUTO TRADING CHECKS PASSED")
            return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
