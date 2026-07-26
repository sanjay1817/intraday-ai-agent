"""Live sanity check for the Angel One adapter against the real SmartAPI.

Read-only: exercises login, profile, LTP, and historical candles only —
never places, modifies, or cancels an order. Intended for manually
verifying real credentials/connectivity during setup, not for CI (it
requires real `.env` credentials and network access to Angel One).

Usage: python scripts/verify_angel_one_live.py
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta

from app.brokers.factory import get_broker_adapter
from app.config.settings import get_settings
from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval
from app.domain.exceptions.broker import BrokerError

_PROBE_SYMBOL = "SBIN-EQ"
_PROBE_EXCHANGE = Exchange.NSE


def _mask(value: str | None) -> str:
    if not value:
        return "<empty>"
    return f"{value[:4]}...({len(value)} chars)"


async def main() -> int:
    settings = get_settings()
    adapter = get_broker_adapter(BrokerName.ANGEL_ONE, settings)

    try:
        print("== login() ==")
        bundle = await adapter.login()
        print(f"access_token:  {_mask(bundle.access_token)}")
        print(f"refresh_token: {_mask(bundle.refresh_token)}")
        print(f"feed_token:    {_mask(bundle.feed_token)}")

        print("\n== get_profile() ==")
        profile = await adapter.get_profile()
        print(f"client_id:         {profile.client_id}")
        print(f"display_name:      {profile.display_name}")
        print(f"email:             {profile.email}")
        print(f"exchanges_enabled: {[e.value for e in profile.exchanges_enabled]}")
        print(f"products_enabled:  {[p.value for p in profile.products_enabled]}")

        print(f"\n== ltp({_PROBE_EXCHANGE.value}, {_PROBE_SYMBOL!r}) ==")
        quote = await adapter.ltp(_PROBE_EXCHANGE, _PROBE_SYMBOL)
        print(f"last_price: {quote.last_price}")

        print(f"\n== historical_data({_PROBE_EXCHANGE.value}, {_PROBE_SYMBOL!r}, 5minute) ==")
        now = datetime.now(UTC)
        bars = await adapter.historical_data(
            _PROBE_EXCHANGE,
            _PROBE_SYMBOL,
            HistoricalInterval.FIVE_MINUTE,
            now - timedelta(days=5),
            now,
        )
        print(f"bar_count: {len(bars)}")
        if bars:
            last = bars[-1]
            print(
                f"last_bar: ts={last.timestamp} O={last.open} H={last.high} "
                f"L={last.low} C={last.close} V={last.volume}"
            )

        print("\nALL CHECKS PASSED")
        return 0
    except BrokerError as exc:
        print(f"\nBROKER ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await adapter.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
