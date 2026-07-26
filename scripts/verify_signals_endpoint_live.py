"""Live end-to-end check for `GET /api/v1/signals` against real Angel One data.

Runs the real FastAPI app in-process (via its own `lifespan`, exactly as
`uvicorn app.main:app` would run it) and drives it over ASGI with
`httpx.AsyncClient` — no separate server process needed. Explicitly logs
the broker in before issuing the request: `app.main.lifespan` deliberately
does not auto-login (Zerodha/Upstox tokens are single-use — see its
docstring), so this script does the operator's part for this one
verification run instead of changing that startup behavior.

Usage: python scripts/verify_signals_endpoint_live.py [tradingsymbol]
"""

import asyncio
import sys

from httpx import ASGITransport, AsyncClient

from app.main import create_app, lifespan

_DEFAULT_SYMBOL = "SBIN-EQ"


async def main() -> int:
    tradingsymbol = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_SYMBOL

    app = create_app()
    async with lifespan(app):
        await app.state.broker.login()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            print(f"== GET /api/v1/signals?tradingsymbol={tradingsymbol} ==")
            response = await client.get(
                "/api/v1/signals",
                params={"exchange": "NSE", "tradingsymbol": tradingsymbol, "interval": "5minute"},
            )
            print(f"status: {response.status_code}")
            print(response.text)
            return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
