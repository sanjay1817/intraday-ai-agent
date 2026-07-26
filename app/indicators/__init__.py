"""The Technical Indicator Engine.

`IndicatorEngine` (app.indicators.engine) computes only the indicators a
caller requests, against a caller-supplied OHLCV DataFrame, using
pandas-ta under the hood, and caches repeated (data, indicator, params)
calculations.

Adding indicator #13 means adding a new module in this package with a
class decorated `@register_indicator` (see `app.indicators.base`) — no
existing file needs to change. This module auto-imports every sibling
module below so registration happens exactly once, at package import
time, regardless of which indicator a caller ultimately requests; that
auto-import (rather than a hand-maintained list) is what makes adding a
new indicator module a zero-edit operation on existing code.
"""

import importlib
import pkgutil

from app.indicators.base import (
    Indicator,
    IndicatorParams,
    LengthParams,
    get_indicator_class,
    list_registered_indicators,
    register_indicator,
)
from app.indicators.engine import IndicatorEngine, IndicatorRequest
from app.indicators.schemas import (
    ADXPoint,
    ADXResult,
    BollingerBandsPoint,
    BollingerBandsResult,
    IchimokuPoint,
    IchimokuResult,
    IndicatorResult,
    MACDPoint,
    MACDResult,
    SingleValuePoint,
    SingleValueResult,
    StochasticRSIPoint,
    StochasticRSIResult,
    SuperTrendPoint,
    SuperTrendResult,
)

#: Modules in this package that are infrastructure, not indicators —
#: auto-discovery skips these so it doesn't try to re-import them (which
#: would be harmless but pointless) or treat them as indicator sources.
_INFRASTRUCTURE_MODULES = frozenset({"base", "engine", "schemas", "utils"})


def _autodiscover_indicators() -> None:
    """Import every sibling module so its `@register_indicator` classes
    run exactly once. See the module docstring: this is what lets a new
    indicator module be picked up without editing this file.
    """

    package = importlib.import_module(__name__)
    for module_info in pkgutil.iter_modules(package.__path__):
        if module_info.name in _INFRASTRUCTURE_MODULES:
            continue
        importlib.import_module(f"{__name__}.{module_info.name}")


_autodiscover_indicators()

__all__ = [
    "ADXPoint",
    "ADXResult",
    "BollingerBandsPoint",
    "BollingerBandsResult",
    "IchimokuPoint",
    "IchimokuResult",
    "Indicator",
    "IndicatorEngine",
    "IndicatorParams",
    "IndicatorRequest",
    "IndicatorResult",
    "LengthParams",
    "MACDPoint",
    "MACDResult",
    "SingleValuePoint",
    "SingleValueResult",
    "StochasticRSIPoint",
    "StochasticRSIResult",
    "SuperTrendPoint",
    "SuperTrendResult",
    "get_indicator_class",
    "list_registered_indicators",
    "register_indicator",
]
