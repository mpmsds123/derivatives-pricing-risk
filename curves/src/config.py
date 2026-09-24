"""Global configuration.

Every convention, bound, and bump size lives here. No magic numbers are
allowed anywhere else in the source tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

CONFIG: dict[str, Any] = {
    # ---- valuation --------------------------------------------------------
    # Last published SOFR fixing is 2026-03-06 (Fri); the curve is built as of
    # the next business day.
    "valuation_date": "2026-03-09",
    "curve_horizon": "2066-12-31",
    "calendar_start": "2025-01-01",
    # ---- data locations ---------------------------------------------------
    # All four input files are project-local. The raw CME / NY Fed pulls used to
    # be read in place from the folder above the repo, which meant a clone could
    # not run `run.py` at all; they were copied into data/ during the merge into
    # derivatives-pricing-risk so the module is self-contained and reproducible.
    "raw_data_dir": PROJECT_ROOT / "data",
    "data_dir": PROJECT_ROOT / "data",
    "output_dir": PROJECT_ROOT / "output",
    "sr1_file": "1m_futures_data.csv",
    "sr3_file": "3m_futures_data.csv",
    "fixings_file": "rzd_rates.csv",
    "swap_file": "swap_rates.csv",
    # ---- day count / schedule conventions ---------------------------------
    "sofr_basis": "ACT/360",  # overnight accrual
    "fixed_basis": "ACT/360",  # USD SOFR OIS fixed leg
    "zero_basis": "ACT/365F",  # reporting basis for zero rates only
    "fixed_frequency_months": 12,  # annual fixed leg
    "spot_lag_bd": 2,  # T+2 effective date
    "payment_lag_bd": 2,  # payment 2 good business days after period end
    "roll_convention": "MODFOL",
    # ---- instrument selection ---------------------------------------------
    # SR1 months and SR3 quarters overlap completely in this data snapshot, so
    # they cannot both be pillars: forcing both pins every disagreement onto the
    # residual stubs and produces a sawtooth forward curve. Options:
    #   "SR3"           SR3 strip as pillars, SR1 quotes reported as a basis check
    #   "SR1_THEN_SR3"  SR1 strip in front, SR3 from the first non-overlapping quarter
    #   "ALL"           everything as pillars (reproduces every quote, sawtooth curve)
    "futures_source": "SR3",
    # ---- modelling assumptions --------------------------------------------
    # Futures convexity is ignored out to the end of the SR3 strip (~1.5Y).
    # Set non-zero to subtract a flat adjustment from every futures rate.
    "convexity_adjustment_bp": 0.0,
    # ---- solver -----------------------------------------------------------
    "brentq_bracket": (-0.02, 0.20),
    "brentq_xtol": 1e-15,
    "brentq_maxiter": 200,
    # ---- risk -------------------------------------------------------------
    "bump_bp": 1.0,
    "benchmark_notional": 100_000_000.0,
    # A par swap whose maturity coincides with an input pillar has a degenerate
    # key-rate profile: bumping any other input is undone by the rebootstrap, so
    # the whole DV01 lands on its own pillar. That is a correct result and worth
    # showing, but it is not a risk profile. The off-pillar maturities (1Y, 11Y)
    # and the off-market strike are what produce a distributed ladder.
    "benchmarks": (
        {"label": "1Y_par", "tenor_years": 1, "strike_offset_bp": 0.0, "coincident": False},
        {"label": "2Y_par", "tenor_years": 2, "strike_offset_bp": 0.0, "coincident": True},
        {"label": "5Y_par", "tenor_years": 5, "strike_offset_bp": 0.0, "coincident": True},
        {"label": "10Y_par", "tenor_years": 10, "strike_offset_bp": 0.0, "coincident": True},
        {"label": "30Y_par", "tenor_years": 30, "strike_offset_bp": 0.0, "coincident": True},
        {"label": "11Y_par", "tenor_years": 11, "strike_offset_bp": 0.0, "coincident": False},
        {
            "label": "10Y_off_market",
            "tenor_years": 10,
            "strike_offset_bp": 100.0,
            "coincident": False,
        },
    ),
    "headline_benchmark": "10Y_par",
    "distributed_benchmark": "11Y_par",
    "off_market_benchmark": "10Y_off_market",
    # ---- reporting --------------------------------------------------------
    "self_consistency_tolerance_bp": 0.01,
}
