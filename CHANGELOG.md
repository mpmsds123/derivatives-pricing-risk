# Changelog

All notable changes to this project. Dates are commit dates; every entry's numbers
were produced by the run at that commit.

## [Unreleased] — reproducibility and claim audit, 2026-11-13

Audit method: the repository was cloned to a scratch directory and all four stages
were re-run end to end. `git status` on that clone came back clean, so the committed
outputs match a fresh run byte for byte, PNGs included. Everything below is a gap the
audit found against that run.

### Added
- `requirements.txt`, `pytest.ini`, `Makefile`, `tests/test_pipeline_checks.py` and
  `.github/workflows/ci.yml` are now tracked. They existed locally but were
  untracked, so a fresh clone could not install dependencies, the CI workflow had
  never run, and `python -m pytest -q` aborted with INTERNALERROR — the two
  `test_sanity.py` scripts call `sys.exit()` at import and pytest collected them
  without the `testpaths` guard.
- `tests/test_vendored_hagan.py`: pins the two vendored copies of the Hagan SABR
  formula (`surface/src/hagan.py`, `attribution/src/hagan.py`) to each other across
  three parameter sets and 41 strikes. They agree to 0.0 today and nothing guarded
  future drift, which would have moved every attribution and risk number while every
  existing check still passed.
- `tests/test_dividend_yield.py`: ties the hardcoded `dividend_yield = 0.00973` to
  its stated provenance — trailing-12-month SPY distributions over spot at the
  calibration date (7.525 / 773.26 = 0.009732) — read from
  `surface/data/raw/spy_dividends.csv`, which was committed but read by no code.
- `surface/tools/smile_roughness.py`: computes the per-expiry RMS implied-vol second
  difference used to exclude expiries, over every expiry in the snapshot including
  the ones that are not calibrated, so an exclusion can be re-derived instead of
  taken on trust.
- `yfinance` to `requirements.txt`, marked as needed only for `--live`.
- `docs/MODEL_VALIDATION.md` §5.2: a multiplicity table. Six tests are run across
  three confidence levels and the two rejections were read off raw p-values.
  Holm-adjusted, the 97.5% Kupiec rejection survives (p = 0.0077 → 0.0462) and the
  95% one does not (p = 0.0465 → 0.2325). The §7 conclusion now rests on the 97.5%
  result and on the exception counts, which no adjustment touches.
- `docs/MODEL_VALIDATION.md` §7: a fourth limitation of the validation itself. The
  only vol calibration is dated the *last* day of the P&L window and is applied
  backwards over the preceding year, and the vol level is a deterministic function
  of the spot path — so each day's Δσ is mechanically tied to its ΔS, which is what
  the vega and vanna terms are charged with. Added as an open item.

### Changed
- `Makefile` calls `python3` / `pip3`. `python` is not on PATH on a stock macOS
  Python 3.11 install, so `make run` and `make install` failed as written.
- `surface/src/config.py` documents **both** expiry exclusions. Previously only
  2028-06-16 was explained, and its figures were restated: RMS second difference
  **0.4275** vol pts against a worst calibrated expiry of **0.0727** (was "0.445"
  against "<0.07"). 2026-08-21 had no stated reason at all and is in fact the
  cleanest smile in the file; it is excluded for **scope, not quality** — the
  downstream book's shortest leg reaches T = 0.114990y on the final valuation date,
  exactly the first calibrated pillar, so a 14-day pillar is never interpolated and
  would only move the flat-extrapolation floor. Verified rather than asserted:
  calibrating it fits at 6.59 bp RMSE with ν = 2.87 inside its bound and leaves every
  butterfly and calendar check at zero.
- `README.md` rewritten problem-first, with every number traced to the file that
  writes it and measured stage runtimes.
- Surface RMSE excluding the 2.36y expiry corrected to **14.58 bp** (was 14.8 bp) in
  `surface/README.md`, twice.
- `docs/MODEL_VALIDATION.md` §3 no longer calls the P&L series "Realised P&L". It is
  this pipeline's own full revaluation of the book, not a trading result, which is
  why §5.2 is a backtest of the model against itself.
- The `futures_source='ALL'` illustration no longer claims "adjacent forward segments
  of 1.83% and 4.40%". Re-running that mode gives segment forwards of 2.93, 4.40,
  3.57, 3.51 and 1.83 percent — the two quoted values are both present but are not
  adjacent. Restated as a 1.83%–4.40% sawtooth against 3.18%–3.54% under the SR3
  strip. Fixed at source in `curves/src/instruments.py` and `curves/run.py`, so the
  generated `curves/README.md` and `bootstrap_diagnostics.txt` follow.
- Invariant-check counts corrected: surface 31 + attribution 32 = **63**
  (`tests/test_pipeline_checks.py` said 64; the old README said 33 for attribution).
  With the two new cross-module tests, `pytest` now reports 4 tests covering 65
  checks.

### Removed
- Stale reference to a non-existent `greeks.py` in `attribution/src/pricing.py`; the
  finite-difference cross-Greeks are `fd_vanna` / `fd_volga` in that same file.

### Unchanged
- No pricing, calibration, attribution or risk number moved. Every edit above is
  documentation, provenance, test coverage or packaging; the full pipeline was re-run
  afterwards and the only regenerated file whose contents changed is
  `curves/README.md`, which `curves/run.py` writes from the run's own numbers.

---

## [0.4.0] — 2026-09-08 — one repository

- Merged `sofr-curve`, `sabr-calibration` and `pnl-explain` into this repository as
  `curves/`, `surface/` and `attribution/` via `git subtree`, history preserved.
- Split the VaR/ES/stress code into `risk/`, importing `attribution/`'s pricer so
  the full-revaluation benchmark and the Greek approximations it benchmarks share
  one pricing path (stated as a limitation, `docs/MODEL_VALIDATION.md` §7.2).
- Vendored `curves/`'s four input files into `curves/data/`. They had been read from
  the folder above the repository, so a clone could not run `run.py` at all.
- Added the top-level README with the cross-module dependency chain.

## [0.3.0] — 2026-09-08 — risk module

- Four VaR/ES estimators on one book and one window: historical, parametric
  delta-gamma-vega, filtered historical simulation, full revaluation.
  1-day 99%: $70,650 / $69,492 / $131,686 / **$65,588**; 99% ES $80,219 by full
  revaluation on a $1,662,294 book.
- Kupiec proportion-of-failures and Christoffersen independence backtests on a
  125-day rolling window. **Historical VaR is rejected at 95% (12 exceptions vs
  6.50, p = 0.047) and 97.5% (9 vs 3.25, p = 0.008)**, Basel red at both;
  Christoffersen finds no clustering.
- Stress by full repricing: 6 historical episodes, 5 hypothetical, a 13×13 spot×vol
  grid and a reverse-stress solver. Worst scenario −$785,466 (−47.3% of book);
  worst grid cell −$1,582,048 at spot −20% / vol −15 pts, interior rather than at a
  corner. Median Taylor error across the 11 scenarios 32.2%, max 89.2%.
- `docs/MODEL_VALIDATION.md`: SR 11-7-structured validation report, including the
  three limitations of the validation itself.

## [0.2.0] — 2026-08-09 — P&L attribution

- 8-leg SPY book (long straddle, short strangle, call spread, calendar) valued daily
  over 255 moves against full revaluation as ground truth.
- Second-order terms cut mean unexplained P&L from $348 to **$43** (−87.6%) against
  $22,031 mean absolute daily P&L; R² 0.999986.
- Delta experiment: SABR delta cuts delta-hedged P&L std from $5,454 to **$3,311**
  (−39.3%) while making the attribution residual worse ($43 → $152). Bartlett's
  delta is worse on both until the surface is given a leverage effect (GJR), which
  improves its hedge std 17.7%.
- Vega experiment: one vol number leaves $3,795, a four-bucket ladder $2,965
  (−21.9%, stable across κ = 2/4/8), per-leg vol $43.
- FRTB PLA test on 11 variants: all pass, 2nd-order variance ratio 0.0013% against
  the 20% limit — close to guaranteed by construction and reported as such.
- Surface evolution: ATM level scaled by EWMA realised vol with mean-reversion
  damping w(T) = (1 − e^{−κT})/(κT); ρ and ν frozen, α re-solved from the ATM cubic.

## [0.1.0] — 2026-08-08 — curve and surface

- **curves/**: sequential Brent bootstrap of a piecewise-constant overnight forward
  curve from 7 SR3 futures and 14 OIS swaps, 21 pillars to 40.04y. Max input
  reproduction error **4.91e-11 bp** against a 0.01 bp tolerance. 6 SR1 quotes held
  out as a cross-check and repriced at 12.7 bp mean basis. Key-rate DV01 by bumping
  input quotes and rebootstrapping; sum of KR01 ties to parallel DV01 within 0.075%.
  README regenerated by `run.py` from the run's own outputs.
- **surface/**: per-expiry Hagan SABR calibration to 735 SPY quotes across 8
  expiries, β fixed at 0.5, α solved from the ATM cubic so the ATM vol reproduces to
  1.1e-12 bp, (ρ, ν) fitted by multi-start least squares. Aggregate RMSE **24.37 bp**
  (57.13 bp at the 2.36y expiry, reported not dropped). Butterfly density check on a
  400k-point log-spaced grid reaching K/F = 1e-3, calendar check on 70 pairs: zero
  violations, with positive controls confirming the detectors fire.
