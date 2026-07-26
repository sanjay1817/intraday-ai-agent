"""Live sanity check for the Angel One WebSocket market-data stream.

Connects, authenticates, subscribes to a couple of instruments, and
prints whatever ticks arrive within a fixed window. Outside NSE trading
hours (09:15-15:30 IST, weekdays) no ticks are expected — the point of
running it then is only to confirm the connection/auth/subscribe
handshake itself succeeds against the real server, not to observe data.

Usage: python scripts/verify_angel_one_ws_live.py [seconds]
"""

import asyncio
import sys

from app.brokers.factory import get_broker_adapter
from app.config.settings import get_settings
from app.domain.entities.broker import Quote
from app.domain.enums.trading import BrokerName, Exchange

_PROBE_INSTRUMENTS = [(Exchange.NSE, "SBIN-EQ"), (Exchange.NSE, "RELIANCE-EQ")]
_DEFAULT_WINDOW_SECONDS = 20.0


async def main() -> int:
    window_seconds = float(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_WINDOW_SECONDS

    settings = get_settings()
    adapter = get_broker_adapter(BrokerName.ANGEL_ONE, settings)
    received: list[Quote] = []

    async def on_tick(quote: Quote) -> None:
        received.append(quote)
        print(
            f"TICK: {quote.tradingsymbol} {quote.exchange.value} ltp={quote.last_price} ts={quote.timestamp}"
        )

    try:
        print("== login() ==")
        await adapter.login()

        print(f"== start_websocket({[s for _, s in _PROBE_INSTRUMENTS]}) ==")
        await adapter.start_websocket(on_tick, _PROBE_INSTRUMENTS)

        print(f"listening for {window_seconds}s ...")
        await asyncio.sleep(window_seconds)

        print(f"\nticks_received: {len(received)}")
        return 0
    finally:
        await adapter.stop_websocket()
        await adapter.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
