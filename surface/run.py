#!/usr/bin/env python3
"""SABR volatility surface calibration and arbitrage diagnostics.

    python run.py            # cached snapshot (reproducible)
    python run.py --live     # re-pull from yfinance
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from data.loader import load_equity_chain
from src.arbitrage import (breeden_litzenberger, calendar_check,
                           calendar_surface, deep_wing_density,
                           extrapolation_stress, summarise_density)
from src.calibrate import calibrate_expiry, check_atm_reproduction
from src.config import CONFIG
from src.hagan import sabr_vol

OUT = ROOT / CONFIG["output"]["dir"]
DPI = CONFIG["output"]["dpi"]
BP = CONFIG["output"]["bp"]
ARB = CONFIG["arbitrage"]


# ---------------------------------------------------------------- plotting
def plot_smiles(market, fits, path: Path) -> None:
    n = len(fits)
    ncol = 3
    nrow = int(np.ceil(n / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(5.2 * ncol, 3.6 * nrow),
                             squeeze=False)

    for ax, fit in zip(axes.flat, fits):
        smile = market.smile(fit.expiry)
        K = smile.strike.values
        dense = np.linspace(K.min(), K.max(), 400)
        model = sabr_vol(dense, fit.forward, fit.T, fit.alpha, fit.beta,
                         fit.rho, fit.nu)

        ax.scatter((K / fit.forward - 1.0) * BP, smile.iv.values * 100,
                   s=14, alpha=0.75, label="market", color="#2b6cb0", zorder=3)
        ax.plot((dense / fit.forward - 1.0) * BP, np.asarray(model) * 100,
                lw=1.8, color="#c53030", label="SABR", zorder=2)
        ax.axvline(0.0, color="grey", lw=0.7, ls=":")
        ax.set_title(f"{fit.expiry}  T={fit.T:.2f}y  RMSE={fit.rmse_bps:.1f}bp",
                     fontsize=10)
        ax.set_xlabel("strike offset (bp of forward)", fontsize=8)
        ax.set_ylabel("implied vol (%)", fontsize=8)
        ax.tick_params(labelsize=8)
        ax.legend(fontsize=8, frameon=False)
        ax.grid(alpha=0.25)

    for ax in axes.flat[n:]:
        ax.axis("off")

    fig.suptitle(f"SABR smile fit — {market.underlying} as of {market.asof} "
                 f"(beta={CONFIG['model']['beta']} fixed)", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(path, dpi=DPI)
    plt.close(fig)


def plot_param_term_structure(fits, path: Path) -> None:
    T = [f.T for f in fits]
    fig, axes = plt.subplots(1, 3, figsize=(14, 3.8))
    for ax, (vals, name, color) in zip(axes, [
        ([f.alpha for f in fits], r"$\alpha$  (vol level)", "#2b6cb0"),
        ([f.rho for f in fits], r"$\rho$  (skew)", "#c53030"),
        ([f.nu for f in fits], r"$\nu$  (vol-of-vol)", "#2f855a"),
    ]):
        ax.plot(T, vals, "o-", color=color, lw=1.8, ms=5)
        ax.set_title(name, fontsize=11)
        ax.set_xlabel("expiry (years)", fontsize=9)
        ax.grid(alpha=0.25)
        ax.tick_params(labelsize=8)
    fig.suptitle("SABR parameter term structure (each expiry calibrated independently)",
                 fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(path, dpi=DPI)
    plt.close(fig)


def plot_densities(densities, path: Path) -> None:
    n = len(densities)
    ncol = 3
    nrow = int(np.ceil(n / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(5.2 * ncol, 3.6 * nrow),
                             squeeze=False)

    for ax, d in zip(axes.flat, densities):
        offset = (d.strikes / d.forward - 1.0) * BP
        ax.plot(offset, d.density, lw=1.4, color="#2b6cb0")
        ax.axhline(0.0, color="grey", lw=0.8)
        ax.fill_between(offset, d.density, 0.0, where=d.density < 0.0,
                        color="#c53030", alpha=0.55, interpolate=True,
                        label="q(K) < 0")
        ax.axvspan(-ARB["calib_window_bp"], ARB["calib_window_bp"],
                   color="grey", alpha=0.10, label="calibration window")
        ax.set_title(f"{d.expiry}  T={d.T:.2f}y  neg={d.n_negative}", fontsize=10)
        ax.set_xlabel("strike offset (bp of forward)", fontsize=8)
        ax.set_ylabel("q(K)", fontsize=8)
        ax.tick_params(labelsize=8)
        ax.grid(alpha=0.25)
        if d.n_negative:
            ax.legend(fontsize=7, frameon=False)

    for ax in axes.flat[n:]:
        ax.axis("off")

    fig.suptitle(f"Breeden–Litzenberger risk-neutral density "
                 f"(stress range ±{ARB['stress_range_bp']:.0f}bp)", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(path, dpi=DPI)
    plt.close(fig)


# ------------------------------------------------------------------- report
def write_arbitrage_report(fits, dens_calib, dens_stress, dens_deep, calendar,
                           path: Path, market) -> None:
    L: list[str] = []
    add = L.append
    add("=" * 78)
    add("SABR SURFACE — STATIC ARBITRAGE REPORT")
    add("=" * 78)
    add(f"underlying        : {market.underlying}")
    add(f"as-of             : {market.asof}")
    add(f"spot              : {market.spot:.4f}")
    add(f"expiries          : {len(fits)}")
    add(f"calibration points: {sum(f.n_points for f in fits)}")
    add(f"beta (fixed)      : {CONFIG['model']['beta']}")
    add("")
    add("Strike offsets are RELATIVE on equity: K = F * (1 + bp/1e4).")
    add(f"calibration window: ±{ARB['calib_window_bp']:.0f}bp")
    add(f"density grid      : ±{ARB['density_range_bp']:.0f}bp, "
        f"step {ARB['density_step_bp']:.0f}bp")
    add(f"stress grid       : ±{ARB['stress_range_bp']:.0f}bp")
    add("")

    add("-" * 78)
    add("1. BUTTERFLY — density q(K) = d2C/dK2 must be >= 0")
    add("-" * 78)
    add("")
    add("  (a) within the density range")
    add(f"  {'expiry':<12} {'T':>6} {'n_neg':>7} {'n_pts':>7} {'min q':>14} {'violation range (bp)':>26}")
    for d in dens_calib:
        rng = (f"{d.neg_low_bp:.0f} .. {d.neg_high_bp:.0f}"
               if d.n_negative else "—")
        add(f"  {d.expiry:<12} {d.T:6.2f} {d.n_negative:7d} {d.n_total:7d} "
            f"{d.min_density:14.6e} {rng:>26}")
    s = summarise_density(dens_calib)
    add(f"  TOTAL: {s['n_negative']} / {s['n_total']} points negative "
        f"({s['pct_negative']:.2f}%), min density {s['min_density']:.6e}")
    add("")

    add("  (b) extrapolation stress")
    add(f"  {'expiry':<12} {'T':>6} {'n_neg':>7} {'n_pts':>7} {'min q':>14} {'violation range (bp)':>26}")
    for d in dens_stress:
        rng = (f"{d.neg_low_bp:.0f} .. {d.neg_high_bp:.0f}"
               if d.n_negative else "—")
        add(f"  {d.expiry:<12} {d.T:6.2f} {d.n_negative:7d} {d.n_total:7d} "
            f"{d.min_density:14.6e} {rng:>26}")
    s2 = summarise_density(dens_stress)
    add(f"  TOTAL: {s2['n_negative']} / {s2['n_total']} points negative "
        f"({s2['pct_negative']:.2f}%), min density {s2['min_density']:.6e}")
    add("")

    add("  (c) deep low-strike wing, log-spaced K/F grid")
    add(f"      K/F from {ARB['deep_wing_kf_min']:g} to {ARB['deep_wing_kf_max']:g}.")
    add("      A symmetric bp grid stops at 0.10*F because K = F(1+bp/1e4) is")
    add("      bounded at -10000bp; SABR's failure region sits below that.")
    add(f"  {'expiry':<12} {'T':>6} {'n_neg':>7} {'n_pts':>7} {'min q':>14} {'violation range (K/F)':>26}")
    for d in dens_deep:
        if d.n_negative:
            rng = (f"{1.0 + d.neg_low_bp / BP:.4f} .. "
                   f"{1.0 + d.neg_high_bp / BP:.4f}")
        else:
            rng = "—"
        add(f"  {d.expiry:<12} {d.T:6.2f} {d.n_negative:7d} {d.n_total:7d} "
            f"{d.min_density:14.6e} {rng:>26}")
    s3 = summarise_density(dens_deep)
    add(f"  TOTAL: {s3['n_negative']} / {s3['n_total']} points negative "
        f"({s3['pct_negative']:.4f}%), min density {s3['min_density']:.6e}")
    add("")

    add("-" * 78)
    add("2. CALENDAR — total variance w = sigma^2 T non-decreasing in T")
    add("-" * 78)
    add(f"  log-moneyness grid : {ARB['calendar_k_grid']}")
    add(f"  adjacent-expiry pairs checked : "
        f"{len(ARB['calendar_k_grid']) * (len(fits) - 1)}")
    add(f"  violations : {len(calendar)}")
    if calendar:
        add("")
        add(f"  {'k':>8} {'T_low':>8} {'T_high':>8} {'w_low':>12} {'w_high':>12} {'dw':>12}")
        for v in calendar:
            add(f"  {v.log_moneyness:8.3f} {v.T_lower:8.3f} {v.T_upper:8.3f} "
                f"{v.w_lower:12.6f} {v.w_upper:12.6f} {v.dw:12.3e}")
    else:
        add("  none — the surface is calendar-arbitrage free on the tested grid.")
    add("")

    add("-" * 78)
    add("3. ATM REPRODUCTION (alpha solved from the cubic, so this is ~0)")
    add("-" * 78)
    add(f"  {'expiry':<12} {'error (bp)':>14}")
    for f in fits:
        add(f"  {f.expiry:<12} {check_atm_reproduction(f):14.3e}")
    add("")
    add("=" * 78)

    path.write_text("\n".join(L))


# --------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true",
                    help="re-pull chains from yfinance instead of the cached snapshot")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)

    print("[1/5] loading market data ...")
    market = load_equity_chain(snapshot_dir=ROOT / CONFIG["data"]["snapshot_dir"],
                               live=args.live)
    print(f"      {market.underlying} as of {market.asof}, spot {market.spot:.2f}")
    print(f"      {len(market.expiries)} expiries, {len(market.quotes)} quotes")

    market.quotes.to_csv(OUT / "market_quotes.csv", index=False)
    market.expiries.to_csv(OUT / "expiry_summary.csv", index=False)
    if not market.filter_report.empty:
        market.filter_report.to_csv(OUT / "filter_report.csv", index=False)

    print("[2/5] calibrating ...")
    fits = []
    for _, row in market.expiries.iterrows():
        smile = market.smile(row.expiry)
        fit = calibrate_expiry(
            expiry=row.expiry, T=float(row["T"]), F=float(row.forward),
            atm_vol=float(row.atm_vol), strikes=smile.strike.values,
            market_vols=smile.iv.values)
        fits.append(fit)
        print(f"      {fit.expiry}  alpha={fit.alpha:.4f}  rho={fit.rho:+.4f}  "
              f"nu={fit.nu:.4f}  RMSE={fit.rmse_bps:6.2f}bp  n={fit.n_points}")

    params = pd.DataFrame([f.as_dict() for f in fits])
    params.insert(len(params.columns), "atm_repro_err_bps",
                  [check_atm_reproduction(f) for f in fits])
    params.to_csv(OUT / "calibrated_params.csv", index=False)

    print("[3/5] arbitrage diagnostics ...")
    dens_calib = [breeden_litzenberger(f) for f in fits]
    dens_stress = [extrapolation_stress(f) for f in fits]
    dens_deep = [deep_wing_density(f) for f in fits]
    calendar = calendar_check(fits)

    pd.DataFrame([{
        "grid": g, "expiry": d.expiry, "T": d.T, "forward": d.forward,
        "range_bp": d.range_bp, "n_negative": d.n_negative,
        "n_total": d.n_total, "min_density": d.min_density,
        "neg_low_bp": d.neg_low_bp, "neg_high_bp": d.neg_high_bp,
        "neg_low_kf": None if d.neg_low_bp is None else 1.0 + d.neg_low_bp / BP,
        "neg_high_kf": None if d.neg_high_bp is None else 1.0 + d.neg_high_bp / BP,
    } for g, group in (("density", dens_calib), ("stress", dens_stress),
                       ("deep_wing", dens_deep))
      for d in group]).to_csv(OUT / "density_summary.csv", index=False)

    pd.DataFrame(
        [{"log_moneyness": v.log_moneyness, "T_lower": v.T_lower,
          "T_upper": v.T_upper, "w_lower": v.w_lower, "w_upper": v.w_upper,
          "dw": v.dw} for v in calendar],
        # Keep the header when there are no violations, so the file is still
        # readable as a table rather than an empty byte.
        columns=["log_moneyness", "T_lower", "T_upper", "w_lower", "w_upper", "dw"],
    ).to_csv(OUT / "calendar_violations.csv", index=False)

    pd.DataFrame(calendar_surface(fits)).to_csv(
        OUT / "calendar_surface.csv", index=False)

    print("[4/5] plotting ...")
    plot_smiles(market, fits, OUT / "smile_fit.png")
    plot_param_term_structure(fits, OUT / "param_term_structure.png")
    plot_densities(dens_stress, OUT / "density.png")

    print("[5/5] writing report ...")
    write_arbitrage_report(fits, dens_calib, dens_stress, dens_deep, calendar,
                           OUT / "arbitrage_report.txt", market)

    agg_rmse = float(np.sqrt(np.mean([f.rmse_bps ** 2 for f in fits])))
    worst = max(fits, key=lambda f: f.max_abs_err_bps)
    print()
    print(f"  aggregate RMSE      : {agg_rmse:.2f} bp")
    print(f"  worst point         : {worst.max_abs_err_bps:.2f} bp at "
          f"{worst.expiry}, offset {worst.max_err_offset_bp:.0f}bp")
    print(f"  calibration points  : {sum(f.n_points for f in fits)}")
    print(f"  butterfly (density) : {summarise_density(dens_calib)['n_negative']} negative")
    print(f"  butterfly (stress)  : {summarise_density(dens_stress)['n_negative']} negative")
    deep = summarise_density(dens_deep)
    n_exp_deep = sum(1 for d in dens_deep if d.n_negative)
    print(f"  butterfly (deep wing): {deep['n_negative']} negative "
          f"across {n_exp_deep}/{len(fits)} expiries")
    print(f"  calendar violations : {len(calendar)}")
    print(f"\n  outputs -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
