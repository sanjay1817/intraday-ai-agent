"""End-to-end verification of every Paper Trading API against the real,
running FastAPI app (via its own `lifespan`, exactly as `uvicorn
app.main:app` would run it) — no separate server process, no mocks.

Price discovery for placed orders goes through the real, configured
broker (`app.state.broker`, e.g. Angel One), so this genuinely exercises
`app.api.v1.routers.paper` -> `app.paper.engine.PaperTradingEngine` end
to end. No real order is ever placed with the real broker — only its
`ltp()` is used.

Usage: python scripts/verify_paper_endpoints_live.py
"""

import asyncio

from httpx import ASGITransport, AsyncClient

from app.main import create_app, lifespan


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
            print("== POST /api/v1/paper/reset (start clean) ==")
            response = await client.post("/api/v1/paper/reset", json={"initial_capital": 100000.0})
            _check("reset -> 200", response.status_code == 200)
            portfolio = response.json()
            _check("reset -> cash == 100000", portfolio["cash"] == 100000.0)
            _check("reset -> no positions", portfolio["positions"] == [])

            print("\n== POST /api/v1/paper/order (BUY MARKET SBIN-EQ x10) ==")
            response = await client.post(
                "/api/v1/paper/order",
                json={
                    "order": {
                        "symbol": "SBIN-EQ",
                        "exchange": "NSE",
                        "side": "BUY",
                        "order_type": "MARKET",
                        "quantity": 10,
                    },
                    "metadata": {
                        "confidence": 78.5,
                        "agreeing_strategies": ["ema_trend", "vwap_volume_breakout"],
                        "indicators_used": ["EMA", "VWAP", "ATR"],
                        "reasoning": "BUY: 2 of 3 strategies agree.",
                    },
                },
            )
            _check("place order -> 201", response.status_code == 201)
            order = response.json()
            print(
                f"    order status={order['status']} fill_price={order.get('average_fill_price')}"
            )
            _check("order status is COMPLETE or OPEN", order["status"] in ("COMPLETE", "OPEN"))
            _check("order metadata carried through", order["metadata"]["confidence"] == 78.5)
            entry_fill_price = order.get("average_fill_price")

            print("\n== GET /api/v1/paper/orders ==")
            response = await client.get("/api/v1/paper/orders")
            _check("get orders -> 200", response.status_code == 200)
            orders = response.json()
            _check("one order recorded", len(orders) == 1)

            print("\n== GET /api/v1/paper/positions ==")
            response = await client.get("/api/v1/paper/positions")
            _check("get positions -> 200", response.status_code == 200)
            positions = response.json()
            if order["status"] == "COMPLETE":
                _check("one open position", len(positions) == 1)
                _check("position quantity == 10", positions[0]["quantity"] == 10)
                print(
                    f"    position: {positions[0]['symbol']} qty={positions[0]['quantity']} avg={positions[0]['average_price']}"
                )

            print("\n== GET /api/v1/paper/portfolio ==")
            response = await client.get("/api/v1/paper/portfolio")
            _check("get portfolio -> 200", response.status_code == 200)
            portfolio = response.json()
            print(
                f"    cash={portfolio['cash']} available_cash={portfolio['available_cash']} "
                f"used_capital={portfolio['used_capital']} equity={portfolio['equity']}"
            )
            _check(
                "equity reconciles (cash + market value)",
                abs(
                    portfolio["equity"]
                    - (portfolio["cash"] + sum(p["quantity"] * p["last_price"] for p in positions))
                )
                < 1e-6,
            )

            if order["status"] == "COMPLETE" and entry_fill_price is not None:
                print("\n== POST /api/v1/paper/order (SELL MARKET SBIN-EQ x10, close it) ==")
                response = await client.post(
                    "/api/v1/paper/order",
                    json={
                        "order": {
                            "symbol": "SBIN-EQ",
                            "exchange": "NSE",
                            "side": "SELL",
                            "order_type": "MARKET",
                            "quantity": 10,
                        }
                    },
                )
                _check("place closing order -> 201", response.status_code == 201)
                close_order = response.json()
                print(
                    f"    close order status={close_order['status']} fill_price={close_order.get('average_fill_price')}"
                )

                print("\n== GET /api/v1/paper/trades ==")
                response = await client.get("/api/v1/paper/trades")
                _check("get trades -> 200", response.status_code == 200)
                trades = response.json()
                if close_order["status"] == "COMPLETE":
                    _check("one closed trade recorded", len(trades) == 1)
                    print(
                        f"    trade pnl={trades[0]['pnl']} entry={trades[0]['entry_price']} exit={trades[0]['exit_price']}"
                    )

                print("\n== GET /api/v1/paper/positions (after close) ==")
                response = await client.get("/api/v1/paper/positions")
                positions_after = response.json()
                if close_order["status"] == "COMPLETE":
                    _check("position closed out", positions_after == [])

            print("\n== POST /api/v1/paper/reset (final) ==")
            response = await client.post("/api/v1/paper/reset", json={})
            _check("final reset -> 200", response.status_code == 200)
            final_portfolio = response.json()
            _check("final cash == initial_capital", final_portfolio["cash"] == 100000.0)

            print("\nALL PAPER TRADING API CHECKS PASSED")
            return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
