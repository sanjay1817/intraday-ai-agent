"""End-to-end test: Automatic Paper Trading driven entirely through the
real REST API surface.

Exercises `POST /api/v1/auto/start` -> the orchestrator's background
loop actually running cycles -> a paper order landing in
`app.paper.engine.PaperTradingEngine` -> that same trade visible through
`GET /api/v1/paper/*` -> a later exit -> `POST /api/v1/auto/stop`, all
through one shared `PaperTradingEngine` instance, proving the
"reuse the Paper Trading Engine" requirement end to end rather than by
unit-testing each side of the wiring separately (already done in
`tests/unit/auto/` and `tests/unit/api/v1/routers/`).

A fake `BrokerInterface` + `RecommendationProvider` stand in for Angel
One and the live strategy pipeline (both already covered by their own
Phase 1/2/3 test suites) so this test is deterministic and fast; a
`time.sleep` polling loop lets the orchestrator's real background
`asyncio.Task` (running on `TestClient`'s own event-loop thread) make
genuine progress while this synchronous test waits for it.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from datetime import time as time_of_day

from fastapi.testclient import TestClient

from app.api.v1.routers.auto import get_orchestrator
from app.api.v1.routers.paper import get_paper_engine
from app.auto.models import AutoTradingConfig
from app.auto.orchestrator import AutoSignal, AutoTradingOrchestrator
from app.brokers.base import BrokerInterface
from app.domain.entities.broker import (
    BrokerFunds,
    BrokerProfile,
    HistoricalBar,
    Holding,
    OrderDetail,
    OrderRequest,
    OrderResponse,
    Position,
    Quote,
    TokenBundle,
)
from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval, OrderVariety
from app.instructor.recommendation import InstructorRecommendation, RecommendationAction
from app.main import create_app
from app.market.dto import MarketSessionState
from app.paper.engine import PaperTradingEngine
from app.strategy.models import ConfluenceResult

_T0 = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
_POLL_TIMEOUT_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.02


class _FakeBroker(BrokerInterface):
    def __init__(self, ltp: float) -> None:
        self.ltp_value = ltp

    async def login(self) -> TokenBundle:
        raise AssertionError

    async def refresh_token(self) -> TokenBundle:
        raise AssertionError

    async def get_profile(self) -> BrokerProfile:
        raise AssertionError

    async def get_funds(self) -> BrokerFunds:
        raise AssertionError

    async def get_positions(self) -> list[Position]:
        raise AssertionError

    async def get_holdings(self) -> list[Holding]:
        raise AssertionError

    async def get_orders(self) -> list[OrderDetail]:
        raise AssertionError

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        raise AssertionError("must only place PAPER orders")

    async def modify_order(self, order_id: str, order: OrderRequest) -> OrderResponse:
        raise AssertionError

    async def cancel_order(
        self, order_id: str, variety: OrderVariety = OrderVariety.REGULAR
    ) -> OrderResponse:
        raise AssertionError

    async def ltp(self, exchange: Exchange, tradingsymbol: str) -> Quote:
        return Quote(
            tradingsymbol=tradingsymbol, exchange=exchange, last_price=self.ltp_value, timestamp=_T0
        )

    async def historical_data(self, *args: object, **kwargs: object) -> list[HistoricalBar]:
        raise AssertionError

    async def start_websocket(self, *args: object, **kwargs: object) -> None:
        raise AssertionError

    async def stop_websocket(self) -> None:
        raise AssertionError

    async def close(self) -> None:
        raise AssertionError


class _FakeProvider:
    """Always returns the currently-set signal for INFY-EQ -- the test
    swaps it out between polling windows to drive entry then exit.
    """

    def __init__(self) -> None:
        self.action = RecommendationAction.HOLD
        self.confidence = 0.0
        self.entry = 1500.0

    async def get_recommendation(
        self,
        broker: object,
        broker_name: object,
        exchange: Exchange,
        symbol: str,
        interval: HistoricalInterval,
    ) -> AutoSignal:
        if self.action is RecommendationAction.HOLD:
            recommendation = InstructorRecommendation(
                symbol=symbol,
                exchange=Exchange.NSE,
                timeframe=interval,
                action=RecommendationAction.HOLD,
                confidence=0.0,
                confirmation_count=0,
                total_strategy_count=3,
                reasoning="HOLD",
                generated_at=_T0,
            )
        else:
            recommendation = InstructorRecommendation(
                symbol=symbol,
                exchange=Exchange.NSE,
                timeframe=interval,
                action=self.action,
                confidence=self.confidence,
                entry=self.entry,
                stop_loss=self.entry * 0.98,
                targets=[self.entry * 1.10],
                confirmation_count=2,
                total_strategy_count=3,
                reasoning=f"{self.action.value}: 2 of 3 agree.",
                generated_at=_T0,
            )
        confluence = ConfluenceResult(
            symbol=symbol,
            timeframe=interval,
            direction="NONE",
            agreeing_strategies=["ema_trend"],
            confirmation_count=2,
            combined_confidence=self.confidence,
            signals=[],
        )
        return AutoSignal(
            recommendation=recommendation, confluence=confluence, indicators_used=["EMA"]
        )


def _wait_until(predicate: Callable[[], bool], *, timeout: float = _POLL_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise AssertionError(f"condition not met within {timeout}s")


def test_auto_trading_full_lifecycle_through_the_rest_api() -> None:
    app = create_app()
    engine = PaperTradingEngine(initial_capital=100_000.0)
    broker = _FakeBroker(ltp=1500.0)
    provider = _FakeProvider()
    orchestrator = AutoTradingOrchestrator(
        broker=broker,
        broker_name=BrokerName.ANGEL_ONE,
        paper_engine=engine,
        config=AutoTradingConfig(
            symbols=["INFY-EQ"],
            poll_interval_seconds=_POLL_INTERVAL_SECONDS,
            confidence_threshold=60.0,
            # This test drives the orchestrator's REAL background loop, which
            # checks the real wall clock (`_run_cycle` defaults `now` to
            # `datetime.now(UTC)`) -- pinning square-off to just before
            # midnight IST keeps the test immune to a premature
            # "market_close" exit no matter what time of day it actually runs.
            square_off_time=time_of_day(23, 59),
        ),
        recommendation_provider=provider,
        session_provider=lambda: MarketSessionState.OPEN,
    )
    app.dependency_overrides[get_orchestrator] = lambda: orchestrator
    app.dependency_overrides[get_paper_engine] = lambda: engine

    with TestClient(app) as client:
        # -- before starting: everything flat -----------------------------------------
        assert client.get("/api/v1/auto/status").json()["running"] is False
        assert client.get("/api/v1/paper/positions").json() == []

        # -- start, and let a BUY signal open a position -------------------------------
        provider.action = RecommendationAction.BUY
        provider.confidence = 80.0
        provider.entry = 1500.0

        start_response = client.post("/api/v1/auto/start")
        assert start_response.status_code == 200
        assert start_response.json()["running"] is True

        _wait_until(lambda: client.get("/api/v1/paper/positions").json() != [])

        positions = client.get("/api/v1/paper/positions").json()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "INFY-EQ"
        assert positions[0]["quantity"] > 0

        status = client.get("/api/v1/auto/status").json()
        assert status["open_position_count"] == 1
        assert status["trades_today"] == 1
        assert status["cycle_count"] >= 1

        orders = client.get("/api/v1/paper/orders").json()
        assert len(orders) == 1
        assert orders[0]["status"] == "COMPLETE"
        assert orders[0]["metadata"]["confidence"] == 80.0

        portfolio = client.get("/api/v1/paper/portfolio").json()
        assert portfolio["cash"] < 100_000.0
        assert portfolio["used_capital"] > 0

        # -- price rises past target: the orchestrator exits on its own -----------------
        provider.action = RecommendationAction.HOLD  # no reversal signal; target does the work
        broker.ltp_value = 1500.0 * 1.11  # above the 10% target

        _wait_until(lambda: client.get("/api/v1/paper/positions").json() == [])

        trades = client.get("/api/v1/paper/trades").json()
        assert len(trades) == 1
        assert trades[0]["symbol"] == "INFY-EQ"
        assert trades[0]["pnl"] > 0  # exited above entry

        final_status = client.get("/api/v1/auto/status").json()
        assert final_status["open_position_count"] == 0
        assert final_status["daily_realized_pnl"] > 0

        final_portfolio = client.get("/api/v1/paper/portfolio").json()
        assert final_portfolio["realized_pnl"] > 0
        assert final_portfolio["positions"] == []

        # -- stop cleanly -----------------------------------------------------------------
        stop_response = client.post("/api/v1/auto/stop")
        assert stop_response.status_code == 200
        assert stop_response.json()["running"] is False
