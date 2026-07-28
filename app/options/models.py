"""Framework-agnostic option-chain domain model.

Plain, immutable dataclasses/enums only (mirrors `app.domain.entities.broker`'s
style) — no Pydantic, no broker-specific shapes. `OptionChainService` and the
selectors in this package operate purely in terms of these types; broker
adapters translate their own wire formats into `OptionInstrument` at the
boundary (see `app.options.option_chain_service.AngelOneOptionInstrumentSource`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from app.domain.enums.trading import Exchange


class OptionType(StrEnum):
    """Call vs. put."""

    CE = "CE"
    PE = "PE"


class StrikeMode(StrEnum):
    """How to pick a strike relative to the underlying's current price."""

    ATM = "ATM"
    ITM = "ITM"
    OTM = "OTM"


class ExpiryMode(StrEnum):
    """How to pick an expiry from an underlying's available option expiries."""

    NEAREST_WEEKLY = "NEAREST_WEEKLY"
    NEXT_WEEKLY = "NEXT_WEEKLY"
    MONTHLY = "MONTHLY"


@dataclass(frozen=True, slots=True)
class OptionInstrument:
    """One concrete, tradable option contract.

    `token`/`lot_size` are broker-specific metadata (Angel One's
    `symboltoken` and contract lot size) and are `None` when the source
    that produced this instrument doesn't have them on hand.
    """

    underlying: str
    expiry: date
    strike: float
    option_type: OptionType
    tradingsymbol: str
    exchange: Exchange
    token: str | None = None
    lot_size: int | None = None


@dataclass(frozen=True, slots=True)
class OptionChain:
    """A snapshot of every option instrument for one underlying.

    `fetched_at` records when this snapshot was built, so a cached copy's
    staleness can be judged by callers that need to (`OptionChainService`
    itself judges staleness by a monotonic clock, not this wall-clock
    field — see that module — but callers inspecting a chain directly
    still want to know when it was captured).
    """

    underlying: str
    fetched_at: datetime
    instruments: tuple[OptionInstrument, ...]

    def expiries(self) -> tuple[date, ...]:
        """Every distinct expiry present in this chain, sorted ascending."""

        return tuple(sorted({instrument.expiry for instrument in self.instruments}))

    def strikes_for_expiry(self, expiry: date) -> tuple[float, ...]:
        """Every distinct strike available for `expiry`, sorted ascending."""

        return tuple(
            sorted({
                instrument.strike
                for instrument in self.instruments
                if instrument.expiry == expiry
            })
        )

    def instrument_for(
        self, expiry: date, strike: float, option_type: OptionType
    ) -> OptionInstrument | None:
        """Return the single instrument matching `expiry`/`strike`/`option_type`, or `None`."""

        for instrument in self.instruments:
            if (
                instrument.expiry == expiry
                and instrument.strike == strike
                and instrument.option_type == option_type
            ):
                return instrument
        return None
