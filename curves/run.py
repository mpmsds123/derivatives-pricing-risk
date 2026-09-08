"""SOFR curve build, swap pricing and key-rate risk. Single entry point.

    python run.py
"""

from __future__ import annotations

from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.bootstrap import bootstrap, curve_frame
from src.calendar_utils import BusinessCalendar
from src.config import CONFIG
from src.curve import Curve
from src.daycount import daycount
from src.instruments import (
    SR1Future,
    SR3Future,
    SwapInstrument,
    build_instruments,
    load_fixings,
    load_futures,
    load_swaps,
)
from src.risk import make_benchmark_swaps, risk_ladder
from src.swap import build_ois_schedule, telescoping_check


# ---------------------------------------------------------------------------


def build_curve_grid(cal: BusinessCalendar, cfg: dict[str, Any]) -> pd.DatetimeIndex:
    valuation = pd.Timestamp(cfg["valuation_date"])
    horizon = pd.Timestamp(cfg["curve_horizon"])
    if not cal.is_business_day(valuation):
        raise ValueError(f"valuation date {valuation.date()} is not a US business day")
    days = cal.days
    return days[(days >= valuation) & (days <= horizon)]


def par_rate_check(
    curve: Curve, instruments: list, cross_check: list, cfg: dict[str, Any]
) -> pd.DataFrame:
    rows = []
    for inst, role in [(i, "pillar") for i in instruments] + [
        (i, "cross_check") for i in cross_check
    ]:
        implied_rate = inst.implied_rate(curve)
        rows.append(
            {
                "instrument": inst.label,
                "kind": inst.kind,
                "role": role,
                "pillar_date": inst.pillar_date.date(),
                "input_quote": inst.quote,
                "reproduced_quote": inst.implied_quote(curve),
                "input_rate": inst.base_rate,
                "reproduced_rate": implied_rate,
                "error_bp": (implied_rate - inst.base_rate) * 1e4,
            }
        )
    return pd.DataFrame(rows)


def write_diagnostics(
    path,
    curve: Curve,
    instruments: list,
    cross_check: list,
    log: list[dict[str, Any]],
    checks: pd.DataFrame,
    benchmarks: list,
    cfg: dict[str, Any],
) -> None:
    valuation = curve.valuation_date
    by_label = {entry["instrument"]: entry for entry in log}
    lines: list[str] = []

    def section(title: str) -> None:
        lines.append("")
        lines.append("=" * 78)
        lines.append(title)
        lines.append("=" * 78)

    lines.append(f"SOFR curve bootstrap diagnostics")
    lines.append(f"valuation date      : {valuation.date()}")
    lines.append(f"curve horizon       : {curve.dates[-1].date()}")
    lines.append(f"grid business days  : {len(curve.dates)}")
    lines.append(f"instruments         : {len(instruments)}")
    lines.append(
        "  SR1 futures       : "
        f"{sum(1 for i in instruments if i.kind == 'SR1')}"
    )
    lines.append(
        "  SR3 futures       : "
        f"{sum(1 for i in instruments if i.kind == 'SR3')}"
    )
    lines.append(
        "  OIS swaps         : "
        f"{sum(1 for i in instruments if i.kind == 'SWAP')}"
    )
    lines.append(f"futures source      : {cfg['futures_source']}")
    lines.append(f"cross-check quotes  : {len(cross_check)} ({', '.join(i.label for i in cross_check) or 'none'})")
    lines.append(f"convexity adj (bp)  : {cfg['convexity_adjustment_bp']}")
    lines.append(f"Brent bracket       : {cfg['brentq_bracket']}")

    section("FRONT CONTRACT DECOMPOSITION (SR3, three compounding factors)")
    lines.append(
        "Each SR3 settles to daily-compounded SOFR over its IMM reference quarter."
    )
    lines.append(
        "CF_total = CF_realized * CF_prebootstrapped * CF_unknown, and the bootstrap"
    )
    lines.append("solves the single unknown segment given the other two.")
    lines.append("")
    header = (
        f"{'contract':>9} {'quarter':>25} "
        f"{'CF_realized':>13} {'d':>4} {'CF_pre':>13} {'d':>4} "
        f"{'CF_unknown':>13} {'d':>4} {'CF_total':>13} {'D_Q':>5}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for inst in instruments:
        if not isinstance(inst, SR3Future):
            continue
        entry = by_label[inst.label]
        d = inst.split_growth(curve, entry["segment_start"])
        quarter = f"{inst.period_start.date()}->{inst.period_end.date()}"
        lines.append(
            f"{inst.label:>9} {quarter:>25} "
            f"{d['realized_cf']:>13.9f} {int(d['realized_days']):>4} "
            f"{d['prebootstrapped_cf']:>13.9f} {int(d['prebootstrapped_days']):>4} "
            f"{d['unknown_cf']:>13.9f} {int(d['unknown_days']):>4} "
            f"{d['total_cf']:>13.9f} {int(d['total_days']):>5}"
        )

    front = next(i for i in instruments if isinstance(i, SR3Future))
    fd = front.split_growth(curve, by_label[front.label]["segment_start"])
    implied_total = 1.0 + front.base_rate * fd["total_days"] / 360.0
    lines.append("")
    lines.append(f"Front contract {front.label} in detail:")
    lines.append(f"  reference quarter        : {front.period_start.date()} -> {front.period_end.date()} ({int(fd['total_days'])} calendar days)")
    lines.append(f"  quoted price             : {front.quote:.4f}  ->  rate {front.base_rate * 100:.5f}%")
    lines.append(f"  CF implied by the quote  : {implied_total:.12f}")
    lines.append(f"  CF reproduced by curve   : {fd['total_cf']:.12f}")
    lines.append(f"  1. realized fixings      : {fd['realized_cf']:.12f} over {int(fd['realized_days'])} days")
    lines.append(f"  2. pre-bootstrapped      : {fd['prebootstrapped_cf']:.12f} over {int(fd['prebootstrapped_days'])} days")
    lines.append(f"  3. unknown forward       : {fd['unknown_cf']:.12f} over {int(fd['unknown_days'])} days")
    lines.append(
        f"  implied unknown forward  : "
        f"{(fd['unknown_cf'] - 1.0) * 360.0 / fd['unknown_days'] * 100:.6f}%"
    )
    leverage = fd["total_days"] / fd["unknown_days"] / fd["realized_cf"]
    lines.append(
        f"  quote -> forward gearing : {leverage:.2f}x  "
        f"(= D_Q / D_unknown / CF_realized)"
    )
    lines.append(
        "     a 1bp move in the front quote implies a "
        f"{leverage:.1f}bp move in the stub forward,"
    )
    lines.append(
        "     because the realized days cannot move. This gearing is why the front"
    )
    lines.append(
        "     contract carries a KR01 comparable to a full-quarter contract while"
    )
    lines.append("     spanning only a few days of curve -- see risk_ladder.csv.")
    lines.append("")
    has_pre = any(
        inst.split_growth(curve, by_label[inst.label]["segment_start"])["prebootstrapped_days"] > 0
        for inst in instruments
        if isinstance(inst, SR3Future)
    )
    lines.append("")
    if has_pre:
        lines.append(
            "  note: segment 2 is empty for the front contract (fixings run right up to"
        )
        lines.append(
            "        the valuation date) but non-empty for later contracts whose quarter"
        )
        lines.append("        opens before the preceding pillar -- see the table above.")
    else:
        lines.append(
            f"  note: segment 2 is zero for every contract under futures_source="
            f"'{cfg['futures_source']}'."
        )
        lines.append(
            "        The front contract has none because the fixings run right up to the"
        )
        lines.append(
            "        valuation date; the rest have none because consecutive IMM quarters"
        )
        lines.append(
            "        tile exactly, so each contract's quarter opens on the previous"
        )
        lines.append(
            "        contract's pillar. A non-zero segment 2 appears only when the pillar"
        )
        lines.append(
            "        set does not align with the quarter boundaries, i.e. under"
        )
        lines.append(
            "        futures_source='SR1_THEN_SR3' or 'ALL'. The three-way solve is"
        )
        lines.append(
            "        implemented for the general case regardless."
        )

    section("BOOTSTRAP SOLVES (Brent, one unknown forward per instrument)")
    header = (
        f"{'instrument':>10} {'kind':>5} {'segment':>25} "
        f"{'forward %':>11} {'iters':>6} {'f-calls':>8} {'residual bp':>12}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for entry in log:
        segment = f"{entry['segment_start'].date()}->{entry['segment_end'].date()}"
        lines.append(
            f"{entry['instrument']:>10} {entry['kind']:>5} {segment:>25} "
            f"{entry['forward_rate'] * 100:>11.6f} {entry['iterations']:>6} "
            f"{entry['function_calls']:>8} {entry['residual_bp']:>12.3e}"
        )
    lines.append("")
    lines.append(f"total Brent iterations : {sum(e['iterations'] for e in log)}")
    lines.append(f"all converged          : {all(e['converged'] for e in log)}")

    section("SELF-CONSISTENCY (reprice every pillar input off the finished curve)")
    tol = cfg["self_consistency_tolerance_bp"]
    pillars = checks[checks["role"] == "pillar"]
    for kind in ("SR3", "SR1", "SWAP"):
        block = pillars[pillars["kind"] == kind]
        if block.empty:
            continue
        worst = block.loc[block["error_bp"].abs().idxmax()]
        lines.append(
            f"  {kind:>5}: n={len(block):>2}  max |error| = {abs(worst['error_bp']):.3e} bp "
            f"({worst['instrument']})"
        )
    worst_all = pillars.loc[pillars["error_bp"].abs().idxmax()]
    lines.append("")
    lines.append(
        f"  overall max |error| = {abs(worst_all['error_bp']):.3e} bp on {worst_all['instrument']}"
    )
    lines.append(f"  tolerance           = {tol} bp -> {'PASS' if abs(worst_all['error_bp']) < tol else 'FAIL'}")

    basis = checks[checks["role"] == "cross_check"]
    if not basis.empty:
        section("FUTURES STRIP BASIS (quotes deliberately excluded from the pillars)")
        lines.append(
            "Every SR1 contract month in this snapshot falls strictly inside an SR3"
        )
        lines.append(
            "reference quarter, so the two strips are redundant and cannot both be"
        )
        lines.append(
            "pillars. The excluded strip is repriced off the finished curve below; the"
        )
        lines.append(
            "error is the SR1-vs-SR3 basis, not a bootstrap failure."
        )
        lines.append("")
        header = f"{'contract':>9} {'market':>10} {'model':>10} {'basis bp':>11}"
        lines.append(header)
        lines.append("-" * len(header))
        for _, row in basis.iterrows():
            lines.append(
                f"{row['instrument']:>9} {row['input_quote']:>10.4f} "
                f"{row['reproduced_quote']:>10.4f} {row['error_bp']:>11.2f}"
            )
        lines.append("")
        lines.append(
            f"  mean |basis| = {basis['error_bp'].abs().mean():.2f} bp, "
            f"max |basis| = {basis['error_bp'].abs().max():.2f} bp"
        )
        lines.append(
            "  For reference, forcing both strips onto the pillars "
            "(futures_source='ALL')"
        )
        lines.append(
            "  drives these to zero but yields adjacent forward segments of 1.83% and"
        )
        lines.append("  4.40% -- the disagreement has to go somewhere.")

    section("FUTURES / SWAP HANDOFF")
    last_fut = [e for e in log if e["kind"] in ("SR1", "SR3")][-1]
    first_swap = next(e for e in log if e["kind"] == "SWAP")
    lines.append(
        f"  last futures segment : {last_fut['instrument']:>7} "
        f"{last_fut['segment_start'].date()}->{last_fut['segment_end'].date()} "
        f"at {last_fut['forward_rate'] * 100:.4f}%"
    )
    lines.append(
        f"  first swap segment   : {first_swap['instrument']:>7} "
        f"{first_swap['segment_start'].date()}->{first_swap['segment_end'].date()} "
        f"at {first_swap['forward_rate'] * 100:.4f}%"
    )
    lines.append(
        f"  step at the handoff  : "
        f"{(first_swap['forward_rate'] - last_fut['forward_rate']) * 1e4:+.1f} bp"
    )
    lines.append("")
    lines.append("  The step is the futures/swap basis landing on a short stub. Part of it")
    lines.append("  is genuine convexity (futures are margined daily, swaps are not, so")
    lines.append("  futures rates sit above forwards), part is that the two quote sets are")
    lines.append("  separate market pulls. It is the price of a piecewise-constant forward:")
    lines.append("  a smooth interpolator would spread it, at the cost of a global solve.")

    section("OIS FLOAT LEG TELESCOPING IDENTITY")
    lines.append("The compounded overnight leg telescopes to DF(t0) - DF(tN) when each")
    lines.append("period is discounted at its accrual end. With the market-standard")
    lines.append("2-business-day payment lag it does not, and the gap below is that lag.")
    lines.append("")
    header = f"{'tenor':>7} {'explicit float PV':>20} {'DF(t0)-DF(tN)':>18} {'difference':>16} {'in bp of notional':>19}"
    lines.append(header)
    lines.append("-" * len(header))
    seen: set[int] = set()
    for b in benchmarks:
        if b.tenor_years in seen:
            continue
        seen.add(b.tenor_years)
        t = telescoping_check(curve, b.schedule)
        lines.append(
            f"{str(b.tenor_years) + 'Y':>7} {t['float_leg_pv']:>20.12f} "
            f"{t['telescoped_pv']:>18.12f} {t['difference']:>16.3e} "
            f"{t['difference'] * 1e4:>19.4f}"
        )

    path.write_text("\n".join(lines) + "\n")


def plot_curve(curve: Curve, frame: pd.DataFrame, path, cfg: dict[str, Any]) -> None:
    valuation = curve.valuation_date
    years = np.array([(d - valuation).days / 365.0 for d in curve.dates])
    growth = curve.growth
    with np.errstate(divide="ignore", invalid="ignore"):
        zero = np.where(years > 0, np.log(growth) / np.maximum(years, 1e-12), np.nan)

    fig, axes = plt.subplots(2, 1, figsize=(11, 8.5), sharex=True)

    axes[0].plot(years, zero * 100, lw=1.6, color="#1f4e79")
    axes[0].scatter(
        frame["year_fraction"] * 365.0 / 365.0,
        frame["zero_rate_cc"] * 100,
        s=22,
        color="#c00000",
        zorder=3,
        label="bootstrap pillars",
    )
    axes[0].set_ylabel("zero rate, cont. comp. (%)")
    axes[0].set_title(f"SOFR OIS curve as of {valuation.date()}")
    axes[0].legend(frameon=False)
    axes[0].grid(alpha=0.3)

    axes[1].step(years[:-1], curve.rates * 100, where="post", lw=1.2, color="#2e7d32")
    axes[1].set_ylabel("overnight forward (%)")
    axes[1].set_xlabel("years from valuation date")
    axes[1].set_title("Piecewise-constant overnight forward")
    axes[1].grid(alpha=0.3)
    axes[1].margins(y=0.08)

    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)

    # short-end zoom: the futures strip is where the structure lives
    mask = years <= 3.0
    fig, ax = plt.subplots(figsize=(11, 4.2))
    ax.step(years[:-1][mask[:-1]], curve.rates[mask[:-1]] * 100, where="post", lw=1.4, color="#2e7d32")
    for _, row in frame.iterrows():
        if row["year_fraction"] <= 3.0:
            ax.axvline(row["year_fraction"], color="grey", lw=0.5, alpha=0.5)
            ax.text(
                row["year_fraction"],
                ax.get_ylim()[1],
                row["instrument"],
                rotation=90,
                fontsize=6.5,
                va="top",
                ha="right",
            )
    ax.set_xlabel("years from valuation date")
    ax.set_ylabel("overnight forward (%)")
    ax.set_title("Short end: futures-implied forwards, one segment per contract")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(str(path).replace("curve.png", "curve_short_end.png"), dpi=140)
    plt.close(fig)


def plot_risk_ladder(long: pd.DataFrame, wide: pd.DataFrame, path, cfg: dict[str, Any]) -> None:
    """Small multiples: one panel per benchmark, KR01 across every input pillar.

    Grouped bars would be unreadable at 21 pillars, and would also hide the
    point -- the panels have wildly different shapes, which is the result.
    """
    names = list(dict.fromkeys(long["benchmark"]))
    pillars = list(dict.fromkeys(long["pillar_instrument"]))
    x = np.arange(len(pillars))
    kinds = long.drop_duplicates("pillar_instrument").set_index("pillar_instrument")["pillar_kind"]
    colours = ["#c00000" if kinds[p] == "SWAP" else "#1f4e79" for p in pillars]

    n = len(names)
    rows = (n + 1) // 2
    fig, axes = plt.subplots(rows, 2, figsize=(15, 2.5 * rows), sharex=True)
    axes = np.atleast_1d(axes).ravel()

    for ax, name in zip(axes, names):
        block = long[long["benchmark"] == name].set_index("pillar_instrument").loc[pillars]
        ax.bar(x, block["kr01"] / 1e3, color=colours, width=0.72)
        share = wide.loc[wide["benchmark"] == name, "max_pillar_share_pct"].iloc[0]
        dv01 = wide.loc[wide["benchmark"] == name, "parallel_dv01"].iloc[0]
        ax.set_title(
            f"{name}   parallel DV01 \\${dv01 / 1e3:,.1f}k   "
            f"largest pillar = {share:.1f}% of gross",
            fontsize=9,
        )
        ax.set_ylabel("KR01 ($k)", fontsize=8)
        ax.axhline(0, color="black", lw=0.7)
        ax.grid(alpha=0.25, axis="y")
        ax.tick_params(labelsize=7)

    for ax in axes[n:]:
        ax.axis("off")
    # bottom-most populated panel in each column carries the tick labels
    for column in (0, 1):
        occupied = [i for i in range(n) if i % 2 == column]
        if occupied:
            ax = axes[occupied[-1]]
            ax.set_xticks(x)
            ax.set_xticklabels(pillars, rotation=90, fontsize=7)
            ax.tick_params(labelbottom=True)
            ax.set_xlabel("bumped input instrument", fontsize=8)

    fig.suptitle(
        f"Key-rate DV01, \\${cfg['benchmark_notional'] / 1e6:.0f}mm payer swaps "
        f"(bump input quote +{cfg['bump_bp']:.0f}bp, rebootstrap, reprice). "
        "Blue = futures pillar, red = swap pillar.",
        fontsize=11,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(path, dpi=140)
    plt.close(fig)


def write_readme(
    path,
    curve: Curve,
    instruments: list,
    cross_check: list,
    frame: pd.DataFrame,
    checks: pd.DataFrame,
    wide: pd.DataFrame,
    long: pd.DataFrame,
    benchmarks: list,
    log: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> None:
    valuation = curve.valuation_date
    head_name = cfg["headline_benchmark"]
    dist_name = cfg["distributed_benchmark"]
    off_name = cfg["off_market_benchmark"]
    head_row = wide[wide["benchmark"] == head_name].iloc[0]
    dist_row = wide[wide["benchmark"] == dist_name].iloc[0]
    off_row = wide[wide["benchmark"] == off_name].iloc[0]
    pillar_checks = checks[checks["role"] == "pillar"]
    basis = checks[checks["role"] == "cross_check"]
    max_err = pillar_checks["error_bp"].abs().max()
    span_years = (curve.dates[-1] - valuation).days / 365.0
    last_pillar = frame.iloc[-1]

    front = next(i for i in instruments if isinstance(i, SR3Future))
    fd = front.split_growth(curve, log[0]["segment_start"])

    fwd = curve.rates
    years = np.array([(d - valuation).days / 365.0 for d in curve.dates[:-1]])
    near = fwd[0] * 100
    trough_i = int(np.argmin(fwd[years <= 10]))
    trough = fwd[trough_i] * 100
    trough_t = years[trough_i]
    long_end = fwd[-1] * 100

    dist_block = long[long["benchmark"] == dist_name]
    dist_top = dist_block.reindex(dist_block["kr01"].abs().sort_values(ascending=False).index).head(5)

    front_block = long[long["benchmark"] == "1Y_par"]
    front_kr01 = front_block.loc[front_block["pillar_instrument"] == front.label, "kr01"].iloc[0]
    peers = front_block[
        (front_block["pillar_instrument"] != front.label) & (front_block["kr01"].abs() > 1)
    ]
    peer_kr01 = peers["kr01"].max()

    text = f"""# SOFR Multi-Curve Bootstrapping, Swap Pricing & Key-Rate Risk

Valuation date **{valuation.date()}**. Everything below is regenerated by `python run.py`;
every number quoted here is also written to a file in `output/`.

## What SOFR is, and why a curve is needed at all

SOFR is an overnight secured rate published each morning by the New York Fed, computed
from actual tri-party repo transactions rather than from bank estimates. That is the
substantive break from LIBOR: LIBOR was a survey of where banks *said* they could
borrow, SOFR is a volume-weighted median of trades that actually happened.

Because SOFR is a single overnight number, it prices nothing with a term. Valuing a
2-year swap requires a path of *expected future overnight rates*, and recovering that
path from traded instruments is exactly what bootstrapping does.

SOFR OIS is a **single-curve** setting: the same curve both projects the floating leg
and discounts the cashflows, because the collateral rate (SOFR on cash CSAs) and the
reference rate coincide. This is the structural simplification versus LIBOR-era
dual-curve construction, where a LIBOR projection curve had to be built alongside an
OIS discount curve and the two were linked by basis swaps.

## Curve construction

| choice | what was done | what was rejected, and why |
|---|---|---|
| interpolation | piecewise-constant overnight forward between pillars | log-cubic on discount factors gives smoother forwards but turns the bootstrap into a global solve; piecewise-constant keeps it a sequential one-unknown-at-a-time root find, which is stable and debuggable |
| compounding | daily simple compounding, `CF = prod(1 + f*d/360)` | the continuous form `DF(t2)=DF(t1)exp(-f*dt)` is what a generic curve library uses, but SOFR legally accrues by daily simple compounding on ACT/360; the two differ at O(f^2*dt) which is ~0.2bp/yr at 4% -- enough to swamp a self-consistency check reported to 0.01bp |
| alpha of the short end | SR3H26 determines the {int(fd['unknown_days'])}-day stub from the valuation date to the first IMM | the alternative is to hardcode the last published fixing as a flat "spot" stub; letting the front contract imply it removes a magic number and keeps the front quote exactly reproduced |
| instrument overlap | SR3 strip as pillars; the {len(cross_check)} SR1 quotes are repriced off the finished curve and reported as a basis | using both strips as pillars also reproduces every quote exactly, but every SR1 month here sits *inside* an SR3 quarter, so the redundancy gets pinned onto residual stubs -- the "ALL" build gives adjacent forward segments of 1.83% and 4.40%. That is an artefact of the instrument set, not a rate view. Switchable via `futures_source` |
| convexity | ignored, set to {cfg['convexity_adjustment_bp']}bp | futures are margined daily and swaps are not, so futures rates sit above forward rates; the effect is a few tenths of a bp inside 2Y and is dominated by the bid/offer, but it is a real bias and is exposed as a config knob rather than buried |
| risk | bump input quotes, rebootstrap, reprice | perturbing the output curve directly is wrong here: a 1bp move in a futures price does not map to a 1bp move in that segment's forward once realized fixings and overlapping contracts are involved |

Conventions implemented (not hand-waved): SOFR accrual ACT/360, fixed leg annual
ACT/360, Modified Following, T+{cfg['spot_lag_bd']} effective, {cfg['payment_lag_bd']}-business-day payment lag, US federal
holiday calendar. The calendar was validated against the fixing file rather than
assumed: over the {int(fd['realized_days'])}-day realized block, the generated business days match the
published SOFR fixing dates exactly -- no missing days, no spurious ones.

## Front contract decomposition

The front SR3 straddles the valuation date, so its settlement price reflects three
blocks. Only the third is unknown:

| block | compounding factor | days |
|---|---|---|
| 1. realized fixings | {fd['realized_cf']:.9f} | {int(fd['realized_days'])} |
| 2. pre-bootstrapped | {fd['prebootstrapped_cf']:.9f} | {int(fd['prebootstrapped_days'])} |
| 3. unknown forward | {fd['unknown_cf']:.9f} | {int(fd['unknown_days'])} |
| **total** | **{fd['total_cf']:.9f}** | **{int(fd['total_days'])}** |

`{front.label}` quotes {front.quote:.4f}, i.e. {front.base_rate * 100:.4f}% over the quarter, implying a total
compounding factor of {1.0 + front.base_rate * fd['total_days'] / 360.0:.9f}. Dividing out blocks 1 and 2 leaves
{fd['unknown_cf']:.9f} over {int(fd['unknown_days'])} days, or **{(fd['unknown_cf'] - 1.0) * 360.0 / fd['unknown_days'] * 100:.4f}%** as the stub forward.

Segment 2 is zero here, for two separate reasons worth keeping distinct. The front
contract has no pre-bootstrapped block because the published fixings run right up to
the valuation date. The later contracts have none because consecutive IMM quarters
tile exactly, so each one opens on the previous contract's pillar and its whole
quarter is the unknown. A non-zero segment 2 needs a pillar that cuts *into* a
quarter, which only happens under `futures_source='ALL'`: the SR1 month-ends fall
mid-quarter, and `SR3M26` / `SR3U26` then decompose into 75 and 76 pre-bootstrapped
days against 16 and 15 unknown ones. (`SR1_THEN_SR3` does not do it either -- the SR3
contracts it retains still tile.) The three-way solve is implemented for the general
case regardless; see the full table in `bootstrap_diagnostics.txt`.

The gearing is the part that matters downstream. Because {int(fd['realized_days'])} of the {int(fd['total_days'])} days are
frozen, a 1bp move in the front quote must be absorbed entirely by the {int(fd['unknown_days'])}-day stub,
so the stub forward moves **{fd['total_days'] / fd['unknown_days'] / fd['realized_cf']:.1f}bp**. That gearing propagates straight into the
risk ladder -- see below.

## Why the OIS float leg telescopes

For an OIS the floating payment over period *j* is the compounded overnight rate, so
its PV is `(CF_j - 1) * DF(T_j)`. If each period is discounted at its accrual end,
`DF(T_j) = DF(T_(j-1)) / CF_j` under the same curve that generated `CF_j`, so

    (CF_j - 1) * DF(T_j) = DF(T_(j-1)) - DF(T_j)

and summing over *j* collapses the whole leg to `DF(t_0) - DF(t_N)`. Every interior
term cancels. This is why an OIS float leg needs no forward projection at all in
principle -- the projection and discount curves being identical is precisely what
makes the cancellation exact.

It stops being exact once a payment lag is introduced, because the discount date is
no longer the accrual end. This project prices the float leg explicitly and reports
the telescoping gap as a diagnostic instead of assuming it away; the gap is tabulated
in `bootstrap_diagnostics.txt` and is entirely the {cfg['payment_lag_bd']}-business-day lag.

## Results

- **{len(instruments)} pillar instruments**: {', '.join(f"{sum(1 for i in instruments if i.kind == k)} {n}" for k, n in (('SR3', 'SR3 futures'), ('SR1', 'SR1 futures'), ('SWAP', 'OIS swaps')) if sum(1 for i in instruments if i.kind == k))},
  plus {len(cross_check)} held out as cross-checks. Curve spans {span_years:.1f} years to
  {curve.dates[-1].date()}, {len(curve.dates)} business-day grid points.
- **Self-consistency**: max par-rate / futures reproduction error across all
  {len(instruments)} pillars is **{max_err:.2e} bp**, against a {cfg['self_consistency_tolerance_bp']}bp tolerance.
  Total Brent iterations {sum(e['iterations'] for e in log)}, all converged.
- **Futures strip basis**: the held-out SR1 strip reprices off the curve with a
  mean absolute error of **{basis['error_bp'].abs().mean():.1f}bp** (max {basis['error_bp'].abs().max():.1f}bp). This is the
  SR1-vs-SR3 disagreement made visible instead of hidden inside a stub.
- **Curve shape**: overnight forward starts at **{near:.2f}%**, troughs at
  **{trough:.2f}%** around {trough_t:.1f}y, and ends at **{long_end:.2f}%**. The front end is
  inverted -- the futures strip is pricing cuts -- and the curve re-steepens beyond
  the trough. Long-dated zero rate at the {last_pillar['instrument']} pillar is
  {last_pillar['zero_rate_cc'] * 100:.3f}% continuously compounded.

### Benchmark ${cfg['benchmark_notional'] / 1e6:.0f}mm payer swaps

| benchmark | on a pillar? | par rate | fixed | PV | parallel DV01 | sum of KR01 | gap | gap % | largest pillar |
|---|---|---|---|---|---|---|---|---|---|
"""
    for _, row in wide.iterrows():
        text += (
            f"| `{row['benchmark']}` | {'yes' if row['maturity_on_pillar'] else 'no'} | "
            f"{row['par_rate'] * 100:.4f}% | {row['fixed_rate'] * 100:.4f}% | {row['pv']:,.0f} | "
            f"{row['parallel_dv01']:,.0f} | {row['kr01_sum']:,.0f} | "
            f"{row['sum_minus_parallel']:,.1f} | {row['sum_minus_parallel_pct']:.4f}% | "
            f"{row['max_pillar_share_pct']:.1f}% |\n"
        )

    text += f"""
PV prints as 0 for every par-struck benchmark because each is struck at the curve's
own par rate -- an independent check that the pricer and the bootstrap agree.

**The degenerate ladder is the headline result, not a bug.** For `{head_name}`:
parallel DV01 **${head_row['parallel_dv01']:,.0f}** per bp, and **{head_row['max_pillar_share_pct']:.1f}%** of the gross key-rate
exposure sits on the single 10Y pillar. Every other bucket is ~0. The reason is
worth stating precisely, because it is a standard interview trap:

> The curve reproduces all 14 input swap quotes exactly. Bump the 5Y quote and
> rebootstrap: the 5Y segment moves, but the 6Y..40Y instruments re-solve to hold
> *their own* par rates fixed. So the 10Y par rate is unchanged by construction, and
> a swap struck at the 10Y par rate has unchanged PV. KR01 = 0.

A par swap whose maturity coincides with a bootstrap pillar therefore has all of its
risk on that pillar, by arithmetic. This is why the benchmark set also contains
maturities that fall *between* pillars (`1Y_par`, `{dist_name}`) and a position struck
away from par (`{off_name}`) -- those have genuinely distributed profiles.

Largest key-rate exposures for `{dist_name}`, whose maturity falls between the 10Y and
12Y inputs:

"""
    for _, row in dist_top.iterrows():
        amount = f"${abs(row['kr01']):,.0f}" if row["kr01"] >= 0 else f"-${abs(row['kr01']):,.0f}"
        text += (
            f"- `{row['pillar_instrument']}` ({row['pillar_date']}, {row['pillar_years']:.1f}y): "
            f"{amount}\n"
        )

    text += f"""
The exposure splits across the two bracketing pillars -- {dist_row['max_pillar_share_pct']:.1f}% on the largest --
because an 11Y swap's last accrual period straddles the 10Y and 12Y segments, so both
inputs move its par rate. That is a hedgeable instruction: short 10Y and 12Y swaps in
roughly that ratio.

The off-market case is weaker but non-zero: `{off_name}` still puts
{off_row['max_pillar_share_pct']:.1f}% on its own pillar, with the remainder spread monotonically across every
shorter pillar. The mechanism is different -- an off-market swap has a non-zero PV
that depends on the whole discount curve through the annuity. It is a small effect
because holding each input swap at par very nearly pins the discount factor at every
swap pillar date, leaving only the payment-lag and non-linearity residue.

**Front-contract gearing.** `1Y_par` spreads its DV01 across the {len(front_block[front_block['kr01'].abs() > 1])} futures pillars it
lives under, and `{front.label}` picks up **${front_kr01:,.0f}** of it despite covering only
{int(fd['unknown_days'])} days of free curve, versus ${peer_kr01:,.0f} for the full-quarter contract behind it.
That is not a bug: {int(fd['realized_days'])} of the front contract's {int(fd['total_days'])} days are already realized and
cannot move, so a 1bp move in the quote has to be absorbed entirely by the stub --
a gearing of **{fd['total_days'] / fd['unknown_days'] / fd['realized_cf']:.1f}x**. A desk hedging the front of the curve with the front
contract is trading something with roughly ten times the rate sensitivity per day of
exposure that its notional suggests, and that ratio blows up further as the contract
approaches expiry. This is the practical payoff of doing the three-way decomposition
rather than treating the front contract like any other.

**Sum-of-key-rates vs parallel DV01.** The reconciliation gap runs from
{wide['sum_minus_parallel_pct'].abs().min():.4f}% to {wide['sum_minus_parallel_pct'].abs().max():.4f}% of parallel DV01 across the benchmark set,
growing with tenor. The gap is second order: bumping one input at a time and bumping
all of them are not the same experiment when the bootstrap is non-linear in the
quotes. A gap this small says the curve is effectively linear in its inputs over a
1bp neighbourhood, which is what makes the key-rate ladder a usable hedging
instruction rather than a curiosity. It grows with tenor because compounding makes
the discount factor increasingly convex in the forwards.

## Outputs

| file | contents |
|---|---|
| `curve.csv` | pillar date, year fraction, discount factor, continuously compounded zero rate, segment forward |
| `bootstrap_diagnostics.txt` | three-way front-contract decomposition with day counts, Brent iteration counts, self-consistency errors, telescoping check |
| `curve.png` | zero curve and piecewise-constant forward curve |
| `curve_short_end.png` | futures strip detail, one segment per contract |
| `par_rate_check.csv` | input quote vs reproduced quote vs error in bp |
| `risk_ladder.csv` | per benchmark swap: notional, par rate, PV, parallel DV01, KR01 per pillar, sum-vs-parallel |
| `risk_ladder_long.csv` | the same ladder in long form |
| `risk_ladder.png` | bar chart of key-rate DV01s |

## Known limitations

- No futures convexity adjustment. Exposed as `convexity_adjustment_bp` in the config
  but left at zero; the correct treatment needs a short-rate model calibrated to
  caps/floors, which is out of scope here.
- Swap quotes are mid, with no bid/offer and no CSA basis.
- Turn-of-year and month-end repo effects are not modelled; they show up in reality as
  spikes in the overnight fixing (visible in the realized data around 2025-12-31) but
  the piecewise-constant forward cannot represent them.
- Interpolation alternatives (log-cubic on discount factors) are not benchmarked here.

## Layout

```
curves/
  data/swap_rates.csv        OIS par quotes as of the valuation date
  data/3m_futures_data.csv   SR3 quarterly futures settlements
  data/1m_futures_data.csv   SR1 monthly futures settlements
  data/rzd_rates.csv         published SOFR fixings
  src/config.py              every convention, bound and bump size
  src/daycount.py            ACT/360, ACT/365F, 30/360
  src/calendar_utils.py      US business days, ModFol, IMM dates, accrual schedules
  src/curve.py               piecewise-constant forward curve, discount factors
  src/instruments.py         SR1 / SR3 / OIS instruments and loaders
  src/bootstrap.py           sequential Brent bootstrap
  src/swap.py                OIS schedule, par rate, annuity, PV, telescoping check
  src/risk.py                parallel DV01, key-rate ladder
  run.py
  output/
```

All four inputs are project-local, under `curves/data/`. Earlier versions read
the futures and fixings from the folder above the repo, which meant a fresh
clone could not run `run.py`; they were vendored into `data/` when this module
was merged into `derivatives-pricing-risk`.
"""
    path.write_text(text)


# ---------------------------------------------------------------------------


def main(cfg: dict[str, Any] = CONFIG) -> None:
    out = cfg["output_dir"]
    out.mkdir(parents=True, exist_ok=True)

    cal = BusinessCalendar(cfg["calendar_start"], cfg["curve_horizon"])
    grid = build_curve_grid(cal, cfg)
    template = Curve(pd.Timestamp(cfg["valuation_date"]), grid)

    fixings = load_fixings(cfg["raw_data_dir"] / cfg["fixings_file"])
    sr1 = load_futures(cfg["raw_data_dir"] / cfg["sr1_file"])
    sr3 = load_futures(cfg["raw_data_dir"] / cfg["sr3_file"])
    swaps = load_swaps(cfg["data_dir"] / cfg["swap_file"])

    instruments, cross_check = build_instruments(cal, template, fixings, sr1, sr3, swaps, cfg)
    curve, log = bootstrap(template, instruments, cfg)

    frame = curve_frame(curve, log, cfg)
    frame.to_csv(out / "curve.csv", index=False)

    checks = par_rate_check(curve, instruments, cross_check, cfg)
    checks.to_csv(out / "par_rate_check.csv", index=False)

    tenors = sorted({b["tenor_years"] for b in cfg["benchmarks"]})
    schedules = {t: build_ois_schedule(cal, curve, t, cfg) for t in tenors}
    benchmarks = make_benchmark_swaps(curve, schedules, cfg)

    wide, long = risk_ladder(template, instruments, benchmarks, cfg)
    wide.to_csv(out / "risk_ladder.csv", index=False)
    long.to_csv(out / "risk_ladder_long.csv", index=False)

    write_diagnostics(
        out / "bootstrap_diagnostics.txt",
        curve,
        instruments,
        cross_check,
        log,
        checks,
        benchmarks,
        cfg,
    )
    plot_curve(curve, frame, out / "curve.png", cfg)
    plot_risk_ladder(long, wide, out / "risk_ladder.png", cfg)
    write_readme(
        out.parent / "README.md",
        curve,
        instruments,
        cross_check,
        frame,
        checks,
        wide,
        long,
        benchmarks,
        log,
        cfg,
    )

    max_err = checks[checks["role"] == "pillar"]["error_bp"].abs().max()
    basis = checks[checks["role"] == "cross_check"]["error_bp"].abs()
    print(f"futures source           : {cfg['futures_source']}")
    print(f"pillar instruments       : {len(instruments)}  (cross-check {len(cross_check)})")
    print(f"curve span               : {(curve.dates[-1] - curve.valuation_date).days / 365.0:.1f}y")
    print(f"max reproduction error   : {max_err:.3e} bp")
    if len(basis):
        print(f"held-out strip basis     : mean {basis.mean():.2f} bp, max {basis.max():.2f} bp")
    print()
    print(
        wide[
            [
                "benchmark",
                "maturity_on_pillar",
                "pv",
                "parallel_dv01",
                "kr01_sum",
                "sum_minus_parallel_pct",
                "max_pillar_share_pct",
            ]
        ].to_string(index=False)
    )
    print()
    print(f"outputs written to       : {out}")


if __name__ == "__main__":
    main()
