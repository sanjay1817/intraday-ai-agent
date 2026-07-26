"""Paper Trading Engine exceptions.

Raised by `app.paper.portfolio.PortfolioManager` (and, in later
milestones, `app.paper.order_manager`/`position_manager`) for business-
rule violations a caller can reasonably expect to catch and react to —
e.g. showing a user "insufficient funds" rather than crashing. A
malformed call (a negative amount, releasing more than was ever
reserved) is a programming-contract violation, not a business rule, and
raises a plain `ValueError` instead — the same distinction
`app.brokers.angel_one` draws between `OrderRejectionError` (a broker's
business rejection) and letting `ValueError`/`TypeError` surface for
genuine misuse.
"""


class PaperTradingError(Exception):
    """Base class for every Paper Trading Engine failure."""


class InsufficientCashError(PaperTradingError):
    """Raised when an operation would spend or reserve more cash than
    `PortfolioManager` currently has available.
    """

    def __init__(self, message: str, *, requested: float, available: float) -> None:
        self.requested = requested
        self.available = available
        super().__init__(message)


class OrderNotFoundError(PaperTradingError):
    """Raised when `app.paper.order_manager.OrderManager` is asked to
    cancel or modify an `order_id` it has no record of.
    """

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        super().__init__(f"paper: no order found with id {order_id!r}")


class InvalidOrderStateError(PaperTradingError):
    """Raised when a cancel/modify is attempted against an order that
    isn't in a resting state (`OPEN`/`TRIGGER_PENDING`) — e.g. one
    that's already `COMPLETE`, `CANCELLED`, or `REJECTED`.
    """

    def __init__(self, order_id: str, status: str) -> None:
        self.order_id = order_id
        self.status = status
        super().__init__(f"paper: order {order_id!r} is {status} and cannot be modified/cancelled")
