# SABR Volatility Surface Calibration & Arbitrage Diagnostics

Calibrates the SABR stochastic volatility model to a market implied-volatility
surface built from SPY option chains, then tests the calibrated surface for
static arbitrage.

```bash
python run.py          # cached snapshot — reproduces every number below
python run.py --live   # re-pull from yfinance (results will differ)
python tests/test_sanity.py
```

Dependencies: `numpy`, `scipy`, `pandas`, `matplotlib`, `yfinance`.

---

## 1. Headline results

| metric | value |
|---|---|
| underlying / as-of | SPY, 2026-08-07 close (spot 773.26) |
| expiries calibrated | 8, T = 0.115y … 2.357y |
| calibration points | **735** (from 3,963 raw quotes) |
| aggregate RMSE | **24.4 bp** (RMS across expiries); 26.4 bp point-weighted |
| aggregate RMSE, excl. the 2.36y expiry | **14.8 bp** |
| best / worst expiry RMSE | 9.4 bp (0.11y) / 57.1 bp (2.36y) |
| max absolute error | **119.2 bp**, at 2028-12-15, strike offset +6842bp |
| ATM reproduction error | **1.1e-12 bp** — exact, by construction |
| butterfly violations | **0** across all three strike grids |
| calendar violations | **0** of 70 (k, T) checks |

Per-expiry fit:

| expiry | T | forward | ATM vol | α | ρ | ν | RMSE (bp) | n |
|---|---|---|---|---|---|---|---|---|
| 2026-09-18 | 0.115 | 775.05 | 13.23% | 3.591 | −0.503 | 2.152 | 9.4 | 107 |
| 2026-10-16 | 0.192 | 776.57 | 14.05% | 3.821 | −0.548 | 1.772 | 11.5 | 105 |
| 2026-12-18 | 0.364 | 781.14 | 15.31% | 4.187 | −0.577 | 1.334 | 15.8 | 48 |
| 2027-03-19 | 0.613 | 787.70 | 16.43% | 4.547 | −0.612 | 0.978 | 11.9 | 67 |
| 2027-06-17 | 0.860 | 793.93 | 17.33% | 4.845 | −0.639 | 0.776 | 13.3 | 84 |
| 2027-09-17 | 1.112 | 800.46 | 17.85% | 5.019 | −0.633 | 0.667 | 12.4 | 96 |
| 2027-12-17 | 1.361 | 806.46 | 18.37% | 5.193 | −0.627 | 0.572 | 23.3 | 112 |
| 2028-12-15 | 2.357 | 831.28 | 19.72% | 5.698 | −0.617 | 0.319 | 57.1 | 116 |

**Parameter stability.** Each expiry is calibrated independently with no
term-structure constraint, so smooth parameter evolution is evidence the fit is
well-identified rather than an artefact:

- **α** — strictly monotone increasing (3.591 → 5.698), no sign changes in the
  first difference.
- **ν** — strictly monotone decreasing (2.152 → 0.319). Moreover `ν·√T` is
  nearly constant at **0.70–0.81** across the first seven expiries (0.49 at
  2.36y), the known `ν ~ 1/√T` regularity. This is a strong stability signal:
  eight independent two-parameter fits landing on one scaling law.
- **ρ** — **not** monotone. It steepens from −0.503 to a minimum of −0.639 at
  T = 0.86y, then flattens back to −0.617. One turning point, reported rather
  than smoothed away. The reversal is small (0.02) and plausibly reflects the
  market's skew term structure rather than instability, but it is the one place
  the surface is not monotone and is flagged as such.

---

## 2. Data

**Source.** SPY option chains via `yfinance`, snapshot pinned on disk at
`data/raw/`. `run.py` reads the cached snapshot by default so every number in
this README reproduces exactly; `--live` re-pulls.

**Expiry ladder.** Eight third-Friday expiries. Third Fridays carry the deepest
open interest on SPY. `2028-06-16` was pulled but **excluded**: its implied-vol
second-difference roughness is 0.445 vol points against <0.07 elsewhere — stale
LEAPS quotes that would corrupt both the fit and the parameter term structure.

### Choice point: which price to invert

**Chosen:** bid/ask **mid**.
**Rejected:** `lastPrice`, and yfinance's own `impliedVolatility` column (which
is derived from `lastPrice`).
**Reason:** `lastPrice` is badly stale away from the money. On this snapshot the
2026-09-18 749-strike call has `lastPrice` 29.68 against a **bid** of 30.97 —
the last trade is below the current bid, i.e. it is not a current price at all.
Any IV built on it is meaningless.

### Choice point: the forward

**Chosen:** pin the discount factor `DF` from a Treasury curve
(`^IRX`/`^FVX`/`^TNX`, linear in T), then imply **only** the forward from
put-call parity, `F = K + (C−P)/DF`, over a window of near-ATM strikes weighted
by quote tightness. Dividend yield becomes an *output*, not an input.

**Rejected #1:** the spec's `F = S·exp((r−q)T)` with an externally assumed `q`.
Using the trailing dividend yield throws away the market's own forward
information; the parity forward is what a desk actually uses.

**Rejected #2:** the textbook joint regression of `C−P` on `K` to recover `DF`
and `F` together. Tested and **it fails on this data**, in two distinct ways:

1. *Ill-conditioning at short T.* The discount signal is `1 − rT`, which at
   T = 0.008 is smaller than the quote noise. The regression returned `DF > 1` —
   an implied rate of **−69%**.
2. *American-exercise bias at all T.* SPY options are **American**. The
   early-exercise premium in ITM puts grows with strike, steepening `C−P` in `K`
   and biasing the fitted `DF` upward. The regression implied `r ≈ 0–1.5%`
   against a 4% Treasury curve at *every* expiry — because it leans on deep ITM
   legs, which is exactly where the American premium lives.

Pinning `r` avoids both. The resulting implied dividend yields (0.72%–1.70%,
against a 0.97% trailing actual) are the check that this is working.

### Choice point: centre the parity window on the forward, not spot

**Chosen:** fixed-point iteration — start at `F = S`, take strikes within ±3% of
the current `F`, recompute, repeat to convergence.
**Rejected:** a spot-centred window (the obvious first implementation).
**Reason:** at 2.36y the SPY forward sits ~7.5% *above* spot. A spot-centred
window therefore selects OTM puts against ITM calls — precisely the asymmetry
that maximises the American-premium bias. Measured effect: the put-vs-call
implied-vol seam at the money on the 2.36y expiry was **1.2 vol points** with a
spot-centred window and **0.43** with a forward-centred one, and the fitted
forward moved 837.9 → 831.3. The seam is visible as a discontinuity in the
smile plot; removing it is what makes that expiry fittable at all.

### Liquidity filter

All thresholds in `src/config.py`. Cascade counts are written to
`output/filter_report.csv`. Applied to 1,841 OTM quotes, leaving 735.

| rule | value | reason |
|---|---|---|
| OTM only | puts K<F, calls K>F | ITM options carry the same information by parity but trade far wider. Near-ATM ITM calls on this snapshot quote $3+ wide against $0.05 for the OTM equivalent. |
| `bid > 0.05` | | Below this, one $0.01 tick dominates the implied vol. In the −4000bp bucket the median quote is mid $0.025 / spread $0.01 — 40% relative — and the resulting IV is tick-quantization noise, not a price. |
| relative spread ≤ 50% | (ask−bid)/mid | Guard against crossed and stale two-sided markets. |
| open interest ≥ 100 | | Liquidity gate. |
| **no volume threshold** | `min_volume: 0` | Deliberate. Open interest is a *stock* measure and is robust to one quiet session; volume is a *flow* measure, and this is a Friday-close snapshot read on a Saturday, so a volume filter would spuriously drop good strikes. Empirically it is also redundant: after OI ≥ 100, requiring volume ≥ 1 removes **zero** additional quotes. |
| \|delta\| ∈ [0.05, 0.95] | | See below. |

**Choice point: delta band rather than a fixed strike window.**
Three designs were tested, scored by smile smoothness (standard deviation of the
implied-vol second difference — lower is cleaner):

| filter | points kept | roughness (vol pts) |
|---|---|---|
| fixed ±3000bp | 1404 | 0.113 |
| standardised moneyness \|z\| ≤ 2.5 | 1374 | 0.136 |
| **\|delta\| ∈ [0.05, 0.95]** | **1066** | **0.103** |

The delta band wins on cleanliness, is how desks actually delimit a smile
(25Δ/10Δ risk reversals and butterflies), and widens automatically with `√T`. A
fixed bp window was rejected because its economic meaning is inconsistent across
the term structure — ±3000bp is a wide smile at 0.1y and a narrow one at 2.4y.

---

## 3. Model and calibration

Hagan et al. (2002) lognormal SABR approximation, implemented in `src/hagan.py`
with an explicit ATM branch for `|K−F| < 1e-12` plus a floating-point fallback
for strikes where `z/x(z)` degenerates to 0/0 near the money.

### Choice point: β fixed at 0.5

**Chosen:** `β = 0.5`, not estimated.
**Rejected:** joint estimation of `(β, ρ, ν)`.
**Reason:** β and ρ are near-degenerate — both control the backbone and the
skew, so joint estimation is ill-conditioned and the two trade off against each
other with almost no change in fit quality. β = 0.5 is the standard rates-market
convention (CIR-like, keeps F ≥ 0 behaviour reasonable). Fixing it is the
market-standard resolution, not a shortcut.

### Choice point: α solved analytically, not optimised

Rearranging the ATM condition gives a cubic in α:

```
[(1−β)²T / (24 F^(2−2β))] α³ + [ρβνT / (4F^(1−β))] α²
  + [1 + (2−3ρ²)ν²T/24] α − σ_atm F^(1−β) = 0
```

solved with `numpy.roots`, taking the smallest strictly positive real root.

**Why:** it removes α from the optimisation, reducing a 3-parameter fit to 2,
and makes the ATM volatility reproduce **exactly** rather than approximately.
Measured ATM reproduction error across all 8 expiries: **1.1e-12 bp**.
This matters because ATM is the most liquid and most-hedged point on the smile —
a calibration that misses it to fit the wings is backwards.

`(ρ, ν)` are then fitted by `scipy.optimize.least_squares` with bounds
`([-0.999, 1e-4], [0.999, 5.0])`, from the specified starting point `(-0.3, 0.4)`
plus 5 seeded random restarts, keeping the lowest cost — guarding against local
minima. Each expiry is calibrated independently (standard practice; no joint
term-structure constraint).

---

## 4. Arbitrage diagnostics

### Choice point: relative rather than additive strike offsets

The spec's grid is written for rates, where a "bp offset" is **additive** on a
~3.85% forward (`K = F + 0.02` for +200bp). On an equity forward of 773 that
reading is meaningless — a $0.02 strike offset. Offsets are therefore applied
**multiplicatively**, `K = F·(1 + bp/1e4)`, throughout the equity path. The
swaption-CSV loader keeps the additive convention, since that is correct there.

Consequently the spec's ±400bp density grid and ±600bp stress grid become ±4%
and ±6% on SPY — both *inside* the calibration window, which would make the
diagnostic vacuous. Ranges are rescaled to the equity smile width, preserving
the spec's structure (density = 2× the calibration window, stress = 3×):
calibration ±3000bp, density ±6000bp, stress ±9000bp.

### Choice point: differentiate OTM prices, and mix densities not prices

The Breeden–Litzenberger density is `q(K) = ∂²C/∂K²`. Two numerical traps here,
both found by testing rather than by inspection, both now covered by regression
tests:

1. **Do not difference call prices across the whole grid.** A deep ITM call is
   `C ≈ F − K`, an almost perfectly linear function; differencing it twice is
   catastrophic cancellation. Doing so produced **88,157 spurious negative
   density points** in the low wing, with call- and put-derived densities
   disagreeing by a factor of 10⁴ below K/F = 0.01. The fix is to evaluate each
   leg where it is OTM (`∂²P/∂K² ≡ ∂²C/∂K²` by parity, since `C−P = F−K` is
   linear in `K`).

2. **Splice the densities, not the prices.** Building an OTM *price* vector
   (puts below F, calls above) and differencing that inserts a kink of slope 1
   at `K = F`, and the second derivative of a kink is a delta spike. This faked
   a violation at exactly K/F = 1.0 and destroyed the probability mass
   (∫q dK collapsed from 1.000 to ≈ 0). Both legs must be differenced
   separately and the *results* selected by region.

Validation: with the correct construction, `∫q dK = 1.0000` and
`∫K q dK = F` to 5 significant figures.

### Choice point: a log-spaced grid for the deep wing

A symmetric bp grid **cannot** probe the low-strike wing: `K = F(1+bp/1e4)` is
bounded below at −10000bp (K = 0), so even the ±9000bp stress grid only reaches
0.10·F. SABR's known failure region sits below that. A third probe therefore
runs on a log-spaced `K/F ∈ [0.001, 1.9]` grid.

### Results

**Butterfly — 0 violations**, on all three grids (±6000bp, ±9000bp, and the deep
log wing down to K/F = 0.001), for all 8 expiries.

This is a genuine result, not a broken test. The detector was validated against
positive controls where SABR is *known* to fail, and it fires as expected:

| regime | negative points | violation region |
|---|---|---|
| equity F=775, T=1, ν=2.0 | 139,404 | K/F 0.003–0.566 |
| equity F=775, T=1, ν=3.0 | 378,082 | K/F 0.001–1.900 |
| rates F=3.85%, T=5, ν=0.8 | 263,420 | K/F 0.004–0.512 |
| rates F=3.85%, T=10, ν=0.8 | 255,810 | K/F 0.006–0.768 |
| **our calibrated surface** | **0** | — |

The pattern is the textbook one: Hagan's expansion breaks down at high
vol-of-vol and at long maturity on a *low* forward. Our surface avoids both —
calibrated ν is at most 2.15 and only at T = 0.115y, where `ν²T` is small, and
an equity forward of ~800 puts K → 0 tens of standard deviations away, so the
density there is numerically zero rather than negative. **The honest conclusion
is that SABR's famous low-strike arbitrage is a rates problem, and does not bind
on a 2-year SPY surface.** It is reachable from this code — just not from these
parameters.

**Calendar — 0 violations** of 70 checks (10 log-moneyness values × 7 adjacent
expiry pairs). Total implied variance `w = σ²T` is non-decreasing in T at every
tested `k`. Full `w(k,T)` values are written to `output/calendar_surface.csv` so
the null result is traceable to numbers rather than asserted.

---

## 5. Known limitations

1. **SPY is American; the model is European.** This is the largest modelling
   compromise, and it is forced by the spec's choice of underlying. SPX options
   are European and would be the correct instrument for a vol-surface study.
   The bias is measurable here: the put-vs-call implied-vol seam at the money
   grows from 0.05 vol points at 0.115y to 0.43 at 2.357y. Mitigated by using
   only OTM options (where early exercise is worth little) and by centring the
   parity window on the forward, but not eliminated.

2. **Fit quality degrades with maturity.** `corr(T, RMSE) = 0.89`. The 2.36y
   expiry fits at 57bp against 9–23bp elsewhere. Two causes, both real: Hagan's
   formula is an asymptotic expansion whose error grows in `T` and in
   `|ln(K/F)|`, and that expiry spans a 10,466bp strike range where the market
   smile drops steeply and then turns *up* past +5000bp — a shape a
   two-parameter smile cannot represent. Excluding it, aggregate RMSE is 14.8bp.

3. **Single snapshot.** One Friday close. No statement is made about parameter
   stability through time, only across the term structure.

4. **The rate curve is a proxy.** Treasury constant-maturity yields interpolated
   linearly in T, not a bootstrapped OIS discount curve. Project 2 builds the
   latter; wiring it in here would be the natural improvement.

---

## 6. Outputs

| file | contents |
|---|---|
| `calibrated_params.csv` | per expiry: forward, ATM vol, α, β, ρ, ν, RMSE bp, max error bp and its strike, ATM reproduction error |
| `market_quotes.csv` | the 735 filtered quotes with strike, offset, delta, IV — the calibration input, traceable |
| `expiry_summary.csv` | forward, dispersion, parity strike count, rate, implied dividend yield per expiry |
| `filter_report.csv` | liquidity cascade counts, rule by rule |
| `density_summary.csv` | negative-density counts and regions, all three grids |
| `calendar_surface.csv` | total variance w(k,T) at every tested point |
| `calendar_violations.csv` | violating (k, T) pairs (empty, with header) |
| `arbitrage_report.txt` | full butterfly / calendar / ATM-reproduction report |
| `smile_fit.png` | market scatter vs SABR curve, one panel per expiry |
| `param_term_structure.png` | α, ρ, ν against expiry |
| `density.png` | recovered risk-neutral density, negative regions shaded |

## 7. Layout

```
sabr-calibration/
  data/loader.py        # yfinance chains + swaption-CSV paths, one output schema
  data/raw/             # pinned snapshot (chains, spot, dividends, meta)
  src/config.py         # every threshold, bound and convention
  src/black.py          # Black-76 price, vega, Brent IV inversion, delta
  src/hagan.py          # sabr_vol with explicit ATM branch
  src/calibrate.py      # alpha cubic, (rho, nu) least squares, multi-start
  src/arbitrage.py      # Breeden-Litzenberger density, calendar, deep wing
  tests/test_sanity.py  # 26 checks incl. 2 regression guards + positive control
  run.py
  output/
```

`tests/test_sanity.py` covers ATM-branch continuity, the α cubic inversion,
Black round-trips, put-call parity, density mass and mean, both numerical traps
of §4 as explicit regression guards, and the positive control that the butterfly
detector fires where SABR is known to fail.
