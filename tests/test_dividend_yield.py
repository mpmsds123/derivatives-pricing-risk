"""The attribution book's dividend yield is a hardcoded constant; this ties it to data.

`attribution/src/config.py` sets `model.dividend_yield = 0.00973` and documents it as
trailing-12-month SPY distributions over spot at the calibration date. The
distribution history is committed at `surface/data/raw/spy_dividends.csv` but no code
reads it, so the constant and its stated provenance could drift apart silently.
"""
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
CALIBRATION_DATE = "2026-08-07"


def _configured_yield() -> float:
    code = ("import sys, json; sys.path.insert(0, sys.argv[1]); "
            "from src.config import CONFIG; "
            "print(json.dumps(CONFIG['model']['dividend_yield']))")
    proc = subprocess.run([sys.executable, "-c", code, str(ROOT / "attribution")],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return float(json.loads(proc.stdout))


def test_dividend_yield_matches_trailing_twelve_months() -> None:
    div = pd.read_csv(ROOT / "surface" / "data" / "raw" / "spy_dividends.csv")
    dates = pd.to_datetime(div["Date"], utc=True).dt.tz_localize(None)
    asof = pd.Timestamp(CALIBRATION_DATE)
    trailing = div.loc[(dates <= asof) & (dates > asof - pd.DateOffset(years=1)), "Dividends"]
    assert len(trailing) == 4, f"expected 4 quarterly distributions, found {len(trailing)}"

    spot = json.loads((ROOT / "surface" / "data" / "raw" / "snapshot_meta.json").read_text())["spot"]
    implied = float(trailing.sum()) / float(spot)

    # Tolerance is the rounding in the configured constant itself (5 decimals).
    assert implied == pytest.approx(_configured_yield(), abs=5e-6), (
        f"trailing-12m distributions {trailing.sum():.3f} / spot {spot:.2f} = {implied:.6f}, "
        "which is not the configured dividend yield"
    )
