"""Correctness checks on the SABR / Black / density machinery."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.arbitrage import _second_derivative, otm_density
from src.black import black_price, implied_vol
from src.calibrate import solve_alpha
from src.config import CONFIG
from src.hagan import sabr_atm_vol, sabr_vol

F, T, BETA = 775.0, 1.0, 0.5
ALPHA, RHO, NU = 4.5, -0.6, 0.8
fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        fails.append(name)


print("\n[1] ATM branch continuity: sabr_vol(K->F) must approach sabr_atm_vol(F)")
atm = sabr_atm_vol(F, T, ALPHA, BETA, RHO, NU)
for eps in (1e-2, 1e-4, 1e-6, 1e-8):
    v = float(sabr_vol(F + eps, F, T, ALPHA, BETA, RHO, NU))
    check(f"eps={eps:g}", abs(v - atm) < 1e-4, f"|diff|={abs(v-atm):.3e}")
v_exact = float(sabr_vol(F, F, T, ALPHA, BETA, RHO, NU))
check("K == F exactly", abs(v_exact - atm) < 1e-14, f"|diff|={abs(v_exact-atm):.3e}")

print("\n[2] alpha cubic inverts the ATM formula")
for sigma_atm in (0.10, 0.15, 0.20, 0.30):
    a = solve_alpha(F, T, sigma_atm, BETA, RHO, NU)
    back = sabr_atm_vol(F, T, a, BETA, RHO, NU)
    check(f"sigma_atm={sigma_atm}", abs(back - sigma_atm) < 1e-12,
          f"alpha={a:.5f} -> {back:.12f}")

print("\n[3] Black price / implied-vol round trip")
for K in (500.0, 700.0, 775.0, 900.0, 1200.0):
    for is_call in (True, False):
        p = float(black_price(F, K, T, 0.18, is_call))
        iv = implied_vol(p, F, K, T, is_call)
        check(f"K={K} {'C' if is_call else 'P'}", abs(iv - 0.18) < 1e-8,
              f"iv={iv:.10f}")

print("\n[4] put-call parity on Black prices (undiscounted)")
for K in (600.0, 775.0, 1000.0):
    c = float(black_price(F, K, T, 0.2, True))
    p = float(black_price(F, K, T, 0.2, False))
    check(f"K={K}", abs((c - p) - (F - K)) < 1e-8, f"C-P-(F-K)={(c-p)-(F-K):.3e}")

print("\n[5] risk-neutral density integrates to 1 and has mean F")
K = np.geomspace(1e-3 * F, 1.9 * F, 400_000)
vols = np.asarray(sabr_vol(K, F, T, ALPHA, BETA, RHO, NU), dtype=float)
q = otm_density(F, K, T, vols)
Km = K[1:-1]
mass = float(np.trapezoid(q, Km))
mean = float(np.trapezoid(q * Km, Km))
check("integral q dK = 1", abs(mass - 1.0) < 5e-3, f"mass={mass:.6f}")
check("integral K q dK = F", abs(mean - F) / F < 5e-3, f"mean={mean:.3f} vs F={F}")

print("\n[6] OTM density beats naive all-calls differencing in the low wing")
calls = np.asarray(black_price(F, K, T, vols, True), dtype=float)
q_calls = _second_derivative(K, calls)
low = Km < 0.5 * F
check("all-calls produces spurious negatives", bool((q_calls[low] < 0).any()),
      f"n_neg={int((q_calls[low] < 0).sum())} (catastrophic cancellation)")
check("OTM density does not", not bool((q[low] < 0).any()),
      f"n_neg={int((q[low] < 0).sum())}")

print("\n[7] splicing PRICES instead of densities fakes a spike at K = F")
puts = np.asarray(black_price(F, K, T, vols, False), dtype=float)
spliced = _second_derivative(K, np.where(K < F, puts, calls))
check("price-splice corrupts the mass", abs(float(np.trapezoid(spliced, Km)) - 1.0) > 0.5,
      f"mass={float(np.trapezoid(spliced, Km)):.6f} (regression guard)")

print("\n[8] POSITIVE CONTROL: the butterfly detector fires where SABR is known to fail")
# High vol-of-vol on an equity forward, and long-dated low-forward rates --
# the two textbook Hagan breakdown regimes.
for label, (Fx, Tx, atm, rho, nu) in {
    "equity F=775 T=1 nu=2.0": (775.0, 1.0, 0.20, -0.6, 2.0),
    "rates  F=0.0385 T=10":    (0.0385, 10.0, 0.34, -0.6, 0.8),
}.items():
    a = solve_alpha(Fx, Tx, atm, BETA, rho, nu)
    Kx = np.geomspace(1e-3 * Fx, 1.9 * Fx, 200_000)
    vx = np.asarray(sabr_vol(Kx, Fx, Tx, a, BETA, rho, nu), dtype=float)
    qx = otm_density(Fx, Kx, Tx, vx)
    check(label, bool((qx < 0).any()),
          f"n_neg={int((qx < 0).sum())}, min q={qx.min():.3e}")

print("\n[9] monotone call prices (dC/dK <= 0) — necessary for no arbitrage")
dC = np.diff(calls) / np.diff(K)
check("dC/dK in [-1, 0]", bool(np.all(dC <= 1e-9) and np.all(dC >= -1.0 - 1e-9)),
      f"max={dC.max():.3e} min={dC.min():.3e}")

print(f"\n{'='*60}")
print("ALL PASS" if not fails else f"FAILURES: {fails}")
print(f"{'='*60}")
raise SystemExit(1 if fails else 0)
