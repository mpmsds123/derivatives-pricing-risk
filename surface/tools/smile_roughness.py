#!/usr/bin/env python3
"""Per-expiry smile roughness, the metric behind the expiry exclusions in config.py.

    cd surface && python3 tools/smile_roughness.py

Roughness is the root-mean-square second difference of implied vol across the
filtered smile, in vol points:

    roughness = sqrt( mean( (IV[k+1] - 2 IV[k] + IV[k-1])^2 ) ) * 100

Second differences, not first, because a smile is *supposed* to slope: curvature
is the signal and the second difference of a clean smile is small and smooth.
Stale or crossed quotes break that and show up here an order of magnitude above
their neighbours. The grid is non-uniform in strike, which inflates the metric
slightly at sparse ladders; it is used only to rank expiries against each other,
never as an absolute threshold.

This runs over every expiry present in the cached chain snapshot, including the
ones `config.py` does not calibrate, so an exclusion can be re-derived rather
than taken on trust.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.loader import load_equity_chain          # noqa: E402
from src.config import CONFIG                      # noqa: E402


def all_snapshot_expiries(snapshot_dir: Path) -> list[str]:
    meta_asof = CONFIG["data"]["asof"]
    chains = pd.read_csv(snapshot_dir / f"spy_chains_{meta_asof}.csv")
    return sorted(chains["expiry"].astype(str).unique())


def main() -> int:
    snapshot_dir = ROOT / CONFIG["data"]["snapshot_dir"]
    calibrated = set(CONFIG["data"]["expiries"])
    expiries = all_snapshot_expiries(snapshot_dir)

    market = load_equity_chain(snapshot_dir=snapshot_dir, expiries=expiries)

    rows = []
    for _, meta in market.expiries.iterrows():
        smile = market.smile(meta["expiry"]).sort_values("strike")
        iv = smile["iv"].to_numpy(float)
        d2 = np.diff(iv, 2)
        rows.append({
            "expiry": meta["expiry"],
            "T": float(meta["T"]),
            "n_quotes": int(len(iv)),
            "roughness_volpts": float(np.sqrt(np.mean(d2 ** 2)) * 100.0),
            "max_abs_d2_volpts": float(np.abs(d2).max() * 100.0),
            "calibrated": meta["expiry"] in calibrated,
        })
    out = pd.DataFrame(rows).sort_values("T")

    print(f"SPY chain snapshot {market.asof}, spot {market.spot:.2f}")
    print(f"{len(out)} expiries in the snapshot, {int(out.calibrated.sum())} calibrated\n")
    print(out.to_string(index=False, formatters={
        "T": "{:.4f}".format,
        "roughness_volpts": "{:.4f}".format,
        "max_abs_d2_volpts": "{:.4f}".format}))

    kept, dropped = out[out.calibrated], out[~out.calibrated]
    print(f"\nroughness, calibrated expiries : max {kept.roughness_volpts.max():.4f} vol pts")
    if not dropped.empty:
        print("roughness, excluded expiries   : "
              + ", ".join(f"{r.expiry} {r.roughness_volpts:.4f}"
                          for r in dropped.itertuples()))
        worst = dropped.loc[dropped.roughness_volpts.idxmax()]
        print(f"\nThe roughest excluded expiry, {worst.expiry}, sits "
              f"{worst.roughness_volpts / kept.roughness_volpts.max():.1f}x above the "
              "roughest expiry that is calibrated.")
    print("\nAny excluded expiry whose roughness is NOT an outlier here was excluded for "
          "a reason other than quote quality; config.py states which.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
