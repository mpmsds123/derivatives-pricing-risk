"""Central configuration.

Every convention, bound, bump size and filter threshold lives here so that no
numeric constant is buried in the source. Modules import CONFIG rather than
declaring their own literals.
"""
from __future__ import annotations

from typing import Any

CONFIG: dict[str, Any] = {
    # ---------------------------------------------------------------- data
    "data": {
        "underlying": "SPY",
        # Snapshot pinned on disk so a rerun reproduces the calibration exactly.
        # yfinance is a live source; re-pulling on another day gives other numbers.
        "snapshot_dir": "data/raw",
        "asof": "2026-08-07",
        # Third-Friday expiries carry the deepest open interest on SPY.
        # 2028-06-16 was pulled but excluded: its IV second-difference roughness
        # is 0.445 vol pts against <0.07 elsewhere (stale LEAPS quotes).
        "expiries": [
            "2026-09-18",
            "2026-10-16",
            "2026-12-18",
            "2027-03-19",
            "2027-06-17",
            "2027-09-17",
            "2027-12-17",
            "2028-12-15",
        ],
        # Risk-free proxy: Treasury constant-maturity yields, linear in T.
        "rate_proxies": {"^IRX": 0.25, "^FVX": 5.0, "^TNX": 10.0},
        "day_count": 365.25,
    },

    # ------------------------------------------------------ liquidity filter
    # Rationale for each rule is given in the README.
    "filter": {
        "otm_only": True,          # puts below F, calls above F
        "min_bid": 0.05,           # drop penny quotes: one $0.01 tick dominates IV
        "max_rel_spread": 0.50,    # (ask-bid)/mid guard against crossed/stale books
        "min_open_interest": 100,  # stock measure of liquidity, robust to a quiet day
        "min_volume": 0,           # flow measure; redundant after OI on this snapshot
        "delta_band": (0.05, 0.95),  # smile range in |delta|, widens with sqrt(T)
        # Forward implication: pin r, solve F from put-call parity. A joint
        # (DF, F) regression is ill-conditioned and returns DF > 1 at short T.
        # Parity window is centred on the FORWARD and found by fixed-point
        # iteration -- spot-centring biases long-dated expiries (see loader).
        "parity_window": 0.03,
        "parity_min_bid": 0.05,
        "parity_min_strikes": 3,
        "parity_max_iter": 50,
        "parity_tolerance": 1e-6,
        "parity_fallback_n": 8,    # nearest-N strikes when the window is empty
    },

    # --------------------------------------------------------------- model
    "model": {
        "beta": 0.5,               # fixed; near-degenerate with rho (see README)
        "atm_tolerance": 1e-12,    # |K-F| below this uses the ATM branch
    },

    # --------------------------------------------------------- calibration
    "calibrate": {
        "bounds_lower": [-0.999, 1e-4],   # (rho, nu)
        "bounds_upper": [0.999, 5.0],
        "initial_guess": [-0.3, 0.4],
        "n_random_starts": 5,
        "seed": 20260807,
        "alpha_min": 1e-8,         # smallest strictly positive real cubic root
        "max_nfev": 10000,
        "xtol": 1e-12,
        "ftol": 1e-12,
    },

    # ---------------------------------------------------------- arbitrage
    # Spec's rates-context offsets are additive bp on a ~3.85% forward. On equity
    # they must be relative: K = F*(1 + bp/1e4). Ranges rescaled to the equity
    # smile width, preserving the spec's structure (density 2x, stress 3x the
    # calibration window) -- +/-400bp would sit inside the fitted region and
    # could not reach SABR's low-strike density failure.
    "arbitrage": {
        "calib_window_bp": 3000.0,
        "density_range_bp": 6000.0,
        "stress_range_bp": 9000.0,
        "density_step_bp": 10.0,
        # The relative parametrisation K = F(1 + bp/1e4) is bounded below at
        # -10000bp (K = 0), so a symmetric bp grid reaches only 0.10*F and
        # structurally cannot probe the low-strike wing -- where SABR fails.
        # The deep-wing probe is therefore run on a log-spaced K/F grid.
        "deep_wing_kf_min": 1e-3,
        "deep_wing_kf_max": 1.9,     # matches the +9000bp upper end
        "deep_wing_points": 400_000,
        "calendar_k_grid": [-0.30, -0.20, -0.15, -0.10, -0.05,
                            0.0, 0.05, 0.10, 0.15, 0.20],
        "calendar_tolerance": 0.0,   # dw/dT must be >= this
    },

    # -------------------------------------------------------------- output
    "output": {
        "dir": "output",
        "dpi": 140,
        "bp": 1e4,
    },
}
