"""Static-arbitrage diagnostics on the calibrated surface.

Butterfly  -- risk-neutral density via Breeden-Litzenberger must be >= 0.
Calendar   -- total implied variance must be non-decreasing in T at fixed
              log-moneyness.
Stress     -- the same density check pushed far outside the fitted strike range,
              where SABR's low-strike wing is known to fail.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .black import black_price
from .calibrate import SABRFit
from .config import CONFIG
from .hagan import sabr_vol

_ARB = CONFIG["arbitrage"]
_BP = CONFIG["output"]["bp"]


@dataclass
class DensityResult:
    expiry: str
    T: float
    forward: float
    range_bp: float
    strikes: np.ndarray
    vols: np.ndarray
    density: np.ndarray
    n_negative: int
    n_total: int
    min_density: float
    neg_low_bp: float | None
    neg_high_bp: float | None


def _second_derivative(K: np.ndarray, y: np.ndarray) -> np.ndarray:
    """d2y/dK2 by the general three-point stencil (handles non-uniform grids)."""
    h1 = K[1:-1] - K[:-2]
    h2 = K[2:] - K[1:-1]
    return 2.0 * (h1 * y[2:] - (h1 + h2) * y[1:-1] + h2 * y[:-2]) \
        / (h1 * h2 * (h1 + h2))


def otm_density(F: float, K: np.ndarray, T: float,
                vols: np.ndarray) -> np.ndarray:
    """q(K) = d2C/dK2, evaluated on whichever leg is OTM at that strike.

    d2P/dK2 == d2C/dK2 exactly, since C - P = F - K is linear in K, so either
    leg recovers the same density analytically. Numerically they do not: a deep
    ITM call is C ~ F - K, an almost perfectly linear function, and differencing
    it twice is catastrophic cancellation -- the low-strike wing comes back with
    tens of thousands of spurious negative points and |q| errors of order 1e-3.

    Note the mix must be applied to the DENSITIES, not to the prices. Splicing
    prices first puts a kink of slope 1 at K = F into the differenced function,
    and the second derivative of a kink is a delta spike that both fakes a
    violation and destroys the probability mass.
    """
    puts = np.asarray(black_price(F, K, T, vols, is_call=False, discount=1.0),
                      dtype=float)
    calls = np.asarray(black_price(F, K, T, vols, is_call=True, discount=1.0),
                       dtype=float)
    q_put = _second_derivative(K, puts)
    q_call = _second_derivative(K, calls)
    return np.where(K[1:-1] < F, q_put, q_call)


def strike_grid(F: float, range_bp: float, step_bp: float) -> np.ndarray:
    """Relative-moneyness strike grid: K = F * (1 + bp/1e4).

    The spec's offsets are additive bp on a rates forward; on equity the only
    coherent reading is relative, so bp are applied multiplicatively.
    """
    n = int(round(2.0 * range_bp / step_bp)) + 1
    offsets = np.linspace(-range_bp, range_bp, n)
    return F * (1.0 + offsets / _BP)


def breeden_litzenberger(fit: SABRFit, range_bp: float | None = None,
                         step_bp: float | None = None) -> DensityResult:
    """q(K) = d2C/dK2 on undiscounted Black calls priced off the SABR smile.

    Central finite difference; the grid is uniform so the step is exact.
    """
    range_bp = _ARB["density_range_bp"] if range_bp is None else range_bp
    step_bp = _ARB["density_step_bp"] if step_bp is None else step_bp

    K = strike_grid(fit.forward, range_bp, step_bp)
    vols = np.asarray(
        sabr_vol(K, fit.forward, fit.T, fit.alpha, fit.beta, fit.rho, fit.nu),
        dtype=float,
    )
    density = otm_density(fit.forward, K, fit.T, vols)
    K_mid = K[1:-1]

    neg = density < 0.0
    if neg.any():
        neg_bp = (K_mid[neg] / fit.forward - 1.0) * _BP
        lo, hi = float(neg_bp.min()), float(neg_bp.max())
    else:
        lo = hi = None

    return DensityResult(
        expiry=fit.expiry, T=fit.T, forward=fit.forward, range_bp=range_bp,
        strikes=K_mid, vols=vols[1:-1], density=density,
        n_negative=int(neg.sum()), n_total=int(density.size),
        min_density=float(density.min()), neg_low_bp=lo, neg_high_bp=hi,
    )


@dataclass
class CalendarViolation:
    log_moneyness: float
    T_lower: float
    T_upper: float
    w_lower: float
    w_upper: float
    dw: float


def calendar_check(fits: list[SABRFit],
                   k_grid: list[float] | None = None) -> list[CalendarViolation]:
    """Total implied variance w = sigma^2 T must be non-decreasing in T at
    fixed log-moneyness k = ln(K/F). Each expiry has its own forward, so k is
    converted to that expiry's absolute strike before evaluating the smile."""
    k_grid = _ARB["calendar_k_grid"] if k_grid is None else k_grid
    tol = _ARB["calendar_tolerance"]

    ordered = sorted(fits, key=lambda f: f.T)
    violations: list[CalendarViolation] = []

    for k in k_grid:
        w_prev, T_prev = None, None
        for fit in ordered:
            K = fit.forward * np.exp(k)
            vol = float(np.asarray(sabr_vol(K, fit.forward, fit.T, fit.alpha,
                                            fit.beta, fit.rho, fit.nu)))
            w = vol * vol * fit.T
            if w_prev is not None and (w - w_prev) < tol:
                violations.append(CalendarViolation(
                    log_moneyness=k, T_lower=T_prev, T_upper=fit.T,
                    w_lower=w_prev, w_upper=w, dw=w - w_prev))
            w_prev, T_prev = w, fit.T

    return violations


def calendar_surface(fits: list[SABRFit],
                     k_grid: list[float] | None = None) -> list[dict]:
    """Total implied variance at each (k, T), so a zero-violation calendar
    result is still traceable to the numbers behind it."""
    k_grid = _ARB["calendar_k_grid"] if k_grid is None else k_grid
    ordered = sorted(fits, key=lambda f: f.T)
    rows: list[dict] = []
    for k in k_grid:
        w_prev = None
        for fit in ordered:
            K = fit.forward * np.exp(k)
            vol = float(np.asarray(sabr_vol(K, fit.forward, fit.T, fit.alpha,
                                            fit.beta, fit.rho, fit.nu)))
            w = vol * vol * fit.T
            rows.append({"log_moneyness": k, "expiry": fit.expiry, "T": fit.T,
                         "strike": K, "vol": vol, "total_variance": w,
                         "dw_from_previous": None if w_prev is None else w - w_prev})
            w_prev = w
    return rows


def extrapolation_stress(fit: SABRFit) -> DensityResult:
    """Density check on the far wings, outside the calibration range."""
    return breeden_litzenberger(fit, range_bp=_ARB["stress_range_bp"])


def deep_wing_density(fit: SABRFit) -> DensityResult:
    """Density on a log-spaced K/F grid reaching arbitrarily close to K = 0.

    A symmetric bp grid cannot get below 0.10*F (see config), but SABR's known
    static-arbitrage failure lives in exactly that region. This probe is what
    makes the butterfly diagnostic non-vacuous on an equity surface.
    """
    F = fit.forward
    K = np.geomspace(_ARB["deep_wing_kf_min"] * F,
                     _ARB["deep_wing_kf_max"] * F,
                     _ARB["deep_wing_points"])
    vols = np.asarray(
        sabr_vol(K, F, fit.T, fit.alpha, fit.beta, fit.rho, fit.nu), dtype=float)
    density = otm_density(F, K, fit.T, vols)
    K_mid = K[1:-1]

    neg = density < 0.0
    if neg.any():
        neg_kf = K_mid[neg] / F
        lo, hi = float((neg_kf.min() - 1.0) * _BP), float((neg_kf.max() - 1.0) * _BP)
    else:
        lo = hi = None

    return DensityResult(
        expiry=fit.expiry, T=fit.T, forward=F,
        range_bp=float("nan"), strikes=K_mid, vols=vols[1:-1], density=density,
        n_negative=int(neg.sum()), n_total=int(density.size),
        min_density=float(density.min()), neg_low_bp=lo, neg_high_bp=hi,
    )


def summarise_density(results: list[DensityResult]) -> dict[str, float]:
    total_neg = sum(r.n_negative for r in results)
    total_pts = sum(r.n_total for r in results)
    return {
        "n_negative": total_neg,
        "n_total": total_pts,
        "pct_negative": 100.0 * total_neg / total_pts if total_pts else 0.0,
        "min_density": min((r.min_density for r in results), default=float("nan")),
    }
