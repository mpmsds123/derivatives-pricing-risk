"""Per-expiry SABR calibration.

beta is fixed. alpha is solved analytically from the ATM constraint, which
removes it from the optimisation and makes the ATM vol reproduce exactly.
Only (rho, nu) are optimised.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

import numpy as np
from scipy.optimize import least_squares

from .config import CONFIG
from .hagan import sabr_vol, sabr_atm_vol

_CAL = CONFIG["calibrate"]
_BP = CONFIG["output"]["bp"]


@dataclass
class SABRFit:
    expiry: str
    T: float
    forward: float
    atm_vol: float
    alpha: float
    beta: float
    rho: float
    nu: float
    rmse_bps: float
    max_abs_err_bps: float
    max_err_strike: float
    max_err_offset_bp: float
    n_points: int
    cost: float
    n_starts_converged: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def solve_alpha(F: float, T: float, atm_vol: float, beta: float,
                rho: float, nu: float) -> float:
    """Smallest strictly positive real root of the ATM cubic in alpha.

        [(1-b)^2 T / (24 F^(2-2b))] a^3
      + [rho b nu T / (4 F^(1-b))]  a^2
      + [1 + (2-3rho^2) nu^2 T/24]  a
      - sigma_atm F^(1-b)          = 0

    Choosing the smallest positive root is the standard convention: the larger
    roots are spurious branches of the cubic, not economically meaningful
    volatility levels.
    """
    one_mb = 1.0 - beta
    c3 = one_mb * one_mb * T / (24.0 * F ** (2.0 - 2.0 * beta))
    c2 = rho * beta * nu * T / (4.0 * F ** one_mb)
    c1 = 1.0 + (2.0 - 3.0 * rho * rho) * nu * nu * T / 24.0
    c0 = -atm_vol * F ** one_mb

    roots = np.roots([c3, c2, c1, c0])
    real = roots[np.abs(roots.imag) < 1e-10].real
    positive = real[real > _CAL["alpha_min"]]
    if positive.size == 0:
        # Degenerate cubic: fall back to the leading-order inversion.
        return atm_vol * F ** one_mb
    return float(positive.min())


def _residuals(params: np.ndarray, K: np.ndarray, vols: np.ndarray, F: float,
               T: float, atm_vol: float, beta: float) -> np.ndarray:
    rho, nu = float(params[0]), float(params[1])
    alpha = solve_alpha(F, T, atm_vol, beta, rho, nu)
    model = sabr_vol(K, F, T, alpha, beta, rho, nu)
    return np.asarray(model, dtype=float) - vols


def calibrate_expiry(expiry: str, T: float, F: float, atm_vol: float,
                     strikes: np.ndarray, market_vols: np.ndarray,
                     beta: float | None = None,
                     seed: int | None = None) -> SABRFit:
    """Least-squares fit of (rho, nu) for one expiry, multi-start."""
    beta = CONFIG["model"]["beta"] if beta is None else beta
    K = np.asarray(strikes, dtype=float)
    vols = np.asarray(market_vols, dtype=float)

    lo = np.array(_CAL["bounds_lower"], dtype=float)
    hi = np.array(_CAL["bounds_upper"], dtype=float)

    rng = np.random.default_rng(_CAL["seed"] if seed is None else seed)
    starts = [np.array(_CAL["initial_guess"], dtype=float)]
    starts += [lo + rng.random(2) * (hi - lo) for _ in range(_CAL["n_random_starts"])]

    best, n_ok = None, 0
    for x0 in starts:
        x0 = np.clip(x0, lo, hi)
        try:
            sol = least_squares(
                _residuals, x0, bounds=(lo, hi),
                args=(K, vols, F, T, atm_vol, beta),
                xtol=_CAL["xtol"], ftol=_CAL["ftol"], max_nfev=_CAL["max_nfev"],
            )
        except (ValueError, FloatingPointError):
            continue
        if not np.isfinite(sol.cost):
            continue
        n_ok += 1
        if best is None or sol.cost < best.cost:
            best = sol

    if best is None:
        raise RuntimeError(f"SABR calibration failed for expiry {expiry}")

    rho, nu = float(best.x[0]), float(best.x[1])
    alpha = solve_alpha(F, T, atm_vol, beta, rho, nu)

    model = np.asarray(sabr_vol(K, F, T, alpha, beta, rho, nu), dtype=float)
    err = model - vols
    rmse_bps = float(np.sqrt(np.mean(err ** 2)) * _BP)
    imax = int(np.argmax(np.abs(err)))

    return SABRFit(
        expiry=expiry, T=T, forward=F, atm_vol=atm_vol,
        alpha=alpha, beta=beta, rho=rho, nu=nu,
        rmse_bps=rmse_bps,
        max_abs_err_bps=float(np.abs(err[imax]) * _BP),
        max_err_strike=float(K[imax]),
        max_err_offset_bp=float((K[imax] / F - 1.0) * _BP),
        n_points=int(K.size), cost=float(best.cost), n_starts_converged=n_ok,
    )


def check_atm_reproduction(fit: SABRFit) -> float:
    """ATM reproduction error in bp. Should be ~0 by construction of the cubic."""
    modelled = sabr_atm_vol(fit.forward, fit.T, fit.alpha, fit.beta, fit.rho, fit.nu)
    return float((modelled - fit.atm_vol) * _BP)
