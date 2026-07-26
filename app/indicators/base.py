"""The indicator plugin contract: `Indicator`, its parameter-validation
base, and the registry `@register_indicator` populates.

This is the mechanism that satisfies the Open/Closed Principle: adding a
new indicator means writing a new module with a new `Indicator` subclass
decorated `@register_indicator` — no existing file changes. See
`app/indicators/__init__.py` for the auto-discovery that imports every
sibling module (so the decorator actually runs) without a hand-maintained
import list.
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Generic, TypeVar

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from app.domain.exceptions.indicators import UnknownIndicatorError
from app.indicators.schemas import IndicatorResult, PointT


class IndicatorParams(BaseModel):
    """Base class for per-indicator parameter validation.

    Concrete indicators subclass this with named, constrained fields
    (e.g. `length: int = Field(gt=0, default=20)`) instead of accepting a
    raw, unchecked `dict` — this is where "no magic numbers" lives for
    the indicator engine: every default and constraint is declared once,
    on the model, not repeated inline at call sites.
    """

    model_config = ConfigDict(frozen=True)


class LengthParams(IndicatorParams):
    """Shared shape for indicators with exactly one `length` parameter
    (SMA, EMA, Volume SMA, RSI, ATR).
    """

    length: int = Field(default=20, gt=0)


ParamsT = TypeVar("ParamsT", bound=IndicatorParams)


class Indicator(ABC, Generic[ParamsT, PointT]):
    """Base class every technical indicator implements.

    `compute` and `to_result` are split so the pandas-ta call (which
    needs the raw DataFrame) and the DataFrame-to-Pydantic mapping (which
    doesn't) can be tested/reasoned about independently.
    """

    #: The key `IndicatorRequest.name` must match to select this indicator.
    name: ClassVar[str]
    #: Validates/defaults this indicator's `IndicatorRequest.params`.
    params_model: ClassVar[type[IndicatorParams]]

    @abstractmethod
    def compute(self, df: pd.DataFrame, params: ParamsT) -> pd.DataFrame:
        """Run the indicator via pandas-ta.

        Returns a DataFrame indexed like `df`, with one column per raw
        output value, column names already renamed to this indicator's
        canonical field names (see concrete indicators for the mapping).
        """

    @abstractmethod
    def to_result(self, raw: pd.DataFrame, params: ParamsT) -> IndicatorResult[PointT]:
        """Convert `compute`'s raw DataFrame into the typed result."""


_REGISTRY: dict[str, type[Indicator[Any, Any]]] = {}


def register_indicator(cls: type[Indicator[Any, Any]]) -> type[Indicator[Any, Any]]:
    """Class decorator: register `cls` under its `name` for lookup by
    `IndicatorEngine`. Raises `ValueError` on a duplicate name so a
    copy-pasted `name` string fails loudly at import time rather than
    silently shadowing an existing indicator.
    """

    if cls.name in _REGISTRY:
        raise ValueError(f"An indicator named {cls.name!r} is already registered")
    _REGISTRY[cls.name] = cls
    return cls


def get_indicator_class(name: str) -> type[Indicator[Any, Any]]:
    """Look up a registered indicator class by name.

    Raises:
        UnknownIndicatorError: `name` has no registered indicator.
    """

    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise UnknownIndicatorError(name, list_registered_indicators()) from exc


def list_registered_indicators() -> list[str]:
    """Every currently-registered indicator name, sorted for stable output."""

    return sorted(_REGISTRY)
