"""USD SOFR OIS: schedule construction, par rate, annuity, PV.

Float leg is priced by explicitly compounding the overnight forwards inside
each annual period and discounting at that period's pay date:

    PV_float = sum_j ( CF_j - 1 ) * DF(pay_j)

With a zero payment lag this telescopes to DF(t0) - DF(tN); see
`telescoping_check`. With the market-standard 2-business-day lag it does not
telescope exactly, which is why the explicit form is used for pricing and the
identity is reported as a diagnostic rather than assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .calendar_utils import BusinessCalendar, accrual_schedule
from .curve import Curve
from .daycount import daycount


@dataclass
class OISSchedule:
    """Precomputed schedule bound to a specific curve grid."""

    tenor_years: int
    effective: pd.Timestamp
    maturity: pd.Timestamp
    roll_dates: list[pd.Timestamp]
    pay_dates: list[pd.Timestamp]
    taus: np.ndarray
    setting_idx: list[np.ndarray] = field(repr=False)
    setting_dt: list[np.ndarray] = field(repr=False)

    @property
    def last_pay_date(self) -> pd.Timestamp:
        return self.pay_dates[-1]


def build_ois_schedule(
    cal: BusinessCalendar,
    curve: Curve,
    tenor_years: int,
    cfg: dict[str, Any],
) -> OISSchedule:
    """Spot-starting USD SOFR OIS schedule: T+2 effective, annual fixed, ModFol."""
    effective = cal.add_business_days(curve.valuation_date, cfg["spot_lag_bd"])

    step = cfg["fixed_frequency_months"]
    n_periods = tenor_years * 12 // step
    roll_dates = [effective] + [
        cal.modified_following(effective + pd.DateOffset(months=step * k))
        for k in range(1, n_periods + 1)
    ]

    pay_dates = [
        cal.add_business_days(roll_dates[j + 1], cfg["payment_lag_bd"]) for j in range(n_periods)
    ]
    taus = np.array(
        [daycount(roll_dates[j], roll_dates[j + 1], cfg["fixed_basis"]) for j in range(n_periods)]
    )

    setting_idx: list[np.ndarray] = []
    setting_dt: list[np.ndarray] = []
    for j in range(n_periods):
        days, counts = accrual_schedule(cal, roll_dates[j], roll_dates[j + 1])
        setting_idx.append(np.array([curve.index_of(d) for d in days], dtype=np.int64))
        setting_dt.append(counts / 360.0)

    return OISSchedule(
        tenor_years=tenor_years,
        effective=effective,
        maturity=roll_dates[-1],
        roll_dates=roll_dates,
        pay_dates=pay_dates,
        taus=taus,
        setting_idx=setting_idx,
        setting_dt=setting_dt,
    )


def float_leg_and_annuity(curve: Curve, sched: OISSchedule) -> tuple[float, float]:
    """(PV of the compounded overnight leg, fixed-leg annuity), both per unit notional."""
    dfs = np.array([curve.discount_factor(p) for p in sched.pay_dates])
    float_pv = 0.0
    for j, (idx, dt) in enumerate(zip(sched.setting_idx, sched.setting_dt)):
        compounding = float(np.prod(1.0 + curve.rates[idx] * dt))
        float_pv += (compounding - 1.0) * dfs[j]
    annuity = float(np.dot(sched.taus, dfs))
    return float_pv, annuity


def par_rate(curve: Curve, sched: OISSchedule) -> float:
    float_pv, annuity = float_leg_and_annuity(curve, sched)
    return float_pv / annuity


def swap_pv(
    curve: Curve,
    sched: OISSchedule,
    fixed_rate: float,
    notional: float,
    payer: bool = True,
) -> float:
    """PV of a payer (pay fixed / receive SOFR) swap unless `payer=False`."""
    float_pv, annuity = float_leg_and_annuity(curve, sched)
    pv = (float_pv - fixed_rate * annuity) * notional
    return pv if payer else -pv


def telescoping_check(curve: Curve, sched: OISSchedule) -> dict[str, float]:
    """Compare the explicit float leg to the DF(t0) - DF(tN) telescoping identity.

    The gap is entirely the 2-business-day payment lag: the identity holds when
    each period is discounted at its accrual end rather than its pay date.
    """
    float_pv, _ = float_leg_and_annuity(curve, sched)
    identity = curve.discount_factor(sched.effective) - curve.discount_factor(sched.maturity)
    return {
        "float_leg_pv": float_pv,
        "telescoped_pv": identity,
        "difference": float_pv - identity,
    }
