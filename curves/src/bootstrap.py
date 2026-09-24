"""Sequential bootstrap: one instrument, one unknown forward segment, one Brent solve."""

from __future__ import annotations

from typing import Any

import pandas as pd
from scipy.optimize import brentq

from .curve import Curve
from .instruments import Instrument


def bootstrap(
    template: Curve,
    instruments: list[Instrument],
    cfg: dict[str, Any],
) -> tuple[Curve, list[dict[str, Any]]]:
    """Solve the piecewise-constant forward curve pinned by `instruments`.

    Instruments must already be sorted by `segment_end`. Each solve sets the
    trial forward on [previous pillar, end of grid) so no part of the curve is
    ever left at zero; the next instrument overwrites the tail beyond its own
    boundary, and nothing later ever touches a window an earlier instrument
    already priced through.
    """
    curve = template.empty_like()
    grid_end = curve.dates[-1]
    lo, hi = cfg["brentq_bracket"]

    log: list[dict[str, Any]] = []
    segment_start = curve.valuation_date

    for inst in instruments:

        def objective(f: float, _inst: Instrument = inst, _start=segment_start) -> float:
            curve.set_forward(_start, grid_end, f)
            return _inst.residual(curve)

        f_lo, f_hi = objective(lo), objective(hi)
        if f_lo * f_hi > 0:
            raise ValueError(
                f"{inst.label}: root not bracketed on [{lo}, {hi}] "
                f"(residuals {f_lo:.3e}, {f_hi:.3e})"
            )

        root, result = brentq(
            objective,
            lo,
            hi,
            xtol=cfg["brentq_xtol"],
            maxiter=cfg["brentq_maxiter"],
            full_output=True,
        )
        curve.set_forward(segment_start, grid_end, root)

        log.append(
            {
                "instrument": inst.label,
                "kind": inst.kind,
                "segment_start": segment_start,
                "segment_end": inst.segment_end,
                "pillar_date": inst.pillar_date,
                "forward_rate": root,
                "iterations": int(result.iterations),
                "function_calls": int(result.function_calls),
                "converged": bool(result.converged),
                "residual_bp": inst.residual(curve) * 1e4,
            }
        )
        segment_start = inst.segment_end

    return curve, log


def rebootstrap(
    template: Curve,
    instruments: list[Instrument],
    cfg: dict[str, Any],
    bumps: dict[str, float],
) -> Curve:
    """Rebuild the curve with `bumps` (label -> rate shift) applied to input quotes.

    This is the market-standard risk convention: shift the *inputs* and rerun
    the whole bootstrap, rather than perturbing the output curve directly.
    """
    for inst in instruments:
        inst.bump = bumps.get(inst.label, 0.0)
    try:
        curve, _ = bootstrap(template, instruments, cfg)
    finally:
        for inst in instruments:
            inst.bump = 0.0
    return curve


def curve_frame(curve: Curve, log: list[dict[str, Any]], cfg: dict[str, Any]) -> pd.DataFrame:
    """`curve.csv` contents: pillar, year fraction, DF, zero rate, segment forward."""
    from .daycount import daycount

    rows = []
    for entry in log:
        pillar = entry["pillar_date"]
        boundary = entry["segment_end"]
        df = curve.discount_factor(boundary)
        year_fraction = daycount(curve.valuation_date, boundary, cfg["zero_basis"])
        import numpy as np

        zero = -np.log(df) / year_fraction if year_fraction > 0 else float("nan")
        rows.append(
            {
                "instrument": entry["instrument"],
                "kind": entry["kind"],
                "pillar_date": pillar.date(),
                "curve_boundary": boundary.date(),
                "year_fraction": year_fraction,
                "discount_factor": df,
                "zero_rate_cc": zero,
                "forward_rate": entry["forward_rate"],
                "segment_start": entry["segment_start"].date(),
            }
        )
    return pd.DataFrame(rows)
