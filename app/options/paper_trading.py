"""Phase 3 bridge: turns an `OptionRecommendation` into a paper option
order against the EXISTING Paper Trading Engine, and reads paper option
trade history back out of it.

This is the one file in `app.options` allowed to import `app.paper.*` —
mirrors how `app.options.recommendation` is the one file allowed to
import `app.instructor`/`app.strategy`/`app.market` (see that module's
docstring): bridging "which option contract does the AI recommend" to
"place/close a simulated order for it" is inherently a cross-package
concern, and keeping that coupling isolated here means the rest of
`app.options` (chain fetch/cache, expiry/strike selection, symbol
building) stays reusable by anything that only needs option-chain
plumbing.

Why no new Portfolio/PnL engine: `app.paper.engine.PaperTradingEngine`
(and the `PortfolioManager`/`PositionManager`/`OrderManager` it wires
together) is already fully generic — every position is keyed by
`(Exchange, symbol)` with plain `quantity`-based P&L math
(`(price - average_price) * quantity`, see `app.paper.models.PaperPosition`'s
own docstring/validators), and `Exchange.NFO` already exists. An option
position is, to that engine, just another symbol on NFO with a bigger
`quantity` (lots * lot_size) and `price` playing the role premium
plays — nothing about cash reservation, fill matching, FIFO lot
bookkeeping, realized/unrealized P&L, or portfolio snapshot composition
needs to change, or be re-implemented here, for that to be correct. This
module is therefore a *thin* bridge: it translates an `OptionRecommendation`
plus a lot count into a `PaperOrderRequest` (quantity = lots * lot_size,
symbol = the option's trading symbol, exchange = NFO), calls
`PaperTradingEngine.place_order`/`get_position`/`get_positions`
/`get_portfolio`/`get_trades` exactly as any other caller would, and adds
only what genuinely doesn't exist yet: an options-specific risk gate
(`app.options.risk.OptionRiskManager`) and read-side trade-history
enrichment (underlying/reason/confidence derived from data the engine
already returns, not new state it stores). If any cash/PnL/portfolio
arithmetic ever needs to live in this file, that is a sign the bridge
has grown into a second engine — a bug in this approach, not a feature.

Bracket orders (stop-loss/target auto-exit legs) are unchanged from the
existing engine: setting `stop_loss_price`/`target_price` on the
`PaperOrderRequest` built by `enter_option_position` makes
`app.paper.order_manager.OrderManager._maybe_spawn_bracket_children`
auto-spawn the OCO stop/limit exit pair once the entry fills — this
module never builds a second exit-triggering mechanism. Only a `MANUAL`
exit (no bracket trigger involved) needs an explicit call, which is what
`exit_option_position` provides.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from app.brokers.base import BrokerInterface
from app.domain.enums.trading import Exchange, OrderSide
from app.options.exceptions import (
    InvalidLotQuantityError,
    OptionPositionNotFoundError,
    OptionRiskLimitExceededError,
)
from app.options.lot_sizing import resolve_lot_size
from app.options.option_chain_service import OptionChainService
from app.options.risk import OptionRiskManager
from app.options.schemas import (
    OptionExitReason,
    OptionRecommendation,
    OptionSignal,
    OptionTradeHistoryEntry,
)
from app.paper.dto import PaperOrderRequest, PaperOrderType
from app.paper.engine import PaperTradingEngine
from app.paper.models import PaperOrder, TradeMetadata


async def get_option_premium_exposure(engine: PaperTradingEngine) -> float:
    """Total premium currently deployed across every open option (NFO)
    position — the same figure `enter_option_position`'s risk check uses,
    factored out so a read-only status endpoint (`GET
    /api/v1/options/risk/status`) never computes it a second,
    possibly-diverging way.
    """

    positions = await engine.get_positions()
    return sum(
        abs(position.quantity) * position.average_price
        for position in positions
        if position.exchange is Exchange.NFO
    )


async def enter_option_position(
    *,
    broker: BrokerInterface,
    engine: PaperTradingEngine,
    option_chain_service: OptionChainService,
    risk_manager: OptionRiskManager,
    recommendation: OptionRecommendation,
    lots: int,
    default_lot_size: int,
    max_lots_per_order: int,
    stop_loss_premium: float | None = None,
    target_premium: float | None = None,
    now: datetime | None = None,
) -> PaperOrder:
    """Open a simulated option position for `recommendation`, sized in
    `lots`.

    Fetches a *fresh* premium via `broker.ltp()` rather than reusing
    `recommendation.premium` — that value may be stale by the time an
    order is actually placed, e.g. a HOLD-then-BUY that traveled through
    a caller's own decision latency.

    Raises:
        ValueError: `recommendation.signal is OptionSignal.NO_TRADE` — a
            caller-contract violation, since a HOLD recommendation names
            no contract to trade; the router must check this itself
            before calling and never reach here with `NO_TRADE`.
        InvalidLotQuantityError: `lots` is not a positive integer, or
            exceeds `max_lots_per_order`.
        UnsupportedUnderlyingError, OptionChainFetchError,
        InvalidOptionChainDataError: propagated from
            `option_chain_service.get_option_chain`.
        OptionRiskLimitExceededError: the candidate order fails one of
            `risk_manager.check_order_allowed`'s checks.
    """

    if recommendation.signal is OptionSignal.NO_TRADE:
        raise ValueError("cannot enter a position for a NO_TRADE recommendation")

    if lots <= 0 or lots > max_lots_per_order:
        raise InvalidLotQuantityError(
            f"lots must be a positive integer no greater than {max_lots_per_order}, got {lots}",
            underlying=recommendation.underlying,
        )

    # Guaranteed non-None: `OptionRecommendation`'s own validator
    # requires every contract field to be set together whenever
    # `signal is not NO_TRADE` (see app.options.schemas).
    assert recommendation.expiry is not None
    assert recommendation.strike is not None
    assert recommendation.option_type is not None
    assert recommendation.tradingsymbol is not None

    chain = await option_chain_service.get_option_chain(recommendation.underlying)
    lot_size = resolve_lot_size(
        chain,
        expiry=recommendation.expiry,
        strike=recommendation.strike,
        option_type=recommendation.option_type,
        default_lot_size=default_lot_size,
    )
    quantity = lots * lot_size

    quote = await broker.ltp(Exchange.NFO, recommendation.tradingsymbol)
    premium = quote.last_price

    order_value = quantity * premium

    current_exposure = await get_option_premium_exposure(engine)

    check = risk_manager.check_order_allowed(
        lots=lots,
        order_premium_value=order_value,
        current_premium_exposure=current_exposure,
        now=now,
    )
    if not check.allowed:
        raise OptionRiskLimitExceededError(
            f"option order for {recommendation.tradingsymbol} rejected by risk gate: {check.reason}",
            underlying=recommendation.underlying,
            reason=check.reason or "unknown",
        )

    metadata = TradeMetadata(confidence=recommendation.confidence, reasoning=recommendation.reasoning)
    request = PaperOrderRequest(
        symbol=recommendation.tradingsymbol,
        exchange=Exchange.NFO,
        side=OrderSide.BUY,
        order_type=PaperOrderType.MARKET,
        quantity=quantity,
        stop_loss_price=stop_loss_premium,
        target_price=target_premium,
        tag="OPTION_ENTRY",
    )

    # A `REJECTED` order (e.g. insufficient paper cash) is a valid,
    # successfully-recorded outcome — see `app.api.v1.routers.paper
    # .place_order`'s own docstring for the same philosophy — so it is
    # returned as-is, never raised.
    return await engine.place_order(request, current_price=premium, metadata=metadata, now=now)


async def exit_option_position(
    *,
    broker: BrokerInterface,
    engine: PaperTradingEngine,
    risk_manager: OptionRiskManager,
    tradingsymbol: str,
    reason: OptionExitReason,
    quantity: int | None = None,
    now: datetime | None = None,
) -> PaperOrder:
    """Close (fully or partially) the open option position for
    `tradingsymbol`.

    Raises:
        OptionPositionNotFoundError: no open position exists for
            `tradingsymbol` on `Exchange.NFO`.
        ValueError: `quantity` is not a positive integer no greater than
            the position's current absolute quantity — a caller-contract
            violation, not a business rule.
    """

    position = await engine.get_position(tradingsymbol, Exchange.NFO)
    if position is None or position.quantity == 0:
        raise OptionPositionNotFoundError(
            f"no open option position for {tradingsymbol!r}", underlying=None
        )

    exit_quantity = quantity if quantity is not None else abs(position.quantity)
    if not (0 < exit_quantity <= abs(position.quantity)):
        raise ValueError(
            f"quantity must be between 1 and {abs(position.quantity)}, got {exit_quantity}"
        )

    exit_side = OrderSide.SELL if position.quantity > 0 else OrderSide.BUY

    quote = await broker.ltp(Exchange.NFO, tradingsymbol)
    premium = quote.last_price

    # Captured before the exit fills so the P&L this specific exit
    # produced can be diffed afterward — reusing the engine's own
    # arithmetic rather than re-deriving it here.
    portfolio_before = await engine.get_portfolio(now=now)

    request = PaperOrderRequest(
        symbol=tradingsymbol,
        exchange=Exchange.NFO,
        side=exit_side,
        order_type=PaperOrderType.MARKET,
        quantity=exit_quantity,
        tag=reason.value,
    )
    order = await engine.place_order(request, current_price=premium, now=now)

    portfolio_after = await engine.get_portfolio(now=now)
    pnl_delta = portfolio_after.realized_pnl - portfolio_before.realized_pnl
    if pnl_delta != 0:
        risk_manager.record_trade_closed(pnl_delta, now=now)

    return order


async def get_option_trade_history(
    engine: PaperTradingEngine, *, underlyings: Sequence[str]
) -> list[OptionTradeHistoryEntry]:
    """Read-only enrichment of `engine.get_trades()` for the options
    paper-trading trade-history endpoint — adds no new storage, and
    computes nothing the engine hasn't already produced.
    """

    trades = await engine.get_trades()
    orders = {order.order_id: order for order in await engine.get_orders()}

    normalized_underlyings = sorted(
        (u.strip().upper() for u in underlyings), key=len, reverse=True
    )

    entries: list[OptionTradeHistoryEntry] = []
    for trade in trades:
        if trade.exchange is not Exchange.NFO:
            continue

        underlying = next(
            (u for u in normalized_underlyings if trade.symbol.startswith(u)),
            trade.symbol,
        )

        exit_order = orders.get(trade.exit_order_id)
        reason = exit_order.tag if exit_order is not None else None
        confidence = trade.metadata.confidence if trade.metadata else None
        reasoning = trade.metadata.reasoning if trade.metadata else None
        holding_seconds = (trade.exit_timestamp - trade.entry_timestamp).total_seconds()

        entries.append(
            OptionTradeHistoryEntry(
                trade_id=trade.trade_id,
                underlying=underlying,
                tradingsymbol=trade.symbol,
                entry_premium=trade.entry_price,
                exit_premium=trade.exit_price,
                pnl=trade.pnl,
                holding_seconds=holding_seconds,
                reason=reason,
                confidence=confidence,
                reasoning=reasoning,
                entry_timestamp=trade.entry_timestamp,
                exit_timestamp=trade.exit_timestamp,
            )
        )

    return entries
