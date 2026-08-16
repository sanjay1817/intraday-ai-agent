"""Historical Dataset Manager: fetches an explicit date-range of OHLCV
candles for a backtest and caches them on disk.

Deliberately separate from `app.market.ingestion.fetch_recent_candles`:
that function is a narrow "give me the last N days for indicator
warm-up" helper with no caller-supplied date range. A backtest needs an
arbitrary, caller-chosen `(from_date, to_date)` window instead — this
module is the general-purpose counterpart, built on the exact same
`BrokerInterface.historical_data` call and `MarketCandle` conversion, but
never assumes "now" is one end of the range.

`HistoricalDataProvider` is a `Protocol` (not a broker-adapter subclass)
so a future non-broker historical-data source (a vendor API, a static
file) can be plugged in without this module or its callers changing —
exactly the "clean data-provider abstraction" the feature's requirements
call for if the configured broker's own historical API is ever
insufficient. `BrokerHistoricalDataProvider` is the only implementation
today, wrapping whichever `BrokerInterface` the caller already has
(Angel One's `getCandleData` in practice).

Never fabricates a price: an empty broker response raises
`NoHistoricalDataError` (reused from `app.domain.exceptions.market`,
which the API layer already maps to a 404) rather than returning
synthetic candles.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import structlog
from pydantic import ValidationError

from app.brokers.base import BrokerInterface
from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval
from app.domain.exceptions.market import InvalidHistoricalDataError, NoHistoricalDataError
from app.market.dto import MarketCandle

logger = structlog.get_logger(__name__)


class HistoricalDataProvider(Protocol):
    """What `app.backtest.replay_engine`/`app.backtest.options_replay`
    need to obtain one instrument's candles for an explicit date range —
    structural, so a non-broker data source can stand in for tests or a
    future vendor integration.
    """

    async def get_candles(
        self,
        symbol: str,
        exchange: Exchange,
        interval: HistoricalInterval,
        from_date: datetime,
        to_date: datetime,
    ) -> list[MarketCandle]: ...


def _cache_key(
    symbol: str, exchange: Exchange, interval: HistoricalInterval, from_date: datetime, to_date: datetime
) -> str:
    raw = f"{exchange.value}:{symbol}:{interval.value}:{from_date.isoformat()}:{to_date.isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class BrokerHistoricalDataProvider:
    """The default `HistoricalDataProvider`: wraps a `BrokerInterface`'s
    `historical_data()` (Angel One's `getCandleData` in practice), with
    an on-disk JSON cache so replaying the same session twice doesn't
    re-download it.

    Read-only by design: this class never calls anything on `broker`
    other than `historical_data()` — see `app.backtest`'s package-level
    safety boundary (no code path from a backtest to a live order
    endpoint).
    """

    def __init__(
        self,
        broker: BrokerInterface,
        broker_name: BrokerName,
        *,
        cache_dir: Path | None = None,
    ) -> None:
        self._broker = broker
        self._broker_name = broker_name
        self._cache_dir = cache_dir

    async def get_candles(
        self,
        symbol: str,
        exchange: Exchange,
        interval: HistoricalInterval,
        from_date: datetime,
        to_date: datetime,
    ) -> list[MarketCandle]:
        cached = self._read_cache(symbol, exchange, interval, from_date, to_date)
        if cached is not None:
            logger.debug("backtest_dataset_cache_hit", symbol=symbol, exchange=exchange.value)
            return cached

        bars = await self._broker.historical_data(exchange, symbol, interval, from_date, to_date)

        if not bars:
            raise NoHistoricalDataError(
                broker=self._broker_name, exchange=exchange, tradingsymbol=symbol, interval=interval
            )

        candles: list[MarketCandle] = []
        for bar in bars:
            try:
                candles.append(
                    MarketCandle(
                        symbol=symbol,
                        exchange=exchange,
                        interval=interval,
                        timestamp=bar.timestamp,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=bar.volume,
                    )
                )
            except ValidationError as exc:
                raise InvalidHistoricalDataError(
                    broker=self._broker_name, exchange=exchange, tradingsymbol=symbol, reason=str(exc)
                ) from exc

        candles.sort(key=lambda candle: candle.timestamp)
        self._write_cache(symbol, exchange, interval, from_date, to_date, candles)
        return candles

    # -- on-disk cache ------------------------------------------------------------------------

    def _cache_path(
        self,
        symbol: str,
        exchange: Exchange,
        interval: HistoricalInterval,
        from_date: datetime,
        to_date: datetime,
    ) -> Path | None:
        if self._cache_dir is None:
            return None
        key = _cache_key(symbol, exchange, interval, from_date, to_date)
        return self._cache_dir / f"{key}.json"

    def _read_cache(
        self,
        symbol: str,
        exchange: Exchange,
        interval: HistoricalInterval,
        from_date: datetime,
        to_date: datetime,
    ) -> list[MarketCandle] | None:
        path = self._cache_path(symbol, exchange, interval, from_date, to_date)
        if path is None or not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return [MarketCandle.model_validate(row) for row in raw]
        except (json.JSONDecodeError, ValidationError, ValueError, OSError):
            logger.warning("backtest_dataset_cache_read_failed", path=str(path))
            return None

    def _write_cache(
        self,
        symbol: str,
        exchange: Exchange,
        interval: HistoricalInterval,
        from_date: datetime,
        to_date: datetime,
        candles: list[MarketCandle],
    ) -> None:
        """Write the cache file atomically: two concurrent requests for
        the same symbol/interval/date-range (e.g. several backtest runs
        fired in parallel from the UI) must never be able to interleave
        their writes into one torn/corrupted JSON file that a later read
        would silently accept as valid. Writing to a uniquely-named
        temp file in the same directory, then `os.replace`-ing it over
        the real path, means every concurrent writer either fully wins
        or is fully overwritten by whichever wrote last — never a partial
        result.
        """

        path = self._cache_path(symbol, exchange, interval, from_date, to_date)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [json.loads(candle.model_dump_json()) for candle in candles]

        tmp_path = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp_path, path)
