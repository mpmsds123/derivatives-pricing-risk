"""SOFR curve: piecewise-constant overnight forward rate on a business day grid.

Representation choice (justified in the README): the curve state is the
overnight forward rate on every business day, piecewise constant between
bootstrap pillars, and discount factors are built by *daily compounding*

    CF(t1, t2) = prod_b (1 + f_b * d_b / 360)
    DF(t)      = 1 / CF(t0, t)

rather than by the continuous form DF(t2) = DF(t1) * exp(-f * (t2 - t1)).
SOFR accrues by daily simple compounding on ACT/360, so this makes futures
settlement and OIS float legs exact instead of approximate. The two forms
differ at O(f^2 * dt), which is ~0.2bp at 4% over a year -- large enough to
pollute a self-consistency check reported to 0.01bp.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


class Curve:
    """Piecewise-constant overnight forward curve on a fixed business day grid.

    The grid is shared (and never mutated) across rebootstraps, so the
    key-rate ladder only pays for the grid construction once.
    """

    def __init__(self, valuation_date: pd.Timestamp, dates: pd.DatetimeIndex) -> None:
        self.valuation_date = pd.Timestamp(valuation_date)
        self.dates = dates
        day_index = dates.values.astype("datetime64[D]").astype(np.int64)
        self.day_counts: np.ndarray = np.diff(day_index)
        self.dt: np.ndarray = self.day_counts / 360.0
        self.rates: np.ndarray = np.zeros(len(dates) - 1)
        self._growth: np.ndarray | None = None

    # -- grid ---------------------------------------------------------------

    def index_of(self, date: pd.Timestamp) -> int:
        """Position of the first grid business day on or after `date`."""
        return int(self.dates.searchsorted(pd.Timestamp(date), "left"))

    def empty_like(self) -> "Curve":
        """A fresh zero curve sharing this curve's grid."""
        return Curve(self.valuation_date, self.dates)

    # -- state --------------------------------------------------------------

    def set_forward(self, start: pd.Timestamp, end: pd.Timestamp, rate: float) -> None:
        """Set the overnight forward to `rate` on [start, end)."""
        self.rates[self.index_of(start) : self.index_of(end)] = rate
        self._growth = None

    @property
    def growth(self) -> np.ndarray:
        """Cumulative compounding factor from the valuation date to each grid day."""
        if self._growth is None:
            g = np.empty(len(self.dates))
            g[0] = 1.0
            np.cumprod(1.0 + self.rates * self.dt, out=g[1:])
            self._growth = g
        return self._growth

    # -- observables --------------------------------------------------------

    def growth_factor(self, d1: pd.Timestamp, d2: pd.Timestamp) -> float:
        g = self.growth
        return float(g[self.index_of(d2)] / g[self.index_of(d1)])

    def discount_factor(self, date: pd.Timestamp) -> float:
        return float(1.0 / self.growth[self.index_of(date)])

    def forward_on(self, date: pd.Timestamp) -> float:
        return float(self.rates[self.index_of(date)])
