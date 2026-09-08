# Derivatives Pricing & Risk

A single dependency chain from market quotes to a risk number, built in four
stages that each hand their output to the next. A 40-year SOFR OIS curve is
bootstrapped from SR3 futures and swaps under exact market conventions; that
curve supplies discounting to a SABR calibration of the SPY implied-volatility
surface; the curve and the surface together price an eight-leg option book each
day and decompose its P&L into Greek contributions with an FRTB profit-and-loss
attribution test; and the same repricing machinery drives a VaR, Expected
Shortfall and stress-testing framework documented to SR 11-7. Nothing in any
stage is mocked — each module consumes the previous module's actual output
files, every number in this README is written to a file by a run, and each
adapter records which source it used so a reader can tell when a fallback fired.

![SABR smile fit](surface/output/smile_fit.png)

*SABR calibrated independently to 8 SPY expiries, 735 quotes, β = 0.5 fixed. The
fit is tight through 1.4y and visibly breaks down at 2.36y — reported at 57.1 bp
rather than dropped.*

![Unexplained PnL diagnostics](attribution/output/residual_diagnostics.png)

*Daily P&L attribution on the eight-leg book. Second-order terms (gamma, vanna,
volga) cut mean unexplained P&L from $348 to $43 against $22,031 mean absolute
daily P&L — a 87.6% reduction, R² 0.99999.*

![Risk: stress surface, estimator disagreement, VaR backtest](risk/output/risk_heatmap.png)

*Left: book P&L over a 13 × 13 spot × vol grid, every cell fully repriced. The
worst cell is interior, not a corner. Centre: the four VaR estimators disagree
by a factor of two at 99%. Right: the rolling backtest, which the model fails at
the lower confidence levels.*

---

## Dependency chain

```
  curves/                surface/               attribution/            risk/
  ─────────              ────────               ────────────            ─────
  SR3 futures            SPY option             8-leg SPY book          4 VaR/ES
  + OIS swaps            chains                 daily P&L               estimators
      │                      │                      │                       │
      ▼                      ▼                      ▼                       ▼
  bootstrap              calibrate SABR         Greek attribution       full repricing
  40Y OIS curve          per expiry             + FRTB PLA test         + stress tests
      │                      │                      │                       │
      └──── curve.csv ──────►│                      │                       │
            (discounting)    │                      │                       │
                             └─ calibrated_params ─►│                       │
                                (implied vol)       │                       │
                                                    ├─ daily_attribution ──►│
                                                    │  exposures_daily      │
                                                    └─ portfolio + pricer ─►│
                                                       (revalue callback)
```

Each arrow is a file on disk, not an in-memory hand-off. `risk/` additionally
imports `attribution/`'s pricer directly, so the full-revaluation VaR and the
Greek approximations it benchmarks are computed by the same code — a shared-model
circularity that is stated as a limitation of the validation rather than hidden
(`docs/MODEL_VALIDATION.md` §7).

---

## Headline results

| Module | Result | Where |
|---|---|---|
| **curves/** | 21 curve pillars reproduce their input quotes to **4.91e-11 bp** against a 0.01 bp tolerance, across a 40-year horizon | `curves/output/par_rate_check.csv` |
| **surface/** | 735 quotes across 8 expiries, aggregate RMSE **24.4 bp** (**14.8 bp** excluding the 2.36y expiry; **9.4–23.3 bp** across the first seven, 57.1 bp at 2.36y). **0** butterfly violations, **0 of 70** calendar checks violated, ATM exact to 1.1e-12 bp | `surface/output/` |
| **attribution/** | Second-order Greek terms cut mean unexplained P&L by **−87.6%** ($348 → $43, 0.196% of mean daily absolute P&L, R² 0.99999). SABR delta cuts delta-hedged P&L std by **−39.3%** ($5,454 → $3,311) | `attribution/output/attribution_summary.csv` |
| **risk/** | 1-day 99% VaR **$65,588** / ES **$80,219** by full revaluation on a $1.66M book. Worst stress cell **−$1,582,048** (−95.2% of book), and it is interior rather than at a corner | `risk/output/var_summary.csv` |

Two of those numbers are ranges rather than points on purpose. The surface RMSE
is quoted three ways because the aggregate is dominated by one bad expiry, and
quoting only "9–23 bp" would be selecting the seven that worked. The risk
figures are one snapshot (2026-08-07) of one book.

**The run also produced two adverse results, which are reported rather than
tuned away:**

- The rolling 125-day historical VaR **fails the Kupiec test** at 95%
  (12 exceptions against 6.50 expected, p = 0.047) and at 97.5% (9 against 3.25,
  p = 0.008) — Basel red zones at both. Christoffersen finds no clustering, so
  the failure is one of level, not timing. That estimator is not fit for
  limit-setting at those confidence levels as configured.
- The delta-gamma-vega Taylor expansion tracks full repricing to within **9.4%**
  at one-day VaR sizes — so the expected "material gap" motivating the framework
  **did not appear** at that horizon. The same expansion misses by a **median
  32%**, and by as much as 89%, at stress-scenario sizes. Both are reported.

---

## Modules

| Directory | What it does | README |
|---|---|---|
| `curves/` | SOFR OIS bootstrap, swap pricing, key-rate DV01 ladders by bump-and-rebootstrap | [curves/README.md](curves/README.md) |
| `surface/` | SABR calibration to a SPY vol surface, butterfly and calendar arbitrage diagnostics | [surface/README.md](surface/README.md) |
| `attribution/` | Greek P&L attribution on an 8-leg book, FRTB PLA test, delta and vega experiments | [attribution/README.md](attribution/README.md) |
| `risk/` | Four VaR/ES estimators, Kupiec and Christoffersen backtests, historical / hypothetical / grid / reverse stress | [docs/MODEL_VALIDATION.md](docs/MODEL_VALIDATION.md) |

### Running it

Order matters — each stage reads the previous stage's output files.

```bash
cd curves      && python run.py    # writes curves/output/curve.csv
cd ../surface  && python run.py    # writes surface/output/calibrated_params.csv
cd ../attribution && python run.py # writes attribution/output/daily_attribution.csv
cd .. && python risk/run_risk.py   # writes risk/output/

python attribution/tests/test_sanity.py   # 33 invariant checks across pricer and risk
python surface/tests/test_sanity.py
```

Dependencies: `numpy`, `scipy`, `pandas`, `matplotlib`, `yfinance`. Every module
caches its market-data pull, so a rerun reproduces the numbers above exactly;
`--live` re-pulls and will differ.

---

## Limitations

The load-bearing caveats from each module, collected. None of these is a
residual uncertainty — each is a known, quantified, uncorrected choice.

**Curve construction (`curves/`)**
- **Convexity is set to 0.0 bp.** Futures are margined daily and swaps are not,
  so futures rates sit above forward rates. The effect is a few tenths of a bp
  inside 2Y and is dominated by the bid/offer, but it is a real bias, exposed as
  a config knob rather than buried.
- **The 6 SR1 monthly quotes are not reproduced.** Every SR1 month sits inside an
  SR3 quarter, so both strips cannot be pillars without pinning the redundancy
  onto residual stubs. SR3 are the pillars; the SR1 quotes are repriced off the
  finished curve as a basis check and show up to 17.9 bp of basis.
- Single valuation date (2026-03-09). There is no curve history anywhere
  downstream, which is why the attribution engine has to synthesise rate moves
  from a short-rate overlay.

**Volatility surface (`surface/`)**
- **Single calibration date.** One snapshot, 2026-08-07. Everything downstream
  that needs a surface through time has to evolve this one fit.
- **The 2.36y expiry does not fit**: 57.1 bp RMSE against 9.4–23.3 bp for the
  other seven, with a visible shape mismatch in `smile_fit.png`. Reported, not
  excluded.
- **ρ is not monotone** across expiries — one turning point at T = 0.86y.
  Reported rather than smoothed away.
- β is fixed at 0.5 rather than fitted.

**P&L attribution (`attribution/`)**
- **ρ and ν are frozen through time.** Only the ATM level is evolved, scaled by
  EWMA realised vol. The surface therefore cannot represent skew steepening in a
  selloff or a vol-of-vol regime shift; the consequence is visible as residual
  P&L correlated with |ΔS| beyond what gamma and vanna absorb.
- **The FRTB PASS on all 11 variants is close to guaranteed by construction.**
  Risk-theoretical and hypothetical P&L are computed from the same surface, so
  the test is not an independent check and does not validate the framework.
- **Bucket-vega additivity is exact by construction, not as a finding.** Every
  leg sits in exactly one bucket, so the bumps touch disjoint legs.

**Risk (`risk/`, full detail in [docs/MODEL_VALIDATION.md](docs/MODEL_VALIDATION.md))**
- **One vol risk factor — the binding limitation.** Volatility risk is a parallel
  level shift. The book's bucket vegas largely cancel (+30,085 / −41,536 /
  +32,499 per point against a parallel total of +21,049), so VaR and stress are
  **downward-biased for any scenario in which the term structure twists** — which
  is what a vol shock actually does.
- **√t scaling is invalid here and used anyway.** It needs iid returns and a
  locally linear book; the book's gamma changes sign 19 times. Every scaled
  output carries a flag.
- **The worst stress number is a lower bound, not an estimate.** Shocked vols are
  clipped at a 3% floor, and the worst grid cell sits on that floor.
- **The historical VaR fails its backtest** at 95% and 97.5% (above).
- **This is a self-validation.** SR 11-7 requires effective challenge from
  parties independent of model development; this report was written by the
  model's developer and does not satisfy that requirement.

None of this is a production risk system. It is a research codebase, and the
open items are tracked in `docs/MODEL_VALIDATION.md` §7.
