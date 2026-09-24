"""Bootstrap instruments: SR1 futures, SR3 futures, SOFR OIS swaps.

Each instrument precomputes, once, the business days whose fixing it depends
on -- split into a realized block (published fixings, fixed forever) and a
curve block (indices straight into `Curve.rates`). Repricing during Brent
iterations is then pure numpy on preindexed slices, which is what makes 28
full rebootstraps for the key-rate ladder cheap.

Every instrument exposes the same contract:

    segment_end  -- the curve boundary this instrument pins (bootstrap order)
    pillar_date  -- the date reported in curve.csv
    target_rate  -- market quote in rate terms, including any risk bump
    implied_rate -- the same quantity recomputed off the curve
    residual     -- implied - target, the function Brent drives to zero
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .calendar_utils import BusinessCalendar, accrual_schedule, imm_date, parse_contract_code
from .curve import Curve
from .swap import OISSchedule, build_ois_schedule, par_rate

_DATE_FORMAT = "%m/%d/%Y"


# ---------------------------------------------------------------------------
# loaders
# ---------------------------------------------------------------------------


def load_fixings(path) -> pd.Series:
    """Published SOFR fixings, percent in the file -> decimal in memory."""
    df = pd.read_csv(path, parse_dates=["Date"])
    return pd.Series(df["SOFR"].to_numpy() / 100.0, index=pd.DatetimeIndex(df["Date"])).sort_index()


def load_futures(path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["Last Trade"] = pd.to_datetime(df["Last Trade"], format=_DATE_FORMAT)
    df["Settlement"] = pd.to_datetime(df["Settlement"], format=_DATE_FORMAT)
    return df


def load_swaps(path) -> pd.DataFrame:
    return pd.read_csv(path).sort_values("tenor_years").reset_index(drop=True)


# ---------------------------------------------------------------------------
# base
# ---------------------------------------------------------------------------


@dataclass
class Instrument:
    label: str
    kind: str
    quote: float  # as quoted in the market (futures price / swap par rate)
    base_rate: float  # quote translated into rate terms
    pillar_date: pd.Timestamp
    segment_end: pd.Timestamp
    bump: float = 0.0

    @property
    def target_rate(self) -> float:
        return self.base_rate + self.bump

    def implied_rate(self, curve: Curve) -> float:
        raise NotImplementedError

    def residual(self, curve: Curve) -> float:
        return self.implied_rate(curve) - self.target_rate

    def implied_quote(self, curve: Curve) -> float:
        """Model value expressed in the instrument's own quoting convention."""
        return self.implied_rate(curve)


@dataclass
class _OvernightAccrual(Instrument):
    """Shared machinery for instruments settling off a strip of daily fixings."""

    period_start: pd.Timestamp = pd.NaT
    period_end: pd.Timestamp = pd.NaT
    realized_rates: np.ndarray = field(default_factory=lambda: np.zeros(0), repr=False)
    realized_days: np.ndarray = field(default_factory=lambda: np.zeros(0), repr=False)
    curve_idx: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64), repr=False)
    curve_days: np.ndarray = field(default_factory=lambda: np.zeros(0), repr=False)
    setting_days: pd.DatetimeIndex = field(
        default_factory=lambda: pd.DatetimeIndex([]), repr=False
    )
    all_days: np.ndarray = field(default_factory=lambda: np.zeros(0), repr=False)
    total_days: int = 0

    def bind(
        self,
        cal: BusinessCalendar,
        curve: Curve,
        fixings: pd.Series,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> None:
        days, counts = accrual_schedule(cal, start, end)
        realized = days < curve.valuation_date

        missing = days[realized].difference(fixings.index)
        if len(missing) > 0:
            raise ValueError(
                f"{self.label}: missing published SOFR fixings for {[str(d.date()) for d in missing]}"
            )

        self.period_start = start
        self.period_end = end
        self.setting_days = days
        self.all_days = counts.astype(float)
        self.realized_rates = fixings.reindex(days[realized]).to_numpy()
        self.realized_days = counts[realized].astype(float)
        self.curve_idx = np.array([curve.index_of(d) for d in days[~realized]], dtype=np.int64)
        self.curve_days = counts[~realized].astype(float)
        self.total_days = int(counts.sum())

    def rate_vector(self, curve: Curve) -> np.ndarray:
        return np.concatenate([self.realized_rates, curve.rates[self.curve_idx]])

    def split_growth(self, curve: Curve, unknown_from: pd.Timestamp) -> dict[str, float]:
        """Three-way compounding decomposition: realized / pre-bootstrapped / unknown.

        `unknown_from` is the start of this instrument's own bootstrap segment;
        everything between the valuation date and it was already solved by
        earlier instruments.
        """
        known = self.setting_days[len(self.realized_rates) :] < unknown_from
        curve_rates = curve.rates[self.curve_idx]

        realized_cf = float(np.prod(1.0 + self.realized_rates * self.realized_days / 360.0))
        pre_cf = float(np.prod(1.0 + curve_rates[known] * self.curve_days[known] / 360.0))
        unknown_cf = float(np.prod(1.0 + curve_rates[~known] * self.curve_days[~known] / 360.0))

        return {
            "realized_cf": realized_cf,
            "realized_days": float(self.realized_days.sum()),
            "prebootstrapped_cf": pre_cf,
            "prebootstrapped_days": float(self.curve_days[known].sum()),
            "unknown_cf": unknown_cf,
            "unknown_days": float(self.curve_days[~known].sum()),
            "total_cf": realized_cf * pre_cf * unknown_cf,
            "total_days": float(self.total_days),
        }


# ---------------------------------------------------------------------------
# SR3: three-month futures, daily compounded over the IMM reference quarter
# ---------------------------------------------------------------------------


@dataclass
class SR3Future(_OvernightAccrual):
    """rate = ( prod(1 + f_i * d_i/360) - 1 ) * 360 / D_quarter."""

    def implied_rate(self, curve: Curve) -> float:
        compounding = float(np.prod(1.0 + self.rate_vector(curve) * self.all_days / 360.0))
        return (compounding - 1.0) * 360.0 / self.total_days

    def implied_quote(self, curve: Curve) -> float:
        return 100.0 * (1.0 - self.implied_rate(curve))


# ---------------------------------------------------------------------------
# SR1: one-month futures, arithmetic average over the calendar month
# ---------------------------------------------------------------------------


@dataclass
class SR1Future(_OvernightAccrual):
    """rate = sum(f_i * n_i) / N_month, with non-publication days carrying the
    preceding fixing -- the CME arithmetic-average convention."""

    def implied_rate(self, curve: Curve) -> float:
        return float(np.dot(self.rate_vector(curve), self.all_days) / self.total_days)

    def implied_quote(self, curve: Curve) -> float:
        return 100.0 * (1.0 - self.implied_rate(curve))


# ---------------------------------------------------------------------------
# OIS swaps
# ---------------------------------------------------------------------------


@dataclass
class SwapInstrument(Instrument):
    schedule: OISSchedule | None = None

    def implied_rate(self, curve: Curve) -> float:
        assert self.schedule is not None
        return par_rate(curve, self.schedule)


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def build_instruments(
    cal: BusinessCalendar,
    curve: Curve,
    fixings: pd.Series,
    sr1: pd.DataFrame,
    sr3: pd.DataFrame,
    swaps: pd.DataFrame,
    cfg: dict[str, Any],
) -> tuple[list[Instrument], list[Instrument]]:
    """Returns (pillar instruments sorted by curve boundary, cross-check instruments).

    Instrument selection is a real modelling choice here, not bookkeeping. Every
    SR1 contract month in this snapshot (Mar-26 .. Aug-26) falls strictly inside
    an SR3 reference quarter, so the two strips are mutually redundant. Using
    both as pillars still reproduces every quote to machine precision, but it
    forces the entire SR1-vs-SR3 disagreement onto whatever short residual stub
    is left over -- producing adjacent forward segments of 1.83% and 4.40%,
    which is an artefact, not a rate view.

    `cfg["futures_source"]` picks the resolution; the discarded strip is
    returned as a cross-check so its reproduction error can be reported as the
    futures-strip basis rather than silently dropped.

    Ordering pillars by `segment_end` guarantees each instrument's accrual
    window is fully determined except for a single trailing unknown segment, so
    the bootstrap stays a sequential one-unknown-at-a-time solve.
    """
    convexity = cfg["convexity_adjustment_bp"] * 1e-4
    sr3_instruments: list[Instrument] = []
    sr1_instruments: list[Instrument] = []
    swap_instruments: list[Instrument] = []

    # -- SR3 --------------------------------------------------------------
    for _, row in sr3.iterrows():
        product, year, month = parse_contract_code(row["Product Code"])
        if product != "SR3":
            raise ValueError(f"{row['Product Code']} is not an SR3 contract")

        quarter_end = imm_date(year, month)
        prev = pd.Timestamp(year=year, month=month, day=1) - pd.DateOffset(months=3)
        quarter_start = imm_date(prev.year, prev.month)
        if quarter_end != row["Last Trade"]:
            raise ValueError(
                f"{row['Product Code']}: derived IMM {quarter_end.date()} != file last trade "
                f"{row['Last Trade'].date()}"
            )

        inst = SR3Future(
            label=row["Product Code"],
            kind="SR3",
            quote=float(row["Price"]),
            base_rate=(100.0 - float(row["Price"])) / 100.0 - convexity,
            pillar_date=quarter_end,
            segment_end=quarter_end,
        )
        inst.bind(cal, curve, fixings, quarter_start, quarter_end)
        sr3_instruments.append(inst)

    # -- SR1 --------------------------------------------------------------
    for _, row in sr1.iterrows():
        product, year, month = parse_contract_code(row["Product Code"])
        if product != "SR1":
            raise ValueError(f"{row['Product Code']} is not an SR1 contract")

        month_start = pd.Timestamp(year=year, month=month, day=1)
        next_month = month_start + pd.DateOffset(months=1)

        inst = SR1Future(
            label=row["Product Code"],
            kind="SR1",
            quote=float(row["Price"]),
            base_rate=(100.0 - float(row["Price"])) / 100.0 - convexity,
            pillar_date=next_month - pd.Timedelta(days=1),
            segment_end=next_month,
        )
        inst.bind(cal, curve, fixings, month_start, next_month)
        sr1_instruments.append(inst)

    # -- swaps ------------------------------------------------------------
    for _, row in swaps.iterrows():
        tenor = int(row["tenor_years"])
        sched = build_ois_schedule(cal, curve, tenor, cfg)
        swap_instruments.append(
            SwapInstrument(
                label=f"{tenor}Y",
                kind="SWAP",
                quote=float(row["par_rate"]),
                base_rate=float(row["par_rate"]),
                pillar_date=sched.maturity,
                segment_end=sched.last_pay_date,
                schedule=sched,
            )
        )

    # -- resolve the SR1 / SR3 overlap ------------------------------------
    source = cfg["futures_source"]
    if source == "SR3":
        futures, cross_check = sr3_instruments, sr1_instruments
    elif source == "ALL":
        futures, cross_check = sr3_instruments + sr1_instruments, []
    elif source == "SR1_THEN_SR3":
        handover = max(i.segment_end for i in sr1_instruments)
        kept = [i for i in sr3_instruments if i.period_start >= handover]
        futures = sr1_instruments + kept
        cross_check = [i for i in sr3_instruments if i.period_start < handover]
    else:
        raise ValueError(
            f"unknown futures_source {source!r}; expected 'SR3', 'SR1_THEN_SR3' or 'ALL'"
        )

    instruments = sorted(futures + swap_instruments, key=lambda x: x.segment_end)

    for a, b in zip(instruments, instruments[1:]):
        if b.segment_end <= a.segment_end:
            raise ValueError(
                f"non-monotone bootstrap pillars: {a.label} ends {a.segment_end.date()}, "
                f"{b.label} ends {b.segment_end.date()}"
            )

    return instruments, sorted(cross_check, key=lambda x: x.segment_end)
