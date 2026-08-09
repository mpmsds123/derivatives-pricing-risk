"""Market data loaders. Two source paths, one output schema.

1. `load_equity_chain`  -- SPY option chains via yfinance (cached snapshot by
   default so a rerun reproduces the calibration exactly).
2. `load_swaption_grid` -- the swaption vol-grid CSV format from the spec.

Both return a `MarketData` holding a tidy quote frame plus per-expiry
(T, forward, atm_vol, discount).
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from src.black import bs_abs_delta, implied_vol
from src.config import CONFIG

_D = CONFIG["data"]
_F = CONFIG["filter"]
_BP = CONFIG["output"]["bp"]

QUOTE_COLUMNS = [
    "expiry", "T", "forward", "discount", "atm_vol", "strike", "offset_bp",
    "log_moneyness", "cp", "bid", "ask", "mid", "volume", "open_interest",
    "abs_delta", "iv",
]


@dataclass
class MarketData:
    asof: str
    underlying: str
    spot: float
    quotes: pd.DataFrame          # one row per surviving option quote
    expiries: pd.DataFrame        # one row per expiry: T, forward, atm_vol, ...
    filter_report: pd.DataFrame   # how many quotes each rule removed

    def smile(self, expiry: str) -> pd.DataFrame:
        return self.quotes[self.quotes.expiry == expiry].sort_values("strike")


# --------------------------------------------------------------------------
# rate curve
# --------------------------------------------------------------------------
def _rate_curve(yields: dict[str, float]) -> tuple[np.ndarray, np.ndarray]:
    pts = sorted((_D["rate_proxies"][sym], y) for sym, y in yields.items()
                 if sym in _D["rate_proxies"])
    return np.array([p[0] for p in pts]), np.array([p[1] for p in pts])


def _rate_at(T: float, Ts: np.ndarray, Rs: np.ndarray) -> float:
    return float(np.interp(T, Ts, Rs))


# --------------------------------------------------------------------------
# forward implication
# --------------------------------------------------------------------------
def implied_forward(calls: pd.DataFrame, puts: pd.DataFrame, spot: float,
                    discount: float) -> tuple[float, float, int]:
    """Forward from put-call parity with the discount factor pinned.

    C - P = DF * (F - K)  =>  F = K + (C - P)/DF

    Two things matter here, and both were found empirically on this snapshot:

    1. Do NOT regress C-P on K to recover DF and F jointly. The discount signal
       (1 - rT) is far smaller than quote noise at short T, so the regression is
       ill-conditioned and returns DF > 1 (negative rates). Worse, on SPY the
       regression leans on deep ITM legs, and SPY options are AMERICAN: the
       early-exercise premium in ITM puts steepens C-P in K and biases DF upward
       at every expiry (implied r came out near 0% against a 4% Treasury curve).
       Pinning r and implying only F avoids both failures.

    2. Centre the parity window on the FORWARD, not on spot. At 2.4y the SPY
       forward sits ~7.5% above spot, so a spot-centred window selects OTM puts
       against ITM calls -- precisely the asymmetry that maximises the American
       premium. Centring on spot left a 1.2 vol-point put/call seam at the
       longest expiry; iterating to a forward-centred window cuts it to 0.43.

    Returns (forward, dispersion across strikes, n strikes used).
    """
    w = _F["parity_window"]
    m = calls.merge(puts, on="strike", suffixes=("_c", "_p"))
    m = m[(m.bid_c > _F["parity_min_bid"]) & (m.bid_p > _F["parity_min_bid"])]
    if len(m) < _F["parity_min_strikes"]:
        return float("nan"), float("nan"), len(m)

    # Weight by quote tightness: the narrower the two-sided market, the more
    # reliable the parity relation at that strike.
    spread = (m.ask_c - m.bid_c) + (m.ask_p - m.bid_p)
    m = m.assign(_w=1.0 / np.maximum(spread, 1e-4),
                 _F_i=m.strike + (m.mid_c - m.mid_p) / discount)

    F = spot
    window = m
    for _ in range(_F["parity_max_iter"]):
        window = m[(m.strike > F * (1.0 - w)) & (m.strike < F * (1.0 + w))]
        if len(window) < _F["parity_min_strikes"]:
            # Sparse strike ladders (long-dated LEAPS) can empty the window;
            # fall back to the n strikes nearest the current forward.
            window = m.iloc[(m.strike - F).abs().argsort()[:_F["parity_fallback_n"]]]
        F_new = float(np.average(window._F_i, weights=window._w))
        if abs(F_new - F) < _F["parity_tolerance"]:
            F = F_new
            break
        F = F_new

    return F, float(window._F_i.std()), len(window)


# --------------------------------------------------------------------------
# equity chain path
# --------------------------------------------------------------------------
def _prepare_side(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["mid"] = (d["bid"] + d["ask"]) / 2.0
    d["spread"] = d["ask"] - d["bid"]
    d["rel_spread"] = np.where(d["mid"] > 0, d["spread"] / d["mid"], np.inf)
    return d


def _fetch_live(underlying: str, expiries: list[str], out_dir: Path) -> dict:
    """Pull a fresh snapshot from yfinance and cache it to disk."""
    import yfinance as yf

    tk = yf.Ticker(underlying)
    hist = tk.history(period="10d")
    spot = float(hist["Close"].iloc[-1])
    asof = str(hist.index[-1].date())

    frames = []
    for e in expiries:
        oc = tk.option_chain(e)
        for side, frame in (("C", oc.calls), ("P", oc.puts)):
            f = frame.copy()
            f["expiry"] = e
            f["cp"] = side
            frames.append(f)
    chains = pd.concat(frames, ignore_index=True)

    yields = {sym: float(yf.Ticker(sym).history(period="5d")["Close"].iloc[-1]) / 100.0
              for sym in _D["rate_proxies"]}

    out_dir.mkdir(parents=True, exist_ok=True)
    chains.to_csv(out_dir / f"spy_chains_{asof}.csv", index=False)
    hist.to_csv(out_dir / f"spy_spot_{asof}.csv")
    meta = {"underlying": underlying, "asof": asof, "spot": spot,
            "expiries": expiries, "treasury_yields": yields,
            "pulled_at": dt.datetime.now().isoformat()}
    (out_dir / "snapshot_meta.json").write_text(json.dumps(meta, indent=2))
    return {"chains": chains, "meta": meta}


def _load_cached(snapshot_dir: Path) -> dict:
    meta = json.loads((snapshot_dir / "snapshot_meta.json").read_text())
    chains = pd.read_csv(snapshot_dir / f"spy_chains_{meta['asof']}.csv")
    return {"chains": chains, "meta": meta}


def load_equity_chain(snapshot_dir: str | os.PathLike | None = None,
                      expiries: list[str] | None = None,
                      live: bool = False) -> MarketData:
    """SPY option chains -> implied-vol smiles, liquidity filtered."""
    snapshot_dir = Path(snapshot_dir or _D["snapshot_dir"])
    expiries = expiries or _D["expiries"]

    payload = (_fetch_live(_D["underlying"], expiries, snapshot_dir) if live
               else _load_cached(snapshot_dir))
    chains, meta = payload["chains"], payload["meta"]
    spot, asof = float(meta["spot"]), meta["asof"]
    Ts, Rs = _rate_curve(meta["treasury_yields"])
    asof_date = dt.date.fromisoformat(asof)

    lo_d, hi_d = _F["delta_band"]
    quote_rows, expiry_rows, filter_rows = [], [], []

    for expiry in expiries:
        block = chains[chains.expiry == expiry]
        if block.empty:
            continue
        T = (dt.date.fromisoformat(expiry) - asof_date).days / _D["day_count"]
        if T <= 0:
            continue
        r = _rate_at(T, Ts, Rs)
        discount = float(np.exp(-r * T))

        calls = _prepare_side(block[block.cp == "C"])
        puts = _prepare_side(block[block.cp == "P"])

        F, F_disp, n_parity = implied_forward(calls, puts, spot, discount)
        if not np.isfinite(F):
            continue
        q_implied = r - np.log(F / spot) / T

        # OTM only: puts below the forward, calls above it. ITM options carry
        # the same information by parity but trade with far wider spreads.
        otm = pd.concat([
            puts[puts.strike < F].assign(is_call=False),
            calls[calls.strike > F].assign(is_call=True),
        ], ignore_index=True)
        n_raw = len(otm)

        otm = otm[(otm.bid > 0) & (otm.ask > otm.bid)]
        otm["iv"] = [implied_vol(m, F, k, T, ic, discount)
                     for m, k, ic in zip(otm["mid"], otm.strike, otm.is_call)]
        otm = otm.dropna(subset=["iv"])
        n_inverted = len(otm)

        otm["log_moneyness"] = np.log(otm.strike / F)
        otm["offset_bp"] = (otm.strike / F - 1.0) * _BP

        # ATM vol: interpolate the smile at k = 0. Needed before the delta band,
        # which requires a reference vol to convert strikes to deltas.
        s = otm.sort_values("log_moneyness")
        if len(s) < 4:
            continue
        atm_vol = float(np.interp(0.0, s.log_moneyness, s.iv))

        otm["abs_delta"] = bs_abs_delta(F, otm.strike.values, T, atm_vol,
                                        otm.is_call.values)

        # ---- liquidity cascade (counts recorded for the README) ----
        f1 = otm[otm.bid >= _F["min_bid"]]
        f2 = f1[f1.rel_spread <= _F["max_rel_spread"]]
        f3 = f2[f2["openInterest"].fillna(0) >= _F["min_open_interest"]]
        f4 = f3[f3["volume"].fillna(0) >= _F["min_volume"]]
        keep = f4[(f4.abs_delta >= lo_d) & (f4.abs_delta <= hi_d)]

        filter_rows.append(dict(
            expiry=expiry, raw_otm=n_raw, iv_inverted=n_inverted,
            after_min_bid=len(f1), after_rel_spread=len(f2),
            after_open_interest=len(f3), after_volume=len(f4),
            after_delta_band=len(keep)))

        if len(keep) < 5:
            continue

        # Recompute ATM vol on the clean set.
        s = keep.sort_values("log_moneyness")
        atm_vol = float(np.interp(0.0, s.log_moneyness, s.iv))

        for _, row in keep.sort_values("strike").iterrows():
            quote_rows.append(dict(
                expiry=expiry, T=T, forward=F, discount=discount, atm_vol=atm_vol,
                strike=float(row.strike), offset_bp=float(row.offset_bp),
                log_moneyness=float(row.log_moneyness),
                cp="C" if row.is_call else "P",
                bid=float(row.bid), ask=float(row.ask), mid=float(row["mid"]),
                volume=float(row["volume"]) if pd.notna(row["volume"]) else 0.0,
                open_interest=float(row["openInterest"]) if pd.notna(row["openInterest"]) else 0.0,
                abs_delta=float(row.abs_delta), iv=float(row.iv)))

        expiry_rows.append(dict(
            expiry=expiry, T=T, forward=F, forward_dispersion=F_disp,
            n_parity_strikes=n_parity, rate=r, discount=discount,
            implied_div_yield=q_implied, atm_vol=atm_vol, n_quotes=len(keep),
            strike_min=float(keep.strike.min()), strike_max=float(keep.strike.max()),
            offset_min_bp=float(keep.offset_bp.min()),
            offset_max_bp=float(keep.offset_bp.max())))

    return MarketData(
        asof=asof, underlying=_D["underlying"], spot=spot,
        quotes=pd.DataFrame(quote_rows, columns=QUOTE_COLUMNS),
        expiries=pd.DataFrame(expiry_rows),
        filter_report=pd.DataFrame(filter_rows),
    )


# --------------------------------------------------------------------------
# swaption grid path (spec source option 1)
# --------------------------------------------------------------------------
def _parse_pct(x: str | float) -> float:
    if isinstance(x, str) and x.strip().endswith("%"):
        return float(x.strip().rstrip("%")) / 100.0
    return float(x)


def _parse_tenor(x: str | float) -> float:
    if isinstance(x, str):
        return float(x.strip().lower().replace("yr", "").replace("y", ""))
    return float(x)


def load_swaption_grid(path: str | os.PathLike) -> MarketData:
    """Swaption vol grid CSV: columns are percentage strike offsets like
    '-2.00%', index like '5Yr', plus `forward` and `atm_vol` columns.

    Here the bp offsets are ADDITIVE on the forward rate (K = F + offset),
    which is the correct convention in rates -- unlike the equity path, where
    they are relative.
    """
    raw = pd.read_csv(path)
    index_col = raw.columns[0]

    offset_cols = [c for c in raw.columns
                   if c not in (index_col, "forward", "atm_vol")]

    quote_rows, expiry_rows = [], []
    for _, row in raw.iterrows():
        T = _parse_tenor(row[index_col])
        F = float(row["forward"])
        atm_vol = float(row["atm_vol"])
        expiry = str(row[index_col])

        n = 0
        for col in offset_cols:
            vol = row[col]
            if pd.isna(vol):
                continue
            offset = _parse_pct(col)          # decimal rate offset
            K = F + offset                     # additive in rates
            if K <= 0:
                continue
            quote_rows.append(dict(
                expiry=expiry, T=T, forward=F, discount=1.0, atm_vol=atm_vol,
                strike=K, offset_bp=offset * _BP,
                log_moneyness=float(np.log(K / F)), cp="C",
                bid=np.nan, ask=np.nan, mid=np.nan, volume=np.nan,
                open_interest=np.nan, abs_delta=np.nan, iv=float(vol)))
            n += 1

        expiry_rows.append(dict(expiry=expiry, T=T, forward=F,
                                forward_dispersion=0.0, n_parity_strikes=0,
                                rate=np.nan, discount=1.0,
                                implied_div_yield=np.nan, atm_vol=atm_vol,
                                n_quotes=n, strike_min=np.nan, strike_max=np.nan,
                                offset_min_bp=np.nan, offset_max_bp=np.nan))

    return MarketData(
        asof="n/a", underlying="swaption-grid", spot=float("nan"),
        quotes=pd.DataFrame(quote_rows, columns=QUOTE_COLUMNS),
        expiries=pd.DataFrame(expiry_rows),
        filter_report=pd.DataFrame(),
    )
