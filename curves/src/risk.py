"""Parallel DV01 and the key-rate ladder.

Both are computed by bumping *input quotes* and rerunning the full bootstrap.
Perturbing the output curve directly would be wrong: a 1bp shift in a futures
price does not translate into a 1bp shift of the forward on that segment once
realized fixings and overlapping contracts are in play, and it would silently
break the sum-of-key-rates vs parallel-DV01 reconciliation.

One consequence of doing it correctly is worth stating up front. A par swap
whose maturity coincides with an input swap pillar has a *degenerate* key-rate
profile: bumping any other input moves that segment, the later instruments
rebootstrap to hold their own par rates fixed, and the benchmark's par rate is
therefore unchanged -- so its PV is unchanged and the KR01 is zero. All of the
DV01 lands on its own pillar. That is arithmetic, not an artefact, and it is
why the benchmark set here also contains off-pillar maturities and an
off-market strike.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from .bootstrap import rebootstrap
from .curve import Curve
from .instruments import Instrument
from .swap import OISSchedule, par_rate, swap_pv


@dataclass
class BenchmarkSwap:
    label: str
    tenor_years: int
    notional: float
    par_rate: float
    fixed_rate: float
    strike_offset_bp: float
    coincident: bool
    schedule: OISSchedule
    pv: float


def make_benchmark_swaps(
    curve: Curve,
    schedules: dict[int, OISSchedule],
    cfg: dict[str, Any],
) -> list[BenchmarkSwap]:
    notional = cfg["benchmark_notional"]
    out: list[BenchmarkSwap] = []
    for spec in cfg["benchmarks"]:
        sched = schedules[spec["tenor_years"]]
        par = par_rate(curve, sched)
        fixed = par + spec["strike_offset_bp"] * 1e-4
        out.append(
            BenchmarkSwap(
                label=spec["label"],
                tenor_years=spec["tenor_years"],
                notional=notional,
                par_rate=par,
                fixed_rate=fixed,
                strike_offset_bp=spec["strike_offset_bp"],
                coincident=spec["coincident"],
                schedule=sched,
                pv=swap_pv(curve, sched, fixed, notional),
            )
        )
    return out


def risk_ladder(
    template: Curve,
    instruments: list[Instrument],
    benchmarks: list[BenchmarkSwap],
    cfg: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (wide ladder, long ladder).

    Wide: one row per benchmark swap, one `kr01_<label>` column per input
    quote, plus parallel DV01 and the sum-vs-parallel reconciliation.
    Long: one row per (benchmark, input quote) pair, convenient for plotting.

    Cost is one rebootstrap per input plus one for the parallel shift --
    `len(instruments) + 1` full curve builds, all benchmarks repriced off each.
    """
    bump = cfg["bump_bp"] * 1e-4

    parallel_curve = rebootstrap(template, instruments, cfg, {i.label: bump for i in instruments})
    parallel_dv01 = {
        b.label: swap_pv(parallel_curve, b.schedule, b.fixed_rate, b.notional) - b.pv
        for b in benchmarks
    }

    key_rate: dict[str, dict[str, float]] = {}
    for inst in instruments:
        bumped = rebootstrap(template, instruments, cfg, {inst.label: bump})
        key_rate[inst.label] = {
            b.label: swap_pv(bumped, b.schedule, b.fixed_rate, b.notional) - b.pv
            for b in benchmarks
        }

    long = pd.DataFrame(
        [
            {
                "benchmark": b.label,
                "tenor_years": b.tenor_years,
                "notional": b.notional,
                "pillar_instrument": inst.label,
                "pillar_kind": inst.kind,
                "pillar_date": inst.pillar_date.date(),
                "pillar_years": (inst.pillar_date - template.valuation_date).days / 365.0,
                "kr01": key_rate[inst.label][b.label],
            }
            for b in benchmarks
            for inst in instruments
        ]
    )

    wide_rows = []
    for b in benchmarks:
        row: dict[str, Any] = {
            "benchmark": b.label,
            "tenor_years": b.tenor_years,
            "notional": b.notional,
            "par_rate": b.par_rate,
            "fixed_rate": b.fixed_rate,
            "strike_offset_bp": b.strike_offset_bp,
            "maturity_on_pillar": b.coincident,
            "pv": b.pv,
            "parallel_dv01": parallel_dv01[b.label],
        }
        total = 0.0
        for inst in instruments:
            value = key_rate[inst.label][b.label]
            row[f"kr01_{inst.label}"] = value
            total += value
        row["kr01_sum"] = total
        gap = total - parallel_dv01[b.label]
        row["sum_minus_parallel"] = gap
        row["sum_minus_parallel_pct"] = (
            100.0 * gap / parallel_dv01[b.label] if parallel_dv01[b.label] else float("nan")
        )
        # how concentrated is the profile: share of gross KR01 on the largest pillar
        gross = sum(abs(key_rate[i.label][b.label]) for i in instruments)
        largest = max(abs(key_rate[i.label][b.label]) for i in instruments)
        row["max_pillar_share_pct"] = 100.0 * largest / gross if gross else float("nan")
        wide_rows.append(row)

    ordered = (
        [
            "benchmark",
            "tenor_years",
            "notional",
            "par_rate",
            "fixed_rate",
            "strike_offset_bp",
            "maturity_on_pillar",
            "pv",
            "parallel_dv01",
        ]
        + [f"kr01_{i.label}" for i in instruments]
        + ["kr01_sum", "sum_minus_parallel", "sum_minus_parallel_pct", "max_pillar_share_pct"]
    )
    return pd.DataFrame(wide_rows)[ordered], long
